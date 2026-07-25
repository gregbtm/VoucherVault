import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0087_userprofile_invited_at_userprofile_invited_by_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("invite_create", "Invite Created"),
                            ("invite_accept", "Invite Accepted"),
                            ("invite_revoke", "Invite Revoked"),
                            ("invite_clear_expired", "Cleared Expired Invites"),
                            ("invite_clear_all", "Cleared All Pending Invites"),
                            ("settings_change", "Settings Changed"),
                            ("user_create", "User Created"),
                            ("user_provision", "User Provisioned via PocketID"),
                            ("password_change", "Password Changed"),
                            ("session_logout_forced", "Session Forced Logout"),
                        ],
                        max_length=50,
                    ),
                ),
                ("description", models.TextField(blank=True)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, max_length=500)),
                ("timestamp", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-timestamp"],
                "indexes": [
                    models.Index(
                        fields=["user", "-timestamp"],
                        name="myapp_audit_user_id_0c1de2_idx",
                    ),
                    models.Index(
                        fields=["action", "-timestamp"],
                        name="myapp_audit_action_9d92c8_idx",
                    ),
                ],
            },
        ),
    ]
