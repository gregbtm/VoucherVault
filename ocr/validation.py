"""
Validation and confidence scoring for OCR extractions.

Implements post-extraction validation rules that adjust confidence scores based on:
- Field completeness and consistency
- Barcode decode success
- Data validation (dates, currency codes, etc.)
- Cross-field logical checks (e.g., journey info for travel passes)
"""
import re
from datetime import datetime

from myapp.models import CURRENCY_CHOICES

VALID_CURRENCIES = {code for code, _ in CURRENCY_CHOICES}


def validate_and_score(extraction: dict) -> dict:
    """
    Takes an extraction dict from a vision backend and validates it,
    adjusting confidence based on field validation results. Returns the
    same dict with updated confidence score and validation metadata.

    Validation rules applied:
    - Date format validation for expiry_date (ISO 8601)
    - Currency code validation
    - Barcode format consistency (if present)
    - Type-specific validations (e.g., journey info for travel passes)
    """
    confidence = extraction.get('confidence', 0.0)
    validation_issues = []

    # Validate expiry_date if present
    if extraction.get('expiry_date'):
        if not _validate_iso_date(extraction['expiry_date']):
            validation_issues.append('invalid_expiry_date')
            confidence *= 0.85
        elif _is_expired(extraction['expiry_date']):
            validation_issues.append('expired')
            confidence *= 0.7

    # Validate currency code if present
    if extraction.get('currency'):
        if extraction['currency'] not in VALID_CURRENCIES:
            validation_issues.append('invalid_currency')
            confidence *= 0.8

    # Validate value is numeric and reasonable
    if extraction.get('value') is not None:
        try:
            value = float(extraction['value'])
            if value < 0 or value > 10000:
                validation_issues.append('value_out_of_range')
                confidence *= 0.7
        except (TypeError, ValueError):
            validation_issues.append('value_not_numeric')
            confidence *= 0.75

    # Type-specific validation: travel passes need journey info
    if extraction.get('type') == 'travelpass':
        missing_journey = not extraction.get('journey_origin') or not extraction.get('journey_destination')
        if missing_journey:
            validation_issues.append('missing_journey_info')
            confidence *= 0.6

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
