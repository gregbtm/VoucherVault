from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0041_userpreference_offline_cache_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='ics_token',
            field=models.CharField(
                max_length=64, null=True, unique=True,
                help_text="Secret token in the subscribe-able .ics calendar feed URL. Regenerating it invalidates the old feed URL.",
            ),
        ),
    ]
