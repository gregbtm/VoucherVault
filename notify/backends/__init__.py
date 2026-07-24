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
    if rule.backend == 'email' and not (config.get('to_addresses') or '').strip():
        # Fall back to the rule owner's registered email address so users
        # can create an email rule without explicitly typing their own address.
        owner_email = getattr(rule.user, 'email', '') or ''
        if owner_email:
            config['to_addresses'] = owner_email
    return backend_cls(config, rule_id=rule.id)
