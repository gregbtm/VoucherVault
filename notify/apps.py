from django.apps import AppConfig


class NotifyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'notify'
    verbose_name = 'Notifications'

    def ready(self):
        import notify.signals  # noqa: F401 — registers post_save signal
