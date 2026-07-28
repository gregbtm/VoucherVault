import re
import uuid
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict, field
from django.db import transaction
from django.contrib.auth.models import User
from myapp.models import Item, ItemEnrichmentLog, Document
from .merchant_lookup import MerchantLookup

logger = logging.getLogger(__name__)


@dataclass
class FieldChange:
    field_name: str
    old_value: Optional[str]
    new_value: Optional[str]
    confidence_score: Decimal
    reason: str


@dataclass
class FieldFlag:
    """
    A suspicion raised about a field VoucherVault deliberately never
    auto-edits (the barcode payload, the PIN) - a wrong guess here would
    silently break a real, working code, so this only ever surfaces a
    note for a human to check, never a FieldChange to apply.
    """
    field_name: str
    current_value: Optional[str]
    message: str
    severity: str = 'info'


@dataclass
class EnrichmentResult:
    item_id: int
    enrichment_run_id: str
    changes: List[FieldChange]
    success: bool
    error_message: Optional[str] = None
    flags: List[FieldFlag] = field(default_factory=list)


class ItemEnricher:
    """Service for enriching item data with OCR and validation."""

    # Fields that should never be modified by enrichment (core identifiers)
    PROTECTED_FIELDS = {'redeem_code', 'code_type', 'type', 'id', 'user'}

    # Fields that can be enriched
    ENRICHABLE_FIELDS = {
        'issuer', 'expiry_date', 'issue_date', 'value', 'value_type',
        'description', 'notes', 'card_number', 'balance_check_url',
        'journey_origin', 'journey_destination', 'order_id', 'discount_applied',
        'pin', 'name', 'minimum_spend', 'points_balance', 'membership_tier',
        'initial_value', 'seat_number'
    }

    # Each corrected-away enrichment fill nudges that field+method's
    # required confidence up - a method that's proven unreliable for a
    # given field needs to be more sure of itself before it's allowed to
    # touch it again, for this user specifically. Capped so a determined
    # run of corrections never fully disables the field outright (an
    # EnrichmentFieldPreference opt-out still exists for that); it just
    # means only very-high-confidence fills get applied automatically from
    # here on, while shakier ones fall back to being surfaced for review.
    CONFIDENCE_PENALTY_PER_CORRECTION = Decimal('0.1')
    MAX_CONFIDENCE_PENALTY = Decimal('0.4')
    MAX_EFFECTIVE_THRESHOLD = Decimal('0.95')

    def __init__(self, created_by: Optional[User] = None):
        self.created_by = created_by
        # (user_id, method) -> set of field names that user has opted out
        # of for that method. Populated lazily and reused across calls so a
        # BulkEnricher run over many items of the same user(s) doesn't issue
        # one EnrichmentFieldPreference query per item per method.
        self._field_pref_cache: Dict[Tuple[int, str], set] = {}
        # (user_id, method, field_name) -> confidence penalty accrued from
        # past corrections, same caching rationale as above.
        self._threshold_penalty_cache: Dict[Tuple[int, str, str], Decimal] = {}

    def _excluded_fields(self, user: Optional[User], method: str) -> set:
        """Fields this user has opted out of for a given enrichment method."""
        if user is None:
            return set()
        cache_key = (user.id, method)
        if cache_key not in self._field_pref_cache:
            from myapp.models import EnrichmentFieldPreference
            self._field_pref_cache[cache_key] = set(
                EnrichmentFieldPreference.objects
                .filter(user=user, method=method)
                .values_list('field_name', flat=True)
            )
        return self._field_pref_cache[cache_key]

    def _effective_confidence_threshold(self, user: Optional[User], method: str,
                                       field_name: str, base_threshold: Decimal) -> Decimal:
        """
        Self-tuning counterpart to the flat confidence_threshold callers
        pass in: raises it for this specific (user, method, field) combo
        based on how many times this user has corrected away a value this
        exact method previously auto-applied for this exact field (see
        ScanFieldCorrection.source='enrichment' and
        myapp.scan_learning.record_enrichment_correction_feedback). A field
        this method has never gotten wrong for this user keeps the caller's
        base threshold unchanged.
        """
        if user is None:
            return base_threshold
        cache_key = (user.id, method, field_name)
        if cache_key not in self._threshold_penalty_cache:
            from myapp.models import ScanFieldCorrection
            correction_count = ScanFieldCorrection.objects.filter(
                user=user, field=field_name, source='enrichment', enrichment_method=method,
            ).count()
            self._threshold_penalty_cache[cache_key] = min(
                self.MAX_CONFIDENCE_PENALTY, self.CONFIDENCE_PENALTY_PER_CORRECTION * correction_count,
            )
        penalty = self._threshold_penalty_cache[cache_key]
        return min(self.MAX_EFFECTIVE_THRESHOLD, base_threshold + penalty)

    def enrich_from_ocr(self, item: Item, confidence_threshold: Decimal = Decimal('0.5'),
                       ocr_backend: Optional[str] = None) -> EnrichmentResult:
        """Re-scan and extract data from attached documents."""
        enrichment_run_id = uuid.uuid4()
        changes = []

        # Get documents for this item
        documents = Document.objects.filter(item=item)
        if not documents.exists():
            return EnrichmentResult(
                item_id=item.id,
                enrichment_run_id=str(enrichment_run_id),
                changes=[],
                success=False,
                error_message="No documents found for this item"
            )

        # Extract data from documents using OCR backend
        extracted_data = self._extract_from_documents(documents, ocr_backend=ocr_backend, user=item.user)
        if not extracted_data:
            return EnrichmentResult(
                item_id=item.id,
                enrichment_run_id=str(enrichment_run_id),
                changes=[],
                success=False,
                error_message="Failed to extract data from documents"
            )

        # Generate field changes, respecting protected fields and the
        # item owner's own per-field opt-outs (EnrichmentFieldPreference).
        excluded = self._excluded_fields(item.user, 'ocr')
        for field_name, extracted_value in extracted_data.items():
            if field_name not in self.ENRICHABLE_FIELDS:
                continue
            if field_name in self.PROTECTED_FIELDS:
                continue
            if field_name in excluded:
                continue

            current_value = getattr(item, field_name, None)
            if current_value != extracted_value and extracted_value is not None:
                confidence = extracted_data.get(f'{field_name}_confidence', Decimal('0.7'))
                effective_threshold = self._effective_confidence_threshold(
                    item.user, 'ocr_rescan', field_name, confidence_threshold,
                )
                if confidence >= effective_threshold:
                    changes.append(FieldChange(
                        field_name=field_name,
                        old_value=str(current_value) if current_value else None,
                        new_value=str(extracted_value),
                        confidence_score=confidence,
                        reason=f"Extracted from attached document via OCR"
                    ))

        return EnrichmentResult(
            item_id=item.id,
            enrichment_run_id=str(enrichment_run_id),
            changes=changes,
            success=len(changes) > 0 or not documents.exists()
        )

    # Free-text fields that only ever get whitespace cleanup (no re-casing,
    # no external lookups) - safe for any item type, never guesses content.
    WHITESPACE_ONLY_FIELDS = (
        'description', 'notes', 'card_number', 'order_id',
        'discount_applied', 'membership_tier', 'seat_number', 'pin',
    )

    def validate_and_normalize(self, item: Item, confidence_threshold: Decimal = Decimal('0.5')) -> EnrichmentResult:
        """
        Validate and normalize existing item fields. Every rule here is
        mechanical (whitespace, casing, known-value lookup, date ordering)
        - nothing is ever invented. Fields where a wrong guess would break
        something real (redeem_code, pin content) are only ever flagged
        for manual review via `flags`, never rewritten - see `PROTECTED_FIELDS`
        and `_detect_review_flags`.
        """
        enrichment_run_id = uuid.uuid4()
        changes = []
        excluded = self._excluded_fields(item.user, 'validation')

        for field_name in self.WHITESPACE_ONLY_FIELDS:
            if field_name in self.PROTECTED_FIELDS or field_name in excluded:
                continue
            current_value = getattr(item, field_name, None)
            if not isinstance(current_value, str) or not current_value:
                continue
            cleaned = self._normalize_whitespace(current_value)
            if cleaned != current_value:
                changes.append(FieldChange(
                    field_name=field_name, old_value=current_value, new_value=cleaned,
                    confidence_score=Decimal('1.0'),
                    reason=f"Removed extra/leading/trailing whitespace from {field_name}",
                ))

        # Issuer gets whitespace + casing cleanup + a check against the
        # curated merchants database; name gets whitespace + casing only
        # (there's no "known name" database to canonicalize against).
        for field_name, use_merchant_lookup in (('issuer', True), ('name', False)):
            if field_name in excluded:
                continue
            original = getattr(item, field_name, None)
            if not original:
                continue
            final_value, reasons = self._pipeline_text_field(original, use_merchant_lookup=use_merchant_lookup)
            if final_value != original:
                changes.append(FieldChange(
                    field_name=field_name, old_value=original, new_value=final_value,
                    confidence_score=Decimal('0.85'),
                    reason='; '.join(reasons),
                ))

        # Issue/expiry date ordering is a hard physical constraint (nothing
        # can be issued after it expires) - if they're reversed, swapping
        # them is a purely mechanical fix that doesn't require guessing
        # either date's true value. issue_date defaults to timezone.now()
        # (a datetime) while expiry_date is date-only, so an unsaved/unreloaded
        # instance can hold a naive-mismatched pair - normalize both to date
        # before comparing rather than risk a TypeError against a real record.
        issue_date = item.issue_date.date() if hasattr(item.issue_date, 'date') and callable(item.issue_date.date) else item.issue_date
        expiry_date = item.expiry_date.date() if hasattr(item.expiry_date, 'date') and callable(item.expiry_date.date) else item.expiry_date
        if (issue_date and expiry_date and issue_date > expiry_date
                and 'issue_date' not in excluded and 'expiry_date' not in excluded):
            changes.append(FieldChange(
                field_name='issue_date', old_value=str(item.issue_date), new_value=str(expiry_date),
                confidence_score=Decimal('0.9'), reason="Issue date was after expiry date - dates appear swapped",
            ))
            changes.append(FieldChange(
                field_name='expiry_date', old_value=str(item.expiry_date), new_value=str(issue_date),
                confidence_score=Decimal('0.9'), reason="Issue date was after expiry date - dates appear swapped",
            ))

        if item.value is not None and 'value' not in excluded:
            normalized_value, value_confidence = self._normalize_value(item.value)
            effective_threshold = self._effective_confidence_threshold(
                item.user, 'validation', 'value', confidence_threshold,
            )
            if normalized_value != item.value and value_confidence >= effective_threshold:
                changes.append(FieldChange(
                    field_name='value', old_value=str(item.value), new_value=str(normalized_value),
                    confidence_score=value_confidence, reason="Normalized monetary value to 2 decimal places",
                ))

        # UK railway station names/codes for travel passes, resolved against
        # the same curated station database the OCR extraction path uses.
        if item.type == 'travelpass':
            from ocr.enrichment import normalize_station
            for field_name in ('journey_origin', 'journey_destination'):
                if field_name in excluded:
                    continue
                original = getattr(item, field_name, None)
                if not original:
                    continue
                cleaned = self._normalize_whitespace(original)
                station = normalize_station(cleaned)
                canonical_name = station.get('name') if station else None
                final_value = canonical_name if canonical_name else cleaned
                if final_value != original:
                    changes.append(FieldChange(
                        field_name=field_name, old_value=original, new_value=final_value,
                        confidence_score=Decimal('0.85') if canonical_name else Decimal('1.0'),
                        reason="Matched to known UK station name" if canonical_name else "Removed extra whitespace",
                    ))

        flags = self._detect_review_flags(item)

        return EnrichmentResult(
            item_id=item.id,
            enrichment_run_id=str(enrichment_run_id),
            changes=changes,
            success=True,
            flags=flags,
        )

    def enrich_from_merchant_lookup(self, item: Item, confidence_threshold: Decimal = Decimal('0.5')) -> EnrichmentResult:
        """Infer enrichment from similar merchants in user's existing items."""
        enrichment_run_id = uuid.uuid4()
        changes = []

        # Get merchant lookup for this user
        lookup = MerchantLookup(item.user)
        enrichment_data = lookup.enrich_from_merchant(item, confidence_threshold)

        if not enrichment_data:
            return EnrichmentResult(
                item_id=item.id,
                enrichment_run_id=str(enrichment_run_id),
                changes=[],
                success=True,
                error_message="No similar merchants found for enrichment"
            )

        # Generate field changes from merchant lookup
        excluded = self._excluded_fields(item.user, 'merchant_lookup')
        for field_name, field_value in enrichment_data.items():
            if field_name.endswith('_confidence'):
                continue

            if field_name not in self.ENRICHABLE_FIELDS:
                continue
            if field_name in self.PROTECTED_FIELDS:
                continue
            if field_name in excluded:
                continue

            current_value = getattr(item, field_name, None)
            if current_value is None or current_value == '':
                confidence_key = f'{field_name}_confidence'
                confidence = enrichment_data.get(confidence_key, Decimal('0.5'))
                effective_threshold = self._effective_confidence_threshold(
                    item.user, 'merchant_lookup', field_name, confidence_threshold,
                )
                if confidence >= effective_threshold:
                    changes.append(FieldChange(
                        field_name=field_name,
                        old_value=None,
                        new_value=str(field_value),
                        confidence_score=confidence,
                        reason=f"Inferred from similar merchants in user's collection"
                    ))

        return EnrichmentResult(
            item_id=item.id,
            enrichment_run_id=str(enrichment_run_id),
            changes=changes,
            success=True
        )

    def apply_changes(self, item: Item, changes: List[FieldChange]) -> Tuple[bool, Optional[str]]:
        """Apply enrichment changes to an item."""
        try:
            for change in changes:
                if change.field_name in self.PROTECTED_FIELDS:
                    return False, f"Cannot modify protected field: {change.field_name}"
                if change.field_name not in self.ENRICHABLE_FIELDS:
                    return False, f"Field not enrichable: {change.field_name}"

                setattr(item, change.field_name, change.new_value)

            item.save()
            return True, None
        except Exception as e:
            return False, str(e)

    def log_enrichment(self, item: Item, enrichment_run_id: str, changes: List[FieldChange],
                      enrichment_type: str) -> None:
        """Log enrichment changes to audit trail."""
        with transaction.atomic():
            for change in changes:
                ItemEnrichmentLog.objects.create(
                    enrichment_run_id=enrichment_run_id,
                    item=item,
                    field_name=change.field_name,
                    old_value=change.old_value,
                    new_value=change.new_value,
                    enrichment_type=enrichment_type,
                    confidence_score=change.confidence_score,
                    reason=change.reason,
                    created_by=self.created_by
                )

    def log_flags(self, item: Item, enrichment_run_id: str, flags: List[FieldFlag]) -> None:
        """
        Record review flags in the same audit trail as real changes, so
        they surface in Enrichment History alongside applied changes.
        old_value == new_value here since nothing about the item actually
        changed - this is a note for a human, not a diff.
        """
        with transaction.atomic():
            for flag in flags:
                ItemEnrichmentLog.objects.create(
                    enrichment_run_id=enrichment_run_id,
                    item=item,
                    field_name=flag.field_name,
                    old_value=flag.current_value,
                    new_value=flag.current_value,
                    enrichment_type='flagged',
                    confidence_score=None,
                    reason=flag.message,
                    created_by=self.created_by
                )

    def get_enrichment_preview(self, changes: List[FieldChange]) -> Dict[str, Any]:
        """Generate a preview of proposed enrichment changes."""
        return {
            'change_count': len(changes),
            'changes': [asdict(c) for c in changes],
            'total_confidence': sum(c.confidence_score for c in changes) / len(changes) if changes else 0
        }

    def _extract_from_documents(self, documents, ocr_backend: Optional[str] = None,
                               user: Optional[User] = None) -> Optional[Dict[str, Any]]:
        """
        Extract structured data from documents using the app's real,
        SiteConfiguration-driven OCR backend (ocr.backends.get_backend()) -
        the same one item creation/edit scanning and the document-upload
        text-extraction task (extract_document_text_task) already use.

        This used to go through myapp.services.ocr_backend_integration, a
        separate, never-actually-working implementation: it read its API
        key from a raw OPENAI_API_KEY env var instead of the key configured
        in Site Settings (so the client was always None in every real
        deployment), and its OpenAI backend called `.messages.create()` -
        the Anthropic SDK's method name, not OpenAI's - so even a correctly
        configured key would have failed. Every enrichment OCR run
        therefore silently produced "No documents found" or "Failed to
        extract data from documents" regardless of what was attached.
        """
        if not documents.exists():
            return None

        from ocr.backends import get_backend, ocr_enabled
        if not ocr_enabled():
            logger.info("OCR enrichment skipped: OCR is disabled in Site Settings")
            return None

        import mimetypes
        merged_data: Dict[str, Any] = {}

        for document in documents:
            if not document.file:
                continue
            try:
                mime_type = mimetypes.guess_type(document.file.name)[0] or 'application/octet-stream'
                document.file.seek(0)
                file_bytes = document.file.read()

                if mime_type == 'application/pdf':
                    from myapp.pdf_ticket import pdf_page_to_png_bytes
                    file_bytes = pdf_page_to_png_bytes(file_bytes)
                    mime_type = 'image/png'

                result = get_backend().extract(file_bytes, mime_type, user=user)
            except Exception as e:
                logger.error(f"OCR extraction failed for document {document.id}: {e}")
                continue

            if not result:
                continue

            confidence = Decimal(str(result.get('confidence') or 0))
            for field_name in self.ENRICHABLE_FIELDS:
                value = result.get(field_name)
                if value is None or value == '':
                    continue
                conf_key = f'{field_name}_confidence'
                # A field with its own self-consistency-checked confidence
                # (see the OCR prompt's second-pass re-read for
                # expiry_date/value - ocr/validation.py) uses that
                # independently of the overall read; everything else still
                # falls back to it, same as before.
                field_confidence = Decimal(str(result.get(conf_key, confidence) or 0))
                # Prefer the value from whichever document OCR'd with higher
                # confidence for this specific field - later documents don't
                # just overwrite earlier, better ones.
                if field_name not in merged_data or field_confidence > merged_data.get(conf_key, Decimal('0')):
                    merged_data[field_name] = value
                    merged_data[conf_key] = field_confidence

        return merged_data if merged_data else None

    def _normalize_whitespace(self, value: str) -> str:
        """Strip and collapse internal whitespace runs - never touches content."""
        return re.sub(r'\s+', ' ', value.strip())

    def _smart_title_case(self, value: str) -> str:
        """
        Title-case a name, preserving apostrophe-s ("mcdonald's" ->
        "McDonald's" not "Mcdonald'S") and treating '&' as a separator
        rather than lowercase filler ("m&s" -> "M&S").
        """
        words = re.split(r'(\s+|&)', value)
        out = []
        for word in words:
            if not word or word.isspace() or word == '&':
                out.append(word)
                continue
            parts = word.split("'")
            out.append("'".join(p[:1].upper() + p[1:].lower() if p else p for p in parts))
        return ''.join(out)

    def _normalize_issuer(self, value: str) -> Tuple[str, Decimal]:
        """
        Re-case a name only when it looks like an OCR/manual-entry artifact
        - i.e. it's currently ALL CAPS or all lowercase. Genuinely
        mixed-case values (e.g. "eBay", "iTunes") are left exactly as
        stored rather than clobbered by a naive title-case.
        """
        if value.isupper() or value.islower():
            normalized = self._smart_title_case(value)
        else:
            normalized = value
        confidence = Decimal('0.9') if normalized != value else Decimal('1.0')
        return normalized, confidence

    def _pipeline_text_field(self, value: str, use_merchant_lookup: bool = False) -> Tuple[str, List[str]]:
        """
        Run whitespace cleanup, then artifact-only re-casing, then
        (optionally) canonicalization against the curated merchants
        database. Returns the final value plus a list of human-readable
        reasons for whichever steps actually changed something.
        """
        reasons = []
        working = self._normalize_whitespace(value)
        if working != value:
            reasons.append("removed extra whitespace")

        recased, _confidence = self._normalize_issuer(working)
        if recased != working:
            reasons.append("corrected ALL CAPS/all lowercase styling")
            working = recased

        if use_merchant_lookup:
            from ocr.enrichment import detect_merchant
            merchant = detect_merchant(working)
            canonical = merchant.get('name') if merchant else None
            if canonical and canonical != working:
                reasons.append("matched to known merchant name")
                working = canonical

        return working, reasons

    def _detect_review_flags(self, item: Item) -> List[FieldFlag]:
        """
        Read-only checks on fields enrichment deliberately never rewrites.
        Purely static analysis of the value already on file - no OCR, no
        guessing at what the "correct" code or PIN should be.
        """
        flags = []
        ambiguous_pairs = (('0', 'O'), ('1', 'I'), ('1', 'l'), ('5', 'S'), ('8', 'B'))

        redeem_code = item.redeem_code or ''
        if redeem_code:
            upper = redeem_code.upper()
            for digit, letter in ambiguous_pairs:
                if digit in redeem_code and letter in upper:
                    flags.append(FieldFlag(
                        field_name='redeem_code', current_value=redeem_code,
                        message=(
                            f"Contains both '{digit}' and '{letter}' - these are commonly "
                            "confused by OCR. Worth double-checking against the physical barcode."
                        ),
                        severity='info',
                    ))
                    break

        pin = item.pin or ''
        if pin and not pin.isdigit():
            flags.append(FieldFlag(
                field_name='pin', current_value=pin,
                message="Contains non-numeric characters - verify this matches the physical PIN.",
                severity='info',
            ))

        return flags

    def _normalize_value(self, value: Any) -> Tuple[Optional[Decimal], Decimal]:
        """Normalize monetary value."""
        try:
            normalized = Decimal(str(value)).quantize(Decimal('0.01'))
            return normalized, Decimal('0.95')
        except:
            return value, Decimal('0.0')


