# myapp/tasks.py
from celery import shared_task
from django.core.management import call_command
from django.utils import timezone
from django.db import models

from .merchant_logos import fetch_merchant_logo, merchant_logos_enabled
from .update_check import check_for_update, check_upstream_version

@shared_task
def run_expiration_check():
    call_command('check_expiration')

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_merchant_logo_task(self, name, domain_hint=None):
    if not name or not merchant_logos_enabled():
        return
    try:
        fetch_merchant_logo(name, domain_hint=domain_hint)
    except Exception as exc:
        raise self.retry(exc=exc)

@shared_task
def check_for_update_task():
    check_for_update()

@shared_task
def check_upstream_version_task():
    check_upstream_version()

@shared_task
def extract_document_text_task(document_id):
    """
    Run OCR on a Document file and store the result in Document.extracted_text.
    Silently no-ops when OCR is disabled; logs and exits on any extraction error
    so a failure never blocks the upload response.
    """
    import logging
    _log = logging.getLogger(__name__)
    from ocr.backends import get_backend, ocr_enabled
    if not ocr_enabled():
        return
    from .models import Document
    try:
        document = Document.objects.get(pk=document_id)
    except Document.DoesNotExist:
        return
    try:
        import mimetypes
        mime_type = mimetypes.guess_type(document.file.name)[0] or 'application/octet-stream'
        if mime_type == 'application/pdf':
            # Rasterise page 1 of the PDF to an image, then OCR it.
            import pypdfium2 as pdfium
            document.file.seek(0)
            pdf = pdfium.PdfDocument(document.file.read())
            page = pdf[0]
            bitmap = page.render(scale=2)
            pil_img = bitmap.to_pil()
            from io import BytesIO
            buf = BytesIO()
            pil_img.save(buf, format='PNG')
            image_bytes = buf.getvalue()
            ocr_mime = 'image/png'
        else:
            document.file.seek(0)
            image_bytes = document.file.read()
            ocr_mime = mime_type
        result = get_backend().extract(image_bytes, ocr_mime)
        parts = []
        for key in ('name', 'issuer', 'description', 'notes'):
            val = result.get(key)
            if val:
                parts.append(str(val))
        if result.get('code'):
            parts.append(result['code'])
        text = '\n'.join(parts)
        Document.objects.filter(pk=document_id).update(extracted_text=text)
    except Exception:
        _log.warning('Document OCR failed for document %s', document_id, exc_info=True)


@shared_task
def check_login_spike_task():
    """
    Hourly check: if failed login attempts in the last 60 minutes exceed
    SiteConfiguration.security_alert_threshold and a security ntfy topic is
    configured, fire a single ntfy alert to that topic.
    """
    import logging
    import requests as _requests
    from datetime import timedelta
    from django.utils import timezone
    from .models import SiteConfiguration, LoginAuditLog

    _log = logging.getLogger(__name__)
    config = SiteConfiguration.load()
    topic = config.security_alert_ntfy_topic.strip()
    if not topic:
        return

    window_start = timezone.now() - timedelta(hours=1)
    failed_count = LoginAuditLog.objects.filter(
        success=False,
        timestamp__gte=window_start,
    ).count()

    if failed_count < config.security_alert_threshold:
        return

    server = (config.ntfy_default_server or 'https://ntfy.sh').rstrip('/')
    try:
        _requests.post(
            f'{server}/{topic}',
            data=f'{failed_count} failed login attempts in the last hour.'.encode('utf-8'),
            headers={
                'Title': 'VoucherVault Security Alert'.encode('utf-8'),
                'Priority': 'high',
                'Tags': 'warning',
            },
            timeout=10,
        )
        _log.warning('Security alert sent: %d failed logins in last hour.', failed_count)
    except Exception as exc:
        _log.warning('Failed to send security alert: %s', exc)


