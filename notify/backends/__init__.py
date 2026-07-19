from .apprise_backend import AppriseBackend
from .base import NotificationBackend
from .firefly_backend import FireflyBackend
from .ntfy import NtfyBackend
from .webhook import WebhookBackend
from .webpush import WebPushBackend, get_vapid_public_key, webpush_enabled

BACKENDS = {
    'apprise': AppriseBackend,
    'firefly': FireflyBackend,
    'ntfy': NtfyBackend,
    'webhook': WebhookBackend,
    'webpush': WebPushBackend,
}


def get_backend(rule) -> NotificationBackend:
    try:
        backend_cls = BACKENDS[rule.backend]
    except KeyError:
        raise ValueError(f'Unknown notification backend: {rule.backend!r}')
    config = dict(rule.config or {})
    if rule.backend == 'webpush':
        # webpush has no per-rule destination in config — it delivers to
        # every subscription the rule's owner has registered.
        config['user_id'] = rule.user_id
    return backend_cls(config)
