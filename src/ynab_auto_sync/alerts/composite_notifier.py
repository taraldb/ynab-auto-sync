from __future__ import annotations

import logging
from typing import Any

from ynab_auto_sync.alerts.base import CycleStats, EventNotifier

logger = logging.getLogger(__name__)


class CompositeNotifier(EventNotifier):
    """Fans out to several EventNotifiers at once - mirrors
    notifications.CompositeSink's role and isolation guarantee: one
    provider's failure/exception is logged and never allowed to block or
    break delivery to the others. Only one provider (ntfy) exists today,
    but this keeps room for a second (Slack/Discord/Pushover/...) without
    touching Scheduler or __main__.py again.
    """

    def __init__(self, notifiers: list[EventNotifier]):
        self._notifiers = notifiers

    async def _fan_out(self, call_name: str, *args: Any, **kwargs: Any) -> None:
        for notifier in self._notifiers:
            try:
                await getattr(notifier, call_name)(*args, **kwargs)
            except Exception:
                logger.exception(
                    "Notifier %r failed handling %s (non-fatal, other notifiers unaffected)",
                    notifier,
                    call_name,
                )

    async def notify_success(self, stats: CycleStats) -> None:
        await self._fan_out("notify_success", stats)

    async def notify_success_with_changes(self, stats: CycleStats) -> None:
        await self._fan_out("notify_success_with_changes", stats)

    async def notify_error(self, message: str, *, auth_required: bool) -> None:
        await self._fan_out("notify_error", message, auth_required=auth_required)
