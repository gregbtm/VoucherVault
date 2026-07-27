"""
Enrichment helpers for OCR extractions using embedded data resources.

Provides fast lookups and suggestions using pre-embedded data:
- UK railway station code/name resolution
- Travel pass route validation
- Merchant detection and validation (airlines, hotels, retailers, etc.)
- BIN validation (card number first 6 digits)
- Industry-specific barcode pattern detection
- Denomination validation against merchant typical values
"""
import json
import logging
import os
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
_UK_STATIONS = None
_MERCHANTS = None
_MERCHANT_CACHE = {}  # Cache fuzzy match results to avoid repeated SequenceMatcher calls


def _load_uk_stations():
    """Lazy-load the UK stations database on first use."""
    global _UK_STATIONS
    if _UK_STATIONS is not None:
        return _UK_STATIONS

    stations_file = os.path.join(_DATA_DIR, 'uk_stations.json')
    try:
        with open(stations_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            stations = {}
            for station in data.get('stations', []):
                # Index by code and name for fast lookups
                code = station.get('code')
                name = station.get('name')
                if code:
                    stations[code.upper()] = station
                if name:
                    stations[name.upper()] = station
                # Index aliases (e.g. "London" -> "LON")
                for alias in station.get('aliases', []):
                    stations[alias.upper()] = station
            _UK_STATIONS = stations
            return stations
    except Exception as e:
        logger.warning(f"Failed to load UK stations database: {e}")
        return {}


def normalize_station(location_str: str) -> dict | None:
    """
    Takes a raw station name/code from vision model and attempts to resolve
    it to a known UK railway station. Returns station dict with code/name/region,
    or None if no match found.

    Supports partial matching: "London" → "LON", "KGX" → full King's Cross record.
    """
    if not location_str or not isinstance(location_str, str):
        return None

    stations = _load_uk_stations()
    if not stations:
        return None

    location_key = location_str.strip().upper()

    # Exact match first (fast path)
    if location_key in stations:
        return stations[location_key]

    # Substring search for partial matches (e.g. "manchester" -> "Manchester Piccadilly")
    for key, station in stations.items():
        if location_key in key or key in location_key:
            return station

    return None


def is_valid_travel_pass(extraction: dict) -> bool:
    """
    Checks if an extraction that claims to be a travel pass has the required
    fields populated. Returns True only if journey_origin, journey_destination,
    and ideally a redeem code are all present.
    """
    required_fields = ['journey_origin', 'journey_destination', 'code']
    return all(extraction.get(field) for field in required_fields)


def _load_merchants():
    """Lazy-load the merchants database on first use."""
    global _MERCHANTS
    if _MERCHANTS is not None:
        return _MERCHANTS

    merchants_file = os.path.join(_DATA_DIR, 'merchants.json')
    try:
        with open(merchants_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            merchants_dict = {}
            for merchant in data.get('merchants', []):
                name = merchant.get('name')
                if name:
                    merchants_dict[name.upper()] = merchant
            _MERCHANTS = merchants_dict
            return merchants_dict
    except Exception as e:
        logger.warning(f"Failed to load merchants database: {e}")
        return {}


def detect_merchant(name_str: str, similarity_threshold: float = 0.75) -> dict | None:
    """
    Fuzzy-matches extracted merchant name against known merchants.
    Uses SequenceMatcher with configurable similarity threshold (default 0.75).
    Returns merchant dict with category, logo_slug, typical values, etc., or None.

    Handles OCR misreads: "Amazn" → "Amazon", "Strbks" → "Starbucks".
    Results are cached to avoid repeated SequenceMatcher calls for common names.
    """
    if not name_str or not isinstance(name_str, str):
        return None

    merchants = _load_merchants()
    if not merchants:
        return None

    name_key = name_str.strip().upper()

    # Check cache first (avoids redundant fuzzy matching)
    if name_key in _MERCHANT_CACHE:
        return _MERCHANT_CACHE[name_key]

    # Exact match first (fast path)
    if name_key in merchants:
        _MERCHANT_CACHE[name_key] = merchants[name_key]
        return merchants[name_key]

    # Fuzzy match with SequenceMatcher - early exit if near-perfect match found
    best_match = None
    best_ratio = 0.0

    for merchant_name, merchant_data in merchants.items():
        ratio = SequenceMatcher(None, name_key, merchant_name).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = merchant_data
            if best_ratio > 0.95:  # Early exit if very high confidence match found
                break

    if best_ratio >= similarity_threshold:
        _MERCHANT_CACHE[name_key] = best_match
        return best_match

    _MERCHANT_CACHE[name_key] = None
    return None


def validate_card_number_format(card_number: str) -> dict:
    """
    Validates card number format and extracts BIN (first 6 digits).
    Returns {valid: bool, bin: str, length: int, luhn_valid: bool}.
    """
    if not card_number or not isinstance(card_number, str):
        return {'valid': False, 'bin': None, 'length': 0, 'luhn_valid': False}

    digits_only = ''.join(c for c in card_number if c.isdigit())
    if not digits_only or len(digits_only) < 13:
        return {'valid': False, 'bin': None, 'length': len(digits_only), 'luhn_valid': False}

    bin_code = digits_only[:6]
    luhn_valid = _validate_luhn(digits_only)

    return {
        'valid': True,
        'bin': bin_code,
        'length': len(digits_only),
        'luhn_valid': luhn_valid,
    }


def _validate_luhn(number_str: str) -> bool:
    """Validates a number against the Luhn algorithm."""
    try:
        digits = [int(d) for d in number_str if d.isdigit()]
        if len(digits) < 13:
            return False

        total = 0
        for i, digit in enumerate(reversed(digits)):
            if i % 2 == 1:
                digit *= 2
                if digit > 9:
                    digit -= 9
            total += digit
        return total % 10 == 0
    except (ValueError, TypeError):
        return False


def validate_denomination(value: float | None, merchant_data: dict | None) -> dict:
    """
    Validates if a value is within typical ranges for a merchant.
    Returns {valid: bool, within_range: bool, suspiciously_low: bool, reason: str}.
    """
    result = {
        'valid': True,
        'within_range': True,
        'suspiciously_low': False,
        'reason': None,
    }

    if value is None or not isinstance(value, (int, float)):
        result['valid'] = False
        result['reason'] = 'value is not numeric'
        return result

    if value < 0:
        result['valid'] = False
        result['reason'] = 'negative value'
        return result

    if value < 0.50 and merchant_data and merchant_data.get('category') not in ['Loyalty', 'Digital']:
        result['suspiciously_low'] = True
        result['reason'] = 'suspiciously low value for this merchant category'

    if value > 10000:
        result['valid'] = False
        result['reason'] = 'unreasonably high value'
        return result

    # Check against merchant typical values if available
    if merchant_data:
        typical_values = merchant_data.get('typical_values', [])
        if typical_values and None not in typical_values:
            typical_values = [v for v in typical_values if v is not None]
            if typical_values and value not in typical_values:
                min_val = min(typical_values)
                max_val = max(typical_values)
                if value < min_val * 0.5 or value > max_val * 2:
                    result['within_range'] = False
                    result['reason'] = f'outside typical range {min_val}-{max_val} for {merchant_data.get("name")}'

    return result


def suggest_barcode_type_for_merchant(merchant_data: dict | None) -> str | None:
    """
    Suggests a barcode type based on merchant industry patterns.
    Returns barcode type (qrcode, code128, datamatrix, etc.) or None.
    """
    if not merchant_data:
        return None

    return merchant_data.get('common_code_type')


def get_category_tags(category: str) -> list[str]:
    """
    Maps merchant category to 1-2 suggested tags.
    Used to enrich OCR extractions with sensible defaults.
    """
    category_to_tags = {
        'Retail': ['Retail'],
        'Electronics': ['Electronics'],
        'Coffee': ['Coffee', 'Food'],
        'Restaurant': ['Restaurant', 'Food'],
        'Grocery': ['Grocery'],
        'Fashion': ['Fashion'],
        'Transport': ['Travel', 'Transport'],
        'Food Delivery': ['Food Delivery'],
        'Entertainment': ['Entertainment'],
        'Music': ['Music'],
        'Gaming': ['Gaming'],
        'Books': ['Books'],
        'Pharmacy': ['Pharmacy'],
        'Home': ['Home'],
        'Furniture': ['Furniture'],
        'Sports': ['Sports'],
        'Pets': ['Pets'],
        'Cycling': ['Cycling'],
        'Luxury Retail': ['Luxury', 'Retail'],
        'Digital': ['Digital'],
        'Travel': ['Travel'],
        'Loyalty': ['Loyalty'],
    }
    return category_to_tags.get(category, [])


def suggest_tags_for_extraction(extraction: dict, merchant_data: dict | None) -> list[str]:
    """
    Combines merchant category tags with item type tags.
    Ensures no duplicates, limits to 4 tags per extraction schema.
    """
    tags = set()

    # Add tags from merchant category
    if merchant_data:
        category = merchant_data.get('category')
        if category:
            category_tags = get_category_tags(category)
            tags.update(category_tags)

    # Add tags from item type (from extraction)
    item_type = extraction.get('type')
    if item_type == 'travelpass':
        tags.update(['Travel', 'Transport'])
    elif item_type == 'giftcard':
        pass
    elif item_type == 'voucher':
        tags.add('Voucher')
    elif item_type == 'coupon':
        tags.add('Coupon')
    elif item_type == 'loyaltycard':
        tags.add('Loyalty')

    # Deduplicate and limit to 4
    tags_list = sorted(list(tags))[:4]
    return tags_list
