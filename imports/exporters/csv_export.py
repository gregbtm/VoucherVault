import csv
import io

FIELDNAMES = [
    'type', 'name', 'issuer', 'redeem_code', 'pin', 'code_type',
    'issue_date', 'expiry_date', 'value', 'value_type', 'currency',
    'description', 'notes', 'wallet', 'tags', 'is_used', 'is_pinned',
    'tile_color', 'notify_days_before', 'logo_slug',
]


def export_items_csv(items) -> str:
    """
    Renders a user's items as CSV using our own native column set (not the
    Catima format). Column names/order match imports.parsers.native_csv
    exactly, so a file downloaded here can be re-uploaded as a full
    backup/restore round trip.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDNAMES)
    writer.writeheader()
    for item in items:
        writer.writerow({
            'type': item.type,
            'name': item.name,
            'issuer': item.issuer,
            'redeem_code': item.redeem_code,
            'pin': item.pin or '',
            'code_type': item.code_type,
            'issue_date': item.issue_date.isoformat() if item.issue_date else '',
            'expiry_date': item.expiry_date.isoformat() if item.expiry_date else '',
            'value': str(item.value),
            'value_type': item.value_type,
            'currency': item.currency,
            'description': item.description or '',
            'notes': item.notes or '',
            'wallet': item.wallet.name if item.wallet_id else '',
            'tags': ','.join(item.tags.values_list('name', flat=True)),
            'is_used': item.is_used,
            'is_pinned': item.is_pinned,
            'tile_color': item.tile_color or '',
            'notify_days_before': item.notify_days_before if item.notify_days_before is not None else '',
            'logo_slug': item.logo_slug or '',
        })
    return buffer.getvalue()
