import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from myapp.models import Item, ItemEnrichmentLog, ItemRecommendation
from myapp.smart_features import MANAGED_REASONS, generate_item_recommendations


class NeedsReviewRecommendationTestCase(TestCase):
    """
    Cross-pollination between the enrichment pipeline and Smart
    Suggestions: a field the validation pass flagged as too risky to
    auto-correct (see ItemEnricher._detect_review_flags /
    get_active_flags_for_items) now also surfaces as a 'needs_review'
    ItemRecommendation, not just the separate inline flag icon - so it
    shows up wherever a user already checks Smart Suggestions.
    """

    def setUp(self):
        self.user = User.objects.create_user('reviewrecuser', 'rr@example.com', 'password123')

    def _create_item(self, **kwargs):
        defaults = {
            'user': self.user, 'name': 'Test Item', 'type': 'giftcard',
            'redeem_code': 'CODE1', 'issuer': 'Test Issuer',
            'expiry_date': timezone.localtime().date() + timedelta(days=30),
            'value': Decimal('50.00'),
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def _flag(self, item, field_name, new_value, reason='Worth checking'):
        ItemEnrichmentLog.objects.create(
            item=item, enrichment_run_id=str(uuid.uuid4()), field_name=field_name,
            old_value=new_value, new_value=new_value, enrichment_type='flagged', reason=reason,
        )

    def test_active_flag_produces_a_needs_review_recommendation(self):
        item = self._create_item(pin='12AB')
        self._flag(item, 'pin', '12AB', reason='Non-numeric PIN')

        recs = generate_item_recommendations(item)

        self.assertIn('needs_review', recs)
        rec = ItemRecommendation.objects.get(item=item, reason='needs_review')
        self.assertIn('pin', rec.action)
        self.assertEqual(rec.priority, 3)

    def test_no_flag_means_no_needs_review_recommendation(self):
        item = self._create_item()

        recs = generate_item_recommendations(item)

        self.assertNotIn('needs_review', recs)
        self.assertFalse(ItemRecommendation.objects.filter(item=item, reason='needs_review').exists())

    def test_editing_the_flagged_field_retires_the_recommendation(self):
        item = self._create_item(pin='12AB')
        self._flag(item, 'pin', '12AB', reason='Non-numeric PIN')
        generate_item_recommendations(item)
        self.assertTrue(ItemRecommendation.objects.filter(item=item, reason='needs_review').exists())

        item.pin = '1234'
        item.save()
        recs = generate_item_recommendations(item)

        self.assertNotIn('needs_review', recs)
        self.assertFalse(ItemRecommendation.objects.filter(item=item, reason='needs_review').exists())

    def test_multiple_flagged_fields_are_summarized_in_one_recommendation(self):
        item = self._create_item(pin='12AB', redeem_code='O0O0')
        self._flag(item, 'pin', '12AB', reason='Non-numeric PIN')
        self._flag(item, 'redeem_code', 'O0O0', reason='Ambiguous characters')

        generate_item_recommendations(item)

        recs = ItemRecommendation.objects.filter(item=item, reason='needs_review')
        self.assertEqual(recs.count(), 1)
        self.assertIn('pin', recs.first().action)
        self.assertIn('redeem_code', recs.first().action)

    def test_needs_review_is_a_managed_reason(self):
        self.assertIn('needs_review', MANAGED_REASONS)
