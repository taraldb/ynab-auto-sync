from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ynab_auto_sync.webapp.routes.status import build_status_payload

logger = logging.getLogger(__name__)

router = APIRouter()

# Well under a typical reverse-proxy idle timeout (SWAG/nginx commonly
# default to ~60s) - keeps the connection visibly non-idle from the proxy's
# point of view without requiring the user to also raise a timeout setting
# there, which this app has no visibility into or control over.
HEARTBEAT_INTERVAL_SECONDS = 20


@router.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Push-update channel for the GUI dashboard - see
    notifications/websocket_sink.py for what gets broadcast here and
    scheduler.py for when. No auth of its own: inherits the GUI's existing
    no-app-level-auth trust model (the reverse proxy in front of this app
    is the boundary), same as every /api/* REST route.

    Reads app.state directly off `websocket` rather than via
    webapp/deps.py's `Depends(get_config)`-style getters: those are typed
    for an HTTP `Request` and FastAPI does not resolve them for a
    WebSocket connection (confirmed - it raises a "missing required
    positional argument" TypeError at connection time, not a clean 4xx).
    """
    config = websocket.app.state.config
    db = websocket.app.state.db
    manager = websocket.app.state.ws_manager
    if manager is None:
        # No app.state.ws_manager wired in (e.g. config.gui.enabled=False,
        # or a test app built without one) - refuse rather than accepting a
        # connection that would never receive a single broadcast.
        await websocket.close(code=1011)
        return

    await websocket.accept()
    manager.register(websocket)
    try:
        await websocket.send_json(
            {"type": "status_snapshot", "data": build_status_payload(config, db)}
        )
        while True:
            try:
                await asyncio.wait_for(
                    websocket.receive_text(), timeout=HEARTBEAT_INTERVAL_SECONDS
                )
            except TimeoutError:
                await websocket.send_json({"type": "ping", "data": {}})
    except WebSocketDisconnect:
        pass
    finally:
        manager.unregister(websocket)
