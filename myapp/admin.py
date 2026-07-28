from django.contrib import admin
from .models import *
from django.urls import path
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.utils import timezone
from decimal import Decimal
from .services.enrichment import BulkEnricher, ItemEnricher, FieldChange

class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'apprise_urls')

class DocumentInline(admin.TabularInline):
    model = Document
    extra = 0
    readonly_fields = ('uploaded_at',)


class ItemAdmin(admin.ModelAdmin):
    # Specify the fields to display in the list view
    list_display = ('name', 'type', 'issuer', 'issue_date', 'expiry_date', 'value', 'is_used', 'is_archived', 'user')

    # Specify the fields to search by
    search_fields = ('name', 'type', 'issuer', 'user__username', 'card_number')

    # Specify the filters to use in the list view
    list_filter = ('type', 'is_used', 'is_archived', 'issue_date', 'expiry_date', 'user')

    inlines = [DocumentInline]
    actions = ['enrich_selected_items']

    def enrich_selected_items(self, request, queryset):
        """Bulk action to enrich selected items."""
        selected_ids = list(queryset.values_list('id', flat=True))

        if request.method == 'POST':
            enrichment_mode = request.POST.get('enrichment_mode', 'ocr')
            confidence_threshold = Decimal(request.POST.get('confidence_threshold', '0.5'))
            dry_run = request.POST.get('dry_run') == 'on'

            enricher = BulkEnricher(created_by=request.user)
            results = enricher.enrich_selected_items(
                selected_ids,
                enrichment_mode=enrichment_mode,
                confidence_threshold=confidence_threshold,
                dry_run=dry_run
            )

            summary = enricher.get_enrichment_summary(results)

            if dry_run:
                messages.info(request, f"Dry run: {summary['successful_enrichments']} items would be enriched with {summary['total_changes']} total changes")
            else:
                messages.success(request, f"Enriched {summary['successful_enrichments']} items with {summary['total_changes']} total changes")

            if summary['errors']:
                for error in summary['errors'][:5]:
                    messages.warning(request, f"Error: {error}")

            return redirect('admin:myapp_item_changelist')

        # Show enrichment dialog
        context = {
            'selected_ids': selected_ids,
            'selected_count': len(selected_ids),
            'items_with_docs': queryset.filter(document__isnull=False).distinct().count(),
        }
        return render(request, 'admin/enrich_dialog.html', context)

    enrich_selected_items.short_description = _("Enrich selected items (OCR, validation)")

    def get_urls(self):
        """Extend the admin URL patterns."""
        urls = super().get_urls()
        custom_urls = [
            path('enrich-preview/', self.admin_site.admin_view(self.enrich_preview_view), name='item_enrich_preview'),
        ]
        return custom_urls + urls

    def enrich_preview_view(self, request):
        """Preview enrichment changes for selected items."""
        if request.method == 'POST':
            selected_ids = request.POST.getlist('selected_ids')
            enrichment_mode = request.POST.get('enrichment_mode', 'ocr')
            confidence_threshold = Decimal(request.POST.get('confidence_threshold', '0.5'))

            enricher = BulkEnricher(created_by=request.user)
            results = enricher.enrich_selected_items(
                [int(id) for id in selected_ids],
                enrichment_mode=enrichment_mode,
                confidence_threshold=confidence_threshold,
                dry_run=True
            )

            # Build preview data
            preview_changes = []
            for item_id, result in results.items():
                if result.changes:
                    preview_changes.append({
                        'item_id': item_id,
                        'item': Item.objects.get(id=item_id),
                        'changes': [
                            {
                                'field': c.field_name,
                                'old_value': c.old_value,
                                'new_value': c.new_value,
                                'confidence': float(c.confidence_score),
                                'reason': c.reason,
                            }
                            for c in result.changes
                        ]
                    })

            context = {
                'preview_changes': preview_changes,
                'selected_ids': selected_ids,
                'total_changes': sum(len(item['changes']) for item in preview_changes),
                'enrichment_mode': enrichment_mode,
                'confidence_threshold': float(confidence_threshold),
            }
            return render(request, 'admin/enrich_preview.html', context)


