import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from myapp.models import EnrichmentFieldPreference, Item, ItemEnrichmentLog, ScanFieldCorrection, SiteConfiguration
from myapp.scan_learning import CIRCUIT_BREAKER_TRIP_THRESHOLD, record_enrichment_correction_feedback


def _make_item(user, **kwargs):
    defaults = {
        'type': 'giftcard', 'name': 'Test Item', 'redeem_code': 'ABC123',
        'issuer': 'Test Issuer', 'expiry_date': date.today() + timedelta(days=30),
        'value': Decimal('10.00'), 'user': user,
    }
    defaults.update(kwargs)
    return Item.objects.create(**defaults)


def _make_log(item, field_name, new_value, enrichment_type='merchant_lookup'):
    return ItemEnrichmentLog.objects.create(
        enrichment_run_id=uuid.uuid4(), item=item, field_name=field_name,
        old_value='irrelevant', new_value=new_value, enrichment_type=enrichment_type,
    )


class CircuitBreakerTestCase(TestCase):
    """
    Once a (user, method, field) combo racks up enough enrichment-sourced
    corrections, the self-tuning confidence penalty alone (#236) isn't
    enough - the circuit breaker auto-creates an EnrichmentFieldPreference
    opt-out so the method stops touching that field for this user until a
    human clears it.
    """

    def setUp(self):
        self.user = User.objects.create_user('breakeruser', 'b@example.com', 'password123')

    def _trip_via_reversals(self, count, field='issuer', method='merchant_lookup'):
        """Simulate `count` separate corrected-away enrichment fills for
        distinct ai_values, which is how record_enrichment_correction_feedback
        actually accrues correction_count (one row per distinct ai_value)."""
        for i in range(count):
            item = _make_item(self.user, issuer=f'Wrong{i}')
            _make_log(item, field, f'Wrong{i}', enrichment_type=method)
            item.issuer = 'Corrected'
            record_enrichment_correction_feedback(self.user, item, {field: f'Wrong{i}'})

    def test_no_preference_created_below_the_trip_threshold(self):
        self._trip_via_reversals(CIRCUIT_BREAKER_TRIP_THRESHOLD - 1)
        self.assertFalse(
            EnrichmentFieldPreference.objects.filter(user=self.user, method='merchant_lookup', field_name='issuer').exists()
        )

    def test_preference_created_at_the_trip_threshold(self):
        self._trip_via_reversals(CIRCUIT_BREAKER_TRIP_THRESHOLD)
        pref = EnrichmentFieldPreference.objects.get(user=self.user, method='merchant_lookup', field_name='issuer')
        self.assertIn(str(CIRCUIT_BREAKER_TRIP_THRESHOLD), pref.reason)

    def test_log_type_is_translated_to_preference_method_vocabulary(self):
        """ItemEnrichmentLog/ScanFieldCorrection say 'ocr_rescan';
        EnrichmentFieldPreference (and _excluded_fields) says 'ocr' - the
        breaker must write the latter or the opt-out would silently do
        nothing."""
        self._trip_via_reversals(CIRCUIT_BREAKER_TRIP_THRESHOLD, method='ocr_rescan')
        self.assertTrue(
            EnrichmentFieldPreference.objects.filter(user=self.user, method='ocr', field_name='issuer').exists()
        )
        self.assertFalse(
            EnrichmentFieldPreference.objects.filter(method='ocr_rescan').exists()
        )

    def test_auto_enrich_has_no_preference_surface_and_never_trips(self):
        self._trip_via_reversals(CIRCUIT_BREAKER_TRIP_THRESHOLD + 5, method='auto_enrich')
        self.assertFalse(EnrichmentFieldPreference.objects.filter(user=self.user).exists())

    def test_field_outside_preference_choices_never_trips(self):
        self._trip_via_reversals(CIRCUIT_BREAKER_TRIP_THRESHOLD + 5, field='journey_origin')
        self.assertFalse(EnrichmentFieldPreference.objects.filter(user=self.user).exists())

    def test_existing_manual_preference_is_not_overwritten_or_duplicated(self):
        EnrichmentFieldPreference.objects.create(user=self.user, method='merchant_lookup', field_name='issuer')
        self._trip_via_reversals(CIRCUIT_BREAKER_TRIP_THRESHOLD)
        prefs = EnrichmentFieldPreference.objects.filter(user=self.user, method='merchant_lookup', field_name='issuer')
        self.assertEqual(prefs.count(), 1)
        self.assertEqual(prefs.first().reason, '')  # manual preference untouched

    @patch('requests.post')
    def test_alert_sent_when_topic_configured(self, mock_post):
        config = SiteConfiguration.load()
        config.security_alert_ntfy_topic = 'breaker-topic'
        config.save()

        self._trip_via_reversals(CIRCUIT_BREAKER_TRIP_THRESHOLD)

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertIn('breaker-topic', args[0])
        self.assertIn(b'issuer', kwargs['data'])

    @patch('requests.post')
    def test_no_alert_when_topic_not_configured(self, mock_post):
        self._trip_via_reversals(CIRCUIT_BREAKER_TRIP_THRESHOLD)
        mock_post.assert_not_called()

    def test_once_tripped_the_preference_is_actually_respected_by_the_pipeline(self):
        """End-to-end: after tripping, ItemEnricher._excluded_fields for
        this user+method now includes the field, via the real pipeline
        entry point rather than just checking the ledger row exists."""
        from myapp.services.enrichment import ItemEnricher
        self._trip_via_reversals(CIRCUIT_BREAKER_TRIP_THRESHOLD, method='merchant_lookup')

        enricher = ItemEnricher()
        excluded = enricher._excluded_fields(self.user, 'merchant_lookup')
        self.assertIn('issuer', excluded)
