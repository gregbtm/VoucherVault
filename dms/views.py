import json
import logging
import os
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from myapp.models import Document, Item
from .clients import get_client
from .forms import DMSProviderForm
from .models import DMSProvider, DMSSyncLog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider management
# ---------------------------------------------------------------------------

@login_required
def providers(request):
    provider_list = list(DMSProvider.objects.filter(user=request.user))

    # Attach per-provider sync stats directly to each provider object
    stats_qs = (
        DMSSyncLog.objects
        .filter(provider__user=request.user)
        .values('provider_id')
        .annotate(
            push_ok=Count('id', filter=Q(direction='push', status='ok')),
            pull_ok=Count('id', filter=Q(direction='pull', status='ok')),
            errors=Count('id', filter=Q(status='error')),
        )
    )
    stats_by_id = {str(row['provider_id']): row for row in stats_qs}
    for p in provider_list:
        s = stats_by_id.get(str(p.id), {})
        p.stat_push_ok = s.get('push_ok', 0)
        p.stat_pull_ok = s.get('pull_ok', 0)
        p.stat_errors = s.get('errors', 0)

    return render(request, 'dms/providers.html', {'providers': provider_list})


@login_required
def add_provider(request):
    if request.method == 'POST':
        form = DMSProviderForm(request.POST)
        if form.is_valid():
            p = form.save(commit=False)
            p.user = request.user
            p.save()
            messages.success(request, f'Provider "{p.name}" added.')
            return redirect('dms:providers')
    else:
        form = DMSProviderForm()
    return render(request, 'dms/provider_form.html', {'form': form, 'title': 'Add DMS Provider'})


@login_required
def edit_provider(request, provider_id):
    provider = get_object_or_404(DMSProvider, id=provider_id, user=request.user)
    if request.method == 'POST':
        form = DMSProviderForm(request.POST, instance=provider)
        if form.is_valid():
            form.save()
            messages.success(request, f'Provider "{provider.name}" updated.')
            return redirect('dms:providers')
    else:
        form = DMSProviderForm(instance=provider)
    return render(request, 'dms/provider_form.html', {
        'form': form,
        'provider': provider,
        'title': f'Edit {provider.name}',
    })


@login_required
@require_POST
def delete_provider(request, provider_id):
    provider = get_object_or_404(DMSProvider, id=provider_id, user=request.user)
    name = provider.name
    provider.delete()
    messages.success(request, f'Provider "{name}" deleted.')
    return redirect('dms:providers')


# ---------------------------------------------------------------------------
# AJAX: test connection + config polling
# ---------------------------------------------------------------------------

@login_required
@require_POST
def test_connection(request, provider_id):
    """AJAX endpoint — test connectivity and auth for a saved provider."""
    provider = get_object_or_404(DMSProvider, id=provider_id, user=request.user)
    client = get_client(provider)
    result = client.test_connection()

    provider.last_checked = timezone.now()
    provider.status = DMSProvider.STATUS_OK if result.get('ok') else DMSProvider.STATUS_ERROR
    provider.status_message = result.get('message', '')
    provider.save(update_fields=['last_checked', 'status', 'status_message'])

    return JsonResponse({
        'ok': result.get('ok', False),
        'message': result.get('message', ''),
        'version': result.get('version', ''),
        'last_checked': provider.last_checked.isoformat(),
        'status': provider.status,
        'status_badge': provider.status_badge_class,
    })


@login_required
def poll_config(request, provider_id):
    """
    AJAX endpoint — return live config data from the DMS for the settings form:
    available tags, correspondents, and server info.  Used to populate select
    dropdowns without a full page reload.
    """
    provider = get_object_or_404(DMSProvider, id=provider_id, user=request.user)
    client = get_client(provider)

    tags = []
    correspondents = []
    server_info = {}
    error = None

    try:
        tags = client.list_tags()
        correspondents = client.list_correspondents()
        server_info = client.get_server_info()
    except Exception as exc:
        error = str(exc)
        logger.warning('DMS poll_config error for provider %s: %s', provider_id, exc)

    return JsonResponse({
        'tags': tags,
        'correspondents': correspondents,
        'server_info': server_info,
        'error': error,
    })


