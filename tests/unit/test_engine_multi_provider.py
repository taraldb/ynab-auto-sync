"""Proves the provider seam actually works with MORE THAN ONE provider.

Nothing else in the suite does: there has only ever been one real provider,
so every other engine test would still pass if the engine silently only
ever consulted the first one. These tests use two lightweight fakes so the
seam itself - not SpareBank1 - is what's under test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import respx

from ynab_auto_sync.config import AppConfig, MqttConfig, SpareBank1Config, SyncConfig, YnabConfig
from ynab_auto_sync.providers.base import (
    BookingStatus,
    NormalizedTransaction,
    ProviderAccount,
    TransactionProvider,
)
from ynab_auto_sync.sync.engine import SyncEngine
from ynab_auto_sync.sync.money import from_milliunits
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.ynab import client as ynab_client


class FakeProvider(TransactionProvider):
    """Returns a canned set of transactions and records the fetch window it
    was handed, so tests can assert the engine passes per-account windows."""

    def __init__(self, name: str, transactions: list[NormalizedTransaction]):
        self._name = name
        self._transactions = transactions
        self.received_windows: dict[str, datetime] | None = None

    def type_name(self) -> str:  # type: ignore[override]
        return self._name

    async def list_accounts(self, force_refresh: bool = False) -> list[ProviderAccount]:
        return []

    async def fetch(self, since_by_account, on_skip=None):
        self.received_windows = since_by_account
        return list(self._transactions)


def make_config() -> AppConfig:
    return AppConfig(
        providers={"sparebank1": SpareBank1Config(
            client_id="cid", client_secret="secret", redirect_uri="http://localhost/cb"
        )},
        ynab=YnabConfig(personal_access_token="pat", budgets={"personal": "budget-1"}),
        accounts=[],
        sync=SyncConfig(lookback_overlap_hours=72, initial_backfill_days=30),
        mqtt=MqttConfig(host="mqtt.local"),
    )


def ntx(tracking_key: str, account_id: str, import_id: str, amount: int, day: int = 20):
    return NormalizedTransaction(
        tracking_key=tracking_key,
        import_id=import_id,
        provider_account_id=account_id,
        date=date(2026, 8, day),
        amount=from_milliunits(amount),
        payee_name="Somewhere",
        memo=None,
        booking_status=BookingStatus.BOOKED,
    )


async def seed(db: StateDB) -> None:
    await db.create_mapping(
        provider="bank_a",
        provider_account_id="a-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-a",
    )
    await db.create_mapping(
        provider="bank_b",
        provider_account_id="b-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-b",
    )


def create_route(transaction_ids, transactions):
    return respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": transaction_ids,
                    "duplicate_import_ids": [],
                    "transactions": transactions,
                }
            },
        )
    )


@respx.mock
async def test_run_cycle_fetches_and_creates_across_two_providers(tmp_path: Path):
    config = make_config()
    db = StateDB(tmp_path / "s.db")
    await seed(db)

    a = FakeProvider("bank_a", [ntx("a-1:tx1", "a-1", "AAA:1111", -1000)])
    b = FakeProvider("bank_b", [ntx("b-1:tx1", "b-1", "BBB:2222", -2000)])

    route = create_route(
        ["ynab-1", "ynab-2"],
        [
            {"id": "ynab-1", "import_id": "AAA:1111", "amount": -1000},
            {"id": "ynab-2", "import_id": "BBB:2222", "amount": -2000},
        ],
    )

    async with httpx.AsyncClient() as http_client:
        engine = SyncEngine(config, http_client, db, {"bank_a": a, "bank_b": b})
        result, account_last_synced = await engine.run_cycle()

    # Both providers were consulted, each with only its OWN account's window.
    assert list(a.received_windows) == ["a-1"]
    assert list(b.received_windows) == ["b-1"]

    assert result.fetched == 2
    assert result.created == 2
    assert result.accounts_processed == 2
    assert set(account_last_synced) == {"a-1", "b-1"}

    # Both landed in one batched submission to the shared budget, each
    # keeping the import_id its own provider derived (pure passthrough).
    sent = route.calls.last.request.read().decode()
    assert "AAA:1111" in sent and "BBB:2222" in sent

    assert db.get_tracked("a-1:tx1")["ynab_account_id"] == "ynab-a"
    assert db.get_tracked("b-1:tx1")["ynab_account_id"] == "ynab-b"


@respx.mock
async def test_two_providers_sharing_an_account_id_do_not_collide(tmp_path: Path):
    """Two providers may legitimately use the same account-id string. The
    tracking key must still keep them apart, and neither may be mistaken for
    the other's account."""
    config = make_config()
    db = StateDB(tmp_path / "s.db")
    await db.create_mapping(
        provider="bank_a",
        provider_account_id="shared-id",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-a",
    )
    await db.create_mapping(
        provider="bank_b",
        provider_account_id="shared-id",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-b",
    )

    a = FakeProvider("bank_a", [ntx("bank_a:shared-id:tx", "shared-id", "AAA:1111", -1000)])
    b = FakeProvider("bank_b", [ntx("bank_b:shared-id:tx", "shared-id", "BBB:2222", -2000)])

    create_route(
        ["ynab-1", "ynab-2"],
        [
            {"id": "ynab-1", "import_id": "AAA:1111", "amount": -1000},
            {"id": "ynab-2", "import_id": "BBB:2222", "amount": -2000},
        ],
    )

    async with httpx.AsyncClient() as http_client:
        engine = SyncEngine(config, http_client, db, {"bank_a": a, "bank_b": b})
        result, _ = await engine.run_cycle()

    assert result.created == 2
    # Routed to the correct YNAB account despite the identical account id.
    assert db.get_tracked("bank_a:shared-id:tx")["ynab_account_id"] == "ynab-a"
    assert db.get_tracked("bank_b:shared-id:tx")["ynab_account_id"] == "ynab-b"


