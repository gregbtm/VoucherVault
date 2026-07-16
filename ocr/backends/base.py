import re
from abc import ABC, abstractmethod
from urllib.parse import urlparse

from myapp.models import CURRENCY_CHOICES, Item

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


class OCRBackend(ABC):
    """
    Extracts a redeem code (and, where possible, other item fields) from a
    photo of a physical voucher/coupon/loyalty card. Implementations must
    never raise on a "nothing found" result — return an all-None payload
    with confidence 0.0 instead. Raising is reserved for backend
    unavailability (e.g. the tesseract binary isn't installed).
    """

    @abstractmethod
    def extract(self, image_bytes: bytes, media_type: str) -> dict:
        """
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
        "London Terminals"/"LON"), used by the Active Today widget - both
        None for anything that isn't that kind of ticket.
        """
        ...
