import logging
import os
from datetime import timedelta
from decimal import Decimal

from celery import shared_task
from django.contrib.auth.models import User
from django.utils import timezone

from myapp.models import Item, SiteConfiguration, Tag, Wallet
from myapp.utils import generate_code_image_base64

from .exporters.full_backup import export_full_backup
from .models import ImportJob
from .parsers import get_parser

logger = logging.getLogger(__name__)

BACKUP_ROOT = os.path.join('database', 'backups')


def _validate_value(item_type, value_type, value):
    """
    Mirrors the value/type business rule enforced by ItemForm.clean() and
    ItemSerializer.validate() — kept as a third, independent implementation
    here rather than a shared import, since import rows come pre-typed
    (Decimal, already-resolved item_type) and don't go through a Form/DRF
    validation pipeline.
    """
    if item_type == 'loyaltycard':
        return Decimal('0'), None
    if item_type == 'coupon':
        if value_type == 'money':
            if value is None or value < 0:
                return None, 'Value must be a positive monetary amount.'
        elif value_type == 'percentage':
            if value is None or value < 0 or value > 100:
                return None, 'Percentage value must be between 0 and 100.'
        elif value_type == 'multiplier':
            if value is None or value < 1:
                return None, 'Multiplier must be 1 or higher.'
        return value, None
    if value is None or value < 0:
        return None, 'Value must be positive.'
    return value, None


def create_item_from_row(user, row):
    """Creates a single Item from a normalized parser row dict. Raises ValueError on failure."""
    value, error = _validate_value(row['type'], row.get('value_type', 'money'), row.get('value'))
    if error:
        raise ValueError(error)

    wallet = None
    wallet_name = row.get('wallet_name')
    if wallet_name:
        wallet, _created = Wallet.objects.get_or_create(user=user, name=wallet_name)

    expiry_date = row.get('expiry_date') or (timezone.now().date() + timedelta(days=50 * 365))
    issue_date = row.get('issue_date') or timezone.now().date()

    item = Item(
        user=user,
        type=row['type'],
        name=row['name'],
        issuer=row.get('issuer') or row['name'],
        redeem_code=row['redeem_code'],
        pin=row.get('pin'),
        code_type=row.get('code_type') or 'qrcode',
        issue_date=issue_date,
        expiry_date=expiry_date,
        value=value,
        value_type=row.get('value_type', 'money'),
        currency=row.get('currency', 'GBP'),
        description=row.get('description', ''),
        notes=row.get('notes', ''),
        wallet=wallet,
        is_used=row.get('is_used', False),
        is_pinned=row.get('is_pinned', False),
        tile_color=row.get('tile_color'),
        notify_days_before=row.get('notify_days_before'),
        logo_slug=row.get('logo_slug'),
        source='csv_import',
    )
    item.qr_code_base64, item.code_type = generate_code_image_base64(item)
    item.save()

    tag_names = row.get('tag_names') or []
    if tag_names:
        tags = [Tag.objects.get_or_create(user=user, name=name)[0] for name in tag_names]
        item.tags.set(tags)

    return item


@shared_task
def process_import_job(job_id):
    job = ImportJob.objects.get(pk=job_id)
    job.status = 'processing'
    job.save(update_fields=['status'])

    try:
        parser = get_parser(job.source_type)
        job.file.open('rb')
        try:
            rows, errors = parser(job.file)
        finally:
            job.file.close()
    except Exception as exc:
        job.status = 'failed'
        job.errors = [{'row': None, 'message': str(exc)}]
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'errors', 'completed_at'])
        return

    errors = list(errors)
    imported_count = 0

    for row in rows:
        try:
            create_item_from_row(job.user, row)
            imported_count += 1
        except Exception as exc:
            errors.append({'row': None, 'message': f'{row.get("name", "?")}: {exc}'})

    job.status = 'complete'
    job.imported_count = imported_count
    job.error_count = len(errors)
    job.errors = errors
    job.completed_at = timezone.now()
    job.save(update_fields=['status', 'imported_count', 'error_count', 'errors', 'completed_at'])


def _user_backup_dir(user) -> str:
    return os.path.join(BACKUP_ROOT, user.username)


def _rotate_backups(backup_dir: str) -> None:
    """Keeps the newest SiteConfiguration.backup_retention_count .zip files in backup_dir, deletes the rest."""
    entries = sorted((f for f in os.listdir(backup_dir) if f.endswith('.zip')), reverse=True)
    for stale in entries[SiteConfiguration.load().backup_retention_count:]:
        try:
            os.remove(os.path.join(backup_dir, stale))
        except OSError as exc:
            logger.warning('Failed to remove stale backup %s: %s', stale, exc)


def backup_user(user) -> str | None:
    """
    Writes one Full Backup zip (same format as the manual Import/Export
    page download - see imports/exporters/full_backup.py) for `user` to
    database/backups/<username>/ and rotates old ones. Returns the path
    written, or None if the user has no items to back up.
    """
    items = Item.objects.filter(user=user).select_related('wallet').prefetch_related('tags', 'documents', 'transactions')
    if not items.exists():
        return None

    backup_dir = _user_backup_dir(user)
    os.makedirs(backup_dir, exist_ok=True)

    filename = f'backup-{timezone.now().strftime("%Y%m%d-%H%M%S-%f")}.zip'
    path = os.path.join(backup_dir, filename)
    with open(path, 'wb') as f:
        f.write(export_full_backup(items, user=user))

    _rotate_backups(backup_dir)
    return path


@shared_task
def run_scheduled_backups():
    """
    Periodic task (see create_default_periodic_tasks) that backs up every
    user with at least one item. A no-op if SCHEDULED_BACKUP_ENABLED=False.
    One user's failure (e.g. a disk error) doesn't stop the others.
    """
    if not SiteConfiguration.load().scheduled_backup_enabled:
        return
    for user in User.objects.filter(item__isnull=False).distinct():
        try:
            backup_user(user)
        except Exception as exc:
            logger.error('Scheduled backup failed for user %s: %s', user.username, exc)
