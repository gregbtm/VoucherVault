"""
Outbound webhook dispatcher for per-user item lifecycle events (Phase E).

Each UserWebhook row stores a URL, an optional HMAC secret, and a list of
event types it should fire for. Call fire_user_webhooks() after any item
lifecycle event; it fans out to every matching, enabled webhook asynchronously
via a background thread so the calling request is never delayed.
"""
import hashlib
import hmac
import json
import logging
import threading
import time

import requests as http_requests

logger = logging.getLogger(__name__)

_TIMEOUT = 10  # seconds per outbound request


def _build_payload(event_type: str, item) -> dict:
    from django.utils import timezone
    return {
        'event': event_type,
        'timestamp': timezone.now().isoformat(),
        'item': {
            'id': str(item.id),
            'name': item.name,
            'issuer': item.issuer,
            'type': item.type,
            'value': str(item.value) if item.value is not None else None,
            'currency': item.currency,
            'expiry_date': item.expiry_date.isoformat() if item.expiry_date else None,
            'is_used': item.is_used,
            'is_archived': item.is_archived,
        },
    }


def _send_one(webhook, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {'Content-Type': 'application/json', 'X-VoucherVault-Event': payload.get('event', '')}
    if webhook.secret:
        sig = hmac.new(webhook.secret.encode(), body, hashlib.sha256).hexdigest()
        headers['X-VoucherVault-Signature'] = f"sha256={sig}"
    try:
        resp = http_requests.post(webhook.url, data=body, headers=headers, timeout=_TIMEOUT)
        if not resp.ok:
            logger.warning("Webhook %s returned HTTP %s", webhook.id, resp.status_code)
    except Exception as exc:
        logger.warning("Webhook %s delivery failed: %s", webhook.id, exc)


def fire_user_webhooks(user, event_type: str, item):
    """Fan out to all enabled webhooks for user that subscribe to event_type."""
    from .models import UserWebhook
    hooks = list(
        UserWebhook.objects.filter(user=user, enabled=True)
        .exclude(events=[])
    )
    matching = [h for h in hooks if event_type in (h.events or [])]
    if not matching:
        return
    payload = _build_payload(event_type, item)

    def _dispatch():
        for hook in matching:
            _send_one(hook, payload)

    threading.Thread(target=_dispatch, daemon=True).start()
