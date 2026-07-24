from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0084_siteconfiguration_smtp_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='userpreference',
            name='email_digest_enabled',
            field=models.BooleanField(default=False, help_text='Receive a periodic email summary of your items expiring soon.'),
        ),
        migrations.AddField(
            model_name='userpreference',
            name='email_digest_frequency',
            field=models.CharField(
                choices=[('weekly', 'Weekly (every Monday)'), ('monthly', 'Monthly (1st of each month)')],
                default='weekly',
                max_length=10,
            ),
        ),
    ]
