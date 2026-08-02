import re
from abc import ABC, abstractmethod
from urllib.parse import urlparse

from myapp.models import CURRENCY_CHOICES, Item
from ocr.validation import validate_and_score

# Kept in sync with the <select id="code_type"> options in
# create-item.html/edit-item.html. A vision backend's code_type guess is
# only ever useful to the frontend if it's one of these - anything else
# would leave the <select> with nothing selected, so callers must validate
# against this set before returning a code_type.
VALID_CODE_TYPES = {
    'qrcode', 'none', 'ean13', 'ean8', 'code128', 'code39', 'code93',
    'codabar', 'upca', 'upce', 'isbn13', 'issn', 'pdf417', 'datamatrix',
    'azteccode', 'interleaved2of5',
}

# Kept in sync with Item.currency's choices - a vision backend's currency
# guess is only useful to the frontend if it's a code the <select> actually
# offers.
VALID_CURRENCIES = {code for code, _ in CURRENCY_CHOICES}

# Kept in sync with Item.type's choices - same reasoning as VALID_CODE_TYPES.
VALID_ITEM_TYPES = {code for code, _ in Item.ITEM_TYPES}

_JSON_FENCE_RE = re.compile(r'^\s*```[a-zA-Z]*\s*\n?|\n?\s*```\s*$')


def strip_json_fences(text: str) -> str:
    """
    Vision models are told to respond with ONLY a JSON object, but
    sometimes wrap it in a markdown code fence anyway (a well-documented
    quirk, especially with smaller/cheaper models) - without this, a
    fenced-but-otherwise-perfectly-correct response fails json.loads() and
    gets silently treated identically to "nothing could be read", which is
    indistinguishable from an actual extraction failure to the end user.
    """
    return _JSON_FENCE_RE.sub('', text).strip()


def parse_float_or_none(value) -> float | None:
    """Best-effort numeric coercion for the "value" field - a vision model
    occasionally returns a currency-formatted string despite instructions
    (e.g. "50.00" as a JSON string, or with a stray currency symbol)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        cleaned = re.sub(r'[^0-9.\-]', '', str(value))
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def sanitize_confidence(value, default: float) -> float:
    """
    Clamps a model-reported per-field confidence (e.g. "value_confidence",
    "expiry_date_confidence" - see the self-consistency re-read
    instructions in the OCR prompt) into [0.0, 1.0]. Falls back to
    `default` (normally the overall "confidence") when the model omits
    the field entirely or returns something unparseable, rather than
    silently treating a missing self-consistency read as zero confidence.
    """
    if value is None:
        return default
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


_DOMAIN_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$')


def sanitize_domain_slug(value) -> str | None:
    """
    Best-effort cleanup for a vision model's "logo_slug" guess - the
    img.logo.dev lookup this feeds (see view-item.html) wants a bare
    domain like "uber.com", but a model occasionally includes a scheme,
    "www.", or trailing path despite being told not to. Strips those,
    then discards anything that still doesn't look like a real domain
    rather than risk feeding garbage into an <img src>.
    """
    if not value or not isinstance(value, str):
        return None
    slug = value.strip().lower()
    slug = re.sub(r'^https?://', '', slug)
    slug = re.sub(r'^www\.', '', slug)
    slug = slug.split('/')[0]
    return slug if _DOMAIN_RE.match(slug) else None


def sanitize_url(value) -> str | None:
    """Best-effort validation for a vision model's "balance_check_url"
    guess - only trusted if it parses as a well-formed http(s) URL, since
    anything else would just be a dead link on the item page."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    parsed = urlparse(value)
    return value if parsed.scheme in ('http', 'https') and parsed.netloc else None


def sanitize_free_text(value, max_length: int) -> str | None:
    """
    Best-effort cleanup for a vision model's free-text guess (description/
    notes) - truncates rather than discards a too-long response, since an
    over-length but otherwise-good answer is still useful with the excess
    trimmed, unlike a malformed domain/URL where partial data is useless.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    return text[:max_length] if text else None


_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def sanitize_time_or_none(value) -> str | None:
    """
    Best-effort validation for a vision model's "travel_time" guess - only
    trusted if it's a well-formed 24-hour "HH:MM" string, since that's
    what the <input type="time"> on the create/edit item forms expects.
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    return value if _TIME_RE.match(value) else None


_MAX_SUGGESTED_TAGS = 4
_MAX_TAG_LENGTH = 30


