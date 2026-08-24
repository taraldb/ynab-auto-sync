from ynab_auto_sync.alerts.base import CycleStats, EventNotifier
from ynab_auto_sync.alerts.composite_notifier import CompositeNotifier

STATS = CycleStats(fetched=3, created=2, updated=1, duplicates=0, resolved_deleted=0)


class RecordingNotifier(EventNotifier):
    """An EventNotifier double that records every call and can optionally
    raise on a chosen method - enough surface to exercise
    CompositeNotifier's fan-out and isolation without a real NtfySink."""

    def __init__(self, *, raise_on: str | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._raise_on = raise_on

    async def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))
        if name == self._raise_on:
            raise RuntimeError(f"simulated failure in {name}")

    async def notify_success(self, stats: CycleStats) -> None:
        await self._record("notify_success", stats)

    async def notify_success_with_changes(self, stats: CycleStats) -> None:
        await self._record("notify_success_with_changes", stats)

    async def notify_error(self, message: str, *, auth_required: bool) -> None:
        await self._record("notify_error", message, auth_required=auth_required)


async def test_notify_fans_out_to_every_child():
    a, b = RecordingNotifier(), RecordingNotifier()
    notifier = CompositeNotifier([a, b])

    await notifier.notify_success_with_changes(STATS)

    assert ("notify_success_with_changes", (STATS,), {}) in a.calls
    assert ("notify_success_with_changes", (STATS,), {}) in b.calls


async def test_one_child_raising_does_not_stop_delivery_to_others():
    failing = RecordingNotifier(raise_on="notify_error")
    healthy = RecordingNotifier()
    notifier = CompositeNotifier([failing, healthy])

    await notifier.notify_error("boom", auth_required=False)  # must not raise

    assert ("notify_error", ("boom",), {"auth_required": False}) in failing.calls
    assert ("notify_error", ("boom",), {"auth_required": False}) in healthy.calls
