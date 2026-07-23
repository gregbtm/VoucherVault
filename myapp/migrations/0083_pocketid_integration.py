from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0082_item_journey_group'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfiguration',
            name='pocket_id_url',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Base URL of your PocketID instance (e.g. https://id.example.com). '
                          'When set, invite creation can auto-provision a PocketID user and '
                          'generate a one-click onboarding link.',
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='siteconfiguration',
            name='pocket_id_api_key',
            field=models.CharField(
                blank=True,
                default='',
                help_text='PocketID admin API key (X-API-KEY). Generate one in PocketID → Admin → API Keys.',
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name='invitelink',
            name='pocket_id_user_id',
            field=models.CharField(
                blank=True,
                default='',
                help_text='PocketID user ID created during OIDC provisioning. Blank for manually-created invites.',
                max_length=255,
            ),
        ),
    ]
