import base64
import io
import json
import os
import re
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.utils import timezone

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
from .backends.firefly_backend import FireflyBackend
from .backends.ntfy import NtfyBackend
from .backends.webhook import WebhookBackend
from .backends.webpush import WebPushBackend, get_vapid_public_key, webpush_enabled
from .forms import NotificationRuleForm
from .models import DigestEntry, NotificationLog, NotificationRule, WebPushSubscription
from .tasks import (
    _find_firefly_rule,
    advance_recurring_items,
    backfill_firefly_transactions,
    check_and_notify_expiry,
    check_and_notify_inactivity,
    check_merchant_health,
    check_next_up_reminders,
    fire_notifications,
    notify_balance_changed,
    notify_item_archived,
    notify_item_created,
    notify_item_shared,
    notify_item_used,
    push_transaction_to_firefly,
    retry_failed_firefly_pushes,
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
        'firefly': {'url': 'https://firefly.example.com', 'token': 'secret-token'},
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

    def test_get_backend_dispatches_to_firefly(self):
        user = User.objects.create_user(username='erin', password='pw12345!')
        rule = make_rule(user, backend='firefly')
        backend = get_backend(rule)
        self.assertIsInstance(backend, FireflyBackend)


class FireflyBackendTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='fiona', password='pw12345!')
        self.item = make_item(self.user, type='giftcard', value='50.00', firefly_account_id='7')

    def _make_transaction(self):
        return Transaction.objects.create(
            item=self.item, date=timezone.now(), description='Spent at shop', value='-10.00',
        )

    def test_missing_url_returns_false(self):
        backend = FireflyBackend({'token': 'tok'})
        self.assertFalse(backend.send('title', 'msg', item=self.item))

    def test_missing_token_returns_false(self):
        backend = FireflyBackend({'url': 'https://firefly.example.com'})
        self.assertFalse(backend.send('title', 'msg', item=self.item))

    def test_null_item_returns_true_without_calling_api(self):
        backend = FireflyBackend({'url': 'https://firefly.example.com', 'token': 'tok'})
        self.assertTrue(backend.send('title', 'msg', item=None))

    def test_no_account_id_returns_true_without_calling_api(self):
        item_no_account = make_item(self.user, name='No Account', firefly_account_id='')
        backend = FireflyBackend({'url': 'https://firefly.example.com', 'token': 'tok'})
        self.assertTrue(backend.send('title', 'msg', item=item_no_account))

    def test_no_transaction_returns_true_without_calling_api(self):
        backend = FireflyBackend({'url': 'https://firefly.example.com', 'token': 'tok'})
        self.assertTrue(backend.send('title', 'msg', item=self.item))

    @patch('notify.backends.firefly_backend.requests.post')
    def test_successful_send_posts_withdrawal(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {'data': {'id': '99'}})
        tx = self._make_transaction()
        backend = FireflyBackend({'url': 'https://firefly.example.com', 'token': 'tok'})
        result = backend.send('title', 'msg', item=self.item, transaction=tx)
        self.assertTrue(result)
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], 'https://firefly.example.com/api/v1/transactions')
        tx_row = kwargs['json']['transactions'][0]
        self.assertEqual(tx_row['type'], 'withdrawal')
        self.assertEqual(tx_row['source_id'], '7')
        self.assertEqual(tx_row['amount'], '10.00')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer tok')

    @patch('notify.backends.firefly_backend.requests.post')
    def test_deposit_direction_for_positive_transaction(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {'data': {'id': '100'}})
        tx = Transaction.objects.create(
            item=self.item, date=timezone.now(), description='Top-up', value='20.00',
        )
        backend = FireflyBackend({'url': 'https://firefly.example.com', 'token': 'tok'})
        result = backend.send('title', 'msg', item=self.item, transaction=tx)
        self.assertTrue(result)
        tx_row = mock_post.call_args[1]['json']['transactions'][0]
        self.assertEqual(tx_row['type'], 'deposit')
        self.assertEqual(tx_row['destination_id'], '7')
        self.assertEqual(tx_row['amount'], '20.00')

    @patch('notify.backends.firefly_backend.requests.post')
    def test_tags_and_category_included(self, mock_post):
        from myapp.models import Tag
        mock_post.return_value = MagicMock(status_code=200, json=lambda: {'data': {'id': '101'}})
        tag = Tag.objects.create(user=self.user, name='groceries')
        self.item.tags.add(tag)
        tx = self._make_transaction()
        backend = FireflyBackend({'url': 'https://firefly.example.com', 'token': 'tok'})
        backend.send('title', 'msg', item=self.item, transaction=tx)
        tx_row = mock_post.call_args[1]['json']['transactions'][0]
        self.assertIn('groceries', tx_row.get('tags', []))
        self.assertEqual(tx_row.get('category_name'), 'Gift Cards')

    @patch('notify.backends.firefly_backend.requests.post')
    def test_network_error_returns_false(self, mock_post):
        import requests as requests_lib
        mock_post.side_effect = requests_lib.RequestException('timeout')
        tx = self._make_transaction()
        backend = FireflyBackend({'url': 'https://firefly.example.com', 'token': 'tok'})
        result = backend.send('title', 'msg', item=self.item, transaction=tx)
        self.assertFalse(result)

    @patch('notify.backends.firefly_backend.requests.post')
    def test_http_error_returns_false(self, mock_post):
        import requests as requests_lib
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests_lib.HTTPError('403 Forbidden')
        mock_post.return_value = mock_response
        tx = self._make_transaction()
        backend = FireflyBackend({'url': 'https://firefly.example.com', 'token': 'tok'})
        result = backend.send('title', 'msg', item=self.item, transaction=tx)
        self.assertFalse(result)


class FireflyLinkActionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='george', password='pw12345!')
        self.client.login(username='george', password='pw12345!')
        self.item = make_item(self.user, type='giftcard', value='50.00')
        from rest_framework.authtoken.models import Token
        token, _ = Token.objects.get_or_create(user=self.user)
        self.auth = f'Token {token.key}'

    def _make_firefly_rule(self, **overrides):
        config = {'url': 'https://firefly.example.com', 'token': 'secret'}
        config.update(overrides)
        return NotificationRule.objects.create(
            user=self.user, name='firefly rule', backend='firefly',
            config=config, enabled=True, event_types=['balance_changed'],
        )

    def test_returns_400_when_no_firefly_rule(self):
        url = f'/api/v1/items/{self.item.pk}/firefly-link/'
        resp = self.client.post(url, HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(resp.status_code, 400)

    @patch('api.views.requests.post')
    @patch('api.views.requests.get')
    def test_creates_account_and_stores_id(self, mock_get, mock_post):
        self._make_firefly_rule()
        # Search returns no match — triggers create
        mock_get_resp = MagicMock()
        mock_get_resp.raise_for_status.return_value = None
        mock_get_resp.json.return_value = {'data': []}
        mock_get.return_value = mock_get_resp
        # Create succeeds
        mock_post_resp = MagicMock()
        mock_post_resp.raise_for_status.return_value = None
        mock_post_resp.json.return_value = {'data': {'id': '42'}}
        mock_post.return_value = mock_post_resp
        url = f'/api/v1/items/{self.item.pk}/firefly-link/'
        resp = self.client.post(url, HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['firefly_account_id'], '42')
        self.assertFalse(resp.json().get('existing'))
        self.item.refresh_from_db()
        self.assertEqual(self.item.firefly_account_id, '42')

    @patch('api.views.requests.post')
    @patch('api.views.requests.get')
    def test_returns_existing_account_without_creating(self, mock_get, mock_post):
        self._make_firefly_rule()
        # The view computes account_name as "Name (Issuer)" when issuer is set
        account_name = (
            f'{self.item.name} ({self.item.issuer})' if self.item.issuer else self.item.name
        )
        mock_get_resp = MagicMock()
        mock_get_resp.raise_for_status.return_value = None
        mock_get_resp.json.return_value = {'data': [{'id': '77', 'attributes': {'name': account_name}}]}
        mock_get.return_value = mock_get_resp
        url = f'/api/v1/items/{self.item.pk}/firefly-link/'
        resp = self.client.post(url, HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['firefly_account_id'], '77')
        self.assertTrue(resp.json().get('existing'))
        mock_post.assert_not_called()

    @patch('api.views.requests.post')
    @patch('api.views.requests.get')
    def test_502_on_network_error(self, mock_get, mock_post):
        import requests as requests_lib
        self._make_firefly_rule()
        # Search fails (caught), then create fails
        mock_get.side_effect = requests_lib.RequestException('network error')
        mock_post.side_effect = requests_lib.RequestException('timeout')
        url = f'/api/v1/items/{self.item.pk}/firefly-link/'
        resp = self.client.post(url, HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(resp.status_code, 502)

    def test_other_user_cannot_link(self):
        other = User.objects.create_user(username='harry', password='pw12345!')
        from rest_framework.authtoken.models import Token
        other_token, _ = Token.objects.get_or_create(user=other)
        url = f'/api/v1/items/{self.item.pk}/firefly-link/'
        resp = self.client.post(url, HTTP_AUTHORIZATION=f'Token {other_token.key}')
        self.assertEqual(resp.status_code, 404)


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

    @patch('notify.backends.webpush.webpush')
    def test_send_includes_item_deep_link_url(self, mock_webpush):
        set_site_config(webpush_vapid_private_key='fake-key')
        WebPushSubscription.objects.create(
            user=self.user, endpoint='https://push.example.com/1', p256dh='a', auth='b',
        )
        item = Item.objects.create(
            user=self.user, type='travelpass', name='Test Ticket',
            issuer='Greater Anglia', redeem_code='ABC123', code_type='none',
            expiry_date=date.today(), value=0,
        )
        backend = WebPushBackend({'user_id': self.user.id})
        backend.send('title', 'body', item=item)
        call_kwargs = mock_webpush.call_args
        payload = json.loads(call_kwargs.kwargs.get('data') or call_kwargs[1]['data'])
        self.assertIn('url', payload)
        self.assertIn(str(item.id), payload['url'])


class FireflyTransactionIdTests(TestCase):
    """Transaction.firefly_transaction_id writeback via _do_firefly_push."""

    def setUp(self):
        self.user = User.objects.create_user(username='firefly_tx_user', password='pw12345!')
        self.item = make_item(self.user, type='giftcard', value='50.00', firefly_account_id='99')

    @patch('notify.backends.firefly_backend.requests.post')
    def test_firefly_transaction_id_written_back_on_success(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={'data': {'id': 'FF-123'}}),
        )
        tx = Transaction.objects.create(
            item=self.item, date=timezone.now(), description='Spent', value='-5.00',
        )
        backend = FireflyBackend({'url': 'https://firefly.example.com', 'token': 'tok'})
        backend.send('title', 'msg', item=self.item, transaction=tx)
        tx.refresh_from_db()
        self.assertEqual(tx.firefly_transaction_id, 'FF-123')

    @patch('notify.backends.firefly_backend.requests.post')
    def test_firefly_transaction_id_not_written_on_failure(self, mock_post):
        import requests as requests_lib
        mock_post.side_effect = requests_lib.RequestException('timeout')
        tx = Transaction.objects.create(
            item=self.item, date=timezone.now(), description='Spent', value='-5.00',
        )
        backend = FireflyBackend({'url': 'https://firefly.example.com', 'token': 'tok'})
        backend.send('title', 'msg', item=self.item, transaction=tx)
        tx.refresh_from_db()
        self.assertEqual(tx.firefly_transaction_id, '')

    def test_firefly_transaction_id_in_transaction_serializer(self):
        from api.serializers import TransactionSerializer
        tx = Transaction.objects.create(
            item=self.item, date=timezone.now(), description='Spent', value='-5.00',
            firefly_transaction_id='FF-456',
        )
        data = TransactionSerializer(tx).data
        self.assertIn('firefly_transaction_id', data)
        self.assertEqual(data['firefly_transaction_id'], 'FF-456')


class FireflyAsyncDispatchTests(TestCase):
    """FireflyBackend.send dispatches to Celery when rule_id is set."""

    def setUp(self):
        self.user = User.objects.create_user(username='async_user', password='pw12345!')
        self.item = make_item(self.user, type='giftcard', value='50.00', firefly_account_id='7')
        self.rule = make_rule(self.user, backend='firefly', event_types=['balance_changed'])

    @patch('notify.tasks.push_transaction_to_firefly.delay')
    def test_async_path_dispatches_celery_task(self, mock_delay):
        tx = Transaction.objects.create(
            item=self.item, date=timezone.now(), description='Async push', value='-10.00',
        )
        backend = FireflyBackend(
            {'url': 'https://firefly.example.com', 'token': 'tok'},
            rule_id=self.rule.id,
        )
        result = backend.send('title', 'msg', item=self.item, transaction=tx)
        self.assertTrue(result)
        mock_delay.assert_called_once_with(self.rule.id, str(self.item.pk), str(tx.pk))

    @patch('notify.backends.firefly_backend.requests.post')
    def test_sync_fallback_when_no_rule_id(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={'data': {'id': 'FF-789'}}),
        )
        tx = Transaction.objects.create(
            item=self.item, date=timezone.now(), description='Sync push', value='-10.00',
        )
        backend = FireflyBackend({'url': 'https://firefly.example.com', 'token': 'tok'})
        result = backend.send('title', 'msg', item=self.item, transaction=tx)
        self.assertTrue(result)
        mock_post.assert_called_once()


