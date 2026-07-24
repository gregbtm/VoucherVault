import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("myapp", "0086_invite_link_enhancements"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="invited_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Timestamp when this account was provisioned via PocketID.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="invited_by",
            field=models.ForeignKey(
                blank=True,
                help_text="The admin user who provisioned this account (if OIDC-provisioned).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="invited_users",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="invited_email",
            field=models.EmailField(
                blank=True,
                default="",
                help_text="Email address used when provisioning this account.",
                max_length=254,
            ),
        ),
    ]
