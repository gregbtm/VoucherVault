"""Tests for merchant lookup enrichment service."""

from decimal import Decimal
from django.test import TestCase
from django.contrib.auth.models import User
from myapp.models import Item
from myapp.services.merchant_lookup import MerchantLookup


class MerchantLookupTestCase(TestCase):
    """Test cases for MerchantLookup service."""

    def setUp(self):
        """Create test users and items."""
        self.user = User.objects.create_user('testuser', 'test@example.com', 'testpass123')

    def _create_test_item(self, name='Test Item', issuer=None, expiry_date=None,
                         value=None, value_type=None, user=None, set_defaults=True):
        """Helper to create test items with required fields."""
        if user is None:
            user = self.user

        # Only set defaults if explicitly requested; otherwise allow None for enrichment testing
        if expiry_date is None and set_defaults:
            expiry_date = '2026-12-31'
        if value is None and set_defaults:
            value = Decimal('10.00')
        if value_type is None and set_defaults:
            value_type = 'GBP'

        return Item.objects.create(
            user=user,
            name=name,
            type='giftcard',
            issuer=issuer,
            expiry_date=expiry_date,
            value=value,
            value_type=value_type or 'GBP',
            redeem_code='TEST123',
            code_type='code128'
        )

    def test_find_similar_items_exact_match(self):
        """Test finding items with exact issuer match."""
        self._create_test_item(issuer='Starbucks', value=Decimal('25.00'), set_defaults=True)
        self._create_test_item(issuer='Starbucks', value=Decimal('50.00'), set_defaults=True)
        self._create_test_item(issuer='Starbucks', set_defaults=False)  # Target with no value

        lookup = MerchantLookup(self.user)
        similar = lookup._find_similar_items('Starbucks')

        # Should find 3 items total with exact issuer match
        self.assertEqual(len(similar), 3)

    def test_find_similar_items_fuzzy_match(self):
        """Test fuzzy matching on issuer names."""
        self._create_test_item(issuer='Starbucks Coffee', value=Decimal('25.00'), set_defaults=True)
        self._create_test_item(issuer='Amazon', value=Decimal('50.00'), set_defaults=True)

        lookup = MerchantLookup(self.user)
        similar = lookup._find_similar_items('Starbucks')

        # Should find Starbucks Coffee via fuzzy matching
        self.assertGreater(len(similar), 0)
        self.assertTrue(any(item.issuer == 'Starbucks Coffee' for item in similar))

    def test_infer_expiry_date(self):
        """Test inferring expiry date from similar items."""
        expiry = '2025-12-31'
        self._create_test_item(issuer='Starbucks', expiry_date=expiry)
        self._create_test_item(issuer='Starbucks', expiry_date=expiry)
        self._create_test_item(issuer='Starbucks', expiry_date='2026-01-15')

        similar = [
            Item.objects.get(issuer='Starbucks', expiry_date='2025-12-31'),
            Item.objects.get(issuer='Starbucks', expiry_date='2025-12-31'),
        ]

        lookup = MerchantLookup(self.user)
        inferred_date, confidence = lookup._infer_expiry_date(similar)

        self.assertEqual(inferred_date, expiry)
        self.assertGreater(confidence, Decimal('0.0'))

    def test_infer_value_average(self):
        """Test inferring value as average of similar items."""
        self._create_test_item(issuer='Amazon', value=Decimal('50.00'))
        self._create_test_item(issuer='Amazon', value=Decimal('75.00'))
        self._create_test_item(issuer='Amazon', value=Decimal('100.00'))

        similar = Item.objects.filter(issuer='Amazon')

        lookup = MerchantLookup(self.user)
        inferred_value, confidence = lookup._infer_value(similar)

        expected = Decimal('75.00')  # Average of 50, 75, 100
        self.assertEqual(inferred_value, expected)
        self.assertGreater(confidence, Decimal('0.0'))

    def test_infer_value_type(self):
        """Test inferring value type from similar items."""
        self._create_test_item(issuer='Tesco', value_type='GBP')
        self._create_test_item(issuer='Tesco', value_type='GBP')
        self._create_test_item(issuer='Tesco', value_type='EUR')

        similar = Item.objects.filter(issuer='Tesco')

        lookup = MerchantLookup(self.user)
        inferred_type, confidence = lookup._infer_value_type(similar)

        self.assertEqual(inferred_type, 'GBP')
        self.assertGreater(confidence, Decimal('0.0'))

    def test_calculate_confidence_low_sample(self):
        """Test confidence calculation for small sample sizes."""
        lookup = MerchantLookup(self.user)

        # 2 items should give 0.4 confidence
        confidence = lookup._calculate_confidence(2)
        self.assertEqual(confidence, Decimal('0.4'))

    def test_calculate_confidence_medium_sample(self):
        """Test confidence calculation for medium sample sizes."""
        lookup = MerchantLookup(self.user)

        # 10 items should give 0.8 confidence
        confidence = lookup._calculate_confidence(10)
        self.assertEqual(confidence, Decimal('0.8'))

    def test_calculate_confidence_high_sample(self):
        """Test confidence calculation for large sample sizes."""
        lookup = MerchantLookup(self.user)

        # 20 items should give 0.95 confidence
        confidence = lookup._calculate_confidence(20)
        self.assertEqual(confidence, Decimal('0.95'))

    def test_enrich_from_merchant_no_similar_items(self):
        """Test enrichment returns empty when no similar merchants found."""
        target_item = self._create_test_item(issuer='UniqueStore123')

        lookup = MerchantLookup(self.user)
        enrichment = lookup.enrich_from_merchant(target_item)

        self.assertEqual(enrichment, {})

    def test_enrich_from_merchant_with_value_inference(self):
        """Test successful value inference from similar merchants."""
        # Create similar merchants with known values
        self._create_test_item(issuer='Starbucks', value=Decimal('25.00'), set_defaults=True)
        self._create_test_item(issuer='Starbucks', value=Decimal('50.00'), set_defaults=True)
        self._create_test_item(issuer='Starbucks', value=Decimal('75.00'), set_defaults=True)

        # Target item with no value
        target_item = self._create_test_item(issuer='Starbucks', value=None, set_defaults=False)

        lookup = MerchantLookup(self.user)
        enrichment = lookup.enrich_from_merchant(target_item, confidence_threshold=Decimal('0.6'))

        # Should infer value
        self.assertIn('value', enrichment)
        self.assertIn('value_confidence', enrichment)
        self.assertEqual(enrichment['value'], Decimal('50.00'))  # Average of 25, 50, 75

    def test_enrich_respects_confidence_threshold(self):
        """Test that enrichment respects confidence threshold."""
        # Create only 2 similar items (low confidence = 0.4)
        self._create_test_item(issuer='Starbucks', value=Decimal('25.00'), set_defaults=True)
        self._create_test_item(issuer='Starbucks', value=Decimal('50.00'), set_defaults=True)

        target_item = self._create_test_item(issuer='Starbucks', value=None, set_defaults=False)

        # With high threshold (0.8), should not infer
        lookup = MerchantLookup(self.user)
        enrichment = lookup.enrich_from_merchant(target_item, confidence_threshold=Decimal('0.8'))

        self.assertEqual(enrichment, {})

    def test_string_similarity_exact(self):
        """Test string similarity for exact matches."""
        lookup = MerchantLookup(self.user)
        similarity = lookup._string_similarity('Starbucks', 'Starbucks')

        self.assertEqual(similarity, 1.0)

    def test_string_similarity_partial(self):
        """Test string similarity for partial matches."""
        lookup = MerchantLookup(self.user)
        similarity = lookup._string_similarity('Starbucks', 'Starbucks Coffee')

        self.assertGreater(similarity, 0.7)
        self.assertLess(similarity, 1.0)

    def test_string_similarity_low(self):
        """Test string similarity for dissimilar strings."""
        lookup = MerchantLookup(self.user)
        similarity = lookup._string_similarity('Starbucks', 'Amazon')

        self.assertLess(similarity, 0.3)

    def test_enrich_from_merchant_empty_issuer(self):
        """Test enrichment with empty issuer returns empty dict."""
        target_item = self._create_test_item(issuer=None)

        lookup = MerchantLookup(self.user)
        enrichment = lookup.enrich_from_merchant(target_item)

        self.assertEqual(enrichment, {})

    def test_cache_similar_items(self):
        """Test that similar items are cached."""
        self._create_test_item(issuer='Starbucks', value=Decimal('25.00'))

        lookup = MerchantLookup(self.user)

        # First call
        similar1 = lookup._find_similar_items('Starbucks')
        # Second call should use cache
        similar2 = lookup._find_similar_items('Starbucks')

        self.assertEqual(len(similar1), len(similar2))

    def test_integration_enrich_from_merchant_multiple_fields(self):
        """Test enriching multiple fields from merchant lookup."""
        # Create comprehensive similar merchant profile
        self._create_test_item(
            issuer='Sainsburys',
            value=Decimal('50.00'),
            value_type='GBP',
            expiry_date='2025-12-31',
            set_defaults=True
        )
        self._create_test_item(
            issuer='Sainsburys',
            value=Decimal('50.00'),
            value_type='GBP',
            expiry_date='2025-12-31',
            set_defaults=True
        )

        # Target item needing enrichment (no defaults set so fields are None)
        target_item = self._create_test_item(
            name='Sainsburys Card',
            issuer='Sainsburys',
            value=None,
            value_type=None,
            expiry_date=None,
            set_defaults=False
        )

        lookup = MerchantLookup(self.user)
        enrichment = lookup.enrich_from_merchant(target_item, confidence_threshold=Decimal('0.5'))

        # Should enrich all three fields
        self.assertIn('value', enrichment)
        self.assertIn('value_type', enrichment)
        self.assertIn('expiry_date', enrichment)
