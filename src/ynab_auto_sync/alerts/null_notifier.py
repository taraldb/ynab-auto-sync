from __future__ import annotations

from ynab_auto_sync.alerts.base import CycleStats, EventNotifier


class NullNotifier(EventNotifier):
    """No-op EventNotifier used when no notification provider is configured
    (config.notifications is None, or its provider sections are all
    absent/disabled). Every method is a no-op - mirrors
    notifications.NullSink's role for the state-sink seam."""

    async def notify_success(self, stats: CycleStats) -> None:
        return None

    async def notify_success_with_changes(self, stats: CycleStats) -> None:
        return None

    async def notify_error(self, message: str, *, auth_required: bool) -> None:
        return None
