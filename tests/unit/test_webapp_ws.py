from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.unit.test_webapp import make_client
from ynab_auto_sync.notifications.websocket_sink import WebSocketSink
from ynab_auto_sync.webapp.connection_manager import ConnectionManager


def test_websocket_connect_sends_status_snapshot_matching_rest_endpoint(tmp_path: Path):
    manager = ConnectionManager()
    client, _db = make_client(tmp_path, ws_manager=manager)

    rest_status = client.get("/api/status").json()

    with client.websocket_connect("/api/ws") as ws:
        message = ws.receive_json()

    assert message["type"] == "status_snapshot"
    assert message["data"] == rest_status


def test_websocket_receives_broadcasts_from_a_driven_sink(tmp_path: Path):
    # Entering the TestClient itself (not just websocket_connect) is what
    # keeps a real anyio BlockingPortal alive on `client.portal` for the
    # whole block - only then can code running in this (main) thread call
    # into the same event loop the websocket connection is actually being
    # served on, to simulate "some other part of the app broadcasts a
    # message while a client is connected."
    manager = ConnectionManager()
    client, _db = make_client(tmp_path, ws_manager=manager)
    sink = WebSocketSink(manager)

    with client, client.websocket_connect("/api/ws") as ws:
        ws.receive_json()  # the initial status_snapshot - not under test here

        async def _publish_progress() -> None:
            await sink.publish_progress("fetching", provider="sparebank1")

        async def _publish_status() -> None:
            await sink.publish_status({"last_error": None})

        client.portal.call(_publish_progress)
        progress_message = ws.receive_json()

        client.portal.call(_publish_status)
        status_message = ws.receive_json()

    assert progress_message == {
        "type": "cycle_progress",
        "data": {"phase": "fetching", "provider": "sparebank1"},
    }
    assert status_message == {"type": "status", "data": {"run_metadata": {"last_error": None}}}


def test_websocket_closes_cleanly_without_a_ws_manager(tmp_path: Path):
    # Every pre-existing app-construction path in test_webapp.py builds
    # without a ws_manager (create_app's kwarg is optional) - a websocket
    # connection attempt against one of those apps must be refused
    # cleanly, never crash the app.
    client, _db = make_client(tmp_path)  # no ws_manager

    # The server closes the connection during accept (routes/ws.py's
    # manager-is-None branch) - the test client surfaces that as a
    # WebSocketDisconnect rather than hanging or 500ing the whole app.
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/ws"):
        pass
