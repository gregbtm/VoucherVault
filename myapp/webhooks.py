"""
Outbound webhook dispatcher for per-user events (Phase E, extended for
event parity with the notification engine - see notify/tasks.py). Most
events are item-scoped, but a few (e.g. wallet_invited) have no single
associated item.

Each UserWebhook row stores a URL, an optional HMAC secret, and a list of
event types it should fire for. Call fire_user_webhooks() after any
lifecycle event this app already notifies a user about; it fans out to
every matching, enabled webhook asynchronously via a background thread so
the calling request is never delayed.
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import threading
import time
from urllib.parse import urlparse

import requests as http_requests

logger = logging.getLogger(__name__)

_TIMEOUT = 10  # seconds per outbound request


class WebhookURLValidationError(ValueError):
    pass


def validate_webhook_url(url: str) -> None:
    """
    Raises WebhookURLValidationError for a URL with no legitimate webhook
    use case: loopback, link-local (this also covers 169.254.169.254 -
    the AWS/GCP/Azure cloud metadata endpoint and the single most common
    real-world SSRF target), unspecified, reserved, or multicast.

    Deliberately does NOT block general private (RFC1918) address
    ranges - self-hosters legitimately point webhooks at automation
    tools (n8n, Home Assistant, etc.) running on their own LAN, and this
    app's own docs assume that works. The targets blocked here have no
    such use case: nothing a webhook should ever legitimately need to
    reach lives on loopback or link-local from this app's perspective.

    Resolves the hostname and checks the resolved address rather than
    just the literal string, so "definitely-not-localhost.example.com"
    pointed at 127.0.0.1 via DNS doesn't sail through - though note this
    only protects the URL at validation time; a DNS record can still
    change between save and send (rebinding), which is why _send_one
    below re-validates immediately before every actual request too.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise WebhookURLValidationError('Webhook URL must use http or https.')
    hostname = parsed.hostname
    if not hostname:
        raise WebhookURLValidationError('Webhook URL must include a hostname.')
    try:
        resolved = socket.gethostbyname(hostname)
    except socket.gaierror:
        raise WebhookURLValidationError('Could not resolve webhook hostname.')
    ip = ipaddress.ip_address(resolved)
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_reserved or ip.is_multicast:
        raise WebhookURLValidationError(
            'Webhook URL resolves to a loopback, link-local, or reserved address, which is never a valid webhook target.'
        )


def _build_payload(event_type: str, item=None, extra: dict = None) -> dict:
    """
    `item` is None for events with no single associated item (e.g.
    wallet_invited, which is about a Wallet, not an Item) - the payload
    just omits the 'item' key entirely rather than sending a null
    placeholder. `extra` merges in event-specific context (e.g. which
    wallet, who shared it) that doesn't fit the item shape.
    """
    from django.utils import timezone
    payload = {
        'event': event_type,
        'timestamp': timezone.now().isoformat(),
    }
    if item is not None:
        payload['item'] = {
            'id': str(item.id),
            'name': item.name,
            'issuer': item.issuer,
            'type': item.type,
            'value': str(item.value) if item.value is not None else None,
            'currency': item.currency,
            'expiry_date': item.expiry_date.isoformat() if item.expiry_date else None,
            'is_used': item.is_used,
            'is_archived': item.is_archived,
        }
    if extra:
        payload.update(extra)
    return payload


def _send_one(webhook, payload: dict):
    # Re-validate right before sending, not just at save time - a DNS
    # record can change between when a webhook was created and when it
    # actually fires, and this also covers any webhook row saved before
    # this guard existed.
    try:
        validate_webhook_url(webhook.url)
    except WebhookURLValidationError as exc:
        logger.warning("Webhook %s not sent - %s", webhook.id, exc)
        return

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


def fire_user_webhooks(user, event_type: str, item=None, extra: dict = None):
    """
    Fan out to all enabled webhooks for `user` that subscribe to
    event_type. `user` is who should receive the webhook, which for a
    handful of events (item_shared_with_you) is the recipient rather
    than the item's owner - callers pass whichever user actually owns
    the webhook subscription that should fire.
    """
    from .models import UserWebhook
    hooks = list(
        UserWebhook.objects.filter(user=user, enabled=True)
        .exclude(events=[])
    )
    matching = [h for h in hooks if event_type in (h.events or [])]
    if not matching:
        return
    payload = _build_payload(event_type, item, extra)

    def _dispatch():
        for hook in matching:
            _send_one(hook, payload)

    threading.Thread(target=_dispatch, daemon=True).start()
