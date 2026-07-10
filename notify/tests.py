from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from myapp.models import Item

from .backends import get_backend
from .backends.apprise_backend import AppriseBackend
from .backends.ntfy import NtfyBackend
from .backends.webhook import WebhookBackend
from .forms import NotificationRuleForm
from .models import NotificationLog, NotificationRule
from .tasks import check_and_notify_expiry, fire_notifications, send_test_notification


def make_item(user, **kwargs):
    defaults = {
        'type': 'voucher',
        'name': 'Test Voucher',
        'redeem_code': 'ABC123',
        'issuer': 'Acme',
        'expiry_date': date.today() + timedelta(days=10),
        'value': '10.00',
        'user': user,
    }
    defaults.update(kwargs)
    return Item.objects.create(**defaults)


def make_rule(user, backend='ntfy', event_types=None, **config_overrides):
    config = {
        'ntfy': {'server': 'https://ntfy.example.com', 'topic': 'vouchervault'},
        'webhook': {'url': 'https://n8n.example.com/webhook/vv'},
        'apprise': {'urls': 'json://example.com/notify'},
    }[backend]
    config.update(config_overrides)
    return NotificationRule.objects.create(
        user=user, name=f'{backend} rule', backend=backend,
        config=config, enabled=True, event_types=event_types or ['expiry_warning'],
    )


class NotificationRuleModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_name_unique_per_user(self):
        NotificationRule.objects.create(user=self.user, name='My Rule', backend='ntfy', config={})
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NotificationRule.objects.create(user=self.user, name='My Rule', backend='webhook', config={})


