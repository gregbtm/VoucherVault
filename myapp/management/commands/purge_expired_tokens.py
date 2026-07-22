from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from rest_framework.authtoken.models import Token


class Command(BaseCommand):
    help = 'Delete API tokens older than API_TOKEN_EXPIRY_DAYS. No-op when expiry is disabled (=0).'

    def handle(self, *args, **options):
        expiry_days = getattr(settings, 'API_TOKEN_EXPIRY_DAYS', 0)
        if not expiry_days:
            self.stdout.write('API_TOKEN_EXPIRY_DAYS=0 — expiry disabled, nothing to purge.')
            return
        cutoff = timezone.now() - timedelta(days=expiry_days)
        deleted, _ = Token.objects.filter(created__lt=cutoff).delete()
        self.stdout.write(self.style.SUCCESS(
            f'Purged {deleted} expired token(s) older than {expiry_days} day(s).'
        ))
