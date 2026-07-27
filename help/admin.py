from django.contrib import admin
from .models import HelpTopic, HelpAccessLog


@admin.register(HelpTopic)
class HelpTopicAdmin(admin.ModelAdmin):
    list_display = ('title', 'slug', 'category', 'is_published', 'created_at')
    list_filter = ('category', 'is_published', 'created_at')
    search_fields = ('title', 'content', 'slug')
    prepopulated_fields = {'slug': ('title',)}
    filter_horizontal = ('related_topics',)
    fieldsets = (
        ('Basic Information', {
            'fields': ('title', 'slug', 'category')
        }),
        ('Content', {
            'fields': ('content', 'description')
        }),
        ('Metadata', {
            'fields': ('tags', 'related_topics', 'is_published')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    readonly_fields = ('created_at', 'updated_at')


@admin.register(HelpAccessLog)
class HelpAccessLogAdmin(admin.ModelAdmin):
    list_display = ('topic', 'user', 'accessed_at')
    list_filter = ('topic', 'accessed_at')
    search_fields = ('topic__title', 'user__username')
    readonly_fields = ('topic', 'user', 'accessed_at', 'session_key')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
