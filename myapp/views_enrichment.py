"""Views for enrichment management and tracking."""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.utils.translation import gettext as _
from django.db.models import Q, Count, Sum, Avg
from decimal import Decimal
from functools import wraps

from .models import EnrichmentConfig, EnrichmentRun, EnrichmentRunItem, Item, ItemEnrichmentLog
from .services.enrichment import ItemEnricher, BulkEnricher


def superuser_required(view_func):
    """
    EnrichmentConfig/EnrichmentRun are app-wide (no user FK) - a scheduled
    run touches every non-archived item across every account, same as the
    admin bulk-enrich action these views are a web-UI counterpart to. Gate
    them the same way the rest of the app gates admin-only pages (see
    trigger_portainer_redeploy/site_settings), rather than leaving them
    reachable by any logged-in user.
    """
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            messages.error(request, _('Only administrators can access enrichment management.'))
            return redirect('show_items')
        return view_func(request, *args, **kwargs)
    return wrapped


@login_required
@superuser_required
def enrichment_dashboard(request):
    """Dashboard showing enrichment status and history."""
    configs = EnrichmentConfig.objects.all()
    recent_runs = EnrichmentRun.objects.order_by('-started_at')[:10]

    # Stats
    total_runs = EnrichmentRun.objects.count()
    completed_runs = EnrichmentRun.objects.filter(status='completed').count()
    pending_runs = EnrichmentRun.objects.filter(status='pending_approval').count()

    # Aggregate stats
    total_changes = EnrichmentRunItem.objects.aggregate(Sum('changes_applied'))['changes_applied__sum'] or 0
    avg_confidence = EnrichmentRun.objects.filter(
        average_confidence__isnull=False
    ).aggregate(Avg('average_confidence'))['average_confidence__avg']

    # Status breakdown for the donut chart - human label + raw status as the
    # JS-side key, since get_status_display() isn't available on a .values() dict.
    status_labels = dict(EnrichmentRun.STATUS_CHOICES)
    status_breakdown = [
        {'status': row['status'], 'label': status_labels.get(row['status'], row['status']), 'count': row['count']}
        for row in EnrichmentRun.objects.values('status').annotate(count=Count('id')).order_by('-count')
    ]

    # Per-method success rate for the method cards' mini progress bar.
    method_stats = {}
    for config in configs:
        runs = EnrichmentRun.objects.filter(method=config.method)
        total = runs.count()
        completed = runs.filter(status='completed').count()
        method_stats[config.method] = {
            'total_runs': total,
            'success_rate': round(100 * completed / total) if total else None,
        }

    context = {
        'configs': configs,
        'recent_runs': recent_runs,
        'method_stats': method_stats,
        'status_breakdown': status_breakdown,
        'stats': {
            'total_runs': total_runs,
            'completed_runs': completed_runs,
            'pending_runs': pending_runs,
            'total_changes_applied': total_changes,
            'avg_confidence': float(avg_confidence) if avg_confidence else 0,
        },
    }
    return render(request, 'enrichment/dashboard.html', context)


@login_required
@superuser_required
def enrichment_config_list(request):
    """List all enrichment configurations."""
    configs = EnrichmentConfig.objects.all().order_by('method')

    # Get run statistics per config
    stats = {}
    for config in configs:
        runs = EnrichmentRun.objects.filter(method=config.method)
        total = runs.count()
        completed = runs.filter(status='completed').count()
        stats[config.method] = {
            'total_runs': total,
            'completed_runs': completed,
            'pending_runs': runs.filter(status='pending_approval').count(),
            'last_run': runs.order_by('-started_at').first(),
            'success_rate': round(100 * completed / total) if total else None,
        }

    context = {
        'configs': configs,
        'stats': stats,
    }
    return render(request, 'enrichment/config_list.html', context)


