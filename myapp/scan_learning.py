"""
Self-healing for AI photo scans: remembers how a user corrected a scan's
fields before saving, and replays those corrections against future scans.

Two halves, wired into opposite ends of the scan-to-save round trip:

- record_scan_corrections() runs when an item is saved with an
  `ai_scan_snapshot` (the raw values the AI extraction returned, captured
  by the form's JS at scan time). Any learnable field the user changed
  between scan and save becomes a ScanFieldCorrection; any field the user
  kept exactly as scanned *deletes* a stale correction for that value, so
  the store also un-learns corrections the user no longer stands by.

- apply_learned_corrections() runs inside the OCR extract endpoint, right
  after the vision backend returns: any field whose extracted value
  matches a remembered `ai_value` gets swapped for the remembered
  `corrected_value` before the frontend ever sees it.

Only fields with stable, recurring values are learnable. Item-specific
fields (name, code, expiry, value, pin, card number) are deliberately
excluded - "corrections" there are just data entry, not a pattern.
"""

import logging

from .models import Item, ItemEnrichmentLog, ScanFieldCorrection

logger = logging.getLogger(__name__)

# field name -> Item attribute it compares against on save. travel_time is
# special-cased in _saved_value (TimeField -> "HH:MM" string).
LEARNABLE_FIELDS = (
    'issuer', 'logo_slug', 'currency', 'type', 'code_type',
    'journey_origin', 'journey_destination', 'travel_time',
)

# ItemEnrichmentLog.enrichment_type values that represent an auto-applied
# value (as opposed to 'flagged', which never changed anything).
ENRICHMENT_LOG_TYPES = ('ocr_rescan', 'validation', 'merchant_lookup', 'auto_enrich')

# ItemEnrichmentLog.enrichment_type (and ScanFieldCorrection.enrichment_method,
# which mirrors it) uses a different vocabulary from
# EnrichmentConfig.ENRICHMENT_METHODS / EnrichmentFieldPreference.method
# ('ocr' vs 'ocr_rescan') - this is what ItemEnricher._excluded_fields()
# actually queries against, so the circuit breaker below has to translate
# before it can opt a user out of a method for a field. 'auto_enrich' has
# no per-method opt-out surface at all, so it can never trip the breaker.
_LOG_TYPE_TO_PREFERENCE_METHOD = {
    'ocr_rescan': 'ocr',
    'validation': 'validation',
    'merchant_lookup': 'merchant_lookup',
}

# Once a (user, method, field) combo has racked up this many corrected-away
# auto-fills, self-tuning the confidence threshold up (see
# ItemEnricher._effective_confidence_threshold) isn't cutting it - stop the
# method from touching this field for this user at all, the same way a
# manual EnrichmentFieldPreference opt-out would, until a human clears it.
CIRCUIT_BREAKER_TRIP_THRESHOLD = 5


def _maybe_trip_circuit_breaker(user, enrichment_method: str, field: str) -> None:
    """
    Best-effort: called right after an enrichment-sourced correction is
    recorded/incremented, since that's the only moment the trip count can
    change. Never raises - this must never be why a save fails.
    """
    from .models import EnrichmentFieldPreference

    preference_method = _LOG_TYPE_TO_PREFERENCE_METHOD.get(enrichment_method)
    if preference_method is None:
        return
    if field not in dict(EnrichmentFieldPreference.FIELD_CHOICES):
        return
    if EnrichmentFieldPreference.objects.filter(
        user=user, method=preference_method, field_name=field,
    ).exists():
        return

    correction_count = ScanFieldCorrection.objects.filter(
        user=user, field=field, source='enrichment', enrichment_method=enrichment_method,
    ).count()
    if correction_count < CIRCUIT_BREAKER_TRIP_THRESHOLD:
        return

    EnrichmentFieldPreference.objects.get_or_create(
        user=user, method=preference_method, field_name=field,
        defaults={'reason': f'Auto-disabled: corrected {correction_count} auto-applied values in a row'},
    )
    logger.warning(
        'Circuit breaker tripped: disabled %s enrichment for %s field %r after %d corrections',
        preference_method, user, field, correction_count,
    )
    _alert_circuit_breaker_tripped(user, preference_method, field, correction_count)


def _alert_circuit_breaker_tripped(user, method: str, field: str, correction_count: int) -> None:
    import requests
    from .models import SiteConfiguration

    config = SiteConfiguration.load()
    topic = config.security_alert_ntfy_topic.strip()
    if not topic:
        return

    server = (config.ntfy_default_server or 'https://ntfy.sh').rstrip('/')
    try:
        requests.post(
            f'{server}/{topic}',
            data=(
                f"Enrichment circuit breaker tripped for {user}: '{method}' auto-disabled for "
                f"field '{field}' after {correction_count} corrections in a row. "
                f"Re-enable it in Django admin under Enrichment Field Preferences if this was a mistake."
            ).encode('utf-8'),
            headers={
                'Title': 'VoucherVault Enrichment Circuit Breaker'.encode('utf-8'),
                'Priority': 'default',
                'Tags': 'electric_plug',
            },
            timeout=10,
        )
    except Exception:
        logger.warning('Failed to send circuit breaker alert', exc_info=True)

