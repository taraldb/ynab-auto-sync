from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from ynab_auto_sync.notifications.base import Command, NotificationSink


class NullSink(NotificationSink):
    """No-op NotificationSink used when MQTT is absent or disabled
    (`config.mqtt is None` or `config.mqtt.enabled is False`). Every publish
    is a no-op; `commands()` never yields - it just waits to be cancelled,
    so the Scheduler's command-consuming task parks cleanly instead of
    spinning or busy-polling."""

    async def publish_availability(self, online: bool) -> None:
        return None

    async def publish_sync_state(self, value: str) -> None:
        return None

    async def publish_status(self, run_metadata: dict[str, Any]) -> None:
        return None

    async def publish_state_value(self, name: str, value: Any) -> None:
        return None

    async def commands(self) -> AsyncIterator[Command]:
        # Blocks until the caller cancels us (e.g. scheduler shutdown) -
        # never wakes up on its own, never yields anything.
        await asyncio.Event().wait()
        return
        yield  # pragma: no cover - unreachable; makes this an async generator