@login_required
@superuser_required
def enrichment_config_detail(request, method):
    """View and edit enrichment configuration."""
    config = get_object_or_404(EnrichmentConfig, method=method)

    if request.method == 'POST':
        config.enabled = request.POST.get('enabled') == 'on'
        config.schedule = request.POST.get('schedule', 'disabled')
        try:
            config.confidence_threshold = Decimal(request.POST.get('confidence_threshold', '0.6'))
        except:
            config.confidence_threshold = Decimal('0.6')
        config.auto_apply = request.POST.get('auto_apply') == 'on'
        config.save()
        messages.success(request, f'Configuration for {method} updated.')
        return redirect('enrichment_config_list')

    # Get run history
    runs = EnrichmentRun.objects.filter(method=method).order_by('-started_at')[:20]

    context = {
        'config': config,
        'runs': runs,
        'schedule_choices': EnrichmentConfig.SCHEDULE_CHOICES,
    }
    return render(request, 'enrichment/config_detail.html', context)


@login_required
@superuser_required
def enrichment_run_list(request):
    """List all enrichment runs."""
    runs = EnrichmentRun.objects.order_by('-started_at')

    # Filtering
    method = request.GET.get('method')
    status = request.GET.get('status')
    if method:
        runs = runs.filter(method=method)
    if status:
        runs = runs.filter(status=status)

    # Pagination
    page = int(request.GET.get('page', 1))
    per_page = 20
    total = runs.count()
    runs = runs[(page - 1) * per_page : page * per_page]

    context = {
        'runs': runs,
        'filters': {
            'method': method,
            'status': status,
        },
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'total_pages': max(1, -(-total // per_page)),
            'has_prev': page > 1,
            'has_next': (page * per_page) < total,
        },
        'methods': list(EnrichmentConfig.objects.values_list('method', flat=True)),
        'method_choices': dict(EnrichmentConfig.ENRICHMENT_METHODS),
        'statuses': EnrichmentRun.STATUS_CHOICES,
    }
    return render(request, 'enrichment/run_list.html', context)


@login_required
@superuser_required
def enrichment_run_detail(request, run_id):
    """View enrichment run details and results."""
    run = get_object_or_404(EnrichmentRun, id=run_id)
    items = run.items.all().select_related('item')

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'approve' and run.status == 'pending_approval':
            enricher = ItemEnricher(created_by=request.user)
            applied_count = 0

            for run_item in items.filter(success=True):
                item = run_item.item
                changes_data = run_item.preview_data.get('changes', [])
                if not changes_data:
                    continue

                from .services.enrichment import FieldChange
                changes = [
                    FieldChange(
                        field_name=c['field_name'],
                        old_value=c['old_value'],
                        new_value=c['new_value'],
                        confidence_score=c['confidence_score'],
                        reason=c['reason']
                    )
                    for c in changes_data
                ]

                success, error = enricher.apply_changes(item, changes)
                if success:
                    enricher.log_enrichment(item, run.id, changes, run.method)
                    run_item.changes_applied = len(changes)
                    run_item.save(update_fields=['changes_applied'])
                    applied_count += 1

            run.status = 'completed'
            run.approved_by = request.user
            run.approved_at = timezone.now()
            run.completed_at = timezone.now()
            run.save()
            messages.success(request, f'Applied changes to {applied_count} items.')

        elif action == 'reject' and run.status == 'pending_approval':
            run.status = 'rejected'
            run.completed_at = timezone.now()
            reason = request.POST.get('reject_reason', '').strip()
            if reason:
                run.notes = reason
            run.save()
            messages.info(request, 'Run rejected. No changes were applied.')

    context = {
        'run': run,
        'items': items,
        'can_approve': run.status == 'pending_approval',
        'can_reject': run.status == 'pending_approval',
    }
    return render(request, 'enrichment/run_detail.html', context)


@login_required
@superuser_required
@require_http_methods(['POST'])
def enrichment_trigger(request):
    """Manually trigger an enrichment run."""
    method = request.POST.get('method')
    config = get_object_or_404(EnrichmentConfig, method=method, enabled=True)

    # Queue the enrichment task
    from myapp.tasks import run_scheduled_enrichment
    try:
        run_scheduled_enrichment.delay(method)
        messages.success(request, f'Enrichment for {method} has been queued.')
    except Exception as e:
        messages.error(request, f'Failed to queue enrichment: {e}')

    from django.utils.http import url_has_allowed_host_and_scheme
    referer = request.META.get('HTTP_REFERER')
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(referer)
    return redirect('enrichment_dashboard')


@login_required
@require_http_methods(['POST'])
def item_enrich_now(request, item_id):
    """
    On-demand enrichment for a single item, from its detail page rather
    than the admin bulk action or a scheduled run. Anyone who can edit the
    item (owner or a shared-wallet editor - the same check edit_item uses)
    can run this; it's scoped to that one item, not the app-wide admin
    tooling, so it doesn't need superuser_required.

    Always previews first (dry_run, the default) and only writes changes
    when the client explicitly confirms with apply=1 - OCR/merchant-lookup
    guesses are wrong often enough that silently overwriting item fields
    on a single click would be a bad default.
    """
    from .views import _check_item_edit_permission

    item = get_object_or_404(Item, id=item_id)
    if not _check_item_edit_permission(item, request.user):
        return JsonResponse({'ok': False, 'error': str(_('You do not have permission to edit this item.'))}, status=403)

    apply_changes = request.POST.get('apply') == '1'
    enricher = BulkEnricher(created_by=request.user)
    results = enricher.enrich_selected_items(
        [item.id], enrichment_mode='all', confidence_threshold=Decimal('0.5'), dry_run=not apply_changes
    )
    result = results.get(item.id)
    if result is None:
        return JsonResponse({'ok': False, 'error': str(_('Enrichment failed to run.'))}, status=500)

    changes = [
        {
            'field': c.field_name,
            'old_value': c.old_value,
            'new_value': c.new_value,
            'confidence': float(c.confidence_score),
            'reason': c.reason,
        }
        for c in result.changes
    ]
    flags = [
        {
            'field': f.field_name,
            'value': f.current_value,
            'message': f.message,
            'severity': f.severity,
        }
        for f in result.flags
    ]
    return JsonResponse({
        'ok': True,
        'applied': apply_changes,
        'success': result.success,
        'error_message': result.error_message,
        'changes': changes,
        'flags': flags,
    })


@login_required
def item_enrichment_history(request, item_id):
    """
    View enrichment history for a specific item. Merges two sources into
    one chronological timeline: EnrichmentRunItem rows from admin
    bulk/scheduled runs, and ItemEnrichmentLog rows from the per-item
    on-demand "Re-scan" (item_enrich_now) - the latter never created a
    run record, so it used to be invisible on the item's own history page.
    """
    item = get_object_or_404(Item, id=item_id, user=request.user)

    run_items = EnrichmentRunItem.objects.filter(item=item).select_related('run').order_by('-run__started_at')

    timeline = []
    for run_item in run_items:
        preview_changes = (run_item.preview_data or {}).get('changes', [])
        timeline.append({
            'timestamp': run_item.run.started_at,
            'source': run_item.run.get_method_display(),
            'success': run_item.success,
            'error_message': run_item.error_message,
            'changes': preview_changes,
            'flags': [],
        })

    logs = list(ItemEnrichmentLog.objects.filter(item=item).order_by('created_at'))
    logs_by_run = {}
    for log in logs:
        logs_by_run.setdefault(log.enrichment_run_id, []).append(log)

    for run_id, group in logs_by_run.items():
        real_changes = [g for g in group if g.enrichment_type != 'flagged']
        flags = [g for g in group if g.enrichment_type == 'flagged']
        if not real_changes and not flags:
            continue
        types_present = {g.enrichment_type for g in real_changes}
        if len(types_present) == 1:
            source = group[0].get_enrichment_type_display() if real_changes else _('Review flags')
        else:
            source = _('Re-scan')
        timeline.append({
            'timestamp': group[0].created_at,
            'source': source,
            'success': True,
            'error_message': None,
            'changes': [
                {
                    'field_name': c.field_name, 'old_value': c.old_value, 'new_value': c.new_value,
                    'confidence_score': c.confidence_score, 'reason': c.reason,
                }
                for c in real_changes
            ],
            'flags': [
                {'field_name': f.field_name, 'value': f.old_value, 'message': f.reason}
                for f in flags
            ],
        })

    timeline.sort(key=lambda entry: entry['timestamp'], reverse=True)

    context = {
        'item': item,
        'timeline': timeline,
    }
    return render(request, 'enrichment/item_history.html', context)
