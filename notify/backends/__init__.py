from .apprise_backend import AppriseBackend
from .base import NotificationBackend
from .discord import DiscordBackend
from .email_smtp import EmailBackend
from .firefly_backend import FireflyBackend
from .ntfy import NtfyBackend
from .telegram import TelegramBackend
from .webhook import WebhookBackend
from .webpush import WebPushBackend, get_vapid_public_key, webpush_enabled

BACKENDS = {
    'apprise': AppriseBackend,
    'discord': DiscordBackend,
    'email': EmailBackend,
    'firefly': FireflyBackend,
    'ntfy': NtfyBackend,
    'telegram': TelegramBackend,
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
    return backend_cls(config, rule_id=rule.id)
