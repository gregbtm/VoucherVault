from abc import ABC, abstractmethod


class NotificationBackend(ABC):
    def __init__(self, config: dict):
        self.config = config or {}

    @abstractmethod
    def send(self, title: str, message: str, item=None) -> bool:
        """Send a notification. Returns True on success, False on failure.
        Must not raise — callers rely on the return value to log outcomes."""
        ...
