from .apprise_backend import AppriseBackend
from .base import NotificationBackend
from .ntfy import NtfyBackend
from .webhook import WebhookBackend

BACKENDS = {
    'apprise': AppriseBackend,
    'ntfy': NtfyBackend,
    'webhook': WebhookBackend,
}


def get_backend(rule) -> NotificationBackend:
    try:
        backend_cls = BACKENDS[rule.backend]
    except KeyError:
        raise ValueError(f'Unknown notification backend: {rule.backend!r}')
    return backend_cls(rule.config or {})
