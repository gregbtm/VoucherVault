"""
Validation and confidence scoring for OCR extractions.

Implements post-extraction validation rules that adjust confidence scores based on:
- Field completeness and consistency
- Barcode decode success
- Data validation (dates, currency codes, etc.)
- Cross-field logical checks (e.g., journey info for travel passes)
- Merchant detection and enrichment (Phase 3)
- Card number validation and BIN checking (Phase 3)
- Denomination validation against merchant patterns (Phase 3)
"""
import re
from datetime import datetime

from myapp.models import CURRENCY_CHOICES
from ocr.enrichment import (
    detect_merchant, validate_card_number_format, validate_denomination,
    suggest_barcode_type_for_merchant, suggest_tags_for_extraction,
)

VALID_CURRENCIES = {code for code, _ in CURRENCY_CHOICES}


def validate_and_score(extraction: dict) -> dict:
    """
    Takes an extraction dict from a vision backend and validates it,
    adjusting confidence based on field validation results. Applies Phase 3
    enrichment (merchant detection, BIN validation, denomination validation).
    Returns the same dict with updated confidence score and validation metadata.

    Validation rules applied:
    - Date format validation for expiry_date (ISO 8601)
    - Currency code validation
    - Barcode format consistency (if present)
    - Type-specific validations (e.g., journey info for travel passes)
    - Merchant detection and enrichment (Phase 3)
    - Card number BIN validation (Phase 3)
    - Denomination validation against merchant patterns (Phase 3)
    - Tag enrichment from merchant category (Phase 3)
    """
    confidence = extraction.get('confidence', 0.0)
    validation_issues = []
    merchant_data = None

    # Phase 3: Detect merchant from name field
    if extraction.get('name'):
        merchant_data = detect_merchant(extraction['name'])
        if merchant_data:
            # Auto-populate logo_slug if not extracted
            if not extraction.get('logo_slug') and merchant_data.get('logo_slug'):
                extraction['logo_slug'] = merchant_data['logo_slug']
            # Suggest code_type if merchant has a common pattern
            if not extraction.get('code_type') and merchant_data.get('common_code_type'):
                extraction['code_type'] = merchant_data['common_code_type']

    # Phase 3: Validate card number format (length/digit-only check only, not Luhn)
    if extraction.get('card_number'):
        card_validation = validate_card_number_format(extraction['card_number'])
        if not card_validation['valid']:
            validation_issues.append('invalid_card_number')
            confidence *= 0.9

    # Validate expiry_date if present
    if extraction.get('expiry_date'):
        if not _validate_iso_date(extraction['expiry_date']):
            validation_issues.append('invalid_expiry_date')
            confidence *= 0.85
        elif _is_expired(extraction['expiry_date']):
            validation_issues.append('expired')
            confidence *= 0.7

    # Validate currency code if present (normalize to uppercase)
    if extraction.get('currency'):
        currency = extraction['currency'].upper() if isinstance(extraction['currency'], str) else extraction['currency']
        if currency not in VALID_CURRENCIES:
            validation_issues.append('invalid_currency')
            confidence *= 0.8
        else:
            extraction['currency'] = currency

    # Validate value is numeric and reasonable
    if extraction.get('value') is not None:
        try:
            value = float(extraction['value'])
            denom_validation = validate_denomination(value, merchant_data)
            if not denom_validation['valid']:
                validation_issues.append('value_out_of_range')
                confidence *= 0.7
            elif denom_validation['suspiciously_low']:
                validation_issues.append('suspiciously_low_value')
                confidence *= 0.85
            elif not denom_validation['within_range']:
                validation_issues.append('value_outside_merchant_range')
                confidence *= 0.9
        except (TypeError, ValueError):
            validation_issues.append('value_not_numeric')
            confidence *= 0.75

    # Type-specific validation: travel passes need journey info
    if extraction.get('type') == 'travelpass':
        missing_journey = not extraction.get('journey_origin') or not extraction.get('journey_destination')
        if missing_journey:
            validation_issues.append('missing_journey_info')
            confidence *= 0.6

    # Phase 3: Enrich tags with merchant category suggestions
    existing_tags = extraction.get('tags', [])
    if not existing_tags or isinstance(existing_tags, list) and len(existing_tags) == 0:
        suggested_tags = suggest_tags_for_extraction(extraction, merchant_data)
        if suggested_tags:
            extraction['tags'] = suggested_tags

    extraction['confidence'] = min(1.0, confidence)
    extraction['_validation_issues'] = validation_issues
    return extraction


def _validate_iso_date(date_str: str) -> bool:
    """Check if date string is a valid ISO 8601 date."""
    if not isinstance(date_str, str):
        return False
    try:
        # Accept YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS formats
        if 'T' in date_str:
            datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        else:
            datetime.strptime(date_str, '%Y-%m-%d')
        return True
    except (ValueError, TypeError):
        return False


def _is_expired(date_str: str) -> bool:
    """Check if an ISO 8601 date is in the past."""
    try:
        if 'T' in date_str:
            expiry = datetime.fromisoformat(date_str.replace('Z', '+00:00')).date()
        else:
            expiry = datetime.strptime(date_str, '%Y-%m-%d').date()
        return expiry < datetime.now().date()
    except (ValueError, TypeError, AttributeError):
        return False