@shared_task
def mark_expired_commute_outward_tickets():
    """
    Bookkeeping companion to analytics.get_active_today_item(): once a
    user's configured Active Today cutoff time has passed, marks today's
    outward-leg commute ticket (journey_origin matching their
    commute_home_station) is_used=True, so it stops counting as available
    everywhere else in the app (Inventory counts, Next Up, etc). Purely a
    bookkeeping flip - the Active Today widget itself decides what to
    *display* directly from the current time vs cutoff on every read,
    independent of whether this task has run yet, so a delay here never
    leaves the widget showing something stale.
    """
    from notify.tasks import notify_item_used

    from .models import Item, UserPreference

    today = timezone.localtime().date()
    now_time = timezone.localtime().time()
    preferences = UserPreference.objects.filter(active_today_enabled=True).exclude(commute_home_station='')
    for prefs in preferences:
        if now_time < prefs.active_today_cutoff_time:
            continue
        outward = Item.objects.filter(
            user=prefs.user, is_used=False, is_archived=False, expiry_date=today,
            journey_origin__iexact=prefs.commute_home_station.strip(),
        ).exclude(journey_destination='').first()
        if outward:
            outward.is_used = True
            outward.save(update_fields=['is_used'])
            notify_item_used(outward)


@shared_task
def run_scheduled_enrichment(method):
    """
    Celery Beat task to run scheduled enrichment for a given method.
    Creates an EnrichmentRun and queues items for enrichment.
    """
    import logging
    import uuid
    from .models import EnrichmentConfig, EnrichmentRun, Item

    _log = logging.getLogger(__name__)

    try:
        config = EnrichmentConfig.objects.get(method=method, enabled=True)
    except EnrichmentConfig.DoesNotExist:
        _log.debug(f"Enrichment config not found or disabled for method: {method}")
        return

    run_id = uuid.uuid4()
    try:
        # Find all items to enrich (exclude archived)
        items = Item.objects.filter(is_archived=False).select_related('user')
        item_count = items.count()

        # Create enrichment run
        run = EnrichmentRun.objects.create(
            id=run_id,
            method=method,
            status='in_progress',
            confidence_threshold=config.confidence_threshold,
            total_items=item_count,
        )

        # Queue items for enrichment
        for item in items:
            queue_item_enrichment.delay(item.id, str(run_id), method, config.auto_apply)

        # If no items, finalize immediately
        if item_count == 0:
            finalize_enrichment_run.delay(str(run_id))
        else:
            # Schedule finalization check after 5 minutes (gives time for all items to process)
            from celery import current_app
            current_app.send_task(
                'myapp.tasks.check_and_finalize_run',
                args=[str(run_id)],
                countdown=300  # 5 minutes
            )

        _log.info(f"Started enrichment run {run_id.hex[:8]} for method {method} with {item_count} items")

    except Exception as exc:
        _log.error(f"Error starting enrichment run for {method}: {exc}", exc_info=True)
        if 'run' in locals():
            run.status = 'failed'
            run.completed_at = timezone.now()
            run.save()


@shared_task
def check_and_finalize_run(run_id):
    """
    Check if an enrichment run has completed all items and finalize if so.
    Retries every minute until all items are done.
    """
    import logging
    from .models import EnrichmentRun, EnrichmentRunItem

    _log = logging.getLogger(__name__)

    try:
        run = EnrichmentRun.objects.get(id=run_id)

        # Count completed run items
        completed_count = EnrichmentRunItem.objects.filter(run=run).count()

        if completed_count >= run.total_items:
            # All items complete, finalize
            finalize_enrichment_run.delay(run_id)
        else:
            # Not done yet, check again in 1 minute
            from celery import current_app
            current_app.send_task(
                'myapp.tasks.check_and_finalize_run',
                args=[run_id],
                countdown=60
            )
            _log.debug(f"Run {run_id.hex[:8]} has {completed_count}/{run.total_items} items, will recheck")

    except Exception as exc:
        _log.error(f"Error checking finalization for run {run_id}: {exc}", exc_info=True)


