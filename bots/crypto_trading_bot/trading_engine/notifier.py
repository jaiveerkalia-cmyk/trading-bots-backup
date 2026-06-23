"""
Pluggable notifier. Stub logs only.
Swap in Telegram/Discord/email later by subclassing and overriding _send().
"""
from __future__ import annotations
import logging

logger = logging.getLogger('notifier')


class Notifier:
    __slots__ = ()

    async def send(self, message: str, level: str = 'info') -> None:
        await self._send(message, level)

    async def _send(self, message: str, level: str) -> None:
        # Stub: log only. Replace body when notification channel is decided.
        getattr(logger, level if level in ('info', 'warning', 'error') else 'info')(
            f"[NOTIFY] {message}"
        )
