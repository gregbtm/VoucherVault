def export_items_json(items) -> list[dict]:
    """
    Renders a user's items as a list of plain dicts using our own native
    field set. Field names match imports.parsers.native_json exactly, so
    a file downloaded here can be re-uploaded as a full backup/restore
    round trip.
    """
    return [
        {
            'type': item.type,
            'name': item.name,
            'issuer': item.issuer,
            'redeem_code': item.redeem_code,
            'pin': item.pin,
            'code_type': item.code_type,
            'issue_date': item.issue_date.isoformat() if item.issue_date else None,
            'expiry_date': item.expiry_date.isoformat() if item.expiry_date else None,
            'value': str(item.value),
            'value_type': item.value_type,
            'currency': item.currency,
            'description': item.description,
            'notes': item.notes,
            'wallet': item.wallet.name if item.wallet_id else None,
            'tags': list(item.tags.values_list('name', flat=True)),
            'is_used': item.is_used,
            'is_pinned': item.is_pinned,
            'tile_color': item.tile_color,
            'notify_days_before': item.notify_days_before,
            'logo_slug': item.logo_slug,
        }
        for item in items
    ]
