from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, Self

from ynab_auto_sync.notifications.base import Command, NotificationSink

logger = logging.getLogger(__name__)


class CompositeSink(NotificationSink):
    """Fans out to several NotificationSinks at once - needed because MQTT
    and the GUI websocket are two independent, both-optional broadcast
    concerns (config.mqtt.enabled vs config.gui.enabled), and the Scheduler
    only ever holds one `sink`. Every publish_* call is isolated per child:
    one child raising is logged and never allowed to stop delivery to the
    others, or to propagate up to the Scheduler - the same best-effort
    discipline every individual sink already applies to its own publishes,
    just applied one layer up so a defect in, say, WebSocketSink can never
    take down MQTT delivery or vice versa.
    """

    def __init__(self, sinks: list[NotificationSink]):
        self._sinks = sinks

    async def __aenter__(self) -> Self:
        await asyncio.gather(*(sink.__aenter__() for sink in self._sinks))
        return self

    async def __aexit__(self, *exc: object) -> None:
        results = await asyncio.gather(
            *(sink.__aexit__(*exc) for sink in self._sinks), return_exceptions=True
        )
        for sink, result in zip(self._sinks, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Error closing sink %r (non-fatal): %s", sink, result)

    async def _fan_out(self, call_name: str, *args: Any, **kwargs: Any) -> None:
        for sink in self._sinks:
            try:
                await getattr(sink, call_name)(*args, **kwargs)
            except Exception:
                logger.exception(
                    "Sink %r failed handling %s (non-fatal, other sinks unaffected)",
                    sink,
                    call_name,
                )

    async def publish_availability(self, online: bool) -> None:
        await self._fan_out("publish_availability", online)

    async def publish_sync_state(self, value: str) -> None:
        await self._fan_out("publish_sync_state", value)

    async def publish_status(self, run_metadata: dict[str, Any]) -> None:
        await self._fan_out("publish_status", run_metadata)

    async def publish_state_value(self, name: str, value: Any) -> None:
        await self._fan_out("publish_state_value", name, value)

    async def publish_progress(self, phase: str, **context: Any) -> None:
        await self._fan_out("publish_progress", phase, **context)

    async def commands(self) -> AsyncIterator[Command]:
        # Merge every child's command stream into one, via a shared queue
        # fed by one pump task per child. Today only ever one child
        # (MqttSink, or NullSink which never yields) produces real commands
        # - WebSocketSink's commands() also never yields - so behavior for
        # MQTT command handling is unchanged; this exists so a future
        # command-originating sink (e.g. a bidirectional websocket channel)
        # slots in without CompositeSink needing to change again.
        queue: asyncio.Queue[Command] = asyncio.Queue()

        async def pump(sink: NotificationSink) -> None:
            async for command in sink.commands():
                await queue.put(command)

        pump_tasks = {asyncio.create_task(pump(sink)) for sink in self._sinks}
        try:
            while True:
                get_task = asyncio.create_task(queue.get())
                done, _pending = await asyncio.wait(
                    {get_task, *pump_tasks}, return_when=asyncio.FIRST_COMPLETED
                )
                if get_task in done:
                    yield get_task.result()
                else:
                    get_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await get_task
                # A finished pump task means that child's commands()
                # generator ended (or raised) - either way it's done
                # contributing. Retrieve its result/exception so asyncio
                # never logs an "exception was never retrieved" warning,
                # and log it ourselves (non-fatal, same isolation as
                # _fan_out: one child dying must never stop the others).
                finished = {t for t in pump_tasks if t.done()}
                for task in finished:
                    if task.exception() is not None:
                        logger.error(
                            "A sink's commands() stream ended with an error "
                            "(non-fatal, other sinks unaffected)",
                            exc_info=task.exception(),
                        )
                pump_tasks -= finished
                if not pump_tasks and queue.empty():
                    return
        finally:
            for task in pump_tasks:
                task.cancel()
            for task in pump_tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