# Blank-fill corrections (AI left it empty, user typed something) replay
# only once the same fill has been seen this many times - one occurrence
# could be item-specific, a repeat is a pattern.
BLANK_FILL_MIN_SEEN = 2

_MAX_VALUE_LENGTH = 255


def _normalize(value) -> str:
    if value is None:
        return ''
    return str(value).strip()[:_MAX_VALUE_LENGTH]


def _saved_value(item: Item, field: str) -> str:
    value = getattr(item, field, None)
    if field == 'travel_time' and value is not None:
        return value.strftime('%H:%M')
    return _normalize(value)


def _healed_value_is_valid(field: str, value: str) -> bool:
    """
    A replayed correction must still be something the item form can
    actually hold - corrected values come from the user's own saved items
    so they nearly always are, but the choice-constrained fields are cheap
    to re-check rather than trust. Imported lazily: ocr.backends.base
    itself imports from myapp.models, so a module-level import here would
    be circular.
    """
    from ocr.backends.base import (
        VALID_CODE_TYPES, VALID_CURRENCIES, VALID_ITEM_TYPES,
        sanitize_time_or_none,
    )
    if field == 'type':
        return value in VALID_ITEM_TYPES
    if field == 'currency':
        return value in VALID_CURRENCIES
    if field == 'code_type':
        return value in VALID_CODE_TYPES
    if field == 'travel_time':
        return sanitize_time_or_none(value) is not None
    return bool(value)


def record_scan_corrections(user, snapshot: dict, item: Item) -> None:
    """
    Diff what the AI scan returned (`snapshot`) against what actually got
    saved (`item`), and upsert/retire corrections accordingly. Best-effort
    by design: learning must never be the reason an item fails to save.
    """
    if not isinstance(snapshot, dict):
        return
    try:
        for field in LEARNABLE_FIELDS:
            if field not in snapshot:
                continue
            ai_value = _normalize(snapshot.get(field))
            final_value = _saved_value(item, field)

            if final_value and final_value.lower() != ai_value.lower():
                correction, created = ScanFieldCorrection.objects.get_or_create(
                    user=user, item_type=item.type, field=field, ai_value=ai_value,
                    defaults={'corrected_value': final_value},
                )
                if not created:
                    if correction.corrected_value.lower() == final_value.lower():
                        correction.times_seen += 1
                    else:
                        # The user now corrects this same scan value to
                        # something new - restart the count rather than
                        # averaging two different intents.
                        correction.corrected_value = final_value
                        correction.times_seen = 1
                    correction.save()
            elif final_value and ai_value and final_value.lower() == ai_value.lower():
                # The scan got it right this time for this item type and the
                # user kept it - any old correction mapping this exact value
                # away for this same item type no longer reflects what they
                # want. Scoped by item_type to match how corrections are
                # recorded and replayed - retiring across item types would
                # un-teach a still-valid, unrelated correction (e.g. a travel
                # ticket's barcode symbology) just because a different item
                # type happened to produce the same value and got kept.
                ScanFieldCorrection.objects.filter(
                    user=user, field=field, ai_value=ai_value, item_type=item.type,
                ).delete()
    except Exception:
        logger.warning('Failed to record scan corrections', exc_info=True)


