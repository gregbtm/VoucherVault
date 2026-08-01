from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import NotificationRule, WebhookRetry
from .tasks import attempt_webhook_retry


def _make_rule(user, **kwargs):
    defaults = {
        'name': 'My Webhook',
        'backend': 'webhook',
        'config': {'url': 'https://example.com/hook'},
    }
    defaults.update(kwargs)
    return NotificationRule.objects.create(user=user, **defaults)


def _make_retry(rule, **kwargs):
    defaults = {
        'event_type': 'item_used',
        'payload': {'title': 't', 'message': 'm'},
        'attempt': 1,
        'last_error': 'Connection timed out',
        'next_retry_at': timezone.now(),
    }
    defaults.update(kwargs)
    return WebhookRetry.objects.create(rule=rule, **defaults)


class AttemptWebhookRetryTests(TestCase):
    """Unit tests for the attempt_webhook_retry() helper shared by the
    periodic sweep (process_webhook_retries) and the manual retry view."""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.rule = _make_rule(self.user)

    @patch('requests.post')
    def test_success_deletes_the_row(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()
        retry = _make_retry(self.rule, attempt=2)
        result = attempt_webhook_retry(retry)
        self.assertEqual(result, 'success')
        self.assertFalse(WebhookRetry.objects.filter(rule=self.rule).exists())

    @patch('requests.post')
    def test_failure_reschedules_and_increments_attempt(self, mock_post):
        import requests
        mock_post.side_effect = requests.RequestException('boom')
        retry = _make_retry(self.rule, attempt=0)
        result = attempt_webhook_retry(retry)
        self.assertEqual(result, 'rescheduled')
        retry.refresh_from_db()
        self.assertEqual(retry.attempt, 1)
        self.assertEqual(retry.last_error, 'boom')
        self.assertIsNotNone(retry.next_retry_at)

    @patch('requests.post')
    def test_failure_on_last_attempt_deletes_the_row(self, mock_post):
        import requests
        mock_post.side_effect = requests.RequestException('boom')
        retry = _make_retry(self.rule, attempt=len(WebhookRetry.RETRY_BACKOFF) - 1)
        result = attempt_webhook_retry(retry)
        self.assertEqual(result, 'exhausted')
        self.assertFalse(WebhookRetry.objects.filter(rule=self.rule).exists())


class NotificationLogWebhookRetryUITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='pw12345!')
        self.other_user = User.objects.create_user(username='bob', password='pw12345!')
        self.rule = _make_rule(self.user)
        self.other_rule = _make_rule(self.other_user, name='Bob Webhook')
        self.client.force_login(self.user)

    def test_log_page_lists_pending_retries_for_the_current_user(self):
        _make_retry(self.rule)
        response = self.client.get(reverse('notification_log'))
        self.assertContains(response, 'Pending Webhook Retries')
        self.assertContains(response, 'My Webhook')

    def test_log_page_hides_the_section_when_no_retries_are_pending(self):
        response = self.client.get(reverse('notification_log'))
        self.assertNotContains(response, 'Pending Webhook Retries')

    def test_log_page_does_not_list_another_users_retries(self):
        _make_retry(self.other_rule)
        response = self.client.get(reverse('notification_log'))
        self.assertNotContains(response, 'Bob Webhook')

    @patch('requests.post')
    def test_retry_now_success_shows_success_message_and_removes_row(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        mock_post.return_value.raise_for_status = MagicMock()
        retry = _make_retry(self.rule)
        response = self.client.post(reverse('retry_webhook_now', args=[retry.id]), follow=True)
        self.assertRedirects(response, reverse('notification_log'))
        self.assertContains(response, 'Webhook delivered successfully')
        self.assertFalse(WebhookRetry.objects.filter(pk=retry.pk).exists())

    @patch('requests.post')
    def test_retry_now_failure_shows_warning_message_and_keeps_row(self, mock_post):
        import requests
        mock_post.side_effect = requests.RequestException('still down')
        retry = _make_retry(self.rule, attempt=0)
        response = self.client.post(reverse('retry_webhook_now', args=[retry.id]), follow=True)
        self.assertContains(response, 'remains queued')
        self.assertTrue(WebhookRetry.objects.filter(pk=retry.pk).exists())

    def test_retry_now_404s_for_another_users_retry(self):
        retry = _make_retry(self.other_rule)
        response = self.client.post(reverse('retry_webhook_now', args=[retry.id]))
        self.assertEqual(response.status_code, 404)

    def test_retry_now_requires_post(self):
        retry = _make_retry(self.rule)
        response = self.client.get(reverse('retry_webhook_now', args=[retry.id]))
        self.assertEqual(response.status_code, 405)

    def test_retry_now_requires_login(self):
        self.client.logout()
        retry = _make_retry(self.rule)
        response = self.client.post(reverse('retry_webhook_now', args=[retry.id]))
        self.assertEqual(response.status_code, 302)
