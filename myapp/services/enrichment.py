import uuid
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
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
class EnrichmentResult:
    item_id: int
    enrichment_run_id: str
    changes: List[FieldChange]
    success: bool
    error_message: Optional[str] = None


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

    def __init__(self, created_by: Optional[User] = None):
        self.created_by = created_by

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
        extracted_data = self._extract_from_documents(documents, ocr_backend=ocr_backend)
        if not extracted_data:
            return EnrichmentResult(
                item_id=item.id,
                enrichment_run_id=str(enrichment_run_id),
                changes=[],
                success=False,
                error_message="Failed to extract data from documents"
            )

        # Generate field changes, respecting protected fields
        for field_name, extracted_value in extracted_data.items():
            if field_name not in self.ENRICHABLE_FIELDS:
                continue
            if field_name in self.PROTECTED_FIELDS:
                continue

            current_value = getattr(item, field_name, None)
            if current_value != extracted_value and extracted_value is not None:
                confidence = extracted_data.get(f'{field_name}_confidence', Decimal('0.7'))
                if confidence >= confidence_threshold:
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

    def validate_and_normalize(self, item: Item, confidence_threshold: Decimal = Decimal('0.5')) -> EnrichmentResult:
        """Validate and normalize existing item fields."""
        enrichment_run_id = uuid.uuid4()
        changes = []

        # Example: normalize issuer names, validate date formats, etc.
        normalization_rules = {
            'issuer': self._normalize_issuer,
            'expiry_date': self._validate_date,
            'issue_date': self._validate_date,
            'value': self._normalize_value,
        }

        for field_name, rule_func in normalization_rules.items():
            if field_name in self.PROTECTED_FIELDS:
                continue

            current_value = getattr(item, field_name, None)
            if current_value is None:
                continue

            normalized_value, normalized_confidence = rule_func(current_value)
            if normalized_value != current_value and normalized_confidence >= confidence_threshold:
                changes.append(FieldChange(
                    field_name=field_name,
                    old_value=str(current_value),
                    new_value=str(normalized_value),
                    confidence_score=normalized_confidence,
                    reason=f"Normalized/validated {field_name}"
                ))

        return EnrichmentResult(
            item_id=item.id,
            enrichment_run_id=str(enrichment_run_id),
            changes=changes,
            success=True
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
        for field_name, field_value in enrichment_data.items():
            if field_name.endswith('_confidence'):
                continue

            if field_name not in self.ENRICHABLE_FIELDS:
                continue
            if field_name in self.PROTECTED_FIELDS:
                continue

            current_value = getattr(item, field_name, None)
            if current_value is None or current_value == '':
                confidence_key = f'{field_name}_confidence'
                confidence = enrichment_data.get(confidence_key, Decimal('0.5'))
                if confidence >= confidence_threshold:
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

    def get_enrichment_preview(self, changes: List[FieldChange]) -> Dict[str, Any]:
        """Generate a preview of proposed enrichment changes."""
        return {
            'change_count': len(changes),
            'changes': [asdict(c) for c in changes],
            'total_confidence': sum(c.confidence_score for c in changes) / len(changes) if changes else 0
        }

    def _extract_from_documents(self, documents, ocr_backend: Optional[str] = None) -> Optional[Dict[str, Any]]:
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

                result = get_backend().extract(file_bytes, mime_type)
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
                # Prefer the value from whichever document OCR'd with higher
                # overall confidence - later documents don't just overwrite
                # earlier, better ones.
                if field_name not in merged_data or confidence > merged_data.get(conf_key, Decimal('0')):
                    merged_data[field_name] = value
                    merged_data[conf_key] = confidence

        return merged_data if merged_data else None

    def _normalize_issuer(self, value: str) -> Tuple[str, Decimal]:
        """Normalize issuer name."""
        # Simple normalization: title case, strip whitespace
        normalized = value.strip().title()
        confidence = Decimal('0.9') if normalized != value else Decimal('1.0')
        return normalized, confidence

    def _validate_date(self, value: str) -> Tuple[Optional[str], Decimal]:
        """Validate date format."""
        # Placeholder: actual implementation would parse and validate dates
        return value, Decimal('0.8')

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
        Enrich multiple items. Modes: 'ocr', 'validation', 'merchant_lookup', 'all'
        """
        results = {}

        for item_id in item_ids:
            try:
                item = Item.objects.get(id=item_id)

                if enrichment_mode in ('ocr', 'all'):
                    result = self.enricher.enrich_from_ocr(item, confidence_threshold)
                    if result.changes and not dry_run:
                        success, error = self.enricher.apply_changes(item, result.changes)
                        if success:
                            self.enricher.log_enrichment(item, result.enrichment_run_id,
                                                        result.changes, 'ocr_rescan')
                    results[item_id] = result
                    continue

                if enrichment_mode in ('validation', 'all'):
                    result = self.enricher.validate_and_normalize(item, confidence_threshold)
                    if result.changes and not dry_run:
                        success, error = self.enricher.apply_changes(item, result.changes)
                        if success:
                            self.enricher.log_enrichment(item, result.enrichment_run_id,
                                                        result.changes, 'validation')
                    results[item_id] = result
                    continue

                if enrichment_mode in ('merchant_lookup', 'all'):
                    result = self.enricher.enrich_from_merchant_lookup(item, confidence_threshold)
                    if result.changes and not dry_run:
                        success, error = self.enricher.apply_changes(item, result.changes)
                        if success:
                            self.enricher.log_enrichment(item, result.enrichment_run_id,
                                                        result.changes, 'merchant_lookup')
                    results[item_id] = result

            except Item.DoesNotExist:
                results[item_id] = EnrichmentResult(
                    item_id=item_id,
                    enrichment_run_id='',
                    changes=[],
                    success=False,
                    error_message="Item not found"
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
