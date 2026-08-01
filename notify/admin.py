from django.contrib import admin

from .models import NotificationLog, NotificationRule, WebhookRetry


class NotificationRuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'backend', 'enabled')
    list_filter = ('backend', 'enabled')
    search_fields = ('name', 'user__username')


class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'user', 'item', 'rule', 'success', 'sent_at')
    list_filter = ('event_type', 'success')
    search_fields = ('user__username', 'item__name')
    readonly_fields = ('user', 'rule', 'item', 'event_type', 'sent_at', 'success', 'detail')


class WebhookRetryAdmin(admin.ModelAdmin):
    list_display = ('rule', 'event_type', 'attempt', 'next_retry_at', 'created_at')
    list_filter = ('event_type',)
    search_fields = ('rule__name', 'rule__user__username')
    readonly_fields = ('rule', 'item', 'event_type', 'payload', 'attempt', 'last_error', 'created_at', 'updated_at')


admin.site.register(NotificationRule, NotificationRuleAdmin)
admin.site.register(NotificationLog, NotificationLogAdmin)
admin.site.register(WebhookRetry, WebhookRetryAdmin)
