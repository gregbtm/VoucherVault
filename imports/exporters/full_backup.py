import io
import json
import os
import zipfile

from .json_export import export_items_json

ITEMS_JSON_NAME = 'items.json'
SETTINGS_JSON_NAME = 'settings.json'
FILES_DIR = 'files'
DOCUMENTS_DIR = 'documents'

# UserPreference fields carried by a Full Backup - deliberately excludes
# the pk/user FK; kept as an explicit allowlist rather than introspecting
# the model's fields so a future preference field must be opted in here,
# not silently included.
PREFERENCE_FIELDS = [
    'show_issue_date', 'show_expiry_date', 'show_value', 'show_description',
    'sort_by', 'sort_order', 'view_mode', 'fixer_api_key', 'default_currency',
    'keep_screen_awake', 'oled_dark_mode', 'offline_cache_enabled',
]


def export_full_backup(items, user=None) -> bytes:
    """
    A "Full Backup" bundle: items.json (same shape as the plain JSON export,
    plus an internal _id/_file/_documents/_transactions index) plus the
    actual item.file and Document attachments, so restoring it doesn't
    lose anything the plain CSV/JSON export can't carry.

    When `user` is given, also writes settings.json: display preferences,
    the legacy Apprise URLs field, and NotificationRule configs - the
    account-level configuration a from-scratch restore would otherwise
    lose even though every item came back. WebPushSubscription rows are
    deliberately NOT included: a push subscription is tied to one
    specific browser's registration with its push service, and a
    still-subscribed browser re-establishes it automatically on next
    visit - restoring a stale one would just create a dead row.
    """
    entries = export_items_json(items)
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for entry, item in zip(entries, items):
            entry['_id'] = str(item.id)

            if item.file:
                arcname = f'{FILES_DIR}/{item.id}_{os.path.basename(item.file.name)}'
                zf.writestr(arcname, item.file.read())
                entry['_file'] = arcname

            doc_names = []
            for document in item.documents.all():
                arcname = f'{DOCUMENTS_DIR}/{item.id}/{document.id}_{os.path.basename(document.file.name)}'
                zf.writestr(arcname, document.file.read())
                doc_names.append(arcname)
            if doc_names:
                entry['_documents'] = doc_names

            transactions = [
                {'date': t.date.isoformat(), 'description': t.description, 'value': str(t.value)}
                for t in item.transactions.all()
            ]
            if transactions:
                entry['_transactions'] = transactions

        zf.writestr(ITEMS_JSON_NAME, json.dumps(entries, indent=2))

        if user is not None:
            settings_payload = _export_settings(user)
            if settings_payload:
                zf.writestr(SETTINGS_JSON_NAME, json.dumps(settings_payload, indent=2))

    return buffer.getvalue()


def _export_settings(user) -> dict:
    from notify.models import NotificationRule

    from myapp.models import UserPreference, UserProfile

    payload = {}

    # Deliberately a fresh manager query, not user.userpreference /
    # user.userprofile: those reverse-O2O accessors cache on first access,
    # and the User row that creates them (e.g. the post_save signal that
    # creates a default UserPreference at signup) can populate that cache
    # with stale data if `user` is a long-lived Python object that had its
    # preferences/profile modified through a *different* query afterwards.
    preferences = UserPreference.objects.filter(user=user).first()
    if preferences is not None:
        payload['preferences'] = {field: getattr(preferences, field) for field in PREFERENCE_FIELDS}

    profile = UserProfile.objects.filter(user=user).first()
    if profile is not None and profile.apprise_urls:
        payload['apprise_urls'] = profile.apprise_urls

    rules = list(NotificationRule.objects.filter(user=user))
    if rules:
        payload['notification_rules'] = [
            {
                'name': rule.name, 'backend': rule.backend, 'config': rule.config,
                'enabled': rule.enabled, 'event_types': rule.event_types,
            }
            for rule in rules
        ]

    return payload
