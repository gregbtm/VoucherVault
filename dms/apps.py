from django.apps import AppConfig


class DmsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'dms'
    verbose_name = 'DMS Integration'

    def ready(self):
        import dms.signals  # noqa: F401