class FireflyBackfillTests(TestCase):
    """backfill_firefly_transactions queues pushes for all unsynced transactions."""

    def setUp(self):
        self.user = User.objects.create_user(username='backfill_user', password='pw12345!')
        self.item = make_item(self.user, type='giftcard', value='100.00', firefly_account_id='5')
        self.rule = make_rule(self.user, backend='firefly', event_types=['balance_changed'])

    @patch('notify.tasks.push_transaction_to_firefly.delay')
    def test_backfill_queues_unsynced_transactions(self, mock_delay):
        tx1 = Transaction.objects.create(item=self.item, date=timezone.now(), description='A', value='-10.00')
        tx2 = Transaction.objects.create(item=self.item, date=timezone.now(), description='B', value='-20.00')
        Transaction.objects.create(
            item=self.item, date=timezone.now(), description='C', value='-5.00',
            firefly_transaction_id='already-synced',
        )
        backfill_firefly_transactions(str(self.item.pk), self.rule.id)
        self.assertEqual(mock_delay.call_count, 2)
        called_tx_ids = {call[0][2] for call in mock_delay.call_args_list}
        self.assertIn(str(tx1.pk), called_tx_ids)
        self.assertIn(str(tx2.pk), called_tx_ids)

    @patch('notify.tasks.push_transaction_to_firefly.delay')
    def test_backfill_skips_already_synced(self, mock_delay):
        Transaction.objects.create(
            item=self.item, date=timezone.now(), description='Synced', value='-5.00',
            firefly_transaction_id='ff-999',
        )
        backfill_firefly_transactions(str(self.item.pk), self.rule.id)
        mock_delay.assert_not_called()


