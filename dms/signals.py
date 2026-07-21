"""
Auto-push new document attachments to DMS providers that have auto_push=True.
"""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='myapp.Document')
def document_auto_push(sender, instance, created, **kwargs):
    if not created:
        return
    # Prevent push-pull loop: documents pulled from DMS must not be re-pushed back.
    if getattr(instance, '_dms_pulled', False):
        return

    from .models import DMSProvider
    from .tasks import push_document_to_dms

    providers = DMSProvider.objects.filter(
        user=instance.item.user,
        enabled=True,
        auto_push=True,
    )
    for provider in providers:
        provider_id = str(provider.id)
        document_id = str(instance.id)
        try:
            transaction.on_commit(
                lambda p=provider_id, d=document_id: push_document_to_dms.delay(p, d)
            )
        except Exception as exc:
            logger.error('document_auto_push: could not queue task for provider %s: %s', provider.id, exc)
