import base64
import io
import json
import os
import re
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from py_vapid import Vapid02
from pywebpush import webpush as real_webpush

from myapp.models import Item, Transaction, UserPreference, Wallet
from myapp.test_utils import set_site_config

from .backends import get_backend
from .backends.apprise_backend import AppriseBackend
from .backends.ntfy import NtfyBackend
from .backends.webhook import WebhookBackend
from .backends.webpush import WebPushBackend, get_vapid_public_key, webpush_enabled
from .forms import NotificationRuleForm
from .models import DigestEntry, NotificationLog, NotificationRule, WebPushSubscription
from .tasks import (
    check_and_notify_expiry,
    check_next_up_reminders,
    fire_notifications,
    notify_balance_changed,
    notify_item_archived,
    notify_item_created,
    notify_item_shared,
    notify_item_used,
    send_daily_digests,
    send_test_notification,
)


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


def make_rule(user, backend='ntfy', event_types=None, digest_frequency='immediate', **config_overrides):
    config = {
        'ntfy': {'server': 'https://ntfy.example.com', 'topic': 'vouchervault'},
        'webhook': {'url': 'https://n8n.example.com/webhook/vv'},
        'apprise': {'urls': 'json://example.com/notify'},
        'webpush': {},
    }[backend]
    config.update(config_overrides)
    return NotificationRule.objects.create(
        user=user, name=f'{backend} rule', backend=backend,
        config=config, enabled=True, event_types=event_types or ['expiry_warning'],
        digest_frequency=digest_frequency,
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

    def test_get_backend_injects_user_id_for_webpush(self):
        user = User.objects.create_user(username='dave', password='pw12345!')
        rule = make_rule(user, backend='webpush')
        backend = get_backend(rule)
        self.assertIsInstance(backend, WebPushBackend)
        self.assertEqual(backend.config['user_id'], user.id)


class WebPushBackendTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_send_fails_without_vapid_key(self):
        set_site_config(webpush_vapid_private_key='')
        backend = WebPushBackend({'user_id': self.user.id})
        self.assertFalse(backend.send('title', 'message'))

    def test_send_fails_with_no_subscriptions(self):
        set_site_config(webpush_vapid_private_key='fake-key')
        backend = WebPushBackend({'user_id': self.user.id})
        self.assertFalse(backend.send('title', 'message'))

    @patch('notify.backends.webpush.webpush')
    def test_send_delivers_to_every_subscription(self, mock_webpush):
        set_site_config(webpush_vapid_private_key='fake-key')
        WebPushSubscription.objects.create(user=self.user, endpoint='https://push.example.com/1', p256dh='a', auth='b')
        WebPushSubscription.objects.create(user=self.user, endpoint='https://push.example.com/2', p256dh='c', auth='d')
        backend = WebPushBackend({'user_id': self.user.id})
        result = backend.send('title', 'message')
        self.assertTrue(result)
        self.assertEqual(mock_webpush.call_count, 2)

    @patch('notify.backends.webpush.webpush')
    def test_send_ignores_other_users_subscriptions(self, mock_webpush):
        set_site_config(webpush_vapid_private_key='fake-key')
        other = User.objects.create_user(username='bob', password='pw12345!')
        WebPushSubscription.objects.create(user=other, endpoint='https://push.example.com/1', p256dh='a', auth='b')
        backend = WebPushBackend({'user_id': self.user.id})
        self.assertFalse(backend.send('title', 'message'))
        mock_webpush.assert_not_called()

    @patch('notify.backends.webpush.webpush')
    def test_expired_subscription_is_deleted_on_410(self, mock_webpush):
        set_site_config(webpush_vapid_private_key='fake-key')
        from pywebpush import WebPushException
        sub = WebPushSubscription.objects.create(user=self.user, endpoint='https://push.example.com/1', p256dh='a', auth='b')
        fake_response = MagicMock(status_code=410)
        mock_webpush.side_effect = WebPushException('gone', response=fake_response)

        backend = WebPushBackend({'user_id': self.user.id})
        result = backend.send('title', 'message')

        self.assertFalse(result)
        self.assertFalse(WebPushSubscription.objects.filter(pk=sub.pk).exists())

    @patch('notify.backends.webpush.webpush')
    def test_network_error_on_one_subscription_does_not_abort_others(self, mock_webpush):
        # pywebpush lets connection errors propagate as raw requests
        # exceptions rather than WebPushException - a dead endpoint on one
        # of a user's devices must not block delivery to their other devices.
        set_site_config(webpush_vapid_private_key='fake-key')
        import requests
        WebPushSubscription.objects.create(user=self.user, endpoint='https://push.example.com/dead', p256dh='a', auth='b')
        WebPushSubscription.objects.create(user=self.user, endpoint='https://push.example.com/alive', p256dh='c', auth='d')
        mock_webpush.side_effect = [requests.ConnectionError('unreachable'), None]

        backend = WebPushBackend({'user_id': self.user.id})
        result = backend.send('title', 'message')

        self.assertTrue(result)
        self.assertEqual(mock_webpush.call_count, 2)

    def test_webpush_enabled_requires_both_keys(self):
        set_site_config(webpush_vapid_public_key='', webpush_vapid_private_key='')
        self.assertFalse(webpush_enabled())
        set_site_config(webpush_vapid_public_key='pub', webpush_vapid_private_key='priv')
        self.assertTrue(webpush_enabled())
        self.assertEqual(get_vapid_public_key(), 'pub')


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


class VapidKeyGenerationIntegrationTests(TestCase):
    """
    Proves the exact key format `generate_vapid_keys` prints is actually
    loadable by pywebpush and produces a cryptographically valid,
    independently-verifiable VAPID signature — not just that our code
    calls a mocked webpush() function.
    """

    def test_generated_keys_produce_a_verifiable_vapid_signature(self):
        out = io.StringIO()
        call_command('generate_vapid_keys', stdout=out)
        output = out.getvalue()

        public_key = re.search(r'WEBPUSH_VAPID_PUBLIC_KEY=(\S+)', output).group(1)
        private_key = re.search(r'WEBPUSH_VAPID_PRIVATE_KEY=(\S+)', output).group(1)

        # A fake "browser" client keypair so pywebpush's payload encryption
        # step (which needs a real EC point, not garbage bytes) succeeds.
        client_key = ec.generate_private_key(ec.SECP256R1())
        client_pub_bytes = client_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        subscription_info = {
            'endpoint': 'https://fcm.googleapis.com/fcm/send/fake-endpoint-id',
            'keys': {'p256dh': _b64url(client_pub_bytes), 'auth': _b64url(os.urandom(16))},
        }

        curl_output = real_webpush(
            subscription_info=subscription_info,
            data='{"title": "Test"}',
            vapid_private_key=private_key,
            vapid_claims={'sub': 'mailto:admin@example.com'},
            curl=True,
        )

        match = re.search(r'authorization:\s*vapid t=([^\s"]+),k=([^\s"]+)', curl_output, re.IGNORECASE)
        self.assertIsNotNone(match, curl_output)
        auth_header = f'vapid t={match.group(1)},k={match.group(2)}'

        self.assertTrue(Vapid02.verify(auth_header))
        self.assertEqual(match.group(2), public_key)


class WebPushSubscriptionModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_endpoint_is_unique(self):
        WebPushSubscription.objects.create(user=self.user, endpoint='https://push.example.com/1', p256dh='a', auth='b')
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                WebPushSubscription.objects.create(user=self.user, endpoint='https://push.example.com/1', p256dh='x', auth='y')


class WebPushSubscribeViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.client.login(username='alice', password='pw12345!')

    def test_subscribe_creates_subscription(self):
        response = self.client.post(
            reverse('webpush_subscribe'),
            data=json.dumps({'endpoint': 'https://push.example.com/1', 'keys': {'p256dh': 'a', 'auth': 'b'}}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(WebPushSubscription.objects.filter(user=self.user, endpoint='https://push.example.com/1').exists())

    def test_subscribe_rejects_malformed_payload(self):
        response = self.client.post(reverse('webpush_subscribe'), data='not json', content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_subscribe_requires_authentication(self):
        self.client.logout()
        response = self.client.post(
            reverse('webpush_subscribe'),
            data=json.dumps({'endpoint': 'https://push.example.com/1', 'keys': {'p256dh': 'a', 'auth': 'b'}}),
            content_type='application/json',
        )
        self.assertNotEqual(response.status_code, 200)

    def test_unsubscribe_removes_subscription(self):
        WebPushSubscription.objects.create(user=self.user, endpoint='https://push.example.com/1', p256dh='a', auth='b')
        response = self.client.post(
            reverse('webpush_unsubscribe'),
            data=json.dumps({'endpoint': 'https://push.example.com/1'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(WebPushSubscription.objects.filter(endpoint='https://push.example.com/1').exists())

    def test_cannot_unsubscribe_another_users_subscription(self):
        other = User.objects.create_user(username='bob', password='pw12345!')
        sub = WebPushSubscription.objects.create(user=other, endpoint='https://push.example.com/1', p256dh='a', auth='b')
        self.client.post(
            reverse('webpush_unsubscribe'),
            data=json.dumps({'endpoint': 'https://push.example.com/1'}),
            content_type='application/json',
        )
        self.assertTrue(WebPushSubscription.objects.filter(pk=sub.pk).exists())


class NotificationRuleFormWebPushGatingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def test_webpush_choice_hidden_when_disabled(self):
        set_site_config(webpush_vapid_public_key='', webpush_vapid_private_key='')
        form = NotificationRuleForm(user=self.user)
        choices = [c[0] for c in form.fields['backend'].choices]
        self.assertNotIn('webpush', choices)

    def test_webpush_choice_shown_when_enabled(self):
        set_site_config(webpush_vapid_public_key='pub', webpush_vapid_private_key='priv')
        form = NotificationRuleForm(user=self.user)
        choices = [c[0] for c in form.fields['backend'].choices]
        self.assertIn('webpush', choices)

    def test_valid_webpush_rule_has_empty_config(self):
        set_site_config(webpush_vapid_public_key='pub', webpush_vapid_private_key='priv')
        form = NotificationRuleForm(data={
            'name': 'push me', 'backend': 'webpush', 'enabled': 'on', 'event_types': ['expiry_warning'],
            'digest_frequency': 'immediate',
        }, user=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        rule = form.save(commit=False)
        rule.user = self.user
        rule.save()
        self.assertEqual(rule.config, {})


class FireNotificationsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.item = make_item(self.user)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_fires_only_for_matching_event_type(self, mock_send):
        make_rule(self.user, event_types=['expiry_final'])
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')
        mock_send.assert_not_called()
        self.assertEqual(NotificationLog.objects.count(), 0)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_fires_and_logs_for_matching_rule(self, mock_send):
        rule = make_rule(self.user, event_types=['expiry_warning'])
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')
        mock_send.assert_called_once()
        log = NotificationLog.objects.get()
        self.assertEqual(log.rule, rule)
        self.assertEqual(log.item, self.item)
        self.assertTrue(log.success)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_does_not_refire_after_success(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'])
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')
        self.assertEqual(mock_send.call_count, 1)
        self.assertEqual(NotificationLog.objects.count(), 1)

    @patch('notify.tasks.send_via_rule', return_value=(False, 'timed out'))
    def test_refires_after_failure(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'])
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(NotificationLog.objects.filter(success=False).count(), 2)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_disabled_rule_is_skipped(self, mock_send):
        rule = make_rule(self.user, event_types=['expiry_warning'])
        rule.enabled = False
        rule.save()
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')
        mock_send.assert_not_called()

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_dedupe_false_refires_every_time(self, mock_send):
        """Repeatable events (balance_changed, item_used, ...) pass dedupe=False
        so a second real occurrence isn't silently swallowed by the "already
        succeeded once" check that protects the periodic expiry re-scan."""
        make_rule(self.user, event_types=['balance_changed'])
        fire_notifications(self.item, 'balance_changed', 'title', 'message', dedupe=False)
        fire_notifications(self.item, 'balance_changed', 'title', 'message', dedupe=False)
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(NotificationLog.objects.count(), 2)


class DigestModeTests(TestCase):
    """
    digest_frequency='daily' routes fire_notifications() through
    DigestEntry instead of an immediate send_via_rule() call, and
    send_daily_digests() is what actually delivers them.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.item = make_item(self.user)

    @patch('notify.tasks.send_via_rule')
    def test_daily_rule_queues_instead_of_sending_immediately(self, mock_send):
        rule = make_rule(self.user, event_types=['expiry_warning'], digest_frequency='daily')
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')

        mock_send.assert_not_called()
        entry = DigestEntry.objects.get()
        self.assertEqual(entry.rule, rule)
        self.assertEqual(entry.item, self.item)
        self.assertEqual(entry.title, 'title')

    @patch('notify.tasks.send_via_rule')
    def test_daily_rule_logs_immediately_to_prevent_rescan_requeue(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'], digest_frequency='daily')
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')  # dedupe=True default
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')

        self.assertEqual(DigestEntry.objects.count(), 1)
        self.assertEqual(NotificationLog.objects.filter(success=True).count(), 1)
        mock_send.assert_not_called()

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_immediate_rule_unaffected(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'], digest_frequency='immediate')
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')

        mock_send.assert_called_once()
        self.assertEqual(DigestEntry.objects.count(), 0)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_send_daily_digests_combines_and_clears_entries(self, mock_send):
        rule = make_rule(self.user, event_types=['expiry_warning', 'item_used'], digest_frequency='daily')
        fire_notifications(self.item, 'expiry_warning', 'Card expiring', 'Details A', dedupe=False)
        fire_notifications(self.item, 'item_used', 'Card used', 'Details B', dedupe=False)

        send_daily_digests()

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        self.assertEqual(call_args.args[0], rule)
        self.assertIn('2 updates', call_args.args[1])
        self.assertIn('Card expiring', call_args.args[2])
        self.assertIn('Card used', call_args.args[2])
        self.assertEqual(DigestEntry.objects.count(), 0)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_send_daily_digests_noop_with_nothing_queued(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'], digest_frequency='daily')
        send_daily_digests()
        mock_send.assert_not_called()

    @patch('notify.tasks.send_via_rule')
    def test_send_daily_digests_skips_disabled_rule_but_still_clears(self, mock_send):
        rule = make_rule(self.user, event_types=['expiry_warning'], digest_frequency='daily')
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')
        rule.enabled = False
        rule.save()

        send_daily_digests()

        mock_send.assert_not_called()
        self.assertEqual(DigestEntry.objects.count(), 0)

    @patch('notify.tasks.send_via_rule', return_value=(False, 'boom'))
    def test_send_daily_digests_clears_entries_even_on_failure(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'], digest_frequency='daily')
        fire_notifications(self.item, 'expiry_warning', 'title', 'message')

        send_daily_digests()

        self.assertEqual(DigestEntry.objects.count(), 0)
        self.assertTrue(NotificationLog.objects.filter(event_type='daily_digest', success=False).exists())


class LifecycleEventNotificationTests(TestCase):
    """
    The five webhook-friendly lifecycle events (item_created, item_used,
    item_archived, balance_changed, item_shared) all reuse fire_notifications
    with dedupe=False, since each occurrence is a distinct real event rather
    than a periodic re-scan repeat.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.item = make_item(self.user)
        self.item.refresh_from_db()

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_notify_item_created(self, mock_send):
        make_rule(self.user, event_types=['item_created'])
        notify_item_created(self.item)
        mock_send.assert_called_once()
        log = NotificationLog.objects.get()
        self.assertEqual(log.event_type, 'item_created')
        self.assertTrue(log.success)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_notify_item_used(self, mock_send):
        make_rule(self.user, event_types=['item_used'])
        notify_item_used(self.item)
        mock_send.assert_called_once()
        self.assertEqual(NotificationLog.objects.get().event_type, 'item_used')

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_notify_item_archived(self, mock_send):
        make_rule(self.user, event_types=['item_archived'])
        notify_item_archived(self.item)
        mock_send.assert_called_once()
        self.assertEqual(NotificationLog.objects.get().event_type, 'item_archived')

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_notify_balance_changed_fires_every_transaction(self, mock_send):
        make_rule(self.user, event_types=['balance_changed'])
        t1 = Transaction.objects.create(item=self.item, description='Spend 1', value='-2.00')
        t2 = Transaction.objects.create(item=self.item, description='Spend 2', value='-3.00')
        notify_balance_changed(self.item, t1)
        notify_balance_changed(self.item, t2)
        self.assertEqual(mock_send.call_count, 2)
        self.assertEqual(NotificationLog.objects.filter(event_type='balance_changed').count(), 2)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_notify_item_shared(self, mock_send):
        make_rule(self.user, event_types=['item_shared'])
        notify_item_shared(self.item, 'bob')
        mock_send.assert_called_once()
        title, message = mock_send.call_args[0][1], mock_send.call_args[0][2]
        self.assertIn('bob', message)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_no_matching_rule_is_a_noop(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'])
        notify_item_created(self.item)
        mock_send.assert_not_called()


class CheckAndNotifyExpiryTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_respects_global_threshold(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'])
        set_site_config(expiry_threshold_days=10, expiry_last_notification_days=3)
        within = make_item(self.user, name='Within', redeem_code='W1', expiry_date=date.today() + timedelta(days=5))
        outside = make_item(self.user, name='Outside', redeem_code='O1', expiry_date=date.today() + timedelta(days=20))
        check_and_notify_expiry()

        logged_items = set(NotificationLog.objects.values_list('item_id', flat=True))
        self.assertIn(within.id, logged_items)
        self.assertNotIn(outside.id, logged_items)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_per_item_override_takes_precedence(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'])
        set_site_config(expiry_threshold_days=5, expiry_last_notification_days=1)
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


class CheckNextUpRemindersTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.wallet = Wallet.objects.create(user=self.user, name='Train Tickets')

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_fires_for_item_due_today_in_a_next_up_wallet(self, mock_send):
        make_rule(self.user, event_types=['next_up_reminder'])
        preferences, _ = UserPreference.objects.get_or_create(user=self.user)
        preferences.next_up_wallets.add(self.wallet)
        item = make_item(self.user, wallet=self.wallet, expiry_date=date.today())

        check_next_up_reminders()

        self.assertTrue(NotificationLog.objects.filter(item=item, event_type='next_up_reminder', success=True).exists())

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_does_not_fire_for_item_not_due_today(self, mock_send):
        make_rule(self.user, event_types=['next_up_reminder'])
        preferences, _ = UserPreference.objects.get_or_create(user=self.user)
        preferences.next_up_wallets.add(self.wallet)
        item = make_item(self.user, wallet=self.wallet, expiry_date=date.today() + timedelta(days=1))

        check_next_up_reminders()

        self.assertFalse(NotificationLog.objects.filter(item=item, event_type='next_up_reminder').exists())

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_does_not_fire_for_item_in_unwatched_wallet(self, mock_send):
        make_rule(self.user, event_types=['next_up_reminder'])
        other_wallet = Wallet.objects.create(user=self.user, name='Other')
        item = make_item(self.user, wallet=other_wallet, expiry_date=date.today())

        check_next_up_reminders()

        self.assertFalse(NotificationLog.objects.filter(item=item, event_type='next_up_reminder').exists())

    def test_noop_when_no_user_has_a_next_up_wallet_configured(self):
        item = make_item(self.user, wallet=self.wallet, expiry_date=date.today())
        check_next_up_reminders()  # should not raise, and log nothing
        self.assertFalse(NotificationLog.objects.filter(item=item).exists())

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_rule_without_the_event_type_does_not_fire(self, mock_send):
        make_rule(self.user, event_types=['expiry_warning'])  # not subscribed to next_up_reminder
        preferences, _ = UserPreference.objects.get_or_create(user=self.user)
        preferences.next_up_wallets.add(self.wallet)
        item = make_item(self.user, wallet=self.wallet, expiry_date=date.today())

        check_next_up_reminders()

        self.assertFalse(NotificationLog.objects.filter(item=item, event_type='next_up_reminder').exists())


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
            'digest_frequency': 'immediate',
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
            'webhook_url': 'https://n8n.example.com/webhook/vv', 'digest_frequency': 'immediate',
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
