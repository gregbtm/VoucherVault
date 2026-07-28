from decimal import Decimal
from datetime import timedelta
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from myapp.models import Item, ItemRecommendation
from myapp.smart_features import (
    generate_item_recommendations, generate_all_recommendations,
    cleanup_recommendations_for_inactive_items, MANAGED_REASONS,
)


class GenerateItemRecommendationsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('smartuser', 'smart@example.com', 'password123')

    def _create_item(self, **kwargs):
        defaults = {
            'user': self.user,
            'name': 'Test Item',
            'type': 'giftcard',
            'redeem_code': 'CODE1',
            'issuer': 'Test Issuer',
            'expiry_date': timezone.localtime().date() + timedelta(days=30),
            'value': Decimal('50.00'),
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def test_expiring_today_or_tomorrow_gets_very_soon_recommendation(self):
        today_item = self._create_item(expiry_date=timezone.localtime().date())
        recs = generate_item_recommendations(today_item)
        self.assertIn('expires_very_soon', recs)
        rec = ItemRecommendation.objects.get(item=today_item, reason='expires_very_soon')
        self.assertIn('today', rec.action.lower())

        tomorrow_item = self._create_item(expiry_date=timezone.localtime().date() + timedelta(days=1))
        recs = generate_item_recommendations(tomorrow_item)
        self.assertIn('expires_very_soon', recs)
        rec = ItemRecommendation.objects.get(item=tomorrow_item, reason='expires_very_soon')
        self.assertIn('tomorrow', rec.action.lower())

    def test_expiring_within_week_gets_soon_recommendation(self):
        item = self._create_item(expiry_date=timezone.localtime().date() + timedelta(days=5))
        recs = generate_item_recommendations(item)
        self.assertIn('expires_soon', recs)

    def test_expired_item_gets_no_recommendation_and_clears_stale_one(self):
        item = self._create_item(expiry_date=timezone.localtime().date() + timedelta(days=5))
        generate_item_recommendations(item)
        self.assertTrue(ItemRecommendation.objects.filter(item=item, reason='expires_soon').exists())

        # Regression: the old implementation had `if days_left < 0: pass`,
        # which meant a stale "expires in N days" recommendation was never
        # cleaned up once the item actually expired.
        item.expiry_date = timezone.localtime().date() - timedelta(days=1)
        item.save()
        recs = generate_item_recommendations(item)
        self.assertEqual(recs, [])
        self.assertFalse(ItemRecommendation.objects.filter(item=item, reason='expires_soon').exists())

    def test_low_balance_recommendation(self):
        item = self._create_item(value=Decimal('2.50'), value_type='money')
        recs = generate_item_recommendations(item)
        self.assertIn('low_balance', recs)

    def test_zero_balance_does_not_trigger_low_balance(self):
        """A fully-drained card (£0) isn't 'low', it's spent - shouldn't nag."""
        item = self._create_item(value=Decimal('0.00'), value_type='money')
        recs = generate_item_recommendations(item)
        self.assertNotIn('low_balance', recs)

    def test_unused_recommendation(self):
        item = self._create_item(last_used_at=timezone.now() - timedelta(days=200))
        recs = generate_item_recommendations(item)
        self.assertIn('unused', recs)

    def test_used_item_clears_all_managed_recommendations(self):
        item = self._create_item(expiry_date=timezone.localtime().date() + timedelta(days=5))
        generate_item_recommendations(item)
        self.assertTrue(ItemRecommendation.objects.filter(item=item).exists())

        item.is_used = True
        item.save()
        recs = generate_item_recommendations(item)
        self.assertEqual(recs, [])
        self.assertFalse(ItemRecommendation.objects.filter(item=item, reason__in=MANAGED_REASONS).exists())

    def test_archived_item_clears_all_managed_recommendations(self):
        item = self._create_item(expiry_date=timezone.localtime().date() + timedelta(days=5))
        generate_item_recommendations(item)
        item.is_archived = True
        item.save()
        recs = generate_item_recommendations(item)
        self.assertEqual(recs, [])
        self.assertFalse(ItemRecommendation.objects.filter(item=item).exists())

    def test_dismissed_recommendation_stays_dismissed_on_regeneration(self):
        item = self._create_item(expiry_date=timezone.localtime().date() + timedelta(days=5))
        generate_item_recommendations(item)
        rec = ItemRecommendation.objects.get(item=item, reason='expires_soon')
        rec.dismissed_at = timezone.now()
        rec.save()

        generate_item_recommendations(item)
        rec.refresh_from_db()
        self.assertIsNotNone(rec.dismissed_at)


class SignalIntegrationTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('signaluser', 'signal@example.com', 'password123')

    def test_item_save_triggers_recommendation_generation(self):
        item = Item.objects.create(
            user=self.user, name='Signal Item', type='giftcard', redeem_code='SIG1',
            issuer='Issuer', expiry_date=timezone.localtime().date() + timedelta(days=1),
            value=Decimal('10.00'),
        )
        self.assertTrue(ItemRecommendation.objects.filter(item=item, reason='expires_very_soon').exists())

    def test_marking_item_used_clears_recommendations_via_signal(self):
        item = Item.objects.create(
            user=self.user, name='Signal Item', type='giftcard', redeem_code='SIG2',
            issuer='Issuer', expiry_date=timezone.localtime().date() + timedelta(days=1),
            value=Decimal('10.00'),
        )
        self.assertTrue(ItemRecommendation.objects.filter(item=item).exists())
        item.is_used = True
        item.save()
        self.assertFalse(ItemRecommendation.objects.filter(item=item).exists())


class CleanupInactiveItemsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('cleanupuser', 'cleanup@example.com', 'password123')

    def test_cleanup_removes_recommendations_for_used_items_bypassing_signal(self):
        item = Item.objects.create(
            user=self.user, name='Bypass Item', type='giftcard', redeem_code='BYP1',
            issuer='Issuer', expiry_date=timezone.localtime().date() + timedelta(days=1),
            value=Decimal('10.00'),
        )
        self.assertTrue(ItemRecommendation.objects.filter(item=item).exists())

        # Simulate the signal having failed to run (e.g. a bulk .update()
        # bypasses post_save) by writing is_used directly.
        Item.objects.filter(id=item.id).update(is_used=True)
        self.assertTrue(ItemRecommendation.objects.filter(item=item).exists())

        cleanup_recommendations_for_inactive_items()
        self.assertFalse(ItemRecommendation.objects.filter(item=item).exists())


class DismissRecommendationViewTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('viewuser', 'view@example.com', 'password123')
        self.other_user = User.objects.create_user('otheruser', 'other@example.com', 'password123')
        self.client = Client()
        self.client.login(username='viewuser', password='password123')
        self.item = Item.objects.create(
            user=self.user, name='View Item', type='giftcard', redeem_code='VIEW1',
            issuer='Issuer', expiry_date=timezone.localtime().date() + timedelta(days=1),
            value=Decimal('10.00'),
        )
        self.recommendation = ItemRecommendation.objects.get(item=self.item, reason='expires_very_soon')

    def test_dismiss_marks_recommendation_dismissed(self):
        response = self.client.post(
            reverse('dismiss_recommendation', args=[self.recommendation.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.recommendation.refresh_from_db()
        self.assertIsNotNone(self.recommendation.dismissed_at)

    def test_dismiss_scoped_to_owner(self):
        """Another user can't dismiss (or even discover via 404-vs-403) someone else's recommendation."""
        other_client = Client()
        other_client.login(username='otheruser', password='password123')
        response = other_client.post(
            reverse('dismiss_recommendation', args=[self.recommendation.id]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 404)
        self.recommendation.refresh_from_db()
        self.assertIsNone(self.recommendation.dismissed_at)

    def test_show_items_includes_active_recommendations(self):
        response = self.client.get(reverse('show_items'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.recommendation, response.context['active_recommendations'])

    def test_dismissed_recommendation_excluded_from_show_items(self):
        self.recommendation.dismissed_at = timezone.now()
        self.recommendation.save()
        response = self.client.get(reverse('show_items'))
        self.assertNotIn(self.recommendation, response.context['active_recommendations'])


class GenerateAllRecommendationsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('bulkuser', 'bulk@example.com', 'password123')

    def test_generate_all_skips_used_and_archived_items(self):
        active = Item.objects.create(
            user=self.user, name='Active', type='giftcard', redeem_code='A1',
            issuer='Issuer', expiry_date=timezone.localtime().date() + timedelta(days=1),
            value=Decimal('10.00'),
        )
        used = Item.objects.create(
            user=self.user, name='Used', type='giftcard', redeem_code='A2',
            issuer='Issuer', expiry_date=timezone.localtime().date() + timedelta(days=1),
            value=Decimal('10.00'), is_used=True,
        )
        generate_all_recommendations(self.user)
        self.assertTrue(ItemRecommendation.objects.filter(item=active).exists())
        self.assertFalse(ItemRecommendation.objects.filter(item=used).exists())