def sanitize_tag_suggestions(value) -> list[str]:
    """
    Best-effort cleanup for a vision model's "tags" guess - a JSON array
    of short category-like strings (e.g. ["Restaurant", "Food Delivery"]).
    Drops anything that isn't a short plain string, dedupes case-
    insensitively, and caps the count - this feeds directly into checking
    boxes / prefilling a text field client-side, so garbage here would be
    directly user-visible rather than just silently wrong.
    """
    if not isinstance(value, list):
        return []
    seen = set()
    tags = []
    for item in value:
        if not isinstance(item, str):
            continue
        tag = item.strip()
        if not tag or len(tag) > _MAX_TAG_LENGTH or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        tags.append(tag)
        if len(tags) >= _MAX_SUGGESTED_TAGS:
            break
    return tags


VISION_CODE_TYPE_OPTIONS = ', '.join(sorted(VALID_CODE_TYPES - {'none'}))
VISION_ITEM_TYPE_OPTIONS = ', '.join(sorted(VALID_ITEM_TYPES))
VISION_MAX_DESCRIPTION_LENGTH = 300
VISION_MAX_NOTES_LENGTH = 1000
VISION_MAX_JOURNEY_STATION_LENGTH = 100

# Shared verbatim between ClaudeOCRBackend and OpenAIOCRBackend - same
# prompt, same response shape, so either can be selected via OCR_BACKEND
# without any other code caring which one is active.
VISION_PROMPT = (
    'This image shows a voucher, coupon, gift card, or loyalty card - it '
    'may be a photo of a physical card, or a screenshot (an emailed gift '
    'card, a retailer app screen, a digital wallet pass, a confirmation '
    'page). Extract whatever of the following you can confidently read: '
    'the redeem/reference code printed or barcoded on it, a separate PIN '
    'or security code if one is shown apart from the main code, a printed '
    'card/member/serial number if different from the redeem code, the '
    'merchant or brand name, the issuer (if different from the merchant), '
    'the monetary value and its currency, and the expiry date. If a '
    'barcode or QR code is physically visible anywhere on the card '
    '(stripes, dots, or a grid pattern), put its encoded value in the '
    '"code" field and its symbology in "code_type" — even when the '
    'barcode encodes a card/account number rather than the separately '
    'printed alphanumeric redemption code; in that case put the '
    'text-only code in "card_number" instead. Only use code_type '
    '"none" when there is absolutely no machine-readable symbol '
    'anywhere on the card. Also try to identify the actual redeemable '
    'brand\'s '
    'website domain for logo lookup purposes - this is often the same as '
    'the issuer, but if the card was sold through a marketplace or '
    'reseller (e.g. a card usable at "Uber" or "Uber Eats" but issued/'
    'sold by "Every Wish" or a similar gift-card marketplace), use the '
    'actual redeemable brand\'s domain, not the reseller\'s. Also extract '
    'a balance or validity check URL if one is visibly printed on the '
    'card itself. Also classify what kind of item this is, write a short '
    'one-sentence factual description of it (e.g. "£50 gift card for '
    'Uber and Uber Eats"), extract any redemption instructions or terms '
    'that are visibly printed on the card itself (e.g. expiry '
    'conditions, where it can be used, exclusions) - never invented, '
    'only if actually printed - and suggest up to 4 short category tags '
    'for organizing it (e.g. "Restaurant", "Food Delivery", "Retail", '
    '"Travel", "Coffee"). If this is a point-to-point travel ticket '
    '(e.g. a train, coach, or ferry ticket showing a departure and an '
    'arrival station/stop), classify "type" as "travelpass"; set '
    '"issuer" to the transport operator (e.g. "National Rail") and '
    '"name" to the journey itself (e.g. "Hatfield Peverel to London '
    'Terminals") rather than repeating the operator in "name" - a list '
    'of many tickets from the same operator is far more useful showing '
    'the route than the operator\'s name over and over. Also extract '
    'the journey origin and destination exactly as printed '
    '(e.g. "Hatfield Peverel" and "London Terminals", or short codes '
    'like "HAP" and "LON" if that is all that is shown), plus a '
    'scheduled departure/travel time if one is printed on the ticket '
    '(e.g. "09:14") - leave journey origin, destination, and travel '
    'time all null for anything that is not a point-to-point journey '
    'ticket, or if no specific time is printed. Respond with ONLY a '
    'JSON object, no '
    'other text and no markdown code fences, in exactly this shape:\n'
    '{"code": "...", "code_type": "...", "name": "...", "issuer": "...", '
    '"expiry_date": "YYYY-MM-DD", "pin": "...", "value": 0.00, '
    '"currency": "...", "card_number": "...", "logo_slug": "...", '
    '"balance_check_url": "...", "type": "...", "description": "...", '
    '"notes": "...", "tags": ["...", "..."], "journey_origin": "...", '
    '"journey_destination": "...", "travel_time": "...", '
    '"confidence": 0.0, "expiry_date_confidence": 0.0, "value_confidence": 0.0}\n'
    f'"code_type" must be exactly one of: {VISION_CODE_TYPE_OPTIONS}, "none" '
    '(ONLY when there is genuinely no barcode, QR code, or any other '
    'machine-readable symbol visible anywhere on the card — if any '
    'bars, dots, or geometric patterns are visible, identify the '
    'symbology instead of choosing "none"), or null (if you cannot '
    'tell). "currency" must be a '
    'three-letter ISO 4217 code (e.g. "GBP", "USD", "EUR") or null. '
    '"logo_slug" must be a bare domain with no scheme or "www." (e.g. '
    '"uber.com"), or null if you cannot confidently tell. "travel_time" '
    'must be a 24-hour "HH:MM" string (e.g. "09:14") if a specific '
    'departure/travel time is printed on the ticket, or null. '
    '"balance_check_url" must be a full URL including "https://", or '
    'null - never a URL you are guessing at, only one actually printed '
    f'on the card. "type" must be exactly one of: {VISION_ITEM_TYPE_OPTIONS}, '
    'or null. "tags" must be a JSON array of short strings (an empty '
    'array if nothing fits) - never invent brand names as tags, only '
    'general categories. Use null for any other field you cannot '
    'confidently determine, or that is genuinely blank on the card '
    'itself (e.g. an empty PIN box) - never invent a value. Before '
    'answering, re-read the "code" character by character as a separate '
    'pass and confirm it against your first read - this is the single '
    'most important field, since it is what actually redeems the card, '
    'and a one-character misread (e.g. "O" vs "0", "1" vs "I" vs "l", '
    '"S" vs "5", "B" vs "8") produces a wrong code that still looks '
    'plausible. If the two reads disagree, or any character is genuinely '
    'ambiguous at the image\'s resolution, prefer the reading you are '
    'more confident in and reflect that uncertainty in "confidence" '
    'rather than silently picking one. "confidence" is your own estimate '
    '(0.0-1.0) of how reliable the "code" extraction specifically is. '
    'Do the same independent second-pass re-read for "expiry_date" and '
    '"value" - a smudged or low-resolution digit in a date or a price is '
    'just as easy to misread as a code character (e.g. "3" vs "8", "1" '
    'vs "7", a "/" mistaken for the date separator changing day and '
    'month) - and report your own confidence in each as '
    '"expiry_date_confidence" and "value_confidence" (0.0-1.0), '
    'independently of "confidence" and of each other - a clearly-printed '
    'date on a card with an ambiguous code should still get a high '
    '"expiry_date_confidence" even if "confidence" is low.'
)


