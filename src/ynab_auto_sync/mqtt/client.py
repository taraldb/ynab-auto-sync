from __future__ import annotations

import json
import logging
from typing import Any

import aiomqtt

from ynab_auto_sync.config import MqttConfig
from ynab_auto_sync.mqtt import topics
from ynab_auto_sync.mqtt.discovery import ENTITIES, build_discovery_payload

logger = logging.getLogger(__name__)


def build_will(config: MqttConfig) -> aiomqtt.Will:
    return aiomqtt.Will(
        topic=topics.status_topic(config.base_topic), payload="offline", qos=1, retain=True
    )


def make_client(config: MqttConfig) -> aiomqtt.Client:
    return aiomqtt.Client(
        hostname=config.host,
        port=config.port,
        username=config.username or None,
        password=config.password or None,
        will=build_will(config),
    )


async def publish_availability(client: aiomqtt.Client, config: MqttConfig, online: bool) -> None:
    await client.publish(
        topics.status_topic(config.base_topic),
        payload="online" if online else "offline",
        qos=1,
        retain=True,
    )


async def publish_discovery(client: aiomqtt.Client, config: MqttConfig) -> None:
    # Republished on every (re)connect, not just once at startup - cheap,
    # and guards against a broker that didn't persist retained messages
    # across a broker restart.
    for entity in ENTITIES:
        discovery_topic, payload = build_discovery_payload(entity, config)
        await client.publish(discovery_topic, payload=json.dumps(payload), qos=1, retain=True)
    logger.info("Published %d Home Assistant discovery entities", len(ENTITIES))


async def publish_state_value(
    client: aiomqtt.Client, config: MqttConfig, name: str, value: Any
) -> None:
    await client.publish(topics.state_topic(config.base_topic, name), payload=str(value), qos=1, retain=True)


async def publish_sync_state(client: aiomqtt.Client, config: MqttConfig, value: str) -> None:
    await publish_state_value(client, config, "sync_state", value)


async def publish_state_snapshot(
    client: aiomqtt.Client, config: MqttConfig, run_metadata: dict[str, Any]
) -> None:
    mapping = {
        "last_run_at": run_metadata.get("last_run_at") or "",
        "last_success_at": run_metadata.get("last_success_at") or "",
        "fetched_last_run": run_metadata.get("fetched_last_run", 0),
        "imported_last_run": run_metadata.get("imported_last_run", 0),
        "updated_last_run": run_metadata.get("updated_last_run", 0),
        "duplicates_last_run": run_metadata.get("duplicates_last_run", 0),
        "resolved_deleted_last_run": run_metadata.get("resolved_deleted_last_run", 0),
        "imported_total": run_metadata.get("imported_total", 0),
        "error_message": run_metadata.get("last_error") or "",
        "problem": "ON" if run_metadata.get("last_error") else "OFF",
        "auth_required": "ON" if run_metadata.get("auth_required") else "OFF",
        "paused": "ON" if run_metadata.get("paused") else "OFF",
    }
    for name, value in mapping.items():
        await publish_state_value(client, config, name, value)
