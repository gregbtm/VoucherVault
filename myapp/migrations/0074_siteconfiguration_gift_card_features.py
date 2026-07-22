from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0073_firefly_enhancements'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfiguration',
            name='inactivity_threshold_days',
            field=models.PositiveIntegerField(
                default=90,
                help_text="Days without use before an item triggers an 'Unused Gift Card Reminder' "
                           "notification (for rules subscribed to that event). Applies to all "
                           "non-loyalty money-type items.",
            ),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='companies_house_api_key',
            field=models.CharField(
                blank=True,
                default='',
                max_length=255,
                help_text="Companies House API key for the Merchant Health Alert notification — "
                           "fires if a gift card issuer enters administration or liquidation. "
                           "Get a free key at https://developer.company-information.service.gov.uk/",
            ),
        ),
    ]
