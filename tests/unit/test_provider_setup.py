import json
from pathlib import Path

import pytest

from ynab_auto_sync.config import AppConfig, SpareBank1Config, YnabConfig
from ynab_auto_sync.provider_setup import resolve_sparebank1_provider, token_store_path


def make_config(providers: dict) -> AppConfig:
    return AppConfig(
        providers=providers,
        ynab=YnabConfig(personal_access_token="pat", budgets={"personal": "b-1"}),
    )


def sb1(**overrides) -> SpareBank1Config:
    return SpareBank1Config(
        client_id=overrides.get("client_id", "cid"),
        client_secret="secret",
        redirect_uri="http://localhost/cb",
    )


def test_token_store_path_is_per_provider(tmp_path: Path):
    a = token_store_path(tmp_path, "bank_a")
    b = token_store_path(tmp_path, "bank_b")

    assert a == tmp_path / "tokens" / "bank_a.json"
    assert b == tmp_path / "tokens" / "bank_b.json"
    assert a != b


def test_token_store_path_creates_the_tokens_directory(tmp_path: Path):
    assert not (tmp_path / "tokens").exists()
    token_store_path(tmp_path, "sparebank1")
    assert (tmp_path / "tokens").is_dir()


def test_legacy_tokens_file_is_migrated_not_ignored(tmp_path: Path):
    """The whole point of the migration: an existing install must not be
    pushed back through an interactive BankID login just because tokens
    moved to a per-provider path."""
    legacy = tmp_path / "tokens.json"
    legacy.write_text(json.dumps({"access_token": "keep-me", "refresh_token": "r"}))

    new_path = token_store_path(tmp_path, "sparebank1")

    assert new_path.exists()
    assert json.loads(new_path.read_text())["access_token"] == "keep-me"
    assert not legacy.exists(), "legacy file should be moved, not copied"


def test_migration_never_clobbers_existing_tokens(tmp_path: Path):
    """If the new path already has (necessarily newer) tokens, the stale
    legacy file must not overwrite them."""
    (tmp_path / "tokens").mkdir()
    new_path = tmp_path / "tokens" / "sparebank1.json"
    new_path.write_text(json.dumps({"access_token": "current"}))
    (tmp_path / "tokens.json").write_text(json.dumps({"access_token": "stale"}))

    resolved = token_store_path(tmp_path, "sparebank1")

    assert json.loads(resolved.read_text())["access_token"] == "current"
    assert (tmp_path / "tokens.json").exists(), "stale legacy file is left alone, not consumed"


def test_legacy_migration_only_applies_to_the_sparebank1_name(tmp_path: Path):
    """The legacy single-token file can only ever have belonged to the one
    SpareBank1 connection that predated named providers - it must not be
    handed to an unrelated provider that happens to be resolved first."""
    (tmp_path / "tokens.json").write_text(json.dumps({"access_token": "sb1-only"}))

    other = token_store_path(tmp_path, "some_other_bank")

    assert not other.exists()
    assert (tmp_path / "tokens.json").exists()


def test_resolve_picks_the_only_sparebank1_provider():
    config = make_config({"mybank": sb1()})
    name, provider_config = resolve_sparebank1_provider(config)
    assert name == "mybank"
    assert provider_config.client_id == "cid"


def test_resolve_by_explicit_name():
    config = make_config({"a": sb1(client_id="A"), "b": sb1(client_id="B")})
    name, provider_config = resolve_sparebank1_provider(config, "b")
    assert name == "b"
    assert provider_config.client_id == "B"


def test_resolve_refuses_to_guess_between_several():
    """These scripts do interactive logins and live API probes - silently
    picking the wrong bank's credentials is worse than refusing."""
    config = make_config({"a": sb1(), "b": sb1()})
    with pytest.raises(SystemExit) as exc:
        resolve_sparebank1_provider(config)
    assert "pass --provider" in str(exc.value)


def test_resolve_unknown_name_lists_what_is_available():
    config = make_config({"a": sb1(), "b": sb1()})
    with pytest.raises(SystemExit) as exc:
        resolve_sparebank1_provider(config, "nope")
    message = str(exc.value)
    assert "a" in message and "b" in message


def test_resolve_with_no_providers_configured():
    config = make_config({})
    with pytest.raises(SystemExit) as exc:
        resolve_sparebank1_provider(config)
    assert "No SpareBank1 provider is configured" in str(exc.value)
