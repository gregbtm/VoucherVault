from django.conf import settings
from django.db import migrations


def seed_from_env(apps, schema_editor):
    """
    Creates the SiteConfiguration singleton (pk=1) seeded from whatever
    django.conf.settings currently resolves each field to - i.e. the
    env vars/defaults in effect at the moment this migration runs. This
    is what makes upgrading non-destructive: an existing deployment with
    OCR_BACKEND=claude set in Portainer gets a SiteConfiguration row that
    already says "claude", not one that resets to the hardcoded default.
    """
    SiteConfiguration = apps.get_model('myapp', 'SiteConfiguration')
    if SiteConfiguration.objects.filter(pk=1).exists():
        return

    SiteConfiguration.objects.create(
        pk=1,
        expiry_threshold_days=settings.EXPIRY_THRESHOLD_DAYS,
        expiry_last_notification_days=settings.EXPIRY_LAST_NOTIFICATION_DAYS,
        ntfy_default_server=settings.NTFY_DEFAULT_SERVER,
        webpush_vapid_public_key=settings.WEBPUSH_VAPID_PUBLIC_KEY or '',
        webpush_vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY or '',
        webpush_vapid_claims_email=settings.WEBPUSH_VAPID_CLAIMS_EMAIL,
        merchant_logos_enabled=settings.MERCHANT_LOGOS_ENABLED,
        ocr_backend=settings.OCR_BACKEND if settings.OCR_BACKEND in ('none', 'claude', 'openai', 'tesseract') else 'none',
        anthropic_api_key=settings.ANTHROPIC_API_KEY or '',
        anthropic_ocr_model=settings.ANTHROPIC_OCR_MODEL,
        openai_api_key=settings.OPENAI_API_KEY or '',
        openai_ocr_model=settings.OPENAI_OCR_MODEL,
        pkpass_cert_path=settings.PKPASS_CERT_PATH or '',
        pkpass_cert_password=settings.PKPASS_CERT_PASSWORD or '',
        pkpass_wwdr_cert_path=settings.PKPASS_WWDR_CERT_PATH or '',
        pkpass_team_id=settings.PKPASS_TEAM_ID or '',
        pkpass_pass_type_id=settings.PKPASS_PASS_TYPE_ID or '',
        pkpass_organization_name=settings.PKPASS_ORGANIZATION_NAME,
        google_wallet_service_account_key_path=settings.GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH or '',
        google_wallet_issuer_id=settings.GOOGLE_WALLET_ISSUER_ID or '',
        google_wallet_class_id=settings.GOOGLE_WALLET_CLASS_ID or '',
        update_check_enabled=settings.UPDATE_CHECK_ENABLED,
        update_check_repo=settings.UPDATE_CHECK_REPO,
        portainer_webhook_url=settings.PORTAINER_WEBHOOK_URL or '',
        scheduled_backup_enabled=settings.SCHEDULED_BACKUP_ENABLED,
        backup_retention_count=settings.BACKUP_RETENTION_COUNT,
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0045_siteconfiguration"),
    ]

    operations = [
        migrations.RunPython(seed_from_env, noop_reverse),
    ]
