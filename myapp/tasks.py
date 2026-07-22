# myapp/tasks.py
from celery import shared_task
from django.core.management import call_command
from django.utils import timezone

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
