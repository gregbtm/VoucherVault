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
        ('ntfy', 'ntfy'),
        ('webhook', 'Webhook (n8n etc.)'),
    ]
    EVENT_CHOICES = [
        ('expiry_warning', 'Expiry Warning'),
        ('expiry_final', 'Final Expiry Warning'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notification_rules')
    name = models.CharField(max_length=100)
    backend = models.CharField(max_length=20, choices=BACKEND_CHOICES)
    config = models.JSONField(default=dict, blank=True)  # backend-specific config blob
    enabled = models.BooleanField(default=True)
    event_types = models.JSONField(default=list, blank=True)  # e.g. ['expiry_warning', 'expiry_final']
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
