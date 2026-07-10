from django.contrib import admin

from .models import NotificationLog, NotificationRule


class NotificationRuleAdmin(admin.ModelAdmin):
    list_display = ('name', 'user', 'backend', 'enabled')
    list_filter = ('backend', 'enabled')
    search_fields = ('name', 'user__username')


class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'user', 'item', 'rule', 'success', 'sent_at')
    list_filter = ('event_type', 'success')
    search_fields = ('user__username', 'item__name')
    readonly_fields = ('user', 'rule', 'item', 'event_type', 'sent_at', 'success', 'detail')


admin.site.register(NotificationRule, NotificationRuleAdmin)
admin.site.register(NotificationLog, NotificationLogAdmin)
