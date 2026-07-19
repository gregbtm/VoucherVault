from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0068_totp_backup_codes'),
    ]

    operations = [
        migrations.AddField(
            model_name='item',
            name='share_message',
            field=models.TextField(blank=True, default='', help_text='Optional message shown to anyone viewing the public share link.'),
        ),
    ]