class FireflyRetryTests(TestCase):
    """retry_failed_firefly_pushes finds unsynced txns on linked items."""

    def setUp(self):
        self.user = User.objects.create_user(username='retry_user', password='pw12345!')
        self.item = make_item(self.user, type='giftcard', value='50.00', firefly_account_id='8')
        self.rule = make_rule(self.user, backend='firefly', event_types=['balance_changed'])

    @patch('notify.tasks.push_transaction_to_firefly.delay')
    def test_retry_queues_failed_pushes(self, mock_delay):
        tx = Transaction.objects.create(item=self.item, date=timezone.now(), description='Failed', value='-10.00')
        retry_failed_firefly_pushes()
        self.assertEqual(mock_delay.call_count, 1)
        mock_delay.assert_called_once_with(self.rule.id, str(self.item.pk), str(tx.pk))

    @patch('notify.tasks.push_transaction_to_firefly.delay')
    def test_retry_skips_items_without_firefly_link(self, mock_delay):
        unlinked = make_item(self.user, name='Unlinked', firefly_account_id='', value='20.00')
        Transaction.objects.create(item=unlinked, date=timezone.now(), description='X', value='-5.00')
        retry_failed_firefly_pushes()
        # Only the linked item's tx should be queued
        called_item_ids = {call[0][1] for call in mock_delay.call_args_list}
        self.assertNotIn(str(unlinked.pk), called_item_ids)


class FireflyValueChangeSignalTests(TestCase):
    """Item._original_value signal creates an adjustment transaction when value changes."""

    def setUp(self):
        self.user = User.objects.create_user(username='signal_user', password='pw12345!')
        self.rule = make_rule(self.user, backend='firefly', event_types=['balance_changed'])

    def test_value_change_creates_adjustment_transaction(self):
        item = make_item(self.user, type='giftcard', value='100.00', currency='GBP', firefly_account_id='42')
        initial_tx_count = Transaction.objects.filter(item=item).count()
        item.value = 80
        item.save()
        self.assertEqual(
            Transaction.objects.filter(item=item).count(),
            initial_tx_count + 1,
        )
        adj = Transaction.objects.filter(item=item, description__startswith='Value adjusted').first()
        self.assertIsNotNone(adj)
        from decimal import Decimal
        self.assertEqual(adj.value, Decimal('-20.00'))
        self.assertIn('100.00', adj.description)
        self.assertIn('80', adj.description)
        self.assertIn('GBP', adj.description)

    def test_no_adjustment_if_value_unchanged(self):
        item = make_item(self.user, type='giftcard', value='100.00', firefly_account_id='42')
        initial_tx_count = Transaction.objects.filter(item=item).count()
        item.save()
        self.assertEqual(Transaction.objects.filter(item=item).count(), initial_tx_count)

    def test_no_adjustment_without_firefly_account(self):
        item = make_item(self.user, type='giftcard', value='100.00', firefly_account_id='')
        initial_tx_count = Transaction.objects.filter(item=item).count()
        item.value = 80
        item.save()
        self.assertEqual(Transaction.objects.filter(item=item).count(), initial_tx_count)

    def test_no_adjustment_on_create(self):
        item = make_item(self.user, type='giftcard', value='50.00', firefly_account_id='42')
        self.assertEqual(Transaction.objects.filter(item=item, description__startswith='Value adjusted').count(), 0)

    def test_no_adjustment_for_archived_item(self):
        item = make_item(self.user, type='giftcard', value='100.00', firefly_account_id='42', is_archived=True)
        initial_count = Transaction.objects.filter(item=item).count()
        item.value = 50
        item.save()
        self.assertEqual(Transaction.objects.filter(item=item).count(), initial_count)


