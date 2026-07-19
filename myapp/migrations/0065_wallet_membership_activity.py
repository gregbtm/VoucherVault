from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def populate_wallet_memberships(apps, schema_editor):
    Wallet = apps.get_model('myapp', 'Wallet')
    WalletMembership = apps.get_model('myapp', 'WalletMembership')
    for wallet in Wallet.objects.prefetch_related('shared_with').all():
        for user in wallet.shared_with.all():
            WalletMembership.objects.get_or_create(
                wallet=wallet,
                user=user,
                defaults={'role': 'editor'},
            )


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0064_item_composite_indexes'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='WalletMembership',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(choices=[('viewer', 'Viewer'), ('editor', 'Editor')], default='editor', max_length=10)),
                ('joined_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='wallet_memberships', to=settings.AUTH_USER_MODEL)),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='memberships', to='myapp.wallet')),
            ],
            options={
                'unique_together': {('wallet', 'user')},
            },
        ),
        migrations.CreateModel(
            name='WalletActivity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(max_length=50)),
                ('item_name', models.CharField(blank=True, max_length=255)),
                ('detail', models.CharField(blank=True, max_length=500)),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='wallet_activities', to=settings.AUTH_USER_MODEL)),
                ('item', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='wallet_activities', to='myapp.item')),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='activities', to='myapp.wallet')),
            ],
            options={
                'ordering': ['-timestamp'],
            },
        ),
        migrations.RunPython(populate_wallet_memberships, migrations.RunPython.noop),
    ]
