from pathlib import Path

import pytest
from pydantic import ValidationError

from ynab_auto_sync.config import NtfyConfig, load_config

VALID_YAML = """
providers:
  sparebank1:
    type: sparebank1
    client_id: "cid"
    client_secret: "secret"
    redirect_uri: "http://localhost:8765/callback"

ynab:
  personal_access_token: "pat"
  budgets:
    personal: "budget-1"
    shared: "budget-2"

accounts:
  - sparebank1_account_key: "acct-key"
    ynab_account_id: "ynab-acct-1"
    ynab_budget: "personal"
    display_name: "Brukskonto"

mqtt:
  host: "mqtt.local"
"""


def test_load_config_parses_valid_yaml(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(VALID_YAML)

    config = load_config(config_path)

    assert config.providers["sparebank1"].client_id == "cid"
    assert config.providers["sparebank1"].type == "sparebank1"
    assert config.providers["sparebank1"].enabled is True
    assert config.ynab.budgets == {"personal": "budget-1", "shared": "budget-2"}
    assert config.accounts[0].sparebank1_account_key == "acct-key"
    assert config.accounts[0].ynab_budget == "personal"
    assert config.mqtt.host == "mqtt.local"
    # defaults
    assert config.sync.cron_expression == "0 6,8,10,12,16,20 * * *"
    assert config.sync.retention_days == 270
    assert config.gui.enabled is True
    assert config.gui.port == 8080
    assert config.mqtt.port == 1883
    assert config.mqtt.discovery_prefix == "homeassistant"


def test_load_config_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_load_config_unknown_budget_alias_raises(tmp_path: Path):
    yaml_text = """
providers:
  sparebank1:
    type: sparebank1
    client_id: "cid"
    client_secret: "secret"
    redirect_uri: "http://localhost:8765/callback"

ynab:
  personal_access_token: "pat"
  budgets:
    personal: "budget-1"

accounts:
  - sparebank1_account_key: "acct-key"
    ynab_account_id: "ynab-acct-1"
    ynab_budget: "personl"
    display_name: "Brukskonto"

mqtt:
  host: "mqtt.local"
"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text)

    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "personl" in message
    assert "personal" in message


def test_load_config_duplicate_account_key_is_no_longer_a_config_error(tmp_path: Path):
    yaml_text = """
providers:
  sparebank1:
    type: sparebank1
    client_id: "cid"
    client_secret: "secret"
    redirect_uri: "http://localhost:8765/callback"

ynab:
  personal_access_token: "pat"
  budgets:
    personal: "budget-1"

accounts:
  - sparebank1_account_key: "acct-key"
    ynab_account_id: "ynab-acct-1"
    ynab_budget: "personal"
    display_name: "Brukskonto"
  - sparebank1_account_key: "acct-key"
    ynab_account_id: "ynab-acct-2"
    ynab_budget: "personal"
    display_name: "Sparekonto"

mqtt:
  host: "mqtt.local"
"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text)

    # Duplicate account keys are NO LONGER a config-load error. Mappings
    # moved into the account_mappings table where the GUI can edit them, so
    # a startup-only check would be trivially bypassed; the invariant is now
    # enforced on every write by StateDB.create_mapping/update_mapping
    # (MappingValidationError -> HTTP 422). See test_state_db.py's mapping
    # validation tests and the 422 cases in test_webapp.py. config.yaml's
    # `accounts:` list is only ever a one-time seed now.
    config = load_config(config_path)
    assert len(config.accounts) == 2


def test_load_config_multiple_budgets_resolve_correctly(tmp_path: Path):
    yaml_text = """
providers:
  sparebank1:
    type: sparebank1
    client_id: "cid"
    client_secret: "secret"
    redirect_uri: "http://localhost:8765/callback"

ynab:
  personal_access_token: "pat"
  budgets:
    personal: "budget-1"
    shared: "budget-2"

accounts:
  - sparebank1_account_key: "acct-key-1"
    ynab_account_id: "ynab-acct-1"
    ynab_budget: "personal"
    display_name: "Brukskonto"
  - sparebank1_account_key: "acct-key-2"
    ynab_account_id: "ynab-acct-2"
    ynab_budget: "shared"
    display_name: "Felleskonto"

mqtt:
  host: "mqtt.local"
"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text)

    config = load_config(config_path)

    assert config.accounts[0].ynab_budget == "personal"
    assert config.accounts[1].ynab_budget == "shared"


def test_load_config_duplicate_import_source_name_is_no_longer_a_config_error(tmp_path: Path):
    yaml_text = """
providers:
  sparebank1:
    type: sparebank1
    client_id: "cid"
    client_secret: "secret"
    redirect_uri: "http://localhost:8765/callback"

ynab:
  personal_access_token: "pat"
  budgets:
    personal: "budget-1"

accounts:
  - sparebank1_account_key: "acct-key-1"
    ynab_account_id: "ynab-acct-1"
    ynab_budget: "personal"
    import_source_name: "norwegian_bank"
  - sparebank1_account_key: "acct-key-2"
    ynab_account_id: "ynab-acct-2"
    ynab_budget: "personal"
    import_source_name: "norwegian_bank"

mqtt:
  host: "mqtt.local"
"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text)

    # Same migration as above: duplicate import_source_name is now rejected
    # at mapping-write time, not config-load time.
    config = load_config(config_path)
    assert [a.import_source_name for a in config.accounts] == [
        "norwegian_bank",
        "norwegian_bank",
    ]


def test_load_config_blank_import_source_name_not_treated_as_duplicate(tmp_path: Path):
    yaml_text = """
providers:
  sparebank1:
    type: sparebank1
    client_id: "cid"
    client_secret: "secret"
    redirect_uri: "http://localhost:8765/callback"

ynab:
  personal_access_token: "pat"
  budgets:
    personal: "budget-1"

accounts:
  - sparebank1_account_key: "acct-key-1"
    ynab_account_id: "ynab-acct-1"
    ynab_budget: "personal"
  - sparebank1_account_key: "acct-key-2"
    ynab_account_id: "ynab-acct-2"
    ynab_budget: "personal"

mqtt:
  host: "mqtt.local"
"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text)

    config = load_config(config_path)

    assert config.accounts[0].import_source_name == ""
    assert config.accounts[1].import_source_name == ""


def test_load_config_invalid_cron_expression_raises(tmp_path: Path):
    yaml_text = """
providers:
  sparebank1:
    type: sparebank1
    client_id: "cid"
    client_secret: "secret"
    redirect_uri: "http://localhost:8765/callback"

ynab:
  personal_access_token: "pat"
  budgets:
    personal: "budget-1"

sync:
  cron_expression: "not a cron expression"

mqtt:
  host: "mqtt.local"
"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text)

    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)

    assert "cron_expression" in str(exc_info.value)


