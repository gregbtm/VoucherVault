from abc import ABC, abstractmethod


class NotificationBackend(ABC):
    def __init__(self, config: dict, rule_id=None):
        self.config = config or {}
        self.rule_id = rule_id

    @abstractmethod
    def send(self, title: str, message: str, item=None, transaction=None) -> bool:
        """Send a notification. Returns True on success, False on failure.
        Must not raise — callers rely on the return value to log outcomes.
        `transaction` is passed for backends that need it (e.g. Firefly III
        balance-changed events); other backends may ignore it."""
        ...