@respx.mock
async def test_mapping_for_unconfigured_provider_is_skipped_not_fatal(tmp_path: Path):
    """A mapping naming a provider that isn't configured (e.g. its config
    block was removed) must skip just those accounts and log, never abort
    the cycle and starve every other provider."""
    config = make_config()
    db = StateDB(tmp_path / "s.db")
    await seed(db)

    a = FakeProvider("bank_a", [ntx("a-1:tx1", "a-1", "AAA:1111", -1000)])

    create_route(["ynab-1"], [{"id": "ynab-1", "import_id": "AAA:1111", "amount": -1000}])

    async with httpx.AsyncClient() as http_client:
        # bank_b is mapped but deliberately not supplied.
        engine = SyncEngine(config, http_client, db, {"bank_a": a})
        result, account_last_synced = await engine.run_cycle()

    assert result.created == 1
    # The reachable provider's cursor advances; the unreachable one's does
    # not, so it retries cleanly once its provider is configured again.
    assert set(account_last_synced) == {"a-1"}


@respx.mock
async def test_disabled_mapping_is_not_fetched(tmp_path: Path):
    config = make_config()
    db = StateDB(tmp_path / "s.db")
    await seed(db)
    mappings = db.list_mappings()
    b_mapping = next(m for m in mappings if m["provider"] == "bank_b")
    await db.update_mapping(b_mapping["id"], enabled=False)

    a = FakeProvider("bank_a", [ntx("a-1:tx1", "a-1", "AAA:1111", -1000)])
    b = FakeProvider("bank_b", [ntx("b-1:tx1", "b-1", "BBB:2222", -2000)])

    create_route(["ynab-1"], [{"id": "ynab-1", "import_id": "AAA:1111", "amount": -1000}])

    async with httpx.AsyncClient() as http_client:
        engine = SyncEngine(config, http_client, db, {"bank_a": a, "bank_b": b})
        result, _ = await engine.run_cycle()

    assert b.received_windows is None, "a disabled mapping must not be fetched at all"
    assert result.created == 1
    assert db.get_tracked("b-1:tx1") is None


