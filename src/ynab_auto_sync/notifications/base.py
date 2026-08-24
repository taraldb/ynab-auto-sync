from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Self


@dataclass(frozen=True)
class Command:
    """A control message from an operator: 'sync_now' (fire an out-of-cycle
    sync) or 'pause' (payload 'ON'/'OFF'). Transport-agnostic - MqttSink
    derives these from MQTT command-topic messages, but nothing downstream
    of the sink needs to know that."""

    name: str
    payload: str


class NotificationSink(ABC):
    """Abstraction over "publish status / consume commands", so the
    Scheduler can drive either a real MQTT connection (MqttSink) or nothing
    at all (NullSink) through the exact same interface, with no
    `if sink is None` branches anywhere in the scheduler.

    Used as an async context manager for the sink's whole lifetime (connect
    on enter, clean disconnect on exit); publish_* methods are best-effort -
    a sink that can't currently deliver a value (not connected, disabled)
    simply drops it rather than raising, since notification delivery is
    never allowed to affect sync correctness.
    """

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @abstractmethod
    async def publish_availability(self, online: bool) -> None: ...

    @abstractmethod
    async def publish_sync_state(self, value: str) -> None: ...

    @abstractmethod
    async def publish_status(self, run_metadata: dict[str, Any]) -> None: ...

    @abstractmethod
    async def publish_state_value(self, name: str, value: Any) -> None: ...

    async def publish_progress(self, phase: str, **context: Any) -> None:
        """Optional, finer-grained than publish_sync_state: reports a
        sub-step within a running cycle (e.g. phase="fetching",
        provider="sparebank1"), for a UI that wants live in-cycle progress
        rather than just before/after. Concrete with a no-op default -
        deliberately NOT abstract - so existing sinks (MqttSink, NullSink)
        and existing test doubles need no changes; only a sink that
        actually has somewhere to put this (WebSocketSink) overrides it.
        Same best-effort contract as every other publish_*: never allowed
        to raise, never a correctness dependency for the sync itself - see
        scheduler.py's _report_progress, which calls this and relies on it
        never propagating.
        """
        return

    @abstractmethod
    def commands(self) -> AsyncIterator[Command]: ...