class BulkEnricher:
    """Handle bulk enrichment operations on multiple items."""

    def __init__(self, created_by: Optional[User] = None):
        self.created_by = created_by
        self.enricher = ItemEnricher(created_by=created_by)

    def enrich_selected_items(self, item_ids: List[int], enrichment_mode: str,
                             confidence_threshold: Decimal = Decimal('0.5'),
                             dry_run: bool = False) -> Dict[int, EnrichmentResult]:
        """
        Enrich multiple items. Modes: 'ocr', 'validation', 'merchant_lookup', 'all'.

        'all' runs every applicable pass and merges them into one result per
        item, rather than picking just the first match - each pass is still
        applied/logged under its own enrichment_type so the audit trail
        stays accurate about which mechanism produced which change.
        """
        results = {}

        for item_id in item_ids:
            try:
                item = Item.objects.get(id=item_id)
            except Item.DoesNotExist:
                results[item_id] = EnrichmentResult(
                    item_id=item_id,
                    enrichment_run_id='',
                    changes=[],
                    success=False,
                    error_message="Item not found"
                )
                continue

            passes: List[Tuple[str, EnrichmentResult]] = []
            if enrichment_mode in ('ocr', 'all'):
                passes.append(('ocr_rescan', self.enricher.enrich_from_ocr(item, confidence_threshold)))
            if enrichment_mode in ('validation', 'all'):
                passes.append(('validation', self.enricher.validate_and_normalize(item, confidence_threshold)))
            if enrichment_mode in ('merchant_lookup', 'all'):
                passes.append(('merchant_lookup', self.enricher.enrich_from_merchant_lookup(item, confidence_threshold)))

            all_changes: List[FieldChange] = []
            all_flags: List[FieldFlag] = []
            error_messages = []
            run_id = None
            any_success = False

            for enrichment_type, result in passes:
                if run_id is None and result.enrichment_run_id:
                    run_id = result.enrichment_run_id
                if result.error_message:
                    error_messages.append(result.error_message)
                all_flags.extend(result.flags)

                if result.changes:
                    if not dry_run:
                        success, error = self.enricher.apply_changes(item, result.changes)
                        if not success:
                            error_messages.append(error)
                            continue
                        self.enricher.log_enrichment(item, result.enrichment_run_id,
                                                    result.changes, enrichment_type)
                    any_success = True
                    all_changes.extend(result.changes)
                elif result.success:
                    any_success = True

                if result.flags and not dry_run:
                    self.enricher.log_flags(item, result.enrichment_run_id, result.flags)

            results[item_id] = EnrichmentResult(
                item_id=item_id,
                enrichment_run_id=run_id or '',
                changes=all_changes,
                success=any_success,
                error_message='; '.join(error_messages) if error_messages else None,
                flags=all_flags,
            )

        return results

    def get_enrichment_summary(self, results: Dict[int, EnrichmentResult]) -> Dict[str, Any]:
        """Generate summary of enrichment results."""
        successful = sum(1 for r in results.values() if r.success and r.changes)
        total_changes = sum(len(r.changes) for r in results.values())
        avg_confidence = sum(
            sum(c.confidence_score for c in r.changes)
            for r in results.values() if r.changes
        ) / max(total_changes, 1)

        return {
            'total_items': len(results),
            'successful_enrichments': successful,
            'total_changes': total_changes,
            'average_confidence': float(avg_confidence),
            'errors': [r.error_message for r in results.values() if not r.success and r.error_message]
        }


def get_active_flags_for_items(items) -> Dict[Any, List[Dict[str, str]]]:
    """
    Reads the 'flagged' ItemEnrichmentLog entries (see
    ItemEnricher._detect_review_flags) for a batch of items and returns
    {item_id: [{'field_name': ..., 'message': ...}, ...]}. Only the most
    recent flag per (item, field) is considered, and only if the flagged
    value still matches what's actually stored - once a user edits the
    field, that flag no longer describes reality and is dropped rather
    than shown as a stale warning. A single query regardless of how many
    items are passed in.
    """
    items_by_id = {item.id: item for item in items}
    if not items_by_id:
        return {}

    logs = (
        ItemEnrichmentLog.objects
        .filter(item_id__in=items_by_id.keys(), enrichment_type='flagged')
        .order_by('-created_at')
    )
    latest_per_field = {}
    for log in logs:
        key = (log.item_id, log.field_name)
        latest_per_field.setdefault(key, log)

    result: Dict[Any, List[Dict[str, str]]] = {}
    for (item_id, field_name), log in latest_per_field.items():
        item = items_by_id[item_id]
        if getattr(item, field_name, None) != log.new_value:
            continue
        result.setdefault(item_id, []).append({'field_name': field_name, 'message': log.reason})
    return result
