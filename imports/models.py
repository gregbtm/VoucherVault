import uuid

from django.contrib.auth.models import User
from django.db import models


class ImportJob(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('complete', 'Complete'),
        ('failed', 'Failed'),
    ]
    SOURCE_CHOICES = [
        ('catima_csv', 'Catima CSV'),
        ('native_csv', 'VoucherVault CSV Backup'),
        ('native_json', 'VoucherVault JSON Backup'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='import_jobs')
    source_type = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    # MEDIA_ROOT isn't set (defaults to cwd), and only `database/` is a
    # persisted volume in docker-compose (see Item.file's own upload_to for
    # the same convention) — uploading to a bare "imports/" would otherwise
    # land inside this Django app's own source directory and not survive a
    # container restart.
    file = models.FileField(upload_to='database/imports/')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    imported_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)  # list of {"row": int, "message": str}
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_source_type_display()} import ({self.status}) for {self.user}'
