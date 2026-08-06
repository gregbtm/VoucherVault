# myapp/tasks.py
from decimal import Decimal, InvalidOperation
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

    Also logs a spend Transaction when the OCR backend's structured
    extraction includes a plausible amount (see _record_receipt_amount) -
    every backend (Claude/OpenAI vision, Tesseract's regex-based guess)
    already computes a 'value' field for this same extract() call, it was
    just discarded here previously.
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
        _record_receipt_amount(document, result, _log)
    except Exception:
        _log.warning('Document OCR failed for document %s', document_id, exc_info=True)


def _record_receipt_amount(document, ocr_result, log):
    """
    Log a spend Transaction against document.item when the OCR result
    includes a plausible amount - additive (never touches Item.value
    directly), so a bad OCR read is just a wrong transaction to delete,
    not corrupted canonical item data. Mirrors the existing pattern in
    notify/signals.py::handle_item_value_change (create a Transaction, then
    notify_balance_changed) for an automated system recording a balance
    change outside the normal view_item POST flow.

    Skipped for non-money items (loyalty cards/travel passes have no
    balance concept), amounts that aren't a sane positive number, and
    documents already processed once (re-running OCR on the same document
    - e.g. a retry - must not log the same spend twice).
    """
    item = document.item
    if item.value_type != 'money':
        return

    raw_value = ocr_result.get('value')
    if raw_value is None:
        return
    try:
        amount = Decimal(str(raw_value))
    except (InvalidOperation, ValueError, TypeError):
        return
    if amount <= 0 or amount > Decimal('100000'):
        return

    from .models import Transaction
    description = f'Auto-extracted from receipt (document #{document.id})'
    if Transaction.objects.filter(item=item, description=description).exists():
        return

    try:
        tx = Transaction.objects.create(
            item=item,
            description=description,
            value=-amount,
            date=timezone.now(),
        )
        from notify.tasks import notify_balance_changed
        notify_balance_changed(item, tx)
    except Exception:
        log.warning('Failed to log receipt-extracted transaction for document %s', document.id, exc_info=True)


@shared_task
def check_database_integrity_task():
    """
    Weekly sweep for the exact failure class behind the
    myapp_itemrecommendation outage: a migration silently rolls back (or a
    table is otherwise dropped/missing) and nothing surfaces it until a
    user hits a 500 and sends a screenshot. tolerate_missing_table()
    covers individual request-time call sites gracefully, but this task
    is the belt-and-braces catch-all - it checks *every* model's table,
    not just the ones someone remembered to wrap, and alerts an admin
    the same way a login spike does.
    """
    import logging
    import requests as _requests
    from .models import SiteConfiguration
    from .db_health import missing_tables

    _log = logging.getLogger(__name__)
    missing = missing_tables()
    if not missing:
        return

    _log.error("Database integrity check found missing tables: %s", missing)

    config = SiteConfiguration.load()
    topic = config.security_alert_ntfy_topic.strip()
    if not topic:
        return

    summary = '\n'.join(f'{model} -> {table}' for model, table in missing)
    server = (config.ntfy_default_server or 'https://ntfy.sh').rstrip('/')
    try:
        _requests.post(
            f'{server}/{topic}',
            data=f'Database tables missing for {len(missing)} model(s):\n{summary}'.encode('utf-8'),
            headers={
                'Title': 'VoucherVault Database Integrity Alert'.encode('utf-8'),
                'Priority': 'urgent',
                'Tags': 'rotating_light',
            },
            timeout=10,
        )
    except Exception:
        _log.warning('Failed to send database integrity alert', exc_info=True)


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
def detect_systemic_misreads_task():
    """
    Weekly sweep: promotes cross-user scan-correction patterns (see
    myapp.scan_learning.detect_systemic_misreads) and, when a security
    ntfy topic is configured, fires a single alert listing what's newly
    promoted this run - a probable systemic OCR misreading is worth an
    admin's attention the same way a login spike is, even though nothing
    here is a security event per se.
    """
    import logging
    import requests as _requests
    from .models import SiteConfiguration
    from .scan_learning import detect_systemic_misreads

    _log = logging.getLogger(__name__)
    newly_promoted = detect_systemic_misreads()
    if not newly_promoted:
        return

    config = SiteConfiguration.load()
    topic = config.security_alert_ntfy_topic.strip()
    if not topic:
        return

    lines = [
        f"- {c.issuer} ({c.item_type}) {c.field}: {c.ai_value!r} -> {c.corrected_value!r} "
        f"(confirmed by {c.confirmed_by_users} users)"
        for c in newly_promoted
    ]
    server = (config.ntfy_default_server or 'https://ntfy.sh').rstrip('/')
    try:
        _requests.post(
            f'{server}/{topic}',
            data=(
                f"{len(newly_promoted)} probable systemic OCR misreading(s) detected across users:\n"
                + '\n'.join(lines)
            ).encode('utf-8'),
            headers={
                'Title': 'VoucherVault Systemic Misread Detected'.encode('utf-8'),
                'Priority': 'default',
                'Tags': 'mag',
            },
            timeout=10,
        )
        _log.warning('Systemic misread alert sent: %d newly promoted pattern(s).', len(newly_promoted))
    except Exception as exc:
        _log.warning('Failed to send systemic misread alert: %s', exc)


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
def run_scheduled_enrichment_if_due(method):
    """
    Fired by the 'biweekly' Celery Beat entry (see myapp/enrichment_scheduling.py)
    every Monday, same as 'weekly' - cron has no native "every 2 weeks"
    expression, so this gates the actual run to even ISO weeks, giving a
    genuine ~14-day cadence instead of firing weekly regardless.
    """
    from django.utils import timezone
    if timezone.now().isocalendar()[1] % 2 != 0:
        return
    run_scheduled_enrichment(method)


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


# queue_item_enrichment gives up after 2 retries without ever creating an
# EnrichmentRunItem row for that item (the row is only written on a
# successful pass) - previously check_and_finalize_run had no cap, so a
# single permanently-failing item left it rescheduling itself every
# minute forever, and the run sat at status='in_progress' indefinitely
# with no error ever surfaced. 30 attempts at the existing 60s cadence
# gives 30 minutes, generously past the ~5 minute happy-path window this
# was originally tuned for.
MAX_FINALIZE_ATTEMPTS = 30
# Halfway through the wait, re-queue whichever items still have no
# EnrichmentRunItem row at all - the one bounded retry this task owes
# them (queue_item_enrichment's own per-task retries only cover
# transient failures within a single attempt, not the item being
# dropped entirely if the worker itself was down).
RETRY_MISSING_ITEMS_AT_ATTEMPT = 15


@shared_task
def check_and_finalize_run(run_id, attempt=0):
    """
    Check if an enrichment run has completed all items and finalize if so.
    Retries every minute until all items are done, up to MAX_FINALIZE_ATTEMPTS,
    at which point the run is marked failed and an admin is alerted rather
    than retrying forever.
    """
    import logging
    from .models import EnrichmentRun, EnrichmentRunItem, Item

    _log = logging.getLogger(__name__)

    try:
        run = EnrichmentRun.objects.get(id=run_id)

        # Count completed run items
        completed_count = EnrichmentRunItem.objects.filter(run=run).count()

        if completed_count >= run.total_items:
            # All items complete, finalize
            finalize_enrichment_run.delay(run_id)
            return

        if attempt == RETRY_MISSING_ITEMS_AT_ATTEMPT:
            missing_items = Item.objects.filter(is_archived=False).exclude(
                enrichment_run_results__run=run
            )
            for item in missing_items:
                queue_item_enrichment.delay(item.id, str(run.id), run.method, False)
            _log.warning(
                "Run %s stalled at %d/%d items after %d minutes - re-queued %d missing item(s)",
                run_id, completed_count, run.total_items,
                attempt, missing_items.count(),
            )

        if attempt >= MAX_FINALIZE_ATTEMPTS:
            _fail_stuck_run(run, completed_count)
            return

        # Not done yet, check again in 1 minute
        from celery import current_app
        current_app.send_task(
            'myapp.tasks.check_and_finalize_run',
            args=[run_id, attempt + 1],
            countdown=60
        )
        _log.debug(f"Run {run_id} has {completed_count}/{run.total_items} items, will recheck")

    except Exception as exc:
        _log.error(f"Error checking finalization for run {run_id}: {exc}", exc_info=True)


def _fail_stuck_run(run, completed_count):
    """
    Shared by check_and_finalize_run's own cap and enrichment_run_watchdog_task.
    Marks the run failed with the true completed/total counts visible in
    `notes` - unlike finalize_enrichment_run, which overwrites total_items
    with just the completed count, silently hiding that some items never
    finished at all.
    """
    import logging
    _log = logging.getLogger(__name__)

    run.status = 'failed'
    run.completed_at = timezone.now()
    run.notes = (
        f"{run.notes}\n" if run.notes else ''
    ) + f"Watchdog: only {completed_count}/{run.total_items} items completed - marked failed."
    run.save(update_fields=['status', 'completed_at', 'notes'])
    _log.error(
        "Enrichment run %s marked failed by watchdog: %d/%d items completed",
        run.id.hex[:8], completed_count, run.total_items,
    )
    _alert_stuck_enrichment_run(run, completed_count)


def _alert_stuck_enrichment_run(run, completed_count):
    import logging
    import requests as _requests
    from .models import SiteConfiguration

    _log = logging.getLogger(__name__)
    config = SiteConfiguration.load()
    topic = config.security_alert_ntfy_topic.strip()
    if not topic:
        return

    server = (config.ntfy_default_server or 'https://ntfy.sh').rstrip('/')
    try:
        _requests.post(
            f'{server}/{topic}',
            data=(
                f'Enrichment run {run.id.hex[:8]} ({run.get_method_display()}) stalled: '
                f'only {completed_count}/{run.total_items} items completed and was marked failed.'
            ).encode('utf-8'),
            headers={
                'Title': 'VoucherVault Enrichment Run Failed'.encode('utf-8'),
                'Priority': 'high',
                'Tags': 'warning',
            },
            timeout=10,
        )
    except Exception:
        _log.warning('Failed to send enrichment run failure alert', exc_info=True)


@shared_task
def enrichment_run_watchdog_task():
    """
    Belt-and-braces sweep for stuck runs, independent of
    check_and_finalize_run's own countdown chain - if a Redis restart (or
    any broker hiccup) drops a scheduled countdown task, that chain can go
    silently dead with nothing left to ever reschedule it, leaving the run
    at status='in_progress' forever with no further sign of life. Anything
    still in_progress after 2 hours (comfortably past the 30-minute cap
    check_and_finalize_run enforces on its own happy path) gets the same
    treatment.
    """
    from datetime import timedelta
    from .models import EnrichmentRun, EnrichmentRunItem

    cutoff = timezone.now() - timedelta(hours=2)
    stuck_runs = EnrichmentRun.objects.filter(status='in_progress', started_at__lt=cutoff)
    for run in stuck_runs:
        completed_count = EnrichmentRunItem.objects.filter(run=run).count()
        _fail_stuck_run(run, completed_count)


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
        elif method == 'learned_correction':
            result = enricher.enrich_from_learned_corrections(item, run.confidence_threshold)

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

        _log.debug(f"Enriched item {item_id} in run {run_id} with {len(result.changes)} changes")

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
        # run.id.hex, not run_id.hex - every real caller passes run_id as a
        # str (see run_scheduled_enrichment/check_and_finalize_run), which
        # has no .hex attribute; run.id is always the actual UUID fetched
        # above regardless of what type run_id arrived as. This previously
        # meant every real finalize silently fell through to the except
        # block below right after this line - the run's own status/stats
        # were already saved and correct, but nothing after this line
        # (including the notify hook below) ever ran.
        _log.info(f"Finalized run {run.id.hex[:8]}: {run.successful_items}/{run.total_items} items, {run.total_changes} changes")

        if run.method == 'learned_correction' and run.status == 'completed':
            from notify.tasks import notify_retroactive_corrections_applied
            notify_retroactive_corrections_applied(run)

    except EnrichmentRun.DoesNotExist:
        _log.error(f"Enrichment run {run_id} not found")
    except Exception as exc:
        _log.error(f"Error finalizing run {run_id}: {exc}", exc_info=True)


@shared_task
def refresh_recommendations_task():
    """
    Daily sweep for Smart Suggestions (myapp/smart_features.py). The
    post_save signal (myapp/signals.py) already keeps recommendations in
    sync the moment an item changes, but conditions like "expires in N
    days" or "unused for 6+ months" shift purely with the passage of time,
    with no save to trigger off - this is what catches those. Also runs
    the inactive-item cleanup as a second line of defense in case the
    signal ever failed silently for a used/archived item.
    """
    import logging
    from django.contrib.auth.models import User
    from .smart_features import generate_all_recommendations, cleanup_recommendations_for_inactive_items

    _log = logging.getLogger(__name__)
    cleanup_recommendations_for_inactive_items()
    for user in User.objects.filter(is_active=True):
        try:
            generate_all_recommendations(user)
        except Exception:
            _log.warning('Failed to refresh recommendations for user %s', user.username, exc_info=True)


@shared_task
def purge_expired_public_shares():
    """
    Deletes ItemPublicShare rows past their expiry. They already 410 on
    access (see ItemPublicShare.is_expired() / public_item_share), so this
    is data hygiene rather than a behavior fix - without it, an expired
    share's view_count/access_pin/failed_pin_attempts history sits in the
    database indefinitely until an owner happens to click Regenerate or
    Revoke. Never-expire links (expires_at=None, i.e.
    SiteConfiguration.share_link_expiry_days=0 at the time they were
    created) are untouched - there's nothing to purge for those.
    """
    from .models import ItemPublicShare

    deleted, _ = ItemPublicShare.objects.filter(
        expires_at__isnull=False, expires_at__lt=timezone.now(),
    ).delete()
    return deleted
