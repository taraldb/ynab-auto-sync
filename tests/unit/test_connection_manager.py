from ynab_auto_sync.webapp.connection_manager import ConnectionManager


class FakeWebSocket:
    """Minimal stand-in for a Starlette WebSocket - just enough surface for
    ConnectionManager.broadcast() to exercise send_json() and, for one
    connection, fail like a genuinely dead client would."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.received: list[dict] = []

    async def send_json(self, message: dict) -> None:
        if self.fail:
            raise RuntimeError("simulated dead connection")
        self.received.append(message)


async def test_broadcast_reaches_every_registered_connection():
    manager = ConnectionManager()
    a, b = FakeWebSocket(), FakeWebSocket()
    manager.register(a)
    manager.register(b)

    await manager.broadcast({"type": "status", "data": {"ok": True}})

    assert a.received == [{"type": "status", "data": {"ok": True}}]
    assert b.received == [{"type": "status", "data": {"ok": True}}]


async def test_unregister_stops_further_broadcasts():
    manager = ConnectionManager()
    ws = FakeWebSocket()
    manager.register(ws)
    manager.unregister(ws)

    await manager.broadcast({"type": "status", "data": {}})

    assert ws.received == []


async def test_one_dead_connection_does_not_block_delivery_to_others():
    # The whole point of catching per-connection - a client that closed
    # their tab (or whose send() otherwise raises) must never prevent every
    # OTHER connected client from getting the update.
    manager = ConnectionManager()
    alive = FakeWebSocket()
    dead = FakeWebSocket(fail=True)
    manager.register(alive)
    manager.register(dead)

    await manager.broadcast({"type": "status", "data": {}})

    assert alive.received == [{"type": "status", "data": {}}]

    # The dead connection is dropped from the registry, not just skipped -
    # a second broadcast must not even attempt to send to it again.
    dead.fail = False
    await manager.broadcast({"type": "status", "data": {"second": True}})
    assert dead.received == []
