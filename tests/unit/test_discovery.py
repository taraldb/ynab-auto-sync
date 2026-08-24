from ynab_auto_sync.config import MqttConfig
from ynab_auto_sync.mqtt.discovery import ENTITIES, build_discovery_payload


def make_config() -> MqttConfig:
    return MqttConfig(host="mqtt.local", base_topic="ynab_auto_sync", discovery_prefix="homeassistant")


def test_sensor_entity_has_state_topic_and_availability():
    config = make_config()
    entity = next(e for e in ENTITIES if e.object_id == "sync_state")
    topic, payload = build_discovery_payload(entity, config)

    assert topic == "homeassistant/sensor/ynab_auto_sync/sync_state/config"
    assert payload["state_topic"] == "ynab_auto_sync/state/sync_state"
    assert payload["availability_topic"] == "ynab_auto_sync/status"
    assert payload["unique_id"] == "ynab_auto_sync_sync_state"


def test_button_entity_has_command_topic_and_payload_press():
    config = make_config()
    entity = next(e for e in ENTITIES if e.object_id == "sync_now")
    topic, payload = build_discovery_payload(entity, config)

    assert topic == "homeassistant/button/ynab_auto_sync/sync_now/config"
    assert payload["command_topic"] == "ynab_auto_sync/command/sync_now"
    assert payload["payload_press"] == "PRESS"
    assert "state_topic" not in payload


def test_switch_entity_has_both_state_and_command_topic():
    config = make_config()
    entity = next(e for e in ENTITIES if e.object_id == "pause")
    _topic, payload = build_discovery_payload(entity, config)

    assert payload["state_topic"] == "ynab_auto_sync/state/paused"
    assert payload["command_topic"] == "ynab_auto_sync/command/pause"


def test_all_entities_share_one_device_identifier():
    config = make_config()
    identifiers = {
        build_discovery_payload(e, config)[1]["device"]["identifiers"][0] for e in ENTITIES
    }
    assert identifiers == {"ynab_auto_sync"}


def test_all_entities_produce_unique_discovery_topics():
    config = make_config()
    topics = [build_discovery_payload(e, config)[0] for e in ENTITIES]
    assert len(topics) == len(set(topics))


def test_fetched_last_run_entity_is_registered():
    config = make_config()
    entity = next(e for e in ENTITIES if e.object_id == "fetched_last_run")
    _topic, payload = build_discovery_payload(entity, config)

    assert payload["state_topic"] == "ynab_auto_sync/state/fetched_last_run"
    assert payload["state_class"] == "measurement"