@respx.mock
async def test_engine_applies_per_account_cutoff_provider_does_not(tmp_path: Path):
    """Providers deliberately over-fetch; the engine owns the authoritative
    per-account cutoff (providers/base.py's fetch() contract)."""
    config = make_config()
    db = StateDB(tmp_path / "s.db")
    await seed(db)

    # Far outside any plausible window (initial_backfill_days=30).
    stale = ntx("a-1:old", "a-1", "AAA:0000", -500)
    stale.date = date(2020, 1, 1)
    fresh = ntx("a-1:new", "a-1", "AAA:1111", -1000)
    fresh.date = datetime.now(UTC).date()

    a = FakeProvider("bank_a", [stale, fresh])
    create_route(["ynab-1"], [{"id": "ynab-1", "import_id": "AAA:1111", "amount": -1000}])

    async with httpx.AsyncClient() as http_client:
        engine = SyncEngine(config, http_client, db, {"bank_a": a, "bank_b": FakeProvider("bank_b", [])})
        result, _ = await engine.run_cycle()

    # Both were fetched (the provider returned them untouched)...
    assert result.fetched == 2
    # ...but only the in-window one was created.
    assert result.created == 1
    assert db.get_tracked("a-1:old") is None
    assert db.get_tracked("a-1:new") is not None

    events, _ = db.list_audit_events(include_skipped=True)
    skipped = [e for e in events if e["event_type"] == "skipped"]
    assert len(skipped) == 1
    assert "stale" in skipped[0]["detail"]
    assert skipped[0]["source"] == "bank_a"
    assert skipped[0]["account_key"] == "a-1"


@respx.mock
async def test_run_cycle_records_audit_event_for_unmapped_account(tmp_path: Path):
    """Defensive path: a provider returning a transaction for an account not
    among the requested since_by_account keys - never true of the real
    SpareBank1Provider today, but engine.py guards against it anyway (see
    providers/base.py's fetch() contract)."""
    config = make_config()
    db = StateDB(tmp_path / "s.db")
    await seed(db)

    a = FakeProvider("bank_a", [ntx("a-1:tx1", "not-a-mapped-account", "AAA:1111", -1000)])

    async with httpx.AsyncClient() as http_client:
        engine = SyncEngine(config, http_client, db, {"bank_a": a, "bank_b": FakeProvider("bank_b", [])})
        result, _ = await engine.run_cycle()

    assert result.created == 0
    events, _ = db.list_audit_events(include_skipped=True)
    skipped = [e for e in events if e["event_type"] == "skipped"]
    assert len(skipped) == 1
    assert "unmapped account" in skipped[0]["detail"]
    assert skipped[0]["source"] == "bank_a"
    assert skipped[0]["account_key"] == "not-a-mapped-account"


@respx.mock
async def test_run_cycle_does_not_record_audit_event_for_unchanged_transaction(tmp_path: Path):
    """The plain "already tracked, nothing changed" outcome must never
    produce an audit row - it would flood the log every cycle for every
    already-synced transaction still inside the overlap window."""
    config = make_config()
    db = StateDB(tmp_path / "s.db")
    await seed(db)
    await db.upsert_tracked(
        "a-1:tx1",
        import_id="AAA:1111",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="a-1",
        booking_status="BOOKED",
        amount=from_milliunits(-1000),
    )

    a = FakeProvider("bank_a", [ntx("a-1:tx1", "a-1", "AAA:1111", -1000)])

    async with httpx.AsyncClient() as http_client:
        engine = SyncEngine(config, http_client, db, {"bank_a": a, "bank_b": FakeProvider("bank_b", [])})
        result, _ = await engine.run_cycle()

    assert result.created == 0
    assert result.updated == 0
    _events, total = db.list_audit_events(include_skipped=True)
    assert total == 0
