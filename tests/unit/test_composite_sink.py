import asyncio

from ynab_auto_sync.notifications.base import Command, NotificationSink
from ynab_auto_sync.notifications.composite_sink import CompositeSink


class RecordingSink(NotificationSink):
    """A NotificationSink double that records every publish call and can
    optionally raise on a chosen method, and/or yield a fixed list of
    Commands then park - enough surface to exercise CompositeSink's
    fan-out and isolation without needing a real MqttSink/WebSocketSink."""

    def __init__(self, *, raise_on: str | None = None, commands: list[Command] | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.entered = False
        self.exited = False
        self._raise_on = raise_on
        self._commands = commands or []
        self._parked = asyncio.Event()

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    async def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))
        if name == self._raise_on:
            raise RuntimeError(f"simulated failure in {name}")

    async def publish_availability(self, online: bool) -> None:
        await self._record("publish_availability", online)

    async def publish_sync_state(self, value: str) -> None:
        await self._record("publish_sync_state", value)

    async def publish_status(self, run_metadata: dict) -> None:
        await self._record("publish_status", run_metadata)

    async def publish_state_value(self, name: str, value) -> None:
        await self._record("publish_state_value", name, value)

    async def publish_progress(self, phase: str, **context) -> None:
        await self._record("publish_progress", phase, **context)

    async def commands(self):
        for command in self._commands:
            yield command
        await self._parked.wait()


async def test_publish_fans_out_to_every_child():
    a, b = RecordingSink(), RecordingSink()
    sink = CompositeSink([a, b])

    await sink.publish_sync_state("running")
    await sink.publish_progress("fetching", provider="sparebank1")

    assert ("publish_sync_state", ("running",), {}) in a.calls
    assert ("publish_sync_state", ("running",), {}) in b.calls
    assert ("publish_progress", ("fetching",), {"provider": "sparebank1"}) in a.calls
    assert ("publish_progress", ("fetching",), {"provider": "sparebank1"}) in b.calls


async def test_one_child_raising_does_not_stop_delivery_to_others():
    failing = RecordingSink(raise_on="publish_status")
    healthy = RecordingSink()
    sink = CompositeSink([failing, healthy])

    await sink.publish_status({"last_error": None})  # must not raise

    assert ("publish_status", ({"last_error": None},), {}) in failing.calls
    assert ("publish_status", ({"last_error": None},), {}) in healthy.calls


async def test_aenter_aexit_fan_out_to_every_child():
    a, b = RecordingSink(), RecordingSink()
    sink = CompositeSink([a, b])

    async with sink:
        assert a.entered
        assert b.entered

    assert a.exited
    assert b.exited


async def test_commands_merges_every_childs_stream():
    a_command = Command(name="sync_now", payload="")
    b_command = Command(name="pause", payload="ON")
    a = RecordingSink(commands=[a_command])
    b = RecordingSink(commands=[b_command])
    sink = CompositeSink([a, b])

    received = []
    gen = sink.commands()
    try:
        async for command in gen:
            received.append(command)
            if len(received) == 2:
                break
    finally:
        # Explicitly close rather than letting GC finalize it - the
        # generator's `finally` block cancels its per-child pump tasks,
        # and we want that to happen deterministically within the test
        # rather than leaving orphaned tasks for the event loop to warn
        # about at teardown.
        await gen.aclose()

    assert a_command in received
    assert b_command in received