def record_enrichment_correction_feedback(user, item: Item, before_values: dict) -> None:
    """
    The enrichment-pipeline counterpart to record_scan_corrections(): if a
    field the user is saving right now was set by the most recent
    enrichment log entry for that field (and hasn't been touched since -
    `before_values[field]` still matches what that log applied), and the
    user is now changing it to something else, that's the same "the
    machine guessed wrong" signal a scan correction is - just sourced from
    the enrichment pipeline instead of OCR. Recorded into the same
    ScanFieldCorrection ledger (source='enrichment') so #236's per-method
    confidence tuning and #237's circuit breaker can query one table
    instead of two. `before_values` must be captured from the item as
    fetched from the DB, before the form overwrote its in-memory fields.

    Best-effort by design: learning must never be the reason a save fails.
    """
    try:
        for field in LEARNABLE_FIELDS:
            if field not in before_values:
                continue
            before_value = _normalize(before_values[field])
            final_value = _saved_value(item, field)

            log = (
                ItemEnrichmentLog.objects
                .filter(item=item, field_name=field, enrichment_type__in=ENRICHMENT_LOG_TYPES)
                .order_by('-created_at')
                .first()
            )
            if log is None:
                continue
            logged_value = _normalize(log.new_value)
            if not logged_value or logged_value.lower() != before_value.lower():
                # Either enrichment never set this field, or it was already
                # edited away since - this save isn't about that fill.
                continue

            if final_value and final_value.lower() != logged_value.lower():
                correction, created = ScanFieldCorrection.objects.get_or_create(
                    user=user, item_type=item.type, field=field, ai_value=logged_value,
                    defaults={
                        'corrected_value': final_value,
                        'source': 'enrichment',
                        'enrichment_method': log.enrichment_type,
                    },
                )
                if not created:
                    if correction.corrected_value.lower() == final_value.lower():
                        correction.times_seen += 1
                    else:
                        correction.corrected_value = final_value
                        correction.times_seen = 1
                    correction.save()
                _maybe_trip_circuit_breaker(user, log.enrichment_type, field)
            elif final_value and final_value.lower() == logged_value.lower():
                # The enrichment pipeline got it right and the user kept it -
                # any old enrichment-sourced correction mapping this exact
                # value away for this same field and item type no longer
                # reflects what they want.
                ScanFieldCorrection.objects.filter(
                    user=user, field=field, ai_value=logged_value, source='enrichment',
                    item_type=item.type,
                ).delete()
    except Exception:
        logger.warning('Failed to record enrichment correction feedback', exc_info=True)


# How many of a user's most-repeated corrections to mention in the OCR
# prompt - enough to cover a handful of recurring patterns without
# bloating every scan's prompt with this user's entire correction history.
MAX_PROMPT_HINTS = 8


def build_ocr_correction_hints(user) -> str:
    """
    A short natural-language addendum for the OCR vision prompt, listing
    this user's most-repeated field corrections (across all sources - a
    scan misread and a reverted merchant_lookup fill are the same kind of
    signal here: "this is what this user's data actually looks like").
    apply_learned_corrections() already fixes an exact repeat of a known
    bad value after the fact; this is the complementary, proactive half -
    it gives the model a chance to get a similar-but-not-identical read
    right on the first pass, which pure post-hoc find-and-replace can't
    catch. Best-effort: never raises, returns '' when there's nothing
    worth mentioning or on any failure.
    """
    if user is None:
        return ''
    try:
        corrections = (
            ScanFieldCorrection.objects
            .filter(user=user, field__in=LEARNABLE_FIELDS)
            .exclude(ai_value='')
            .order_by('-times_seen', '-updated_at')[:MAX_PROMPT_HINTS]
        )
        lines = [
            f'- for "{c.field}", something that looks like "{c.ai_value}" is usually actually "{c.corrected_value}"'
            for c in corrections
        ]
        if not lines:
            return ''
        return (
            "\n\nThis user has corrected the following misreadings before - keep these "
            "patterns in mind, but still report exactly what the card shows if it clearly "
            "doesn't match:\n" + '\n'.join(lines)
        )
    except Exception:
        logger.warning('Failed to build OCR correction hints', exc_info=True)
        return ''


def apply_learned_corrections(user, result: dict) -> list[str]:
    """
    Mutates an OCR extraction `result` in place, swapping values this user
    has corrected before. Returns the list of healed field names (for the
    frontend's "adjusted from your history" note). Type is healed first so
    every other field's lookup - blank-fill and non-blank alike - can use
    the corrected type as context: a correction learned in one item type's
    context (e.g. a travel ticket's barcode symbology) should never bleed
    into an unrelated item type whose scan happens to produce the same
    misreading. 'type' itself has no item-type context to scope by, and
    when the extraction didn't guess a type at all there's nothing to scope
    with either, so both cases fall back to the unscoped match.
    """
    healed = []
    try:
        ordered = ('type',) + tuple(f for f in LEARNABLE_FIELDS if f != 'type')
        for field in ordered:
            ai_value = _normalize(result.get(field))
            item_type = _normalize(result.get('type'))
            if ai_value:
                candidates = ScanFieldCorrection.objects.filter(
                    user=user, field=field, ai_value__iexact=ai_value,
                )
                if field != 'type' and item_type:
                    candidates = candidates.filter(item_type=item_type)
            else:
                # Blank fills need stronger evidence and the right context:
                # same item type, seen at least twice.
                if not item_type:
                    continue
                candidates = ScanFieldCorrection.objects.filter(
                    user=user, field=field, ai_value='', item_type=item_type,
                    times_seen__gte=BLANK_FILL_MIN_SEEN,
                )
            best = candidates.order_by('-times_seen', '-updated_at').first()
            if best is None:
                continue
            if best.corrected_value.lower() == ai_value.lower():
                continue
            if not _healed_value_is_valid(field, best.corrected_value):
                continue
            result[field] = best.corrected_value
            healed.append(field)
    except Exception:
        logger.warning('Failed to apply learned scan corrections', exc_info=True)
    return healed
