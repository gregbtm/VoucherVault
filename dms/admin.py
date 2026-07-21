from django.contrib import admin
from .models import DMSProvider, DMSSyncLog


@admin.register(DMSProvider)
class DMSProviderAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'provider', 'base_url', 'status', 'enabled', 'last_checked']
    list_filter = ['provider', 'status', 'enabled']
    search_fields = ['name', 'user__username', 'base_url']
    readonly_fields = ['last_checked', 'status', 'status_message', 'created_at', 'updated_at']


@admin.register(DMSSyncLog)
class DMSSyncLogAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'provider', 'direction', 'status', 'dms_document_title', 'item']
    list_filter = ['direction', 'status', 'provider']
    search_fields = ['dms_document_id', 'dms_document_title', 'detail']
    readonly_fields = ['created_at']