class BackendTests(TestCase):
    def test_ntfy_missing_config_fails_without_raising(self):
        backend = NtfyBackend({})
        self.assertFalse(backend.send('title', 'message'))

    @patch('notify.backends.ntfy.requests.post')
    def test_ntfy_sends_expected_request(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        backend = NtfyBackend({'server': 'https://ntfy.example.com/', 'topic': 'vv', 'token': 'secret'})
        result = backend.send('Hello', 'World')
        self.assertTrue(result)
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], 'https://ntfy.example.com/vv')
        self.assertEqual(kwargs['headers']['Title'], b'Hello')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer secret')

    @patch('requests.adapters.HTTPAdapter.send')
    def test_ntfy_title_with_emoji_does_not_crash_header_encoding(self, mock_send):
        # Regression test: a plain `str` header containing non-latin-1
        # characters (e.g. an emoji) makes `requests` raise UnicodeEncodeError
        # while preparing the request. The backend must send it as UTF-8
        # bytes instead so PreparedRequest.prepare_headers() succeeds.
        import requests as requests_lib
        fake_response = requests_lib.Response()
        fake_response.status_code = 200
        mock_send.return_value = fake_response

        backend = NtfyBackend({'server': 'https://ntfy.example.com', 'topic': 'vv'})
        result = backend.send('⏰ Item expires soon', 'World')
        self.assertTrue(result)

    @patch('notify.backends.ntfy.requests.post', side_effect=Exception('boom'))
    def test_ntfy_network_error_returns_false(self, mock_post):
        import requests
        mock_post.side_effect = requests.RequestException('boom')
        backend = NtfyBackend({'server': 'https://ntfy.example.com', 'topic': 'vv'})
        self.assertFalse(backend.send('title', 'message'))

    def test_webhook_rejects_non_http_url(self):
        backend = WebhookBackend({'url': 'file:///etc/passwd'})
        self.assertFalse(backend.send('title', 'message'))

    @patch('notify.backends.webhook.requests.post')
    def test_webhook_sends_item_payload(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        user = User.objects.create_user(username='bob', password='pw12345!')
        item = make_item(user)
        backend = WebhookBackend({'url': 'https://n8n.example.com/webhook/vv', 'headers': {'X-Secret': 's3cr3t'}})
        result = backend.send('title', 'message', item=item)
        self.assertTrue(result)
        args, kwargs = mock_post.call_args
        self.assertEqual(kwargs['json']['item']['name'], 'Test Voucher')
        self.assertEqual(kwargs['headers']['X-Secret'], 's3cr3t')

    def test_apprise_missing_urls_fails(self):
        backend = AppriseBackend({})
        self.assertFalse(backend.send('title', 'message'))

    @patch('notify.backends.apprise_backend.apprise.Apprise')
    def test_apprise_wraps_existing_library(self, mock_apprise_cls):
        mock_instance = MagicMock()
        mock_instance.notify.return_value = True
        mock_apprise_cls.return_value = mock_instance
        backend = AppriseBackend({'urls': 'json://example.com,mailto://user:pass@example.com'})
        result = backend.send('title', 'message')
        self.assertTrue(result)
        self.assertEqual(mock_instance.add.call_count, 2)

    def test_get_backend_dispatches_by_rule_backend(self):
        user = User.objects.create_user(username='carol', password='pw12345!')
        rule = make_rule(user, backend='webhook')
        backend = get_backend(rule)
        self.assertIsInstance(backend, WebhookBackend)


class FireNotificationsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.item = make_item(self.user)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_fires_only_for_matching_event_type(self, mock_send):
        make_rule(self.user, event_types=['expiry_final'])
        fire_notifications(self.item, 'expiry_warning', 5)
        mock_send.assert_not_called()
        self.assertEqual(NotificationLog.objects.count(), 0)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_fires_and_logs_for_matching_rule(self, mock_send):
        rule = make_rule(self.user, event_types=['expiry_warning'])
        fire_notifications(self.item, 'expiry_warning', 5)
        mock_send.assert_called_once()
        log = NotificationLog.objects.get()
        self.assertEqual(log.rule, rule)
        self.assertEqual(log.item, self.item)
        self.assertTrue(log.success)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_does_not_refire_after_success(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'])
        fire_notifications(self.item, 'expiry_warning', 5)
        fire_notifications(self.item, 'expiry_warning', 4)
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(NotificationLog.objects.count(), 1)

    @patch('notify.tasks.send_via_rule', return_value=(False, 'timed out'))
    def test_refires_after_failure(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'])
        fire_notifications(self.item, 'expiry_warning', 5)
        fire_notifications(self.item, 'expiry_warning', 4)
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(NotificationLog.objects.filter(success=False).count(), 2)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_disabled_rule_is_skipped(self, mock_send):
        rule = make_rule(self.user, event_types=['expiry_warning'])
        rule.enabled = False
        rule.save()
        fire_notifications(self.item, 'expiry_warning', 5)
        mock_send.assert_not_called()


class CheckAndNotifyExpiryTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_respects_global_threshold(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'])
        with patch.dict('os.environ', {'EXPIRY_THRESHOLD_DAYS': '10', 'EXPIRY_THRESHOLD_DAYS_FINAL': '3'}):
            within = make_item(self.user, name='Within', redeem_code='W1', expiry_date=date.today() + timedelta(days=5))
            outside = make_item(self.user, name='Outside', redeem_code='O1', expiry_date=date.today() + timedelta(days=20))
            check_and_notify_expiry()

        logged_items = set(NotificationLog.objects.values_list('item_id', flat=True))
        self.assertIn(within.id, logged_items)
        self.assertNotIn(outside.id, logged_items)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_per_item_override_takes_precedence(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'])
        with patch.dict('os.environ', {'EXPIRY_THRESHOLD_DAYS': '5', 'EXPIRY_THRESHOLD_DAYS_FINAL': '1'}):
            item = make_item(
                self.user, name='Overridden', redeem_code='OV1',
                expiry_date=date.today() + timedelta(days=20), notify_days_before=25,
            )
            check_and_notify_expiry()

        self.assertTrue(NotificationLog.objects.filter(item=item, event_type='expiry_warning').exists())

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_used_items_are_skipped(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'])
        item = make_item(self.user, is_used=True, expiry_date=date.today() + timedelta(days=1))
        check_and_notify_expiry()
        self.assertFalse(NotificationLog.objects.filter(item=item).exists())

    def test_noop_without_any_rules(self):
        make_item(self.user, expiry_date=date.today() + timedelta(days=1))
        check_and_notify_expiry()  # should not raise, and log nothing
        self.assertEqual(NotificationLog.objects.count(), 0)


class SendTestNotificationTests(TestCase):
    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_logs_test_event(self, mock_send):
        user = User.objects.create_user(username='alice', password='pw12345!')
        rule = make_rule(user)
        success, detail = send_test_notification(rule)
        self.assertTrue(success)
        log = NotificationLog.objects.get()
        self.assertEqual(log.event_type, 'test')
        self.assertIsNone(log.item)


class NotificationRuleFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_ntfy_requires_server_and_topic(self):
        form = NotificationRuleForm(data={
            'name': 'x', 'backend': 'ntfy', 'enabled': 'on', 'event_types': ['expiry_warning'],
        }, user=self.user)
        self.assertFalse(form.is_valid())

    def test_valid_ntfy_rule_assembles_config(self):
        form = NotificationRuleForm(data={
            'name': 'x', 'backend': 'ntfy', 'enabled': 'on', 'event_types': ['expiry_warning'],
            'ntfy_server': 'https://ntfy.example.com/', 'ntfy_topic': 'vv', 'ntfy_priority': 'high',
        }, user=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        rule = form.save(commit=False)
        rule.user = self.user
        rule.save()
        self.assertEqual(rule.config, {'server': 'https://ntfy.example.com', 'topic': 'vv', 'priority': 'high'})

    def test_duplicate_name_rejected(self):
        NotificationRule.objects.create(user=self.user, name='dup', backend='ntfy', config={})
        form = NotificationRuleForm(data={
            'name': 'dup', 'backend': 'webhook', 'enabled': 'on', 'event_types': ['expiry_warning'],
            'webhook_url': 'https://example.com/hook',
        }, user=self.user)
        self.assertFalse(form.is_valid())


class NotificationRuleViewTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='pw12345!')
        self.bob = User.objects.create_user(username='bob', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_create_rule(self):
        response = self.client.post(reverse('manage_notification_rules'), {
            'name': 'My Webhook', 'backend': 'webhook', 'enabled': 'on', 'event_types': ['expiry_warning'],
            'webhook_url': 'https://n8n.example.com/webhook/vv',
        })
        self.assertRedirects(response, reverse('manage_notification_rules'))
        self.assertTrue(NotificationRule.objects.filter(user=self.alice, name='My Webhook').exists())

    def test_cannot_edit_another_users_rule(self):
        bob_rule = make_rule(self.bob)
        response = self.client.get(reverse('edit_notification_rule', args=[bob_rule.id]))
        self.assertEqual(response.status_code, 404)

    def test_cannot_delete_another_users_rule(self):
        bob_rule = make_rule(self.bob)
        response = self.client.post(reverse('delete_notification_rule', args=[bob_rule.id]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(NotificationRule.objects.filter(pk=bob_rule.pk).exists())

    @patch('notify.views.send_test_notification', return_value=(True, ''))
    def test_test_button_fires_notification(self, mock_send):
        rule = make_rule(self.alice)
        response = self.client.post(reverse('test_notification_rule', args=[rule.id]))
        self.assertRedirects(response, reverse('manage_notification_rules'))
        mock_send.assert_called_once_with(rule)

    def test_notification_log_only_shows_own_entries(self):
        NotificationLog.objects.create(user=self.alice, event_type='test', success=True)
        NotificationLog.objects.create(user=self.bob, event_type='test', success=True)
        response = self.client.get(reverse('notification_log'))
        self.assertEqual(len(response.context['logs']), 1)
