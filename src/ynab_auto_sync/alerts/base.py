from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class CycleStats:
    """Counters from one completed sync cycle, shared by both success
    notification methods below so implementations don't repeat five
    positional params across two methods. Mirrors the fields already
    tracked in run_metadata / CycleResult (see sync/engine.py, state_db.py)
    - this is a read-only projection of those, not a new source of truth."""

    fetched: int
    created: int
    updated: int
    duplicates: int
    resolved_deleted: int


class EventNotifier(ABC):
    """Abstraction over "push a discrete per-cycle event somewhere a human
    will see it" (a phone, a chat channel, ...) - deliberately separate
    from notifications.NotificationSink, which is a different concern:
    continuous state (sync_state/status/state_value/progress) for a
    dashboard or Home Assistant, not discrete events. NotificationSink
    stays exactly as it is; this is a parallel seam, not a replacement.

    Three explicit methods rather than one generic notify(event) dispatch,
    matching NotificationSink's own style - and matching exactly the three
    independently-toggleable cases a provider config exposes (see
    config.NtfyConfig's notify_on_* flags): plain success, success with
    changes, and error.

    Same contract as NotificationSink's publish_*: best-effort, must NEVER
    raise - notification delivery can never be allowed to affect sync
    correctness. Unlike NotificationSink, this is each implementation's own
    responsibility to enforce (e.g. NtfySink catches its own httpx errors)
    rather than something a shared wrapper enforces on every call.
    """

    @abstractmethod
    async def notify_success(self, stats: CycleStats) -> None: ...

    @abstractmethod
    async def notify_success_with_changes(self, stats: CycleStats) -> None: ...

    @abstractmethod
    async def notify_error(self, message: str, *, auth_required: bool) -> None: ...
