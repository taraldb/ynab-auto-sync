from __future__ import annotations

from dataclasses import dataclass

from ynab_auto_sync.config import MqttConfig
from ynab_auto_sync.mqtt import topics

DEVICE_IDENTIFIER = "ynab_auto_sync"


@dataclass(frozen=True)
class EntityDescriptor:
    object_id: str
    component: str  # sensor | binary_sensor | switch | button
    name: str
    state_name: str | None = None  # bare name, passed through topics.state_topic()
    command_name: str | None = None  # bare name, passed through topics.command_topic()
    device_class: str | None = None
    state_class: str | None = None
    entity_category: str | None = None


ENTITIES: tuple[EntityDescriptor, ...] = (
    EntityDescriptor("sync_state", "sensor", "Sync State", state_name="sync_state"),
    EntityDescriptor(
        "last_run_at", "sensor", "Last Run", state_name="last_run_at", device_class="timestamp"
    ),
    EntityDescriptor(
        "last_success_at",
        "sensor",
        "Last Successful Sync",
        state_name="last_success_at",
        device_class="timestamp",
    ),
    EntityDescriptor(
        "fetched_last_run",
        "sensor",
        "Fetched (Last Run)",
        state_name="fetched_last_run",
        state_class="measurement",
    ),
    EntityDescriptor(
        "imported_last_run",
        "sensor",
        "Imported (Last Run)",
        state_name="imported_last_run",
        state_class="measurement",
    ),
    EntityDescriptor(
        "updated_last_run",
        "sensor",
        "Updated to Cleared (Last Run)",
        state_name="updated_last_run",
        state_class="measurement",
    ),
    EntityDescriptor(
        "duplicates_last_run",
        "sensor",
        "Duplicates Skipped (Last Run)",
        state_name="duplicates_last_run",
        state_class="measurement",
    ),
    EntityDescriptor(
        "resolved_deleted_last_run",
        "sensor",
        "Resolved as Deleted (Last Run)",
        state_name="resolved_deleted_last_run",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    EntityDescriptor(
        "imported_total",
        "sensor",
        "Imported (Total)",
        state_name="imported_total",
        state_class="total_increasing",
    ),
    EntityDescriptor(
        "error_message",
        "sensor",
        "Last Error",
        state_name="error_message",
        entity_category="diagnostic",
    ),
    EntityDescriptor(
        "problem", "binary_sensor", "Problem", state_name="problem", device_class="problem"
    ),
    EntityDescriptor(
        "auth_required",
        "binary_sensor",
        "Auth Required",
        state_name="auth_required",
        device_class="problem",
    ),
    EntityDescriptor(
        "pause",
        "switch",
        "Sync Paused",
        state_name="paused",
        command_name="pause",
    ),
    EntityDescriptor("sync_now", "button", "Sync Now", command_name="sync_now"),
)


def build_discovery_payload(entity: EntityDescriptor, config: MqttConfig) -> tuple[str, dict]:
    device = {
        "identifiers": [DEVICE_IDENTIFIER],
        "name": "YNAB Auto Sync",
        "manufacturer": "ynab-auto-sync",
    }
    payload: dict = {
        "name": entity.name,
        "unique_id": f"{DEVICE_IDENTIFIER}_{entity.object_id}",
        "device": device,
        "availability_topic": topics.status_topic(config.base_topic),
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    if entity.state_name:
        payload["state_topic"] = topics.state_topic(config.base_topic, entity.state_name)
    if entity.command_name:
        payload["command_topic"] = topics.command_topic(config.base_topic, entity.command_name)
    if entity.device_class:
        payload["device_class"] = entity.device_class
    if entity.state_class:
        payload["state_class"] = entity.state_class
    if entity.entity_category:
        payload["entity_category"] = entity.entity_category
    if entity.component == "button":
        payload["payload_press"] = "PRESS"

    discovery_topic = (
        f"{config.discovery_prefix}/{entity.component}/{DEVICE_IDENTIFIER}/{entity.object_id}/config"
    )
    return discovery_topic, payload