def test_load_config_notifications_section_absent_is_none(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(VALID_YAML)

    config = load_config(config_path)

    assert config.notifications is None


def test_load_config_parses_ntfy_section(tmp_path: Path):
    yaml_text = (
        VALID_YAML
        + """
notifications:
  ntfy:
    topic: "my-secret-topic"
    notify_on_success: true
"""
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text)

    config = load_config(config_path)

    assert config.notifications is not None
    assert config.notifications.ntfy is not None
    assert config.notifications.ntfy.topic == "my-secret-topic"
    assert config.notifications.ntfy.notify_on_success is True
    # defaults
    assert config.notifications.ntfy.enabled is True
    assert config.notifications.ntfy.server == "https://ntfy.sh"
    assert config.notifications.ntfy.notify_on_success_with_changes is True
    assert config.notifications.ntfy.notify_on_error is True


def test_ntfy_config_requires_topic():
    with pytest.raises(ValidationError):
        NtfyConfig()


def test_load_config_retention_days_below_fetch_horizon_raises(tmp_path: Path):
    yaml_text = """
providers:
  sparebank1:
    type: sparebank1
    client_id: "cid"
    client_secret: "secret"
    redirect_uri: "http://localhost:8765/callback"

ynab:
  personal_access_token: "pat"
  budgets:
    personal: "budget-1"

sync:
  initial_backfill_days: 30
  lookback_overlap_hours: 72
  retention_days: 10

mqtt:
  host: "mqtt.local"
"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text)

    with pytest.raises(ValidationError) as exc_info:
        load_config(config_path)

    assert "retention_days" in str(exc_info.value)
