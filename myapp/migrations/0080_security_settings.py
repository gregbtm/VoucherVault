from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0079_pocketid_integration"),
    ]

    operations = [
        migrations.AddField(
            model_name="siteconfiguration",
            name="oidc_require_totp",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, users who have TOTP set up must complete the TOTP "
                          "check even after a successful OIDC login.",
            ),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="security_alert_ntfy_topic",
            field=models.CharField(
                blank=True,
                default="",
                max_length=255,
                help_text="ntfy topic for admin security alerts (e.g. login-failure spikes). "
                          "Leave blank to disable.",
            ),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="security_alert_threshold",
            field=models.PositiveIntegerField(
                default=10,
                help_text="Failed logins in 60 minutes before a security alert fires.",
            ),
        ),
    ]
