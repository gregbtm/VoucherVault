from django.core.checks import Warning, register


@register()
def check_secret_key_from_env(app_configs, **kwargs):
    from django.conf import settings
    errors = []
    if not getattr(settings, '_SECRET_KEY_FROM_ENV', True):
        errors.append(Warning(
            'SECRET_KEY is not set in the environment — a random key is generated on each '
            'startup, which invalidates all existing sessions and signed cookies on restart.',
            hint='Set the SECRET_KEY environment variable to a stable, secret value.',
            id='myapp.W001',
        ))
    return errors


@register()
def check_webdav_ssl(app_configs, **kwargs):
    from django.conf import settings
    errors = []
    if hasattr(settings, 'WEBDAV_VERIFY_SSL') and not settings.WEBDAV_VERIFY_SSL:
        errors.append(Warning(
            'WEBDAV_VERIFY_SSL is False — TLS certificates are not verified for DMS connections.',
            hint='Set WEBDAV_VERIFY_SSL=True in production, or use a properly signed certificate.',
            id='myapp.W002',
        ))
    return errors