class FireflyTestConnectionViewTests(TestCase):
    """POST /notifications/firefly-test-connection/ validates Firefly credentials via /api/v1/about."""

    def setUp(self):
        self.user = User.objects.create_user(username='ffconn_user', password='pw12345!')
        self.client.login(username='ffconn_user', password='pw12345!')
        self.url = reverse('firefly_test_connection')

    @patch('notify.views.req_lib.get')
    def test_successful_connection_returns_ok_and_version(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {'data': {'version': '6.1.0'}},
        )
        resp = self.client.post(self.url, {'url': 'https://firefly.example.com', 'token': 'mytoken'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['version'], '6.1.0')

    @patch('notify.views.req_lib.get')
    def test_bad_token_returns_auth_error(self, mock_get):
        mock_get.return_value = MagicMock(status_code=401)
        resp = self.client.post(self.url, {'url': 'https://firefly.example.com', 'token': 'bad'})
        data = resp.json()
        self.assertFalse(data['ok'])
        self.assertIn('auth', data['error'].lower())

    def test_missing_url_returns_error(self):
        resp = self.client.post(self.url, {'url': '', 'token': 'tok'})
        data = resp.json()
        self.assertFalse(data['ok'])

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.post(self.url, {'url': 'https://x.com', 'token': 'tok'})
        self.assertNotEqual(resp.status_code, 200)


class FireflyZeroAmountSkipTests(TestCase):
    """Zero-value transactions must not be pushed (Firefly rejects them with 422)."""

    def setUp(self):
        self.user = User.objects.create_user(username='zero_user', password='pw12345!')
        self.rule = make_rule(self.user, backend='firefly', event_types=['balance_changed'])

    @patch('notify.backends.firefly_backend.requests.post')
    def test_zero_amount_skips_http_post(self, mock_post):
        from notify.backends.firefly_backend import _do_firefly_push
        item = make_item(self.user, type='giftcard', value='50.00', firefly_account_id='99')
        tx = Transaction.objects.create(item=item, date=timezone.now(), description='zero', value='0.00')
        result = _do_firefly_push(self.rule.config, item, tx)
        self.assertTrue(result)
        mock_post.assert_not_called()


class FireflyArchivedExpiryTests(TestCase):
    """Archived items must not receive expiry notifications."""

    def setUp(self):
        self.user = User.objects.create_user(username='arch_exp_user', password='pw12345!')
        make_rule(self.user, backend='ntfy', event_types=['expiry_warning'])

    def test_archived_item_skipped_in_expiry_check(self):
        from notify.tasks import check_and_notify_expiry
        make_item(self.user, is_archived=True, expiry_date=date.today() + timedelta(days=1), notify_days_before=5)
        with patch('notify.tasks.fire_notifications') as mock_fire:
            check_and_notify_expiry()
            mock_fire.assert_not_called()


class FireflyCloseAccountTests(TestCase):
    """notify_item_archived closes Firefly account when close_account_on_archive is set."""

    def setUp(self):
        self.user = User.objects.create_user(username='close_acct_user', password='pw12345!')
        self.item = make_item(self.user, type='giftcard', value='50.00', firefly_account_id='11')

    def _make_close_rule(self):
        return NotificationRule.objects.create(
            user=self.user, name='firefly close rule', backend='firefly',
            config={
                'url': 'https://firefly.example.com',
                'token': 'secret',
                'close_account_on_archive': True,
            },
            enabled=True, event_types=['item_archived'],
        )

    @patch('notify.tasks.requests_lib.patch')
    def test_archive_closes_firefly_account(self, mock_patch):
        self._make_close_rule()
        mock_patch.return_value = MagicMock(raise_for_status=MagicMock())
        notify_item_archived(self.item)
        mock_patch.assert_called_once()
        call_url = mock_patch.call_args[0][0]
        self.assertIn('/api/v1/accounts/11', call_url)
        self.assertEqual(mock_patch.call_args[1]['json'], {'active': False})

    @patch('notify.tasks.requests_lib.patch')
    def test_archive_skips_close_when_flag_not_set(self, mock_patch):
        NotificationRule.objects.create(
            user=self.user, name='firefly no close', backend='firefly',
            config={'url': 'https://firefly.example.com', 'token': 'secret'},
            enabled=True, event_types=['item_archived'],
        )
        notify_item_archived(self.item)
        mock_patch.assert_not_called()

    @patch('notify.tasks.requests_lib.patch')
    def test_archive_skips_close_without_firefly_account(self, mock_patch):
        self._make_close_rule()
        self.item.firefly_account_id = ''
        self.item.save()
        notify_item_archived(self.item)
        mock_patch.assert_not_called()


class FindFireflyRuleTests(TestCase):
    """_find_firefly_rule cascades item → wallet → user global."""

    def setUp(self):
        self.user = User.objects.create_user(username='cascade_user', password='pw12345!')
        self.global_rule = make_rule(self.user, backend='firefly', event_types=['balance_changed'])
        self.wallet = Wallet.objects.create(user=self.user, name='My Wallet')
        self.item = make_item(self.user, type='giftcard', value='50.00', wallet=self.wallet)

    def test_global_fallback_returned_when_no_overrides(self):
        rule = _find_firefly_rule(self.item)
        self.assertEqual(rule, self.global_rule)

    def test_wallet_rule_takes_precedence_over_global(self):
        wallet_rule = NotificationRule.objects.create(
            user=self.user, name='wallet firefly', backend='firefly',
            config={'url': 'https://firefly.example.com', 'token': 'tok'},
            enabled=True, event_types=['balance_changed'],
        )
        self.wallet.firefly_rule = wallet_rule
        self.wallet.save()
        rule = _find_firefly_rule(self.item)
        self.assertEqual(rule, wallet_rule)

    def test_item_rule_takes_precedence_over_wallet_and_global(self):
        wallet_rule = NotificationRule.objects.create(
            user=self.user, name='wallet firefly 2', backend='firefly',
            config={'url': 'https://firefly.example.com', 'token': 'tok'},
            enabled=True, event_types=['balance_changed'],
        )
        self.wallet.firefly_rule = wallet_rule
        self.wallet.save()
        item_rule = NotificationRule.objects.create(
            user=self.user, name='item firefly', backend='firefly',
            config={'url': 'https://firefly.example.com', 'token': 'tok'},
            enabled=True, event_types=['balance_changed'],
        )
        self.item.firefly_rule = item_rule
        self.item.save()
        rule = _find_firefly_rule(self.item)
        self.assertEqual(rule, item_rule)

    def test_none_returned_when_no_firefly_rules(self):
        self.global_rule.delete()
        rule = _find_firefly_rule(self.item)
        self.assertIsNone(rule)

    def test_disabled_item_rule_skipped(self):
        item_rule = NotificationRule.objects.create(
            user=self.user, name='disabled item firefly', backend='firefly',
            config={'url': 'https://firefly.example.com', 'token': 'tok'},
            enabled=False, event_types=['balance_changed'],
        )
        self.item.firefly_rule = item_rule
        self.item.save()
        rule = _find_firefly_rule(self.item)
        self.assertEqual(rule, self.global_rule)


class FireflyBackfillOnLinkTests(TestCase):
    """firefly-link action triggers backfill after linking."""

    def setUp(self):
        self.user = User.objects.create_user(username='backfill_link_user', password='pw12345!')
        self.client.login(username='backfill_link_user', password='pw12345!')
        self.item = make_item(self.user, type='giftcard', value='50.00')
        self.rule = make_rule(self.user, backend='firefly', event_types=['balance_changed'])
        from rest_framework.authtoken.models import Token
        token, _ = Token.objects.get_or_create(user=self.user)
        self.auth = f'Token {token.key}'

    @patch('api.views.backfill_firefly_transactions.delay')
    @patch('api.views.requests.post')
    @patch('api.views.requests.get')
    def test_backfill_triggered_after_create(self, mock_get, mock_post, mock_backfill):
        mock_get.return_value = MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value={'data': []}))
        mock_post.return_value = MagicMock(raise_for_status=MagicMock(), json=MagicMock(return_value={'data': {'id': '55'}}))
        url = f'/api/v1/items/{self.item.pk}/firefly-link/'
        self.client.post(url, HTTP_AUTHORIZATION=self.auth)
        mock_backfill.assert_called_once_with(str(self.item.pk), self.rule.id)

    @patch('api.views.backfill_firefly_transactions.delay')
    @patch('api.views.requests.get')
    def test_backfill_triggered_after_existing_account_found(self, mock_get, mock_backfill):
        account_name = f'{self.item.name} ({self.item.issuer})' if self.item.issuer else self.item.name
        mock_get.return_value = MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={'data': [{'id': '77', 'attributes': {'name': account_name}}]}),
        )
        url = f'/api/v1/items/{self.item.pk}/firefly-link/'
        self.client.post(url, HTTP_AUTHORIZATION=self.auth)
        mock_backfill.assert_called_once_with(str(self.item.pk), self.rule.id)

    @patch('notify.backends.webpush.webpush')
    def test_send_defaults_to_root_url_when_no_item(self, mock_webpush):
        set_site_config(webpush_vapid_private_key='fake-key')
        WebPushSubscription.objects.create(
            user=self.user, endpoint='https://push.example.com/1', p256dh='a', auth='b',
        )
        backend = WebPushBackend({'user_id': self.user.id})
        backend.send('title', 'body', item=None)
        call_kwargs = mock_webpush.call_args
        payload = json.loads(call_kwargs.kwargs.get('data') or call_kwargs[1]['data'])
        self.assertEqual(payload['url'], '/')


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

    def test_firefly_requires_url_and_token(self):
        form = NotificationRuleForm(data={
            'name': 'firefly-test', 'backend': 'firefly', 'enabled': 'on',
            'event_types': ['balance_changed'], 'digest_frequency': 'immediate',
            'firefly_url': 'https://firefly.example.com',
        }, user=self.user)
        self.assertFalse(form.is_valid())

    def test_firefly_requires_url(self):
        form = NotificationRuleForm(data={
            'name': 'firefly-test', 'backend': 'firefly', 'enabled': 'on',
            'event_types': ['balance_changed'], 'digest_frequency': 'immediate',
            'firefly_token': 'secret-token',
        }, user=self.user)
        self.assertFalse(form.is_valid())

    def test_valid_firefly_rule_assembles_config(self):
        form = NotificationRuleForm(data={
            'name': 'ff', 'backend': 'firefly', 'enabled': 'on',
            'event_types': ['balance_changed'], 'digest_frequency': 'immediate',
            'firefly_url': 'https://firefly.example.com/',
            'firefly_token': 'my-pat',
        }, user=self.user)
        self.assertTrue(form.is_valid(), form.errors)
        rule = form.save(commit=False)
        rule.user = self.user
        rule.save()
        self.assertEqual(rule.config, {'url': 'https://firefly.example.com', 'token': 'my-pat'})

    def test_firefly_edit_populates_initial(self):
        rule = NotificationRule.objects.create(
            user=self.user, name='ff-edit', backend='firefly',
            config={'url': 'https://firefly.example.com', 'token': 'tok123'},
            event_types=['balance_changed'],
        )
        form = NotificationRuleForm(instance=rule, user=self.user)
        self.assertEqual(form.fields['firefly_url'].initial, 'https://firefly.example.com')
        self.assertEqual(form.fields['firefly_token'].initial, 'tok123')


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


class CheckAndNotifyInactivityTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    def _make_gc(self, **kwargs):
        defaults = {
            'type': 'giftcard', 'value_type': 'money',
            'name': 'Test GC', 'redeem_code': 'GC001',
            'issuer': 'Acme', 'value': '20.00',
            'expiry_date': date.today() + timedelta(days=90),
        }
        defaults.update(kwargs)
        return make_item(self.user, **defaults)

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_fires_for_gc_not_used_past_threshold(self, mock_send):
        make_rule(self.user, event_types=['item_inactive'])
        set_site_config(inactivity_threshold_days=30)
        stale_ts = timezone.now() - timedelta(days=31)
        item = self._make_gc(redeem_code='GC-STALE')
        item.last_used_at = stale_ts
        item.save(update_fields=['last_used_at'])

        check_and_notify_inactivity()

        self.assertTrue(NotificationLog.objects.filter(item=item, event_type='item_inactive', success=True).exists())

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_fires_for_gc_with_null_last_used_at(self, mock_send):
        make_rule(self.user, event_types=['item_inactive'])
        set_site_config(inactivity_threshold_days=30)
        item = self._make_gc(redeem_code='GC-NEVER')

        check_and_notify_inactivity()

        self.assertTrue(NotificationLog.objects.filter(item=item, event_type='item_inactive').exists())

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_does_not_fire_for_recently_used_gc(self, mock_send):
        make_rule(self.user, event_types=['item_inactive'])
        set_site_config(inactivity_threshold_days=30)
        item = self._make_gc(redeem_code='GC-RECENT')
        item.last_used_at = timezone.now() - timedelta(days=5)
        item.save(update_fields=['last_used_at'])

        check_and_notify_inactivity()

        self.assertFalse(NotificationLog.objects.filter(item=item, event_type='item_inactive').exists())

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_does_not_fire_for_used_items(self, mock_send):
        make_rule(self.user, event_types=['item_inactive'])
        set_site_config(inactivity_threshold_days=30)
        item = self._make_gc(redeem_code='GC-USED', is_used=True)

        check_and_notify_inactivity()

        self.assertFalse(NotificationLog.objects.filter(item=item, event_type='item_inactive').exists())

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_does_not_fire_for_loyalty_cards(self, mock_send):
        make_rule(self.user, event_types=['item_inactive'])
        set_site_config(inactivity_threshold_days=30)
        item = make_item(self.user, type='loyaltycard', value_type='money', redeem_code='LC-001')

        check_and_notify_inactivity()

        self.assertFalse(NotificationLog.objects.filter(item=item, event_type='item_inactive').exists())

    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_period_dedup_prevents_refiring_within_threshold(self, mock_send):
        rule = make_rule(self.user, event_types=['item_inactive'])
        set_site_config(inactivity_threshold_days=30)
        item = self._make_gc(redeem_code='GC-DEDUP')

        check_and_notify_inactivity()
        first_count = NotificationLog.objects.filter(item=item, event_type='item_inactive', success=True).count()

        check_and_notify_inactivity()
        second_count = NotificationLog.objects.filter(item=item, event_type='item_inactive', success=True).count()

        self.assertEqual(first_count, 1)
        self.assertEqual(second_count, 1, 'Second run should not re-fire within threshold period')

    def test_noop_without_any_rules(self):
        set_site_config(inactivity_threshold_days=30)
        self._make_gc(redeem_code='GC-NORULE')
        check_and_notify_inactivity()
        self.assertEqual(NotificationLog.objects.count(), 0)


class CheckMerchantHealthTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')

    @patch('notify.tasks.check_companies_house_status')
    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_fires_alert_for_bad_company_status(self, mock_send, mock_ch):
        make_rule(self.user, event_types=['merchant_health_alert'])
        set_site_config(companies_house_api_key='dummy-key')
        item = make_item(self.user, issuer='Acme Retail', redeem_code='MH-001')

        mock_ch.return_value = {
            'company_name': 'Acme Retail Ltd',
            'company_status': 'administration',
            'company_number': '12345678',
        }

        check_merchant_health()

        self.assertTrue(NotificationLog.objects.filter(item=item, event_type='merchant_health_alert', success=True).exists())

    @patch('notify.tasks.check_companies_house_status')
    @patch('notify.tasks.send_via_rule', return_value=(True, ''))
    def test_no_alert_for_active_company(self, mock_send, mock_ch):
        make_rule(self.user, event_types=['merchant_health_alert'])
        set_site_config(companies_house_api_key='dummy-key')
        item = make_item(self.user, issuer='Acme Retail', redeem_code='MH-002')

        mock_ch.return_value = None  # None means active / not in bad status

        check_merchant_health()

        self.assertFalse(NotificationLog.objects.filter(item=item, event_type='merchant_health_alert').exists())

    @patch('notify.tasks.check_companies_house_status')
    def test_noop_without_api_key(self, mock_ch):
        make_rule(self.user, event_types=['merchant_health_alert'])
        set_site_config(companies_house_api_key='')
        make_item(self.user, issuer='Acme Retail', redeem_code='MH-003')

        check_merchant_health()

        mock_ch.assert_not_called()
        self.assertEqual(NotificationLog.objects.count(), 0)
