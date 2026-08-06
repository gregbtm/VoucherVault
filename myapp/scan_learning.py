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

retract_learned_correction() is the escape hatch for when the above gets
it wrong: a one-click "Undo" on a healed field deletes the specific
ScanFieldCorrection that applied, without waiting for a save to un-teach
it the slow way.

detect_systemic_misreads() looks across every user's corrections for a
third kind of signal apply_learned_corrections can't see on its own:
several different users independently teaching the exact same
merchant/field misreading is a sign of a systemic vision-model quirk for
that merchant, not personal preference - worth applying even for a user
who's never scanned that merchant before (see GlobalScanCorrection).

Only fields with stable, recurring values are learnable. Item-specific
fields (name, code, expiry, value, pin, card number) are deliberately
excluded - "corrections" there are just data entry, not a pattern.
"""

import logging
from datetime import timedelta

from django.db.models import Case, Count, F, IntegerField, Q, Value, When
from django.utils import timezone

from .models import GlobalScanCorrection, Item, ItemEnrichmentLog, ScanFieldCorrection
from .utils import levenshtein_distance

logger = logging.getLogger(__name__)

# field name -> Item attribute it compares against on save. travel_time is
# special-cased in _saved_value (TimeField -> "HH:MM" string).
LEARNABLE_FIELDS = (
    'issuer', 'logo_slug', 'currency', 'type', 'code_type',
    'journey_origin', 'journey_destination', 'travel_time',
)

# The retroactive-healing enrichment method (find_retroactive_heal_candidates
# below) may rewrite any learnable field *except* type/code_type - those two
# are already excluded from every other enrichment method via
# ItemEnricher.PROTECTED_FIELDS, for the same reason: a wrong guess there
# risks breaking per-type field visibility or barcode symbology, not just a
# cosmetic misread. Retroactive healing respects that existing boundary
# rather than carving out an exception for itself.
RETROACTIVE_HEAL_FIELDS = tuple(f for f in LEARNABLE_FIELDS if f not in ('type', 'code_type'))

# ItemEnrichmentLog.enrichment_type values that represent an auto-applied
# value (as opposed to 'flagged', which never changed anything).
ENRICHMENT_LOG_TYPES = ('ocr_rescan', 'validation', 'merchant_lookup', 'auto_enrich', 'learned_correction')

# ItemEnrichmentLog.enrichment_type (and ScanFieldCorrection.enrichment_method,
# which mirrors it) uses a different vocabulary from
# EnrichmentConfig.ENRICHMENT_METHODS / EnrichmentFieldPreference.method
# ('ocr' vs 'ocr_rescan') - this is what ItemEnricher._excluded_fields()
# actually queries against, so the circuit breaker below has to translate
# before it can opt a user out of a method for a field. 'auto_enrich' has
# no per-method opt-out surface at all, so it can never trip the breaker.
# 'learned_correction' (retroactive healing) deliberately uses the same
# string on both sides - unlike 'ocr'/'ocr_rescan', there was no pre-existing
# vocabulary to split, so there's no reason to introduce a second one.
_LOG_TYPE_TO_PREFERENCE_METHOD = {
    'ocr_rescan': 'ocr',
    'validation': 'validation',
    'merchant_lookup': 'merchant_lookup',
    'learned_correction': 'learned_correction',
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

# Exact match only catches a *repeat* of the exact same misreading - a
# different typo of an already-learned pattern (e.g. "Natinal Rail" when
# the learned correction is for "Nationl Rail") slips through entirely.
# The fuzzy fallback below only tries once no exact match exists, and is
# deliberately conservative: a tight edit-distance ceiling (matching
# check_duplicate_code's own precedent in myapp/views.py for "same code,
# one misread character"), and a higher times_seen bar than exact
# matches need - a single one-off typo correction shouldn't get to
# "generalize" to near-miss strings it was never actually taught for.
FUZZY_MATCH_MAX_DISTANCE = 2
FUZZY_MATCH_MIN_SEEN = 2

# How many distinct users independently teaching the exact same
# merchant/field misreading it takes before detect_systemic_misreads()
# promotes it to a GlobalScanCorrection. Two could be coincidence; three
# independently-arrived-at identical corrections for the same merchant is
# a much stronger signal of a shared vision-model quirk.
SYSTEMIC_MISREAD_MIN_USERS = 3

# How long a correction can go without healing a live scan before
# find_stale_corrections() surfaces it for an admin to judge - long enough
# that a merchant simply not being scanned for a season isn't noise, short
# enough that a correction whose merchant genuinely changed its card
# layout (making the correction actively wrong) doesn't sit silently
# unnoticed for years.
STALE_CORRECTION_THRESHOLD_DAYS = 365

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

            # Issuer-scoped only for fields that aren't the issuer/type
            # itself - scoping 'issuer' corrections by issuer would be
            # circular, and 'type' deliberately stays item_type/issuer
            # -unscoped to match its own healing-time treatment.
            issuer = _normalize(item.issuer) if field not in ('issuer', 'type') else ''

            if final_value and final_value.lower() != ai_value.lower():
                correction, created = ScanFieldCorrection.objects.get_or_create(
                    user=user, item_type=item.type, issuer=issuer, field=field, ai_value=ai_value,
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
                # want. Scoped by item_type (and issuer, for issuer-scoped
                # fields) to match how corrections are recorded and replayed
                # - retiring more broadly would un-teach a still-valid,
                # unrelated correction (e.g. a travel ticket's barcode
                # symbology, or a different merchant's card layout) just
                # because a different item type/merchant happened to
                # produce the same value and got kept.
                ScanFieldCorrection.objects.filter(
                    user=user, field=field, ai_value=ai_value, item_type=item.type, issuer=issuer,
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

            # Uses the pre-edit snapshot, not item.issuer - by this point
            # `item` already reflects the form's new values (possibly
            # including a just-changed issuer in this same save), but the
            # enrichment being corrected away happened against whatever
            # issuer was current *then*.
            issuer = (
                _normalize(before_values.get('issuer', '')) if field not in ('issuer', 'type') else ''
            )

            if final_value and final_value.lower() != logged_value.lower():
                correction, created = ScanFieldCorrection.objects.get_or_create(
                    user=user, item_type=item.type, issuer=issuer, field=field, ai_value=logged_value,
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
                # value away for this same field/item type/merchant no
                # longer reflects what they want.
                ScanFieldCorrection.objects.filter(
                    user=user, field=field, ai_value=logged_value, source='enrichment',
                    item_type=item.type, issuer=issuer,
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


def _order_by_relevance(qs, issuer: str):
    """
    Merchant-aware ranking for a candidates queryset that may contain both
    merchant-specific rows (issuer=<this scan's issuer>) and merchant-
    unscoped rows (issuer='', predating this feature or genuinely never
    merchant-specific). When the current scan has an issuer, a row scoped
    to that exact issuer wins over a blank one for the same ai_value -
    but the blank row is still a valid fallback rather than excluded
    outright, since excluding it would silently stop every legacy
    correction from ever replaying again.
    """
    if issuer:
        qs = qs.filter(Q(issuer='') | Q(issuer=issuer)).annotate(
            _specificity=Case(
                When(issuer=issuer, then=Value(1)), default=Value(0), output_field=IntegerField(),
            ),
        ).order_by('-_specificity', '-times_seen', '-updated_at')
    else:
        qs = qs.order_by('-times_seen', '-updated_at')
    return qs


def _find_fuzzy_correction(user, field: str, ai_value: str, item_type: str, issuer: str):
    """
    Only called when no exact (case-insensitive) match exists. Scans
    this user's (and, when given, this item type's) corrections for
    `field` for one within FUZZY_MATCH_MAX_DISTANCE edits of `ai_value`,
    requiring FUZZY_MATCH_MIN_SEEN - see the constants' docstring above
    for why both are conservative rather than matching the exact-match
    path's laxer bar. When the scan has an issuer, a merchant-unscoped
    candidate is still allowed (never excluded outright) but a same-
    issuer candidate always wins the tie-break first, ahead of
    times_seen and distance.
    """
    qs = ScanFieldCorrection.objects.filter(
        user=user, field=field, times_seen__gte=FUZZY_MATCH_MIN_SEEN,
    ).exclude(ai_value='')
    if item_type:
        qs = qs.filter(item_type=item_type)
    if issuer:
        qs = qs.filter(Q(issuer='') | Q(issuer=issuer))

    best_match, best_distance, best_specificity = None, None, None
    ai_value_lower = ai_value.lower()
    for candidate in qs:
        candidate_value = candidate.ai_value.lower()
        if abs(len(candidate_value) - len(ai_value_lower)) > FUZZY_MATCH_MAX_DISTANCE:
            continue
        distance = levenshtein_distance(ai_value_lower, candidate_value)
        if distance == 0 or distance > FUZZY_MATCH_MAX_DISTANCE:
            continue
        specificity = 1 if issuer and candidate.issuer == issuer else 0
        if (
            best_match is None
            or specificity > best_specificity
            or (specificity == best_specificity and candidate.times_seen > best_match.times_seen)
            or (
                specificity == best_specificity
                and candidate.times_seen == best_match.times_seen
                and distance < best_distance
            )
        ):
            best_match, best_distance, best_specificity = candidate, distance, specificity
    return best_match


def _find_global_correction(field: str, ai_value: str, item_type: str, issuer: str):
    """
    Last-resort fallback once this user's own corrections (exact and
    fuzzy) have both come up empty: a cross-user pattern several other
    users independently taught for this exact merchant (see
    GlobalScanCorrection/detect_systemic_misreads). Only reached when
    the scan actually has both an item_type and issuer to scope by -
    global promotion itself requires a non-blank issuer, so there's
    nothing to look up without one.
    """
    if not item_type or not issuer:
        return None
    return GlobalScanCorrection.objects.filter(
        item_type=item_type, issuer=issuer, field=field, ai_value__iexact=ai_value,
    ).first()


def apply_learned_corrections(user, result: dict, originals: dict = None) -> list[str]:
    """
    Mutates an OCR extraction `result` in place, swapping values this user
    has corrected before. Returns the list of healed field names (for the
    frontend's "adjusted from your history" note). When `originals` is
    given (a dict the caller owns), each healed field's pre-heal value and
    the id of the ScanFieldCorrection row that healed it are recorded into
    it as `{field: {'ai_value': ..., 'correction_id': ...}}` - the one-click
    "Undo" control's other half (see retract_learned_correction below)
    needs both: the ai_value to put back in the form, and the id to know
    exactly which row to retract, since a fuzzy-matched heal's `ai_value`
    doesn't equal the correction's own `ai_value`. Type and issuer are
    healed first (in that order - issuer immediately follows type in
    LEARNABLE_FIELDS) so every other field's lookup - blank-fill and
    non-blank alike - can use the corrected type/issuer as context: a
    correction learned in one item type's or merchant's context (e.g. a
    travel ticket's barcode symbology, or one merchant's card layout)
    should never bleed into an unrelated item type or merchant whose scan
    happens to produce the same misreading. Neither 'type' nor 'issuer'
    scopes its own lookup by item_type/issuer - that would be circular -
    and when the extraction didn't guess a type/issuer at all there's
    nothing to scope with either, so all of these fall back to the
    unscoped match. Item-type scoping is a hard filter (no item-type
    context yet on this correction means it can't have been learned for
    a different type), but issuer scoping is a soft preference, not an
    exclusion (see _order_by_relevance): a correction recorded before
    this feature existed, or for a field where issuer context genuinely
    wasn't captured, has issuer='' and must keep replaying for every
    merchant - a same-issuer match just outranks it when both exist.

    Non-blank fields that find no exact match fall back to a fuzzy,
    edit-distance match (see _find_fuzzy_correction) - a different typo
    of an already-learned misreading would otherwise never heal at all.
    Failing that too, a cross-user GlobalScanCorrection for this exact
    merchant is tried last (see _find_global_correction) - this is the
    only branch that can heal a field this particular user has never
    personally corrected before. Global matches are never recorded into
    `originals`: they aren't this user's own ScanFieldCorrection row to
    retract, so the one-click Undo control simply doesn't offer one for
    a globally-healed field (retracting a cross-user pattern because one
    user disagrees isn't a one-click action).
    """
    healed = []
    try:
        ordered = ('type',) + tuple(f for f in LEARNABLE_FIELDS if f != 'type')
        for field in ordered:
            ai_value = _normalize(result.get(field))
            item_type = _normalize(result.get('type'))
            issuer = _normalize(result.get('issuer')) if field not in ('type', 'issuer') else ''
            source_kind = 'exact'
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
            best = _order_by_relevance(candidates, issuer).first()
            if best is None and ai_value:
                best = _find_fuzzy_correction(
                    user, field, ai_value, item_type if field != 'type' else '', issuer,
                )
                if best is not None:
                    source_kind = 'fuzzy'
            if best is None and ai_value:
                best = _find_global_correction(
                    field, ai_value, item_type if field != 'type' else '', issuer,
                )
                if best is not None:
                    source_kind = 'global'
            if best is None:
                continue
            if best.corrected_value.lower() == ai_value.lower():
                continue
            if not _healed_value_is_valid(field, best.corrected_value):
                continue
            if source_kind == 'fuzzy':
                logger.info(
                    'Fuzzy-healed %s for user %s: %r -> %r (learned from %r)',
                    field, user.pk, ai_value, best.corrected_value, best.ai_value,
                )
            elif source_kind == 'global':
                logger.info(
                    'Cross-user-healed %s for user %s: %r -> %r (confirmed by %d users for %r)',
                    field, user.pk, ai_value, best.corrected_value, best.confirmed_by_users, issuer,
                )
            if originals is not None and source_kind != 'global':
                originals[field] = {'ai_value': ai_value, 'correction_id': best.pk}
            # .update() rather than best.save() - bypasses the auto_now
            # updated_at/promoted_at fields entirely (those track when the
            # mapping was last *taught*, not when it last *fired*), and
            # avoids a stale-instance overwrite race between concurrent
            # scans healing from the same row.
            model_cls = GlobalScanCorrection if source_kind == 'global' else ScanFieldCorrection
            model_cls.objects.filter(pk=best.pk).update(
                times_applied=F('times_applied') + 1, last_applied_at=timezone.now(),
                # A heal firing again means any prior staleness alert no
                # longer describes reality - clearing it lets a future
                # dry spell be reported as a genuinely new episode
                # instead of staying silently suppressed forever (see
                # find_stale_corrections).
                stale_alerted_at=None,
            )
            result[field] = best.corrected_value
            healed.append(field)
    except Exception:
        logger.warning('Failed to apply learned scan corrections', exc_info=True)
    return healed


def find_retroactive_heal_candidates(item: Item) -> list[dict]:
    """
    The backward-looking counterpart to apply_learned_corrections(): that
    function only ever heals a *future* scan, so a correction learned
    today never touches items scanned and saved before it existed. This
    checks an already-saved item's *current* field values against the same
    ScanFieldCorrection/GlobalScanCorrection tables, scoped by the exact
    same rules (item type hard filter, issuer soft preference via
    _order_by_relevance, personal correction before cross-user fallback) -
    so a heal that would have fired at scan time still fires now, applied
    retroactively (see ItemEnricher.enrich_from_learned_corrections, the
    caller that turns this into actual FieldChanges).

    Deliberately exact-match only, unlike apply_learned_corrections' fuzzy
    fallback: fuzzy-matching an item's already-saved value against a
    misreading is a materially different, riskier claim than fuzzy-matching
    a fresh OCR read - rewriting old, presumably-reviewed data on a fuzzy
    guess isn't worth the false-positive risk.

    Returns one dict per healable field: {field, old_value, new_value,
    source_kind, source}. source_kind is 'personal' or 'global'; source is
    the ScanFieldCorrection or GlobalScanCorrection row that supplied
    new_value (its times_seen/confirmed_by_users drives the caller's
    confidence score).
    """
    candidates = []
    item_type = _normalize(item.type)
    for field in RETROACTIVE_HEAL_FIELDS:
        current_value = _saved_value(item, field)
        if not current_value:
            continue
        issuer = _normalize(item.issuer) if field != 'issuer' else ''

        qs = ScanFieldCorrection.objects.filter(
            user=item.user, field=field, ai_value__iexact=current_value,
        )
        if item_type:
            qs = qs.filter(item_type=item_type)
        best = _order_by_relevance(qs, issuer).first()
        source_kind = 'personal'
        if best is None and item_type and issuer:
            best = GlobalScanCorrection.objects.filter(
                item_type=item_type, issuer=issuer, field=field, ai_value__iexact=current_value,
            ).first()
            source_kind = 'global'
        if best is None:
            continue
        if best.corrected_value.lower() == current_value.lower():
            continue
        if not _healed_value_is_valid(field, best.corrected_value):
            continue
        candidates.append({
            'field': field, 'old_value': current_value, 'new_value': best.corrected_value,
            'source_kind': source_kind, 'source': best,
        })
    return candidates


def retract_learned_correction(user, correction_id: int) -> bool:
    """
    One-click undo for a single healed field: deletes the exact
    ScanFieldCorrection row apply_learned_corrections just applied (its id
    came back to the frontend via that call's `originals` argument), so
    this user's next scan of the same misreading is left alone instead of
    "healed" the same wrong way again. A merchant that changes its card
    layout, or a one-off bad correction that slipped past the times_seen
    bar, both recover the same way: the user just says "no, that's wrong"
    once.

    Deletes outright rather than decrementing times_seen - if this really
    is still a good correction for a different context, the next genuine
    save will teach it again from scratch (record_scan_corrections), and
    a decayed-but-still-present row would just be a confusing halfway
    state. Scoped to `user` so one user can never retract another's
    correction by guessing an id; a missing/already-gone row is not an
    error, since the caller's local undo (reverting the form field) has
    already happened either way by the time this is called.
    """
    deleted_count, _ = ScanFieldCorrection.objects.filter(pk=correction_id, user=user).delete()
    return deleted_count > 0


def detect_systemic_misreads() -> list:
    """
    Periodic sweep (see myapp.tasks.detect_systemic_misreads_task): groups
    every user's ScanFieldCorrection rows by (item_type, issuer, field,
    ai_value) - excluding merchant-unscoped rows, since "N users agree"
    is a weak signal without a specific merchant to anchor it - and
    promotes/refreshes a GlobalScanCorrection wherever at least
    SYSTEMIC_MISREAD_MIN_USERS distinct users independently taught the
    same corrected_value for that group.

    When more than one corrected_value exists for the same group (rare,
    but two different users could have "corrected" the same misreading
    two different ways), the value with the most distinct users wins -
    ties keep whichever the query visits first, since `-distinct_users`
    ordering makes them equivalent anyway.

    Returns the GlobalScanCorrection rows that are newly promoted this
    run (didn't exist before), for the caller's admin alert - an
    already-promoted pattern getting its confirmed_by_users count
    refreshed isn't news. Best-effort: never raises.
    """
    newly_promoted = []
    try:
        rows = (
            ScanFieldCorrection.objects
            .exclude(issuer='')
            .values('item_type', 'issuer', 'field', 'ai_value', 'corrected_value')
            .annotate(distinct_users=Count('user', distinct=True))
            .order_by('-distinct_users')
        )
        best_per_key = {}
        for row in rows:
            key = (row['item_type'], row['issuer'], row['field'], row['ai_value'])
            best_per_key.setdefault(key, row)

        for (item_type, issuer, field, ai_value), row in best_per_key.items():
            if row['distinct_users'] < SYSTEMIC_MISREAD_MIN_USERS:
                continue
            obj, created = GlobalScanCorrection.objects.update_or_create(
                item_type=item_type, issuer=issuer, field=field, ai_value=ai_value,
                defaults={
                    'corrected_value': row['corrected_value'],
                    'confirmed_by_users': row['distinct_users'],
                },
            )
            if created:
                newly_promoted.append(obj)
    except Exception:
        logger.warning('Failed to detect systemic misreads', exc_info=True)
    return newly_promoted


def find_stale_corrections():
    """
    Read-only sweep (see myapp.tasks.flag_stale_corrections_task) for
    corrections that haven't healed anything in over
    STALE_CORRECTION_THRESHOLD_DAYS - either the merchant changed its
    card layout (the correction may now be actively wrong) or the user
    simply stopped seeing that merchant (harmless, but noise); either way
    worth a human's judgment rather than a guess.

    Neither model has a true creation timestamp, so "never fired and
    hasn't had a chance to prove itself yet" is approximated with the
    closest existing field: ScanFieldCorrection.updated_at (set on
    creation, refreshed only when a user re-teaches it - see
    apply_learned_corrections' use of .update() to avoid touching it on
    every heal) for personal corrections, and GlobalScanCorrection.
    promoted_at (refreshed weekly by detect_systemic_misreads() only
    while a pattern still meets quorum, so it freezes once support for a
    pattern erodes) for cross-user ones.

    Only returns rows with stale_alerted_at still null - already-reported
    rows aren't returned again on a later sweep unless they heal and go
    stale a second time (apply_learned_corrections clears
    stale_alerted_at whenever a heal actually fires), matching
    detect_systemic_misreads_task's "only alert on what's new" precedent.
    Never mutates state itself - the caller marks rows as alerted only
    once it has actually delivered the alert.
    """
    cutoff = timezone.now() - timedelta(days=STALE_CORRECTION_THRESHOLD_DAYS)
    try:
        personal = list(
            ScanFieldCorrection.objects.filter(stale_alerted_at__isnull=True)
            .filter(Q(last_applied_at__lt=cutoff) | Q(last_applied_at__isnull=True, updated_at__lt=cutoff))
            .select_related('user')
        )
    except Exception:
        logger.warning('Failed to query stale personal corrections', exc_info=True)
        personal = []
    try:
        global_rows = list(
            GlobalScanCorrection.objects.filter(stale_alerted_at__isnull=True)
            .filter(Q(last_applied_at__lt=cutoff) | Q(last_applied_at__isnull=True, promoted_at__lt=cutoff))
        )
    except Exception:
        logger.warning('Failed to query stale global corrections', exc_info=True)
        global_rows = []
    return personal, global_rows
