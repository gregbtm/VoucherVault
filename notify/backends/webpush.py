import json
import logging

import requests
from pywebpush import WebPushException, webpush

from myapp.models import SiteConfiguration

from ..models import WebPushSubscription
from .base import NotificationBackend

logger = logging.getLogger(__name__)


def webpush_enabled() -> bool:
    config = SiteConfiguration.load()
    return bool(config.webpush_vapid_public_key) and bool(config.webpush_vapid_private_key)


def get_vapid_public_key() -> str | None:
    return SiteConfiguration.load().webpush_vapid_public_key or None


class WebPushBackend(NotificationBackend):
    """
    Delivers to every active Web Push subscription for the rule's owner —
    unlike ntfy/webhook there's no single fixed destination in the rule's
    config, since a user may be subscribed from several browsers/devices.
    """

    def send(self, title: str, message: str, item=None, transaction=None) -> bool:
        config = SiteConfiguration.load()
        vapid_private_key = config.webpush_vapid_private_key
        if not vapid_private_key:
            logger.warning('WEBPUSH_VAPID_PRIVATE_KEY is not set; cannot send web push.')
            return False

        user_id = self.config.get('user_id')
        subscriptions = list(WebPushSubscription.objects.filter(user_id=user_id))
        if not subscriptions:
            return False

        claims_email = config.webpush_vapid_claims_email
        item_url = f'/en/items/view/{item.id}/' if item is not None else '/'
        payload = json.dumps({'title': title, 'body': message, 'url': item_url})

        any_success = False
        for sub in subscriptions:
            subscription_info = {
                'endpoint': sub.endpoint,
                'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
            }
            try:
                webpush(
                    subscription_info=subscription_info,
                    data=payload,
                    vapid_private_key=vapid_private_key,
                    vapid_claims={'sub': claims_email},
                )
                any_success = True
            except WebPushException as exc:
                status_code = getattr(exc.response, 'status_code', None)
                logger.warning('Web push failed for subscription %s: %s', sub.id, exc)
                if status_code in (404, 410):
                    # Browser unsubscribed or the subscription expired; stop retrying it.
                    sub.delete()
            except requests.RequestException as exc:
                # pywebpush lets network-level errors (unreachable push
                # service, timeout, DNS failure) propagate as raw requests
                # exceptions rather than wrapping them in WebPushException.
                logger.warning('Web push network error for subscription %s: %s', sub.id, exc)
        return any_success
