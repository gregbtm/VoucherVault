"""
Utility functions for displaying OCR results and validation feedback to users.
Provides human-readable messages about extraction quality, enrichment, and validation issues.
"""


def get_validation_message(extraction: dict) -> str:
    """
    Generates a human-readable message about extraction quality based on confidence
    and validation issues. Useful for UI tooltips and feedback messages.
    """
    confidence = extraction.get('confidence', 0.0)
    issues = extraction.get('_validation_issues', [])

    if confidence >= 0.9:
        return "Extraction looks good"
    elif confidence >= 0.75:
        msg = "Extraction is mostly reliable"
        if issues:
            msg += f" (some issues: {', '.join(issues[:2])})"
        return msg
    elif confidence >= 0.5:
        return "Extraction has some issues, review carefully"
    else:
        return "Extraction quality is low, please verify manually"


def get_enrichment_details(extraction: dict) -> dict:
    """
    Extracts enrichment metadata that indicates which fields were auto-populated
    or enriched by the validation/enrichment pipeline. Helps users understand
    which fields came from the vision model vs. enrichment.
    """
    details = {
        'auto_populated': [],
        'enriched_fields': {},
        'confidence_score': extraction.get('confidence', 0.0),
        'validation_issues': extraction.get('_validation_issues', []),
    }

    # Detect which fields were enriched by checking for characteristic patterns
    # These would be populated by detect_merchant, suggest_tags_for_extraction, etc.
    if extraction.get('logo_slug') and not extraction.get('name'):
        details['auto_populated'].append('logo_slug')
        details['enriched_fields']['logo_slug'] = 'Populated from merchant database'

    if extraction.get('code_type') == 'code128' and not extraction.get('code'):
        details['auto_populated'].append('code_type')
        details['enriched_fields']['code_type'] = 'Suggested from merchant pattern'

    if extraction.get('tags') and isinstance(extraction['tags'], list) and len(extraction['tags']) > 0:
        if not extraction.get('name'):
            details['enriched_fields']['tags'] = 'Generated from item type'
        else:
            details['enriched_fields']['tags'] = 'Suggested from merchant category'

    return details


def confidence_level_badge(confidence: float) -> dict:
    """
    Returns a badge-style indicator for extraction confidence.
    Useful for UI displays showing extraction quality at a glance.

    Returns: {level: 'high'|'medium'|'low', color: 'green'|'amber'|'red',
              label: 'High Confidence'|'Medium Confidence'|'Low Confidence'}
    """
    if confidence >= 0.85:
        return {
            'level': 'high',
            'color': 'green',
            'label': 'High Confidence',
            'description': 'Model is confident in this extraction',
        }
    elif confidence >= 0.7:
        return {
            'level': 'medium',
            'color': 'amber',
            'label': 'Medium Confidence',
            'description': 'Some fields may need review',
        }
    else:
        return {
            'level': 'low',
            'color': 'red',
            'label': 'Low Confidence',
            'description': 'Please verify fields carefully',
        }


def format_card_info(card_number: str) -> dict:
    """
    Formats card number information for UI display.
    Returns masked card number and BIN info for security and usability.
    """
    from ocr.enrichment import validate_card_number_format

    if not card_number:
        return {'masked': None, 'bin': None, 'length': 0, 'format': 'invalid'}

    validation = validate_card_number_format(card_number)

    if not validation['valid']:
        return {'masked': None, 'bin': None, 'length': validation['length'], 'format': 'invalid'}

    # Mask the card number (show first 4 and last 4 digits)
    digits = ''.join(c for c in card_number if c.isdigit())
    if len(digits) >= 8:
        masked = f"{digits[:4]} **** **** {digits[-4:]}"
    else:
        masked = '**** ' * (len(digits) // 4)

    return {
        'masked': masked,
        'bin': validation['bin'],
        'length': validation['length'],
        'format': 'valid',
    }


def suggest_improvements(extraction: dict) -> list[str]:
    """
    Suggests improvements to help users refine the extraction.
    Returns a list of actionable suggestions.
    """
    suggestions = []

    if not extraction.get('code'):
        suggestions.append('Add the redemption code if visible on the card')

    if not extraction.get('expiry_date'):
        suggestions.append('Add the expiry date if visible')

    if not extraction.get('value'):
        suggestions.append('Add the monetary value if this is a gift card')

    if not extraction.get('name') and extraction.get('type') == 'giftcard':
        suggestions.append('Identify which retailer this gift card is for')

    if extraction.get('type') == 'travelpass':
        if not extraction.get('journey_origin'):
            suggestions.append('Add departure station or airport code')
        if not extraction.get('journey_destination'):
            suggestions.append('Add destination station or airport code')

    if extraction.get('confidence', 0.0) < 0.7:
        suggestions.append('Consider re-scanning the card with better lighting')

    return suggestions