def empty_vision_extraction() -> dict:
    """The all-None/zero-confidence fallback shape both vision backends
    (Claude, OpenAI) return when the model's response isn't valid JSON."""
    return {
        'code': None, 'code_type': None, 'name': None, 'issuer': None, 'expiry_date': None,
        'pin': None, 'value': None, 'currency': None, 'card_number': None,
        'logo_slug': None, 'balance_check_url': None, 'type': None,
        'description': None, 'notes': None, 'tags': [], 'journey_origin': None,
        'journey_destination': None, 'travel_time': None, 'confidence': 0.0,
    }


def finalize_vision_extraction(result: dict) -> dict:
    """
    Shared post-processing for both vision backends' raw JSON `result`:
    validates code_type/currency/type against the sets the frontend
    actually offers, sanitizes free-text/URL/domain/time fields, falls a
    travelpass classification back to giftcard when the model didn't
    also extract a journey origin+destination (an "I saw travel-ish
    wording but this isn't actually a ticket" false positive), propagates
    the overall confidence to any per-field confidence the model omitted,
    and runs the result through validate_and_score() before returning.
    """
    code_type = result.get('code_type') or None
    if code_type not in VALID_CODE_TYPES:
        code_type = None

    currency = (result.get('currency') or '').upper() or None
    if currency not in VALID_CURRENCIES:
        currency = None

    item_type = result.get('type') or None
    if item_type not in VALID_ITEM_TYPES:
        item_type = None

    journey_origin = sanitize_free_text(result.get('journey_origin'), VISION_MAX_JOURNEY_STATION_LENGTH)
    journey_destination = sanitize_free_text(result.get('journey_destination'), VISION_MAX_JOURNEY_STATION_LENGTH)

    if item_type == 'travelpass' and not (journey_origin and journey_destination):
        item_type = 'giftcard'

    extraction = {
        'code': result.get('code') or None,
        'code_type': code_type,
        'name': result.get('name') or None,
        'issuer': result.get('issuer') or None,
        'expiry_date': result.get('expiry_date') or None,
        'pin': result.get('pin') or None,
        'value': parse_float_or_none(result.get('value')),
        'currency': currency,
        'card_number': result.get('card_number') or None,
        'logo_slug': sanitize_domain_slug(result.get('logo_slug')),
        'balance_check_url': sanitize_url(result.get('balance_check_url')),
        'type': item_type,
        'description': sanitize_free_text(result.get('description'), VISION_MAX_DESCRIPTION_LENGTH),
        'notes': sanitize_free_text(result.get('notes'), VISION_MAX_NOTES_LENGTH),
        'tags': sanitize_tag_suggestions(result.get('tags')),
        'journey_origin': journey_origin,
        'journey_destination': journey_destination,
        'travel_time': sanitize_time_or_none(result.get('travel_time')),
        'confidence': float(result.get('confidence') or 0.0),
    }
    overall_confidence = extraction['confidence']
    extraction['expiry_date_confidence'] = sanitize_confidence(
        result.get('expiry_date_confidence'), overall_confidence,
    )
    extraction['value_confidence'] = sanitize_confidence(
        result.get('value_confidence'), overall_confidence,
    )
    return validate_and_score(extraction)


