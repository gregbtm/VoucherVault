import io
import json
import os
import zipfile
from decimal import Decimal, InvalidOperation

from django.core.files.base import ContentFile

from myapp.models import Document

from .exporters.full_backup import ITEMS_JSON_NAME
from .parsers.native_json import VALID_TYPES
from .parsers.utils import parse_date
from .tasks import create_item_from_row

MAX_BACKUP_SIZE = 50 * 1024 * 1024  # 50MB — a personal vault's receipts/photos add up
MAX_UNCOMPRESSED_SIZE = 200 * 1024 * 1024  # zip-bomb guard
MAX_ENTRIES = 5000


class FullBackupImportError(Exception):
    pass


def _row_from_entry(entry):
    name = (entry.get('name') or '').strip()
    redeem_code = (entry.get('redeem_code') or '').strip()
    item_type = (entry.get('type') or '').strip()
    if not name or not redeem_code:
        raise ValueError('Missing required "name" or "redeem_code".')
    if item_type not in VALID_TYPES:
        raise ValueError(f'Invalid "type" value "{item_type}".')

    value_raw = entry.get('value', 0)
    try:
        value = Decimal(str(value_raw).replace(',', '.')) if value_raw not in (None, '') else Decimal('0')
    except InvalidOperation:
        raise ValueError(f'Invalid "value" "{value_raw}".')

    tags = entry.get('tags') or []
    if not isinstance(tags, list):
        tags = [t.strip() for t in str(tags).split(',') if t.strip()]

    return {
        'type': item_type,
        'name': name,
        'issuer': (entry.get('issuer') or '').strip() or name,
        'redeem_code': redeem_code,
        'pin': entry.get('pin') or None,
        'code_type': entry.get('code_type') or 'qrcode',
        'issue_date': parse_date(entry.get('issue_date')) if isinstance(entry.get('issue_date'), str) else None,
        'expiry_date': parse_date(entry.get('expiry_date')) if isinstance(entry.get('expiry_date'), str) else None,
        'value': value,
        'value_type': entry.get('value_type') or 'money',
        'currency': entry.get('currency') or 'GBP',
        'description': entry.get('description') or '',
        'notes': entry.get('notes') or '',
        'wallet_name': entry.get('wallet') or None,
        'tag_names': [str(t).strip() for t in tags if str(t).strip()],
        'is_used': bool(entry.get('is_used', False)),
        'is_pinned': bool(entry.get('is_pinned', False)),
        'tile_color': entry.get('tile_color') or None,
        'notify_days_before': entry.get('notify_days_before'),
        'logo_slug': entry.get('logo_slug') or None,
    }


def import_full_backup(user, file_bytes: bytes) -> dict:
    """
    Restore a "Full Backup" zip (see imports.exporters.full_backup). Every
    item is created fresh with a new ID — restoring a backup never
    overwrites or merges with existing items, so it's safe to run against a
    vault that already has data in it.
    """
    if len(file_bytes) > MAX_BACKUP_SIZE:
        raise FullBackupImportError('Backup file is too large.')

    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile:
        raise FullBackupImportError('This does not look like a valid backup file.')

    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise FullBackupImportError('Backup contains too many files.')
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_SIZE:
        raise FullBackupImportError('Backup is too large once decompressed.')

    try:
        items_json = zf.read(ITEMS_JSON_NAME)
    except KeyError:
        raise FullBackupImportError(f'{ITEMS_JSON_NAME} not found in backup.')

    try:
        entries = json.loads(items_json)
    except json.JSONDecodeError as exc:
        raise FullBackupImportError(f'{ITEMS_JSON_NAME} is not valid JSON: {exc}')

    if not isinstance(entries, list):
        raise FullBackupImportError(f'{ITEMS_JSON_NAME} must be a JSON array.')

    names_in_zip = set(zf.namelist())
    imported_count = 0
    errors = []

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            errors.append({'row': index, 'message': 'Entry is not a JSON object, skipped.'})
            continue
        try:
            row = _row_from_entry(entry)
            item = create_item_from_row(user, row)
        except Exception as exc:
            errors.append({'row': index, 'message': f'{entry.get("name", "?")}: {exc}'})
            continue

        imported_count += 1

        file_arcname = entry.get('_file')
        if file_arcname and file_arcname in names_in_zip:
            # Matches the path convention myapp.views.create_item/edit_item build
            # manually, so restored files land in the same gitignored/persisted
            # uploads/ tree instead of loose at the storage root.
            relative_path = f'uploads/{user.username}/{item.id}_{os.path.basename(file_arcname)}'
            item.file.save(relative_path, ContentFile(zf.read(file_arcname)), save=True)

        for doc_arcname in entry.get('_documents') or []:
            if doc_arcname in names_in_zip:
                Document.objects.create(
                    item=item,
                    file=ContentFile(zf.read(doc_arcname), name=os.path.basename(doc_arcname)),
                )

    return {'imported_count': imported_count, 'error_count': len(errors), 'errors': errors}