@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def queue_item_enrichment(self, item_id, run_id, method, auto_apply=False):
    """
    Enrich a single item using the specified method.
    Queued by run_scheduled_enrichment.
    """
    import logging
    from .models import Item, EnrichmentRun, EnrichmentRunItem
    from .services.enrichment import ItemEnricher

    _log = logging.getLogger(__name__)

    try:
        item = Item.objects.get(id=item_id)
        run = EnrichmentRun.objects.get(id=run_id)
    except (Item.DoesNotExist, EnrichmentRun.DoesNotExist) as exc:
        _log.error(f"Item or run not found: {exc}")
        return

    try:
        enricher = ItemEnricher(created_by=None)
        result = None

        if method == 'ocr':
            result = enricher.enrich_from_ocr(item, run.confidence_threshold)
        elif method == 'validation':
            result = enricher.validate_and_normalize(item, run.confidence_threshold)
        elif method == 'merchant_lookup':
            result = enricher.enrich_from_merchant_lookup(item, run.confidence_threshold)

        if result is None:
            raise ValueError(f"Unknown enrichment method: {method}")

        # Store result and preview
        run_item, created = EnrichmentRunItem.objects.get_or_create(
            run=run,
            item=item,
            defaults={
                'success': result.success,
                'changes_proposed': len(result.changes),
                'error_message': result.error_message or '',
                'preview_data': {
                    'changes': [
                        {
                            'field_name': c.field_name,
                            'old_value': c.old_value,
                            'new_value': c.new_value,
                            'confidence_score': float(c.confidence_score),
                            'reason': c.reason,
                        }
                        for c in result.changes
                    ],
                    'flags': [
                        {
                            'field_name': f.field_name,
                            'value': f.current_value,
                            'message': f.message,
                        }
                        for f in result.flags
                    ],
                }
            }
        )

        # Apply changes if auto_apply is True
        if auto_apply and result.changes:
            success, error = enricher.apply_changes(item, result.changes)
            if success:
                enricher.log_enrichment(item, result.enrichment_run_id, result.changes, method)
                run_item.changes_applied = len(result.changes)
                run_item.success = True
                run_item.save(update_fields=['changes_applied', 'success'])
            else:
                run_item.error_message = error or 'Failed to apply changes'
                run_item.save(update_fields=['error_message'])

        if result.flags:
            enricher.log_flags(item, result.enrichment_run_id, result.flags)

        _log.debug(f"Enriched item {item_id} in run {run_id.hex[:8]} with {len(result.changes)} changes")

    except Exception as exc:
        _log.error(f"Error enriching item {item_id}: {exc}", exc_info=True)
        # Retry once on transient errors
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)


@shared_task
def finalize_enrichment_run(run_id):
    """
    Finalize an enrichment run: calculate summary stats and update status.
    Called manually or via scheduled check.
    """
    import logging
    from decimal import Decimal
    from django.db.models import Count, Sum, Q
    from .models import EnrichmentRun, EnrichmentRunItem, EnrichmentConfig

    _log = logging.getLogger(__name__)

    try:
        run = EnrichmentRun.objects.get(id=run_id)

        # Recalculate stats from run items
        stats = EnrichmentRunItem.objects.filter(run=run).aggregate(
            total=Count('id'),
            successful=Count('id', filter=Q(success=True)),
            total_changes=Sum('changes_proposed'),
        )

        run.total_items = stats['total'] or 0
        run.successful_items = stats['successful'] or 0
        run.total_changes = stats['total_changes'] or 0

        # Calculate average confidence from preview data
        if run.total_items > 0:
            confidence_scores = []
            for item in EnrichmentRunItem.objects.filter(run=run):
                changes = item.preview_data.get('changes', [])
                if changes:
                    confidence_scores.extend([c['confidence_score'] for c in changes])
            if confidence_scores:
                run.average_confidence = Decimal(str(sum(confidence_scores) / len(confidence_scores)))

        run.completed_at = timezone.now()

        # Set status based on auto_apply config
        try:
            cfg = EnrichmentConfig.objects.get(method=run.method)
            if cfg.auto_apply:
                run.status = 'completed'
            else:
                run.status = 'pending_approval'
        except EnrichmentConfig.DoesNotExist:
            run.status = 'completed'

        run.save()
        _log.info(f"Finalized run {run_id.hex[:8]}: {run.successful_items}/{run.total_items} items, {run.total_changes} changes")

    except EnrichmentRun.DoesNotExist:
        _log.error(f"Enrichment run {run_id} not found")
    except Exception as exc:
        _log.error(f"Error finalizing run {run_id}: {exc}", exc_info=True)
