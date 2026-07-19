from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0070_item_type_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='firefly_account_id',
            field=models.CharField(
                blank=True, default='', max_length=50,
                help_text='Firefly III asset account ID linked to this item. Set via "Link to Firefly III" or enter manually.',
            ),
        ),
    ]
