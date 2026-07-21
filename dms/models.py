import uuid
from django.db import models
from django.contrib.auth.models import User


class DMSProvider(models.Model):
    PROVIDER_PAPERLESS = 'paperless'
    PROVIDER_DOCSPELL = 'docspell'
    PROVIDER_PAPERMERGE = 'papermerge'

    PROVIDER_CHOICES = [
        (PROVIDER_PAPERLESS, 'Paperless-ngx'),
        (PROVIDER_DOCSPELL, 'Docspell'),
        (PROVIDER_PAPERMERGE, 'PaperMerge'),
    ]

    STATUS_OK = 'ok'
    STATUS_ERROR = 'error'
    STATUS_UNCHECKED = 'unchecked'

    STATUS_CHOICES = [
        (STATUS_OK, 'Connected'),
        (STATUS_ERROR, 'Error'),
        (STATUS_UNCHECKED, 'Not tested'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dms_providers')
    name = models.CharField(max_length=120)
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    base_url = models.URLField(max_length=500)

    # Auth — provider-specific fields, nullable and encrypted at env level
    api_token = models.CharField(max_length=512, blank=True, help_text='Paperless-ngx / PaperMerge API token')
    username = models.CharField(max_length=255, blank=True, help_text='Docspell username')
    password = models.CharField(max_length=512, blank=True, help_text='Docspell password (stored plaintext; use a dedicated account)')
    docspell_collective = models.CharField(max_length=255, blank=True, help_text='Docspell collective name')
    docspell_source_id = models.CharField(max_length=255, blank=True, help_text='Docspell upload source ID')

    # Sync behaviour
    auto_push = models.BooleanField(default=False, help_text='Automatically push new attachments to this DMS')
    auto_pull = models.BooleanField(default=False, help_text='Periodically pull documents tagged with the VoucherVault tag')
    pull_tag = models.CharField(max_length=120, blank=True, help_text='Tag name in the DMS to pull (leave blank to pull all)')
    pull_correspondent = models.CharField(max_length=120, blank=True, help_text='Correspondent/source to pull from (optional filter)')

    # Connection health
    last_checked = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_UNCHECKED)
    status_message = models.TextField(blank=True)

    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'DMS Provider'
        verbose_name_plural = 'DMS Providers'

    def __str__(self):
        return f'{self.name} ({self.get_provider_display()})'

    @property
    def status_badge_class(self):
        return {
            self.STATUS_OK: 'success',
            self.STATUS_ERROR: 'danger',
            self.STATUS_UNCHECKED: 'secondary',
        }.get(self.status, 'secondary')


class DMSSyncLog(models.Model):
    DIRECTION_PUSH = 'push'
    DIRECTION_PULL = 'pull'

    DIRECTION_CHOICES = [
        (DIRECTION_PUSH, 'Push (VV → DMS)'),
        (DIRECTION_PULL, 'Pull (DMS → VV)'),
    ]

    STATUS_OK = 'ok'
    STATUS_ERROR = 'error'
    STATUS_SKIPPED = 'skipped'

    STATUS_CHOICES = [
        (STATUS_OK, 'Success'),
        (STATUS_ERROR, 'Error'),
        (STATUS_SKIPPED, 'Skipped'),
    ]

    provider = models.ForeignKey(DMSProvider, on_delete=models.CASCADE, related_name='sync_logs')
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)

    # What was synced
    item = models.ForeignKey('myapp.Item', on_delete=models.SET_NULL, null=True, blank=True, related_name='dms_sync_logs')
    document = models.ForeignKey('myapp.Document', on_delete=models.SET_NULL, null=True, blank=True, related_name='dms_sync_logs')
    dms_document_id = models.CharField(max_length=255, blank=True, help_text='Remote document ID in the DMS')
    dms_document_title = models.CharField(max_length=500, blank=True)

    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'DMS Sync Log'
        verbose_name_plural = 'DMS Sync Logs'

    def __str__(self):
        return f'{self.get_direction_display()} — {self.provider.name} — {self.get_status_display()} ({self.created_at:%Y-%m-%d %H:%M})'

    @property
    def status_badge_class(self):
        return {
            self.STATUS_OK: 'success',
            self.STATUS_ERROR: 'danger',
            self.STATUS_SKIPPED: 'secondary',
        }.get(self.status, 'secondary')
