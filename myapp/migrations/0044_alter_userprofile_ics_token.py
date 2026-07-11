import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0043_populate_ics_token'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='ics_token',
            field=models.CharField(
                max_length=64, unique=True, default=uuid.uuid4,
                help_text="Secret token in the subscribe-able .ics calendar feed URL. Regenerating it invalidates the old feed URL.",
            ),
        ),
    ]
