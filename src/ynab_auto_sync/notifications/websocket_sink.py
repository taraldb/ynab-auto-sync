from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from ynab_auto_sync.notifications.base import Command, NotificationSink
from ynab_auto_sync.webapp.connection_manager import ConnectionManager


class WebSocketSink(NotificationSink):
    """NotificationSink that broadcasts every publish to every currently
    connected GUI websocket client, via ConnectionManager - structurally
    like NullSink except each publish_* does one broadcast() instead of
    nothing. Owns no connection lifecycle of its own (routes/ws.py handles
    accept/register/unregister per client); this class only ever writes to
    an already-built ConnectionManager it's handed at construction time.

    commands() parks forever, like NullSink - websocket clients don't
    originate commands in this design (sync-now/pause already have direct
    REST routes); this keeps the class ready to be wrapped in CompositeSink
    alongside MqttSink/NullSink without special-casing.
    """

    def __init__(self, manager: ConnectionManager):
        self._manager = manager

    async def publish_availability(self, online: bool) -> None:
        await self._manager.broadcast(
            {"type": "availability", "data": {"online": online}}
        )

    async def publish_sync_state(self, value: str) -> None:
        await self._manager.broadcast({"type": "sync_state", "data": {"value": value}})

    async def publish_status(self, run_metadata: dict[str, Any]) -> None:
        await self._manager.broadcast(
            {"type": "status", "data": {"run_metadata": run_metadata}}
        )

    async def publish_state_value(self, name: str, value: Any) -> None:
        await self._manager.broadcast(
            {"type": "state_value", "data": {"name": name, "value": value}}
        )

    async def publish_progress(self, phase: str, **context: Any) -> None:
        await self._manager.broadcast(
            {"type": "cycle_progress", "data": {"phase": phase, **context}}
        )

    async def commands(self) -> AsyncIterator[Command]:
        await asyncio.Event().wait()
        return
        yield  # pragma: no cover - unreachable; makes this an async generator
