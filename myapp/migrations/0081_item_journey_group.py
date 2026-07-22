from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0080_security_settings'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='journey_group_id',
            field=models.UUIDField(
                blank=True,
                db_index=True,
                null=True,
                help_text='Shared UUID linking tickets that belong to the same booking (e.g. outward + return). Set automatically when a multi-page PDF is imported. Never set manually.',
            ),
        ),
        migrations.AddField(
            model_name='item',
            name='journey_sequence',
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                help_text='Position of this ticket within its journey group (1 = first leg, 2 = second leg, etc.). Set automatically alongside journey_group_id.',
            ),
        ),
    ]
