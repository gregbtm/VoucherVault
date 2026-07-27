from django.contrib import admin
from .models import *
from django.urls import path
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse
from decimal import Decimal
from .services.enrichment import BulkEnricher

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

admin.site.register(Item, ItemAdmin)
admin.site.register(Transaction)
admin.site.register(AppSettings, AppSettingsAdmin)
admin.site.register(UserPreference, UserPreferenceAdmin)
admin.site.register(Wallet, WalletAdmin)
admin.site.register(Tag, TagAdmin)
admin.site.register(Document, DocumentAdmin)