class OCRBackend(ABC):
    """
    Extracts a redeem code (and, where possible, other item fields) from a
    photo of a physical voucher/coupon/loyalty card. Implementations must
    never raise on a "nothing found" result — return an all-None payload
    with confidence 0.0 instead. Raising is reserved for backend
    unavailability (e.g. the tesseract binary isn't installed).
    """

    @abstractmethod
    def extract(self, image_bytes: bytes, media_type: str, user=None) -> dict:
        """
        `user` is optional context, not a permission check - when given, a
        vision-model backend may use it to look up that user's past field
        corrections (see myapp.scan_learning.build_ocr_correction_hints)
        and mention recurring patterns in the prompt, so a value it would
        otherwise keep misreading has a chance to come back right on the
        first pass rather than only being fixed after the fact by
        apply_learned_corrections. A backend that has no prompt to enrich
        (e.g. local tesseract) accepts and ignores it.

        Returns a dict with keys: code, code_type, name, issuer,
        expiry_date (ISO 8601 string or None), pin, value (float or None),
        currency, card_number, logo_slug, balance_check_url, type,
        description, notes, tags (list of str), journey_origin,
        journey_destination, confidence (0.0-1.0).
        code_type is one of VALID_CODE_TYPES or None if the backend can't
        or didn't try to determine the barcode symbology. currency is one
        of VALID_CURRENCIES or None. logo_slug is a bare domain (e.g.
        "uber.com") for the actual redeemable brand - which a vision
        backend may distinguish from "issuer" when the card was sold
        through a marketplace/reseller - or None. balance_check_url is a
        full http(s) URL if one is visibly printed on the card, else
        None. type is one of VALID_ITEM_TYPES or None. description is a
        short factual one-liner or None. notes is redemption
        instructions/terms if visibly printed on the card, else None -
        never invented. tags is a list of 0-4 short suggested category
        names (e.g. ["Restaurant"]) for the frontend to match against the
        user's existing tags or suggest as new ones. journey_origin and
        journey_destination are the departure/arrival station or stop for
        a point-to-point travel ticket (e.g. "Hatfield Peverel"/"HAP" and
        "London Terminals"/"LON"), used by the Active Today widget -
        expected alongside type="travelpass". travel_time is a 24-hour
        "HH:MM" string if a specific departure/travel time is printed on
        the ticket, else None. journey_origin, journey_destination, and
        travel_time are all None for anything that isn't that kind of
        ticket.
        """
        ...