class AppSettingsAdmin(admin.ModelAdmin):
    list_display = ('api_token', 'updated_at', 'regenerate_token_button')
    readonly_fields = ('api_token', 'updated_at', 'regenerate_token_button')  # Include the button here

    def has_add_permission(self, request):
        # Check if there is already an existing object in the model
        if self.model.objects.count() >= 1:
            return False  # Disallow adding a new object
        else:
            return True   # Allow adding a new object    

    def regenerate_token_button(self, obj):
        """Add a button to regenerate the API token."""
        if obj.pk:  # Only display the button for existing objects
            url = reverse('admin:regenerate_api_token', args=[obj.pk])
            return format_html(
                '<a class="button" href="{}">Regenerate API Token</a>', url
            )
        return "Save this object before regenerating the token."
    regenerate_token_button.short_description = "Actions"
    regenerate_token_button.allow_tags = True

    def get_urls(self):
        """Extend the admin URL patterns to include custom actions."""
        urls = super().get_urls()
        custom_urls = [
            path('<int:pk>/regenerate-token/', self.admin_site.admin_view(self.regenerate_token), name='regenerate_api_token'),
        ]
        return custom_urls + urls

    def regenerate_token(self, request, pk):
        """Regenerate the API token for the selected AppSettings instance."""
        from django.http import HttpResponseRedirect
        from django.contrib import messages
        try:
            app_settings = AppSettings.objects.get(pk=pk)
            app_settings.regenerate_api_token()
            messages.success(request, "API token regenerated successfully!")
        except AppSettings.DoesNotExist:
            messages.error(request, "AppSettings instance not found.")
        return HttpResponseRedirect(reverse('admin:myapp_appsettings_changelist'))

class UserPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user', 'show_issue_date', 'show_expiry_date', 'show_value', 'show_description')
    list_filter = ('show_issue_date', 'show_expiry_date', 'show_value', 'show_description')
    search_fields = ('user__username',)

class WalletAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'created_at')
    search_fields = ('name', 'user__username')
    list_filter = ('user',)
    filter_horizontal = ('shared_with',)


class DocumentAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'item', 'uploaded_at')
    search_fields = ('item__name', 'item__user__username')
    list_filter = ('uploaded_at',)

class TagAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'color')
    search_fields = ('name', 'user__username')
    list_filter = ('user',)

class EnrichmentConfigAdmin(admin.ModelAdmin):
    """Admin interface for enrichment method configuration."""
    list_display = ('get_method_display', 'enabled', 'get_schedule_display', 'confidence_threshold', 'auto_apply', 'updated_at')
    list_editable = ('enabled', 'confidence_threshold', 'auto_apply')
    list_filter = ('enabled', 'schedule')
    fieldsets = (
        (_('Method'), {
            'fields': ('method',),
            'description': _('Which enrichment method this configuration applies to.')
        }),
        (_('Schedule'), {
            'fields': ('enabled', 'schedule'),
            'description': _('Enable/disable this method and choose how often to run it.')
        }),
        (_('Settings'), {
            'fields': ('confidence_threshold', 'auto_apply'),
            'description': _('Confidence threshold and whether to auto-apply changes without admin review.')
        }),
    )
    readonly_fields = ('updated_at',)

    def get_schedule_display(self, obj):
        return obj.get_schedule_display()
    get_schedule_display.short_description = _('Schedule')


class EnrichmentRunItemInline(admin.TabularInline):
    """Inline display of enrichment results for an item."""
    model = EnrichmentRunItem
    extra = 0
    readonly_fields = ('item', 'success', 'changes_proposed', 'changes_applied', 'error_message', 'preview_changes')
    can_delete = False
    fields = ('item', 'success', 'changes_proposed', 'changes_applied', 'preview_changes', 'error_message')

    def preview_changes(self, obj):
        """Display proposed changes from JSON preview."""
        if not obj.preview_data or 'changes' not in obj.preview_data:
            return '-'
        changes = obj.preview_data['changes']
        if not changes:
            return 'No changes'
        html = '<ul style="margin: 0;">'
        for change in changes:
            confidence = change.get('confidence_score', 0)
            confidence_color = '#28a745' if confidence >= 0.8 else '#ffc107' if confidence >= 0.6 else '#dc3545'
            html += f'<li><strong>{change["field_name"]}</strong>: {change.get("old_value", "null")} → {change.get("new_value", "null")} '
            html += f'<span style="color: {confidence_color}; font-weight: bold;">({confidence:.1%})</span></li>'
        html += '</ul>'
        return format_html(html)
    preview_changes.short_description = _('Preview')


