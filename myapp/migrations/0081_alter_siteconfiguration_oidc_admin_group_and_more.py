from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0080_security_settings'),
    ]

    operations = [
        migrations.AlterField(
            model_name='siteconfiguration',
            name='oidc_admin_group',
            field=models.CharField(
                blank=True,
                default='',
                max_length=255,
                help_text="PocketID group name whose members are automatically given superuser access. "
                          "Members are promoted on login; non-members are demoted. Leave blank to disable.",
            ),
        ),
        migrations.AlterField(
            model_name='siteconfiguration',
            name='oidc_autologin',
            field=models.BooleanField(
                default=False,
                help_text="Redirect straight to the OIDC provider on the login page, skipping the "
                          "username/password form. Only useful when OIDC is the sole login method.",
            ),
        ),
        migrations.AlterField(
            model_name='siteconfiguration',
            name='oidc_client_id',
            field=models.CharField(
                blank=True,
                default='',
                max_length=255,
                help_text="OIDC application client ID. Takes precedence over the OIDC_RP_CLIENT_ID env var.",
            ),
        ),
        migrations.AlterField(
            model_name='siteconfiguration',
            name='oidc_create_user',
            field=models.BooleanField(
                default=True,
                help_text="Create a VoucherVault account automatically when a new user authenticates "
                          "via OIDC for the first time. Disable to allow only pre-existing accounts to log in via OIDC.",
            ),
        ),
        migrations.AlterField(
            model_name='siteconfiguration',
            name='oidc_discovery_url',
            field=models.CharField(
                blank=True,
                default='',
                max_length=500,
                help_text="OpenID Connect discovery URL "
                          "(e.g. https://id.example.com/.well-known/openid-configuration). "
                          "When set, endpoint URLs are fetched automatically. Requires a server restart to apply.",
            ),
        ),
        migrations.AlterField(
            model_name='siteconfiguration',
            name='oidc_provider_name',
            field=models.CharField(
                blank=True,
                default='SSO',
                max_length=100,
                help_text="Display name shown on the 'Login with …' button (e.g. PocketID, Authentik).",
            ),
        ),
        migrations.AlterField(
            model_name='siteconfiguration',
            name='oidc_require_totp',
            field=models.BooleanField(
                default=False,
                help_text="When enabled, users who have TOTP set up must complete the TOTP check even "
                          "after a successful OIDC login — OIDC alone is not sufficient to open a session. "
                          "Opt-in; default off so OIDC acts as a complete single authentication factor.",
            ),
        ),
        migrations.AlterField(
            model_name='siteconfiguration',
            name='security_alert_ntfy_topic',
            field=models.CharField(
                blank=True,
                default='',
                max_length=255,
                help_text="ntfy topic to receive admin security alerts (e.g. login-failure spikes). "
                          "Uses the Default ntfy Server above. Leave blank to disable security alerts.",
            ),
        ),
        migrations.AlterField(
            model_name='siteconfiguration',
            name='security_alert_threshold',
            field=models.PositiveIntegerField(
                default=10,
                help_text="Number of failed login attempts in a rolling 60-minute window that triggers "
                          "a security alert notification. Applies only when a security alert ntfy topic is set.",
            ),
        ),
        migrations.AlterField(
            model_name='userprofile',
            name='oidc_sub',
            field=models.CharField(
                blank=True,
                default='',
                max_length=255,
                help_text="OIDC subject identifier from the identity provider (stable unique ID for this user).",
            ),
        ),
    ]
