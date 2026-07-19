from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0067_totp_login_audit'),
    ]

    operations = [
        migrations.CreateModel(
            name='TOTPBackupCode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code_hash', models.CharField(max_length=128)),
                ('used', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('device', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='backup_codes',
                    to='myapp.totpdevice',
                )),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
    ]
