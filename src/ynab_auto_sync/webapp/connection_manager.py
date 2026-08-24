from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Registry of live websocket connections for the GUI's push-update
    channel (routes/ws.py). Transport-layer only - owns Starlette
    `WebSocket` objects and nothing about what gets sent; the payload shape
    is entirely notifications/websocket_sink.py's concern, mirroring how
    mqtt/client.py owns MQTT payload shape while MqttSink owns connection
    lifecycle.
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    def register(self, websocket: WebSocket) -> None:
        self._connections.add(websocket)

    def unregister(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        # Iterate a snapshot: a connection that disconnects mid-broadcast
        # must not mutate the set out from under this loop, and one
        # connection's send failure must never block delivery to the
        # others - this is observability, the same "best-effort, never a
        # correctness dependency" discipline notifications/base.py already
        # documents for every NotificationSink.
        for websocket in list(self._connections):
            try:
                await websocket.send_json(message)
            except Exception:
                logger.exception("Dropping dead websocket connection during broadcast")
                self._connections.discard(websocket)