class EnrichmentFieldPreferenceAdmin(admin.ModelAdmin):
    """
    Admin interface for per-user field opt-outs. The model previously had
    no admin registration and no other UI, so nothing could ever create a
    row here - the pipeline-side exclusion check that reads this table had
    no way to ever actually fire. Registering it makes it usable end to
    end without building a dedicated self-service settings page yet.
    """
    list_display = ('user', 'get_method_display', 'get_field_name_display', 'reason', 'created_at')
    list_filter = ('method', 'field_name')
    search_fields = ('user__username',)
    autocomplete_fields = ('user',)


class EnrichmentRunAdmin(admin.ModelAdmin):
    """Admin interface for enrichment run tracking and approval."""
    list_display = ('run_id', 'get_method_display', 'get_status_display', 'total_items', 'successful_items', 'total_changes', 'average_confidence', 'started_at', 'actions_buttons')
    list_filter = ('status', 'method', 'started_at')
    search_fields = ('id',)
    readonly_fields = ('id', 'started_at', 'completed_at', 'approved_at', 'approved_by', 'stats_summary')
    inlines = [EnrichmentRunItemInline]
    actions = ['approve_runs', 'reject_runs']

    fieldsets = (
        (_('Run Information'), {
            'fields': ('id', 'method', 'status', 'started_at', 'completed_at'),
            'description': _('Basic information about this enrichment run.')
        }),
        (_('Results'), {
            'fields': ('total_items', 'successful_items', 'total_changes', 'average_confidence', 'stats_summary'),
            'description': _('Summary of enrichment results.')
        }),
        (_('Approval'), {
            'fields': ('approved_by', 'approved_at', 'confidence_threshold'),
            'description': _('Approval workflow tracking.')
        }),
        (_('Notes'), {
            'fields': ('notes',),
            'description': _('Admin notes about this run.')
        }),
    )

    def run_id(self, obj):
        """Display truncated run ID."""
        return obj.id.hex[:8]
    run_id.short_description = _('Run ID')

    def stats_summary(self, obj):
        """Display formatted summary of run stats."""
        return format_html(
            '<strong>Success Rate:</strong> {}/{} ({:.1%})<br>'
            '<strong>Total Changes:</strong> {}<br>'
            '<strong>Avg Confidence:</strong> {:.1%}',
            obj.successful_items,
            obj.total_items,
            obj.successful_items / max(obj.total_items, 1),
            obj.total_changes,
            obj.average_confidence
        )
    stats_summary.short_description = _('Summary')

    def actions_buttons(self, obj):
        """Display action buttons for approval/rejection."""
        if obj.status == 'pending_approval':
            approve_url = reverse('admin:approve_enrichment_run', args=[obj.id])
            reject_url = reverse('admin:reject_enrichment_run', args=[obj.id])
            return format_html(
                '<a class="button" href="{approve_url}">Approve</a>&nbsp;'
                '<a class="button" style="background-color: #dc3545;" href="{reject_url}">Reject</a>',
                approve_url=approve_url,
                reject_url=reject_url,
            )
        return obj.get_status_display()
    actions_buttons.short_description = _('Actions')

    def approve_runs(self, request, queryset):
        """Bulk action to approve runs."""
        updated = 0
        for run in queryset.filter(status='pending_approval'):
            run.status = 'approved'
            run.approved_by = request.user
            run.approved_at = timezone.now()
            run.save()

            # Apply all changes in the run's items
            from .services.enrichment import ItemEnricher
            enricher = ItemEnricher(created_by=request.user)
            for run_item in run.items.all():
                if run_item.preview_data and 'changes' in run_item.preview_data:
                    # Reconstruct FieldChange objects from preview
                    from .services.enrichment import FieldChange
                    changes = [
                        FieldChange(
                            field_name=c['field_name'],
                            old_value=c['old_value'],
                            new_value=c['new_value'],
                            confidence_score=Decimal(str(c['confidence_score'])),
                            reason=c['reason'],
                        )
                        for c in run_item.preview_data['changes']
                    ]
                    success, error = enricher.apply_changes(run_item.item, changes)
                    if success:
                        enricher.log_enrichment(run_item.item, str(run.id), changes, run.method)
                        run_item.changes_applied = len(changes)
                        run_item.success = True
                        run_item.save()
                    updated += 1

        messages.success(request, f"Approved {updated} enrichment runs.")
    approve_runs.short_description = _('Approve selected enrichment runs')

    def reject_runs(self, request, queryset):
        """Bulk action to reject runs."""
        updated = queryset.filter(status='pending_approval').update(
            status='rejected',
            approved_by=request.user,
            approved_at=timezone.now(),
        )
        messages.success(request, f"Rejected {updated} enrichment runs.")
    reject_runs.short_description = _('Reject selected enrichment runs')

    def get_urls(self):
        """Add custom approval/rejection endpoints."""
        urls = super().get_urls()
        custom_urls = [
            path('<uuid:run_id>/approve/', self.admin_site.admin_view(self.approve_run_view), name='approve_enrichment_run'),
            path('<uuid:run_id>/reject/', self.admin_site.admin_view(self.reject_run_view), name='reject_enrichment_run'),
        ]
        return custom_urls + urls

    def approve_run_view(self, request, run_id):
        """View to approve a single enrichment run."""
        from django.http import HttpResponseRedirect
        try:
            run = EnrichmentRun.objects.get(id=run_id)
            if run.status == 'pending_approval':
                run.status = 'approved'
                run.approved_by = request.user
                run.approved_at = timezone.now()
                run.save()

                # Apply all changes
                from .services.enrichment import ItemEnricher, FieldChange
                enricher = ItemEnricher(created_by=request.user)
                for run_item in run.items.all():
                    if run_item.preview_data and 'changes' in run_item.preview_data:
                        changes = [
                            FieldChange(
                                field_name=c['field_name'],
                                old_value=c['old_value'],
                                new_value=c['new_value'],
                                confidence_score=Decimal(str(c['confidence_score'])),
                                reason=c['reason'],
                            )
                            for c in run_item.preview_data['changes']
                        ]
                        success, error = enricher.apply_changes(run_item.item, changes)
                        if success:
                            enricher.log_enrichment(run_item.item, str(run.id), changes, run.method)
                            run_item.changes_applied = len(changes)
                            run_item.success = True
                            run_item.save()

                messages.success(request, f"Approved enrichment run {run_id.hex[:8]} and applied changes.")
            else:
                messages.warning(request, f"Run is already {run.get_status_display().lower()}.")
        except EnrichmentRun.DoesNotExist:
            messages.error(request, "Enrichment run not found.")

        return HttpResponseRedirect(reverse('admin:myapp_enrichmentrun_change', args=[run_id]))

    def reject_run_view(self, request, run_id):
        """View to reject a single enrichment run."""
        from django.http import HttpResponseRedirect
        try:
            run = EnrichmentRun.objects.get(id=run_id)
            if run.status == 'pending_approval':
                run.status = 'rejected'
                run.approved_by = request.user
                run.approved_at = timezone.now()
                run.save()
                messages.success(request, f"Rejected enrichment run {run_id.hex[:8]}.")
            else:
                messages.warning(request, f"Run is already {run.get_status_display().lower()}.")
        except EnrichmentRun.DoesNotExist:
            messages.error(request, "Enrichment run not found.")

        return HttpResponseRedirect(reverse('admin:myapp_enrichmentrun_change', args=[run_id]))


admin.site.register(Item, ItemAdmin)
admin.site.register(Transaction)
admin.site.register(AppSettings, AppSettingsAdmin)
admin.site.register(UserPreference, UserPreferenceAdmin)
admin.site.register(Wallet, WalletAdmin)
admin.site.register(Tag, TagAdmin)
admin.site.register(Document, DocumentAdmin)
admin.site.register(EnrichmentConfig, EnrichmentConfigAdmin)
admin.site.register(EnrichmentRun, EnrichmentRunAdmin)
admin.site.register(EnrichmentFieldPreference, EnrichmentFieldPreferenceAdmin)