# ---------------------------------------------------------------------------
# AJAX: document browser
# ---------------------------------------------------------------------------

@login_required
def browse(request, provider_id):
    """
    AJAX document browser.  Returns a JSON page of documents from the DMS.
    Parameters: q (search), page, page_size, tag, correspondent.
    """
    provider = get_object_or_404(DMSProvider, id=provider_id, user=request.user)
    client = get_client(provider)

    query = request.GET.get('q', '')
    try:
        page = max(1, int(request.GET.get('page', 1)))
        page_size = min(50, max(5, int(request.GET.get('page_size', 20))))
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'page and page_size must be integers'}, status=400)
    tag = request.GET.get('tag', provider.pull_tag)
    correspondent = request.GET.get('correspondent', provider.pull_correspondent)

    try:
        result = client.browse(query=query, page=page, page_size=page_size, tag=tag, correspondent=correspondent)
        return JsonResponse({
            'ok': True,
            'documents': [d.as_dict() for d in result.documents],
            'total_count': result.total_count,
            'page': result.page,
            'page_size': result.page_size,
            'has_next': result.has_next,
            'has_prev': result.has_prev,
        })
    except Exception as exc:
        logger.error('DMS browse error provider=%s: %s', provider_id, exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


# ---------------------------------------------------------------------------
# Push: VoucherVault document → DMS
# ---------------------------------------------------------------------------

@login_required
@require_POST
def push_document(request, provider_id, document_id):
    """
    Push a VoucherVault document attachment to the specified DMS provider.
    """
    provider = get_object_or_404(DMSProvider, id=provider_id, user=request.user)
    doc = get_object_or_404(Document, id=document_id, item__user=request.user)

    client = get_client(provider)
    try:
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

        log = DMSSyncLog.objects.create(
            provider=provider,
            direction=DMSSyncLog.DIRECTION_PUSH,
            status=DMSSyncLog.STATUS_OK,
            item=doc.item,
            document=doc,
            dms_document_id=str(dms_id),
            dms_document_title=title,
            detail=f'Uploaded to {provider.name} (remote id: {dms_id})',
        )
        return JsonResponse({'ok': True, 'dms_id': str(dms_id), 'log_id': log.id})
    except Exception as exc:
        DMSSyncLog.objects.create(
            provider=provider,
            direction=DMSSyncLog.DIRECTION_PUSH,
            status=DMSSyncLog.STATUS_ERROR,
            item=doc.item,
            document=doc,
            detail=str(exc),
        )
        logger.error('DMS push error provider=%s doc=%s: %s', provider_id, document_id, exc)
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


@login_required
@require_POST
def push_item_file(request, provider_id, item_uuid):
    """Push the primary item scan (Item.file) to the DMS."""
    provider = get_object_or_404(DMSProvider, id=provider_id, user=request.user)
    item = get_object_or_404(Item, id=item_uuid, user=request.user)

    if not item.file:
        return JsonResponse({'ok': False, 'error': 'This item has no file attached.'}, status=400)

    client = get_client(provider)
    try:
        content = item.file.read()
        filename = item.file.name.rsplit('/', 1)[-1]
        title = f'{item.name} (scan)'

        dms_id = client.upload_document(
            filename=filename,
            content=content,
            title=title,
            tags=['vouchervault'],
            correspondent=item.issuer or '',
        )

        log = DMSSyncLog.objects.create(
            provider=provider,
            direction=DMSSyncLog.DIRECTION_PUSH,
            status=DMSSyncLog.STATUS_OK,
            item=item,
            dms_document_id=str(dms_id),
            dms_document_title=title,
            detail=f'Uploaded primary file to {provider.name}',
        )
        return JsonResponse({'ok': True, 'dms_id': str(dms_id), 'log_id': log.id})
    except Exception as exc:
        DMSSyncLog.objects.create(
            provider=provider,
            direction=DMSSyncLog.DIRECTION_PUSH,
            status=DMSSyncLog.STATUS_ERROR,
            item=item,
            detail=str(exc),
        )
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


# ---------------------------------------------------------------------------
# Pull: DMS document → VoucherVault item
# ---------------------------------------------------------------------------

@login_required
@require_POST
def pull_document(request, provider_id):
    """
    Pull a single DMS document by its ID and attach it to a VoucherVault item.
    POST body (JSON): {'dms_doc_id': '...', 'item_uuid': '...' (optional)}
    If item_uuid is not provided, a new Item is created.
    """
    provider = get_object_or_404(DMSProvider, id=provider_id, user=request.user)
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, AttributeError):
        body = {}

    dms_doc_id = body.get('dms_doc_id') or request.POST.get('dms_doc_id', '')
    item_uuid = body.get('item_uuid') or request.POST.get('item_uuid', '')

    if not dms_doc_id:
        return JsonResponse({'ok': False, 'error': 'dms_doc_id is required'}, status=400)

    client = get_client(provider)
    try:
        dms_doc = client.get_document(dms_doc_id)
        raw_bytes = client.download_document(dms_doc_id)
    except Exception as exc:
        return JsonResponse({'ok': False, 'error': f'Could not fetch document: {exc}'}, status=502)

    try:
        if item_uuid:
            item = get_object_or_404(Item, id=item_uuid, user=request.user)
        else:
            item = Item.objects.create(
                user=request.user,
                name=dms_doc.title or f'DMS import {dms_doc_id}',
                type='voucher',
                issuer='',
                redeem_code='',
                value='0.00',
                source='api',
                expiry_date=date(9999, 12, 31),
            )

        ext = os.path.splitext(dms_doc.original_filename or 'document.pdf')[1] or '.pdf'
        safe_title = ''.join(c for c in (dms_doc.title or '') if c.isalnum() or c in ' -_')[:60]
        filename = f'{safe_title or dms_doc_id}{ext}'

        doc = Document(item=item, extracted_text=(dms_doc.content or '')[:10000])
        doc._dms_pulled = True
        doc.file.save(filename, ContentFile(raw_bytes), save=True)

        log = DMSSyncLog.objects.create(
            provider=provider,
            direction=DMSSyncLog.DIRECTION_PULL,
            status=DMSSyncLog.STATUS_OK,
            item=item,
            document=doc,
            dms_document_id=dms_doc_id,
            dms_document_title=dms_doc.title,
            detail=f'Pulled from {provider.name}',
        )
        return JsonResponse({
            'ok': True,
            'item_uuid': str(item.id),
            'document_id': doc.id,
            'log_id': log.id,
        })
    except Exception as exc:
        logger.error('DMS pull error provider=%s doc=%s: %s', provider_id, dms_doc_id, exc)
        DMSSyncLog.objects.create(
            provider=provider,
            direction=DMSSyncLog.DIRECTION_PULL,
            status=DMSSyncLog.STATUS_ERROR,
            dms_document_id=dms_doc_id,
            detail=str(exc),
        )
        return JsonResponse({'ok': False, 'error': str(exc)}, status=500)


# ---------------------------------------------------------------------------
# Sync logs
# ---------------------------------------------------------------------------

@login_required
def sync_logs(request):
    provider_id = request.GET.get('provider')
    direction = request.GET.get('direction')
    status = request.GET.get('status')

    base_qs = DMSSyncLog.objects.filter(provider__user=request.user)

    # Aggregate stats across all logs (unfiltered) for the summary row
    all_stats = base_qs.aggregate(
        total_push_ok=Count('id', filter=Q(direction='push', status='ok')),
        total_pull_ok=Count('id', filter=Q(direction='pull', status='ok')),
        total_errors=Count('id', filter=Q(status='error')),
        last_activity=Max('created_at'),
    )

    qs = base_qs.select_related('provider', 'item', 'document')

    if provider_id:
        qs = qs.filter(provider_id=provider_id)
    if direction:
        qs = qs.filter(direction=direction)
    if status:
        qs = qs.filter(status=status)

    qs = qs[:200]
    provider_list = DMSProvider.objects.filter(user=request.user)

    return render(request, 'dms/sync_logs.html', {
        'logs': qs,
        'providers': provider_list,
        'filter_provider': provider_id,
        'filter_direction': direction,
        'filter_status': status,
        'stats': all_stats,
    })
