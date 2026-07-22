import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0078_webpush_key_version"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # --- UserProfile: OIDC identity fields ---
        migrations.AddField(
            model_name="userprofile",
            name="oidc_sub",
            field=models.CharField(blank=True, default="", max_length=255,
                                   help_text="OIDC subject identifier from the identity provider."),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="oidc_avatar_url",
            field=models.URLField(blank=True, default="", max_length=500,
                                  help_text="Avatar URL fetched from the OIDC provider's userinfo endpoint."),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="oidc_last_login",
            field=models.DateTimeField(blank=True, null=True,
                                       help_text="Timestamp of the last successful OIDC authentication."),
        ),

        # --- InviteLink model ---
        migrations.CreateModel(
            name="InviteLink",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.UUIDField(default=uuid.uuid4, unique=True, editable=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked", models.BooleanField(default=False)),
                ("note", models.CharField(blank=True, default="", max_length=255)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_invites",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "used_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="invite_used",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),

        # --- SiteConfiguration: registration invite expiry + OIDC fields ---
        migrations.AddField(
            model_name="siteconfiguration",
            name="invite_expiry_days",
            field=models.PositiveIntegerField(default=7,
                                              help_text="How many days a generated invite link stays valid. 0 means it never expires."),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="oidc_discovery_url",
            field=models.CharField(blank=True, default="", max_length=500,
                                   help_text="OpenID Connect discovery URL."),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="oidc_client_id",
            field=models.CharField(blank=True, default="", max_length=255,
                                   help_text="OIDC application client ID."),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="oidc_client_secret",
            field=models.CharField(blank=True, default="", max_length=500,
                                   help_text="OIDC application client secret."),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="oidc_provider_name",
            field=models.CharField(blank=True, default="SSO", max_length=100,
                                   help_text="Display name shown on the 'Login with …' button."),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="oidc_create_user",
            field=models.BooleanField(default=True,
                                      help_text="Create a VoucherVault account automatically for new OIDC users."),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="oidc_autologin",
            field=models.BooleanField(default=False,
                                      help_text="Redirect straight to the OIDC provider on the login page."),
        ),
        migrations.AddField(
            model_name="siteconfiguration",
            name="oidc_admin_group",
            field=models.CharField(blank=True, default="", max_length=255,
                                   help_text="PocketID group name whose members get superuser access automatically."),
        ),
    ]
