"""
Celery tasks for DMS integration.
"""
import logging
import os
from datetime import date

from celery import shared_task
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def push_document_to_dms(self, provider_id, document_id):
    """
    Push a VoucherVault Document to a DMS provider.
    Called from the auto-push signal handler.
    """
    from .clients import get_client
    from .models import DMSProvider, DMSSyncLog
    from myapp.models import Document

    try:
        provider = DMSProvider.objects.get(id=provider_id)
        doc = Document.objects.select_related('item').get(id=document_id)
    except (DMSProvider.DoesNotExist, Document.DoesNotExist) as exc:
        logger.error('push_document_to_dms: object not found — %s', exc)
        return

    if not provider.enabled:
        logger.debug('push_document_to_dms: provider %s disabled, skipping', provider_id)
        return

    # Avoid duplicate pushes
    if DMSSyncLog.objects.filter(
        provider=provider,
        document=doc,
        direction=DMSSyncLog.DIRECTION_PUSH,
        status=DMSSyncLog.STATUS_OK,
    ).exists():
        logger.debug('push_document_to_dms: already pushed doc %s to provider %s', document_id, provider_id)
        return

    client = get_client(provider)
    try:
        doc.file.seek(0)
        content = doc.file.read()
        filename = doc.file.name.rsplit('/', 1)[-1]
        title = f'{doc.item.name} — {filename}'
        tags = ['vouchervault']
        if doc.item.wallet:
            tags.append(doc.item.wallet.name)

        dms_id = client.upload_document(
            filename=filename,
            content=content,
            title=title,
            tags=tags,
            correspondent=doc.item.issuer or '',
        )
        DMSSyncLog.objects.create(
            provider=provider,
            direction=DMSSyncLog.DIRECTION_PUSH,
            status=DMSSyncLog.STATUS_OK,
            item=doc.item,
            document=doc,
            dms_document_id=str(dms_id),
            dms_document_title=title,
            detail=f'Auto-pushed to {provider.name}',
        )
        logger.info('push_document_to_dms: pushed doc %s to %s (dms_id=%s)', document_id, provider.name, dms_id)
    except Exception as exc:
        DMSSyncLog.objects.create(
            provider=provider,
            direction=DMSSyncLog.DIRECTION_PUSH,
            status=DMSSyncLog.STATUS_ERROR,
            item=doc.item,
            document=doc,
            detail=str(exc),
        )
        logger.error('push_document_to_dms error: %s', exc)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2)
def auto_pull_from_dms(self, provider_id):
    """
    Pull new documents from a DMS provider and create VoucherVault items+documents.
    Skips documents already pulled (checked via DMSSyncLog dms_document_id).
    """
    from .clients import get_client
    from .models import DMSProvider, DMSSyncLog
    from myapp.models import Document, Item

    try:
        provider = DMSProvider.objects.get(id=provider_id)
    except DMSProvider.DoesNotExist:
        return

    if not provider.enabled or not provider.auto_pull:
        return

    client = get_client(provider)
    pulled_ids = set(
        DMSSyncLog.objects.filter(
            provider=provider,
            direction=DMSSyncLog.DIRECTION_PULL,
            status=DMSSyncLog.STATUS_OK,
        ).values_list('dms_document_id', flat=True)
    )

    page = 1
    page_size = 50
    while True:
        try:
            result = client.browse(
                tag=provider.pull_tag,
                correspondent=provider.pull_correspondent,
                page=page,
                page_size=page_size,
            )
        except Exception as exc:
            logger.error('auto_pull_from_dms browse error provider=%s page=%s: %s', provider_id, page, exc)
            return

        for dms_doc in result.documents:
            if dms_doc.id in pulled_ids:
                continue
            try:
                raw_bytes = client.download_document(dms_doc.id)
                ext = os.path.splitext(dms_doc.original_filename or 'document.pdf')[1] or '.pdf'
                safe_title = ''.join(c for c in (dms_doc.title or '') if c.isalnum() or c in ' -_')[:60]
                filename = f'{safe_title or dms_doc.id}{ext}'

                item = Item.objects.create(
                    user=provider.user,
                    name=dms_doc.title or filename,
                    type='voucher',
                    issuer='',
                    redeem_code='',
                    value='0.00',
                    source='api',
                    expiry_date=date(9999, 12, 31),
                )
                doc = Document(item=item, extracted_text=(dms_doc.content or '')[:10000])
                doc._dms_pulled = True
                doc.file.save(filename, ContentFile(raw_bytes), save=True)

                DMSSyncLog.objects.create(
                    provider=provider,
                    direction=DMSSyncLog.DIRECTION_PULL,
                    status=DMSSyncLog.STATUS_OK,
                    item=item,
                    document=doc,
                    dms_document_id=dms_doc.id,
                    dms_document_title=dms_doc.title,
                    detail=f'Auto-pulled from {provider.name}',
                )
                logger.info('auto_pull_from_dms: pulled %s from %s', dms_doc.id, provider.name)
            except Exception as exc:
                DMSSyncLog.objects.create(
                    provider=provider,
                    direction=DMSSyncLog.DIRECTION_PULL,
                    status=DMSSyncLog.STATUS_ERROR,
                    dms_document_id=dms_doc.id,
                    dms_document_title=dms_doc.title,
                    detail=str(exc),
                )
                logger.error('auto_pull_from_dms item create error: %s', exc)

        if not result.has_next:
            break
        page += 1


@shared_task
def dms_scheduled_pull_all():
    """
    Periodic task: fire auto_pull_from_dms for every enabled auto-pull provider.
    Registered in Celery Beat via CELERY_BEAT_SCHEDULE in settings.py.
    """
    from .models import DMSProvider
    for provider in DMSProvider.objects.filter(enabled=True, auto_pull=True):
        auto_pull_from_dms.delay(str(provider.id))
