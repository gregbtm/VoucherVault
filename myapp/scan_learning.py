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

from .models import Item, ScanFieldCorrection

logger = logging.getLogger(__name__)

# field name -> Item attribute it compares against on save. travel_time is
# special-cased in _saved_value (TimeField -> "HH:MM" string).
LEARNABLE_FIELDS = (
    'issuer', 'logo_slug', 'currency', 'type', 'code_type',
    'journey_origin', 'journey_destination', 'travel_time',
)

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
                # The scan got it right this time and the user kept it -
                # any old correction mapping this exact value elsewhere no
                # longer reflects what they want.
                ScanFieldCorrection.objects.filter(
                    user=user, field=field, ai_value=ai_value,
                ).delete()
    except Exception:
        logger.warning('Failed to record scan corrections', exc_info=True)


def apply_learned_corrections(user, result: dict) -> list[str]:
    """
    Mutates an OCR extraction `result` in place, swapping values this user
    has corrected before. Returns the list of healed field names (for the
    frontend's "adjusted from your history" note). Type is healed first so
    blank-fill lookups can use the corrected type as context.
    """
    healed = []
    try:
        ordered = ('type',) + tuple(f for f in LEARNABLE_FIELDS if f != 'type')
        for field in ordered:
            ai_value = _normalize(result.get(field))
            if ai_value:
                candidates = ScanFieldCorrection.objects.filter(
                    user=user, field=field, ai_value__iexact=ai_value,
                )
            else:
                # Blank fills need stronger evidence and the right context:
                # same item type, seen at least twice.
                item_type = _normalize(result.get('type'))
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
