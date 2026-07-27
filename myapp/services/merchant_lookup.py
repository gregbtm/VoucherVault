"""Merchant lookup service for inferring item enrichment from similar existing items."""

from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any
from django.core.cache import cache
from django.contrib.auth.models import User
from difflib import SequenceMatcher
from myapp.models import Item


class MerchantLookup:
    """Infer enrichment values from similar merchants in user's existing items."""

    # Cache TTL for merchant profiles (24 hours)
    CACHE_TTL = 86400

    # Minimum similarity score for fuzzy matching (0-1)
    MIN_SIMILARITY = 0.6

    # Confidence thresholds based on sample size
    CONFIDENCE_THRESHOLDS = {
        'very_low': (0, 3),      # 0-2 items → 0.4 confidence
        'low': (3, 7),            # 3-6 items → 0.6 confidence
        'medium': (7, 15),        # 7-14 items → 0.8 confidence
        'high': (15, float('inf'))  # 15+ items → 0.95 confidence
    }

    def __init__(self, user: User):
        self.user = user

    def enrich_from_merchant(self, item: Item, confidence_threshold: Decimal = Decimal('0.5'),
                           enrichable_fields: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Infer enrichment data from similar merchants in user's existing items.
        Returns dict with field: value mappings and confidence scores.
        Enrichable fields default to: issuer, expiry_date, value, value_type.
        """
        if not item.issuer:
            return {}

        if enrichable_fields is None:
            enrichable_fields = ['expiry_date', 'value', 'value_type']

        # Find similar merchants by fuzzy matching on issuer name
        similar_items = self._find_similar_items(item.issuer)
        if not similar_items:
            return {}

        enrichment = {}

        # Infer expiry_date pattern from similar merchants
        if 'expiry_date' in enrichable_fields and similar_items:
            # Skip if field is already set (not default)
            if item.expiry_date is None or (isinstance(item.expiry_date, str) and not item.expiry_date):
                expiry_value, expiry_confidence = self._infer_expiry_date(similar_items)
                if expiry_value and expiry_confidence >= confidence_threshold:
                    enrichment['expiry_date'] = expiry_value
                    enrichment['expiry_date_confidence'] = expiry_confidence

        # Infer value from similar merchants
        if 'value' in enrichable_fields and similar_items:
            # Only enrich if value is zero or less (essentially empty)
            if not item.value or item.value <= 0:
                value, value_confidence = self._infer_value(similar_items)
                if value and value_confidence >= confidence_threshold:
                    enrichment['value'] = value
                    enrichment['value_confidence'] = value_confidence

        # Infer value_type from similar merchants
        if 'value_type' in enrichable_fields and similar_items:
            # Only enrich if value_type is empty
            if not item.value_type or item.value_type.strip() == '':
                value_type, vtype_confidence = self._infer_value_type(similar_items)
                if value_type and vtype_confidence >= confidence_threshold:
                    enrichment['value_type'] = value_type
                    enrichment['value_type_confidence'] = vtype_confidence

        return enrichment

    def _find_similar_items(self, issuer: str, limit: int = 20) -> List[Item]:
        """Find items with similar issuer names using fuzzy matching."""
        cache_key = f"merchant_lookup:{self.user.id}:{issuer}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        # Get all items for this user
        user_items = Item.objects.filter(user=self.user).exclude(issuer__isnull=True)

        # Fuzzy match on issuer name
        similar = []
        for user_item in user_items:
            if user_item.issuer:
                similarity = self._string_similarity(issuer.lower(), user_item.issuer.lower())
                if similarity >= self.MIN_SIMILARITY:
                    similar.append(user_item)

        # Sort by similarity score and limit results
        similar = similar[:limit]
        cache.set(cache_key, similar, self.CACHE_TTL)
        return similar

    def _infer_expiry_date(self, similar_items: List[Item]) -> Tuple[Optional[str], Decimal]:
        """Infer expiry_date pattern from similar items."""
        expiry_dates = [
            item.expiry_date for item in similar_items
            if item.expiry_date is not None
        ]

        if not expiry_dates:
            return None, Decimal('0.0')

        # Return the most common expiry date with confidence based on consistency
        from collections import Counter
        most_common = Counter(expiry_dates).most_common(1)[0][0]
        frequency = sum(1 for d in expiry_dates if d == most_common) / len(expiry_dates)

        confidence = self._calculate_confidence(len(expiry_dates)) * Decimal(str(frequency))
        return str(most_common), confidence

    def _infer_value(self, similar_items: List[Item]) -> Tuple[Optional[Decimal], Decimal]:
        """Infer value from similar items (average of non-null values)."""
        values = [
            item.value for item in similar_items
            if item.value is not None and item.value > 0
        ]

        if not values:
            return None, Decimal('0.0')

        average_value = sum(values) / len(values)
        confidence = self._calculate_confidence(len(values))

        # Quantize to 2 decimal places
        return average_value.quantize(Decimal('0.01')), confidence

    def _infer_value_type(self, similar_items: List[Item]) -> Tuple[Optional[str], Decimal]:
        """Infer value_type from similar items."""
        value_types = [
            item.value_type for item in similar_items
            if item.value_type is not None
        ]

        if not value_types:
            return None, Decimal('0.0')

        # Return the most common value_type
        from collections import Counter
        most_common = Counter(value_types).most_common(1)[0][0]
        frequency = sum(1 for vt in value_types if vt == most_common) / len(value_types)

        confidence = self._calculate_confidence(len(value_types)) * Decimal(str(frequency))
        return most_common, confidence

    def _calculate_confidence(self, sample_size: int) -> Decimal:
        """Calculate confidence score based on sample size."""
        for threshold_name, (min_size, max_size) in self.CONFIDENCE_THRESHOLDS.items():
            if min_size <= sample_size < max_size:
                if threshold_name == 'very_low':
                    return Decimal('0.4')
                elif threshold_name == 'low':
                    return Decimal('0.6')
                elif threshold_name == 'medium':
                    return Decimal('0.8')
                elif threshold_name == 'high':
                    return Decimal('0.95')
        return Decimal('0.0')

    def _string_similarity(self, a: str, b: str) -> float:
        """Calculate string similarity using SequenceMatcher (0-1)."""
        return SequenceMatcher(None, a, b).ratio()
