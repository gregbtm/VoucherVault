from django.contrib import admin

from .models import ImportJob


class ImportJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'source_type', 'status', 'imported_count', 'error_count', 'created_at')
    list_filter = ('source_type', 'status')
    search_fields = ('user__username',)
    readonly_fields = ('id', 'user', 'source_type', 'file', 'status', 'imported_count', 'error_count', 'errors', 'created_at', 'completed_at')


admin.site.register(ImportJob, ImportJobAdmin)
