from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0069_item_share_message'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='minimum_spend',
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=10, null=True,
                help_text='Minimum basket value required to redeem this voucher or coupon.',
            ),
        ),
        migrations.AddField(
            model_name='item',
            name='points_balance',
            field=models.PositiveIntegerField(
                blank=True, null=True,
                help_text='Current points or stamps total on a loyalty card. Updated manually.',
            ),
        ),
        migrations.AddField(
            model_name='item',
            name='membership_tier',
            field=models.CharField(
                blank=True, default='', max_length=50,
                help_text='Loyalty scheme tier, e.g. Silver, Gold, Platinum.',
            ),
        ),
        migrations.AddField(
            model_name='item',
            name='initial_value',
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=10, null=True,
                help_text='Face/loaded value when purchased — useful for tracking discount or spend on gift cards.',
            ),
        ),
        migrations.AddField(
            model_name='item',
            name='seat_number',
            field=models.CharField(
                blank=True, default='', max_length=50,
                help_text='Seat or coach reservation on a travel ticket, e.g. Coach C, Seat 42.',
            ),
        ),
    ]
