"""Views for enrichment management and tracking."""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.db.models import Q, Count, Sum, Avg
from decimal import Decimal

from .models import EnrichmentConfig, EnrichmentRun, EnrichmentRunItem, Item
from .services.enrichment import ItemEnricher


@login_required
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

    context = {
        'configs': configs,
        'recent_runs': recent_runs,
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
def enrichment_config_list(request):
    """List all enrichment configurations."""
    configs = EnrichmentConfig.objects.all().order_by('method')

    # Get run statistics per config
    stats = {}
    for config in configs:
        runs = EnrichmentRun.objects.filter(method=config.method)
        stats[config.method] = {
            'total_runs': runs.count(),
            'completed_runs': runs.filter(status='completed').count(),
            'pending_runs': runs.filter(status='pending_approval').count(),
            'last_run': runs.order_by('-started_at').first(),
        }

    context = {
        'configs': configs,
        'stats': stats,
    }
    return render(request, 'enrichment/config_list.html', context)


@login_required
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
            'has_prev': page > 1,
            'has_next': (page * per_page) < total,
        },
        'methods': [c.method for c in EnrichmentConfig.objects.all()],
        'statuses': EnrichmentRun.STATUS_CHOICES,
    }
    return render(request, 'enrichment/run_list.html', context)


@login_required
def enrichment_run_detail(request, run_id):
    """View enrichment run details and results."""
    run = get_object_or_404(EnrichmentRun, id=run_id)
    items = run.enrichmentrunitem_set.all().select_related('item')

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
            run.completed_at = timezone.now()
            run.save()
            messages.success(request, f'Applied changes to {applied_count} items.')

        elif action == 'reject' and run.status == 'pending_approval':
            run.status = 'rejected'
            run.completed_at = timezone.now()
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

    return redirect('enrichment_dashboard')


@login_required
def item_enrichment_history(request, item_id):
    """View enrichment history for a specific item."""
    item = get_object_or_404(Item, id=item_id, user=request.user)

    # Get all runs that touched this item
    run_items = EnrichmentRunItem.objects.filter(item=item).select_related('run').order_by('-run__started_at')

    context = {
        'item': item,
        'run_items': run_items,
    }
    return render(request, 'enrichment/item_history.html', context)
