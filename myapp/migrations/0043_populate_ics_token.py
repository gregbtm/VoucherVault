import uuid

from django.db import migrations


def populate_ics_token(apps, schema_editor):
    """
    A callable default on a unique field can't be applied by AddField alone
    (it would give every existing row the same value) - so this backfills a
    distinct token per existing UserProfile row before the field is made
    non-nullable in the next migration.
    """
    UserProfile = apps.get_model('myapp', 'UserProfile')
    for profile in UserProfile.objects.filter(ics_token__isnull=True):
        profile.ics_token = str(uuid.uuid4())
        profile.save(update_fields=['ics_token'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0042_userprofile_ics_token'),
    ]

    operations = [
        migrations.RunPython(populate_ics_token, noop_reverse),
    ]
