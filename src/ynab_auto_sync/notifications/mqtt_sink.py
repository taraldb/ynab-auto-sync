from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import aiomqtt

from ynab_auto_sync.config import MqttConfig
from ynab_auto_sync.mqtt import client as mqtt_client
from ynab_auto_sync.mqtt import topics
from ynab_auto_sync.notifications.base import Command, NotificationSink

logger = logging.getLogger(__name__)

RECONNECT_BACKOFF_SECONDS = 15


class MqttSink(NotificationSink):
    """NotificationSink backed by a real MQTT broker connection. Wraps the
    existing mqtt/client.py, mqtt/topics.py, mqtt/discovery.py helpers
    verbatim - this class owns connection lifecycle only, not payload
    shape.

    All connection management (initial connect, subscribing to the two
    command topics, and the reconnect-with-backoff loop) lives inside
    `commands()`, since that's the one coroutine the Scheduler keeps
    running for this sink's entire lifetime (mirroring the old
    `_listen_commands` background task). Discovery is republished on every
    reconnect, not just once - see the comment in mqtt/client.py for why
    (a broker that doesn't persist retained messages across its own
    restart). publish_* methods are best-effort: if there's currently no
    live connection (not yet connected, or mid-reconnect), the update is
    simply dropped rather than raising - MQTT delivery is observability,
    never a correctness dependency for the sync itself.
    """

    def __init__(self, config: MqttConfig):
        self._config = config
        self._client: aiomqtt.Client | None = None

    async def __aexit__(self, *exc: object) -> None:
        # Best-effort only - if `commands()` is still running as a
        # cancelled background task, its own finally block (below) is what
        # actually publishes "offline" and closes the connection; this
        # covers the case where the sink was entered but `commands()` was
        # never consumed.
        if self._client is not None:
            with contextlib.suppress(aiomqtt.MqttError):
                await mqtt_client.publish_availability(self._client, self._config, False)
        self._client = None

    async def publish_availability(self, online: bool) -> None:
        await self._publish(lambda c: mqtt_client.publish_availability(c, self._config, online))

    async def publish_sync_state(self, value: str) -> None:
        await self._publish(lambda c: mqtt_client.publish_sync_state(c, self._config, value))

    async def publish_status(self, run_metadata: dict[str, Any]) -> None:
        await self._publish(
            lambda c: mqtt_client.publish_state_snapshot(c, self._config, run_metadata)
        )

    async def publish_state_value(self, name: str, value: Any) -> None:
        await self._publish(lambda c: mqtt_client.publish_state_value(c, self._config, name, value))

    async def _publish(self, call) -> None:
        client = self._client
        if client is None:
            return
        try:
            await call(client)
        except aiomqtt.MqttError:
            logger.warning("MQTT publish failed (connection currently down) - dropping this update")

    async def commands(self) -> AsyncIterator[Command]:
        sync_now_topic = topics.command_topic(self._config.base_topic, "sync_now")
        pause_topic = topics.command_topic(self._config.base_topic, "pause")

        while True:
            try:
                async with mqtt_client.make_client(self._config) as client:
                    self._client = client
                    try:
                        await mqtt_client.publish_availability(client, self._config, True)
                        await mqtt_client.publish_discovery(client, self._config)
                        await client.subscribe(sync_now_topic)
                        await client.subscribe(pause_topic)

                        async for message in client.messages:
                            topic_str = str(message.topic)
                            if topic_str not in (sync_now_topic, pause_topic):
                                continue
                            payload = message.payload
                            if isinstance(payload, (bytes, bytearray)):
                                payload = payload.decode(errors="replace")
                            else:
                                payload = str(payload)
                            name = "sync_now" if topic_str == sync_now_topic else "pause"
                            yield Command(name=name, payload=payload)
                    finally:
                        # Runs on both a clean cancellation (scheduler
                        # shutdown) and a broken connection - suppress in
                        # the latter case since the client is already gone.
                        with contextlib.suppress(aiomqtt.MqttError):
                            await mqtt_client.publish_availability(client, self._config, False)
            except aiomqtt.MqttError as e:
                logger.warning(
                    "MQTT connection error: %s - reconnecting in %ds", e, RECONNECT_BACKOFF_SECONDS
                )
                self._client = None
                await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)
            finally:
                self._client = None
