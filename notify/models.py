from django.contrib.auth.models import User
from django.db import models

from myapp.models import Item


class NotificationRule(models.Model):
    """
    Per-user notification channel configuration. Apprise remains available
    as a backend choice here for users migrating off the legacy single
    apprise_urls field on UserProfile — that legacy field and its Celery
    task are untouched and keep working independently of this app.
    """
    BACKEND_CHOICES = [
        ('apprise', 'Apprise URL'),
        ('firefly', 'Firefly III'),
        ('ntfy', 'ntfy'),
        ('webhook', 'Webhook (n8n etc.)'),
        ('webpush', 'Web Push'),
    ]
    EVENT_CHOICES = [
        ('expiry_warning', 'Expiry Warning'),
        ('expiry_final', 'Final Expiry Warning'),
        ('item_created', 'Item Created'),
        ('item_used', 'Item Marked Used'),
        ('item_archived', 'Item Archived'),
        ('balance_changed', 'Balance Changed'),
        ('item_shared', 'Item Shared'),
        ('next_up_reminder', 'Next Up Item Due Today'),
    ]
    DIGEST_FREQUENCY_CHOICES = [
        ('immediate', 'Immediate'),
        ('daily', 'Daily Digest'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notification_rules')
    name = models.CharField(max_length=100)
    backend = models.CharField(max_length=20, choices=BACKEND_CHOICES)
    config = models.JSONField(default=dict, blank=True)  # backend-specific config blob
    enabled = models.BooleanField(default=True)
    event_types = models.JSONField(default=list, blank=True)  # e.g. ['expiry_warning', 'expiry_final']
    digest_frequency = models.CharField(
        max_length=10, choices=DIGEST_FREQUENCY_CHOICES, default='immediate',
        help_text='"Daily Digest" batches matching events into one combined message sent once a day '
                   'instead of pinging for each one separately.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        unique_together = ('user', 'name')

    def __str__(self):
        return f'{self.name} ({self.get_backend_display()})'


class NotificationLog(models.Model):
    """
    Audit trail for every notification attempt. Also doubles as the dedup
    source of truth: a rule won't re-fire the same event_type for the same
    item once a successful log entry exists for that combination.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notification_logs')
    rule = models.ForeignKey(NotificationRule, on_delete=models.SET_NULL, null=True, blank=True, related_name='logs')
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True, related_name='notification_logs')
    event_type = models.CharField(max_length=50)
    sent_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField()
    detail = models.TextField(blank=True)

    class Meta:
        ordering = ['-sent_at']

    def __str__(self):
        return f'{self.event_type} via {self.rule_id} @ {self.sent_at}'


class DigestEntry(models.Model):
    """
    A notification queued for a rule whose digest_frequency is 'daily'
    instead of sent immediately. send_daily_digests() (notify/tasks.py)
    groups these by rule once a day, sends one combined message per rule,
    and clears them - this table only ever holds same-day, not-yet-sent
    entries.
    """
    rule = models.ForeignKey(NotificationRule, on_delete=models.CASCADE, related_name='digest_entries')
    item = models.ForeignKey(Item, on_delete=models.SET_NULL, null=True, blank=True)
    event_type = models.CharField(max_length=50)
    title = models.CharField(max_length=255)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.title} (queued for {self.rule})'


class WebPushSubscription(models.Model):
    """
    A browser/device's Push API subscription, created client-side via
    PushManager.subscribe() and posted to us. A user may have several (one
    per browser/device) — the webpush backend delivers to all of a user's
    active subscriptions, unlike ntfy/webhook which target one fixed
    destination per rule.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='webpush_subscriptions')
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user} @ {self.endpoint[:50]}'
