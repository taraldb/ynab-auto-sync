import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from tests.conftest import make_engine, seed_mappings
from ynab_auto_sync.config import (
    AccountMapping,
    AppConfig,
    MqttConfig,
    SpareBank1Config,
    SyncConfig,
    YnabConfig,
)
from ynab_auto_sync.providers.sparebank1 import client as sb1_client
from ynab_auto_sync.providers.sparebank1.auth import TokenStore
from ynab_auto_sync.providers.sparebank1.transform import derive_import_id
from ynab_auto_sync.state import JsonStateStore
from ynab_auto_sync.sync.file_import.base import ImportedTransactionRow
from ynab_auto_sync.sync.file_import.dedup import derive_import_id as derive_file_import_id
from ynab_auto_sync.sync.file_import.dedup import get_tracking_key as get_file_tracking_key
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.ynab import client as ynab_client


def make_config(accounts=None) -> AppConfig:
    return AppConfig(
        providers={"sparebank1": SpareBank1Config(
            client_id="cid", client_secret="secret", redirect_uri="http://localhost:8765/callback"
        )},
        ynab=YnabConfig(
            personal_access_token="pat",
            budgets={"personal": "budget-1", "shared": "budget-2"},
        ),
        accounts=accounts
        if accounts is not None
        else [
            AccountMapping(
                sparebank1_account_key="acct-1",
                ynab_account_id="ynab-acct-1",
                ynab_budget="personal",
                display_name="Test",
            )
        ],
        sync=SyncConfig(lookback_overlap_hours=72, initial_backfill_days=30),
        mqtt=MqttConfig(host="mqtt.local"),
    )


def make_token_store(tmp_path: Path) -> TokenStore:
    store = JsonStateStore(tmp_path / "tokens.json")
    future = datetime.now(UTC) + timedelta(minutes=5)
    store.write(
        {
            "access_token": "valid-access-token",
            "refresh_token": "valid-refresh-token",
            "obtained_at": datetime.now(UTC).isoformat(),
            "expires_at": future.isoformat(),
        }
    )
    config = make_config()
    return TokenStore(store, config.providers["sparebank1"])


def make_db(tmp_path: Path) -> StateDB:
    return StateDB(tmp_path / "sync_state.db")


def create_route(budget_id: str, transaction_ids=None, duplicate_import_ids=None):
    return respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/{budget_id}/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": transaction_ids or [],
                    "duplicate_import_ids": duplicate_import_ids or [],
                    "transactions": [],
                }
            },
        )
    )


@respx.mock
async def test_run_cycle_creates_new_booked_transaction(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-booked-1",
                        "nonUniqueId": "tx-booked-1",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -50,
                        "description": "Coffee",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-booked-1")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-1"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-tx-1", "import_id": import_id, "amount": -50000}
                    ],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, account_last_synced = await engine.run_cycle()

    assert result.created == 1
    assert result.updated == 0
    assert result.duplicates == 0
    assert account_last_synced["acct-1"] is not None

    tracked = db.get_tracked("acct-1:tx-booked-1")
    assert tracked is not None
    assert tracked["ynab_transaction_id"] == "ynab-tx-1"
    assert tracked["ynab_budget_id"] == "budget-1"
    assert tracked["booking_status"] == "BOOKED"
    assert tracked["account_key"] == "acct-1"


@respx.mock
async def test_run_cycle_unaffected_by_raising_progress_callback(tmp_path: Path):
    # Direct regression test for the "a progress-broadcast bug must never
    # affect a real sync cycle" invariant (see engine.py's _report()): an
    # on_progress callback that raises on every call must still leave
    # run_cycle() with the exact same result as if no callback were passed
    # at all - see test_run_cycle_creates_new_booked_transaction above,
    # which this mirrors.
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-booked-1",
                        "nonUniqueId": "tx-booked-1",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -50,
                        "description": "Coffee",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-booked-1")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-1"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-tx-1", "import_id": import_id, "amount": -50000}
                    ],
                }
            },
        )
    )

    calls: list[tuple[str, dict]] = []

    async def exploding_on_progress(phase: str, context: dict) -> None:
        calls.append((phase, context))
        raise RuntimeError("simulated broadcast failure")

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, account_last_synced = await engine.run_cycle(on_progress=exploding_on_progress)

    assert result.created == 1
    assert result.updated == 0
    assert result.duplicates == 0
    assert account_last_synced["acct-1"] is not None
    assert db.get_tracked("acct-1:tx-booked-1") is not None
    # The callback really was invoked (and really did raise) - this isn't
    # passing because on_progress was silently never called.
    assert len(calls) >= 2  # at least one "fetching" and one "submitting"
    assert {phase for phase, _ in calls} == {"fetching", "submitting"}


@respx.mock
async def test_run_cycle_caches_payee_id_from_create_response(tmp_path: Path):
    # No mapping exists yet for "Coffee" - after a successful create, the
    # payee_id YNAB assigns should be persisted so a future sync of the
    # same raw payee text can anchor to it (see StateDB.payee_mappings).
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-booked-1",
                        "nonUniqueId": "tx-booked-1",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -50,
                        "description": "Coffee",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-booked-1")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-1"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-tx-1",
                            "import_id": import_id,
                            "amount": -50000,
                            "payee_id": "payee-fresh-1",
                            "payee_name": "Coffee",
                        }
                    ],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        await engine.run_cycle()

    assert db.get_payee_id("budget-1", "Coffee") == "payee-fresh-1"


@respx.mock
async def test_run_cycle_uses_cached_payee_id_instead_of_payee_name(tmp_path: Path):
    # A mapping already exists for "Coffee" in this budget (e.g. from an
    # earlier sync, possibly renamed by the user in YNAB since) - the
    # create payload must submit payee_id, not the raw payee_name, so it
    # anchors to whatever that payee is called now.
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    await db.upsert_payee_mapping("budget-1", "Coffee", "payee-cached-1")

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-booked-1",
                        "nonUniqueId": "tx-booked-1",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -50,
                        "description": "Coffee",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-booked-1")
    create_route = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-1"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-tx-1",
                            "import_id": import_id,
                            "amount": -50000,
                            "payee_id": "payee-cached-1",
                            "payee_name": "Renamed by user",
                        }
                    ],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        await engine.run_cycle()

    sent = json.loads(create_route.calls[0].request.content)["transactions"][0]
    assert sent["payee_id"] == "payee-cached-1"
    assert "payee_name" not in sent
    # First-write-wins: an already-cached mapping is never overwritten by a
    # later create, even one whose response reports a different payee_name.
    assert db.get_payee_id("budget-1", "Coffee") == "payee-cached-1"


@respx.mock
async def test_run_cycle_skips_pending_transaction_entirely(tmp_path: Path):
    # SpareBank1's amount is unreliable while PENDING (confirmed against
    # real data) - the provider layer (providers/sparebank1/provider.py)
    # filters PENDING rows out of fetch() entirely, so a PENDING-only fetch
    # must create nothing and leave no tracked row. It will only be created
    # once it books, via the ordinary "new" classification path (see
    # test_run_cycle_updates_when_pending_transitions_to_booked for the case
    # where it was already tracked as PENDING from before this behavior).
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-pending-1",
                        "nonUniqueId": "tx-pending-1",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -130,
                        "description": "Card purchase",
                        "bookingStatus": "PENDING",
                    }
                ]
            },
        )
    )
    create_route_mock = create_route("budget-1")

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 0
    assert not create_route_mock.called
    assert db.get_tracked("acct-1:tx-pending-1") is None


@respx.mock
async def test_run_cycle_skips_already_tracked_unchanged_status(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    await db.upsert_tracked(
        "acct-1:tx-known",
        import_id=derive_import_id("acct-1:tx-known"),
        ynab_transaction_id="ynab-known",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-50000,
    )

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-known",
                        "nonUniqueId": "tx-known",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -50,
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    create_route_mock = create_route("budget-1")
    patch_route = respx.patch(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(return_value=httpx.Response(200, json={"data": {}}))

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 0
    assert result.updated == 0
    assert not create_route_mock.called
    assert not patch_route.called


@respx.mock
async def test_run_cycle_updates_when_pending_transitions_to_booked(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    await db.upsert_tracked(
        "acct-1:tx-transition",
        import_id=derive_import_id("acct-1:tx-transition"),
        ynab_transaction_id="ynab-transition",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="PENDING",
        amount_milliunits=-130000,
    )

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-transition",
                        "nonUniqueId": "tx-transition",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -142.5,  # final booked amount differs from the pending preview
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    patch_route = respx.patch(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(return_value=httpx.Response(200, json={"data": {"transaction_ids": ["ynab-transition"]}}))

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.updated == 1
    assert result.created == 0
    sent_body = json.loads(patch_route.calls.last.request.content)
    assert sent_body["transactions"] == [
        {"id": "ynab-transition", "cleared": "cleared", "amount": -142500}
    ]

    tracked = db.get_tracked("acct-1:tx-transition")
    assert tracked["booking_status"] == "BOOKED"
    assert tracked["amount_milliunits"] == -142500


@respx.mock
async def test_run_cycle_groups_creates_by_resolved_budget(tmp_path: Path):
    accounts = [
        AccountMapping(
            sparebank1_account_key="acct-personal",
            ynab_account_id="ynab-acct-personal",
            ynab_budget="personal",
        ),
        AccountMapping(
            sparebank1_account_key="acct-shared",
            ynab_account_id="ynab-acct-shared",
            ynab_budget="shared",
        ),
    ]
    config = make_config(accounts=accounts)
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    # A single combined fetch now covers both accounts (see
    # scripts/probe_multi_account_transactions.py) - one mocked response
    # containing both accounts' transactions, each tagged with its own
    # accountKey, exactly like the real API's confirmed behavior.
    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-a",
                        "nonUniqueId": "tx-a",
                        "date": "2026-08-20",
                        "amount": -10,
                        "bookingStatus": "BOOKED",
                        "accountKey": "acct-personal",
                    },
                    {
                        "id": "tx-b",
                        "nonUniqueId": "tx-b",
                        "date": "2026-08-20",
                        "amount": -10,
                        "bookingStatus": "BOOKED",
                        "accountKey": "acct-shared",
                    },
                ]
            },
        )
    )

    personal_route = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-a"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-a",
                            "import_id": derive_import_id("acct-personal:tx-a"),
                            "amount": -10000,
                        }
                    ],
                }
            },
        )
    )
    shared_route = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-2/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-b"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-b",
                            "import_id": derive_import_id("acct-shared:tx-b"),
                            "amount": -10000,
                        }
                    ],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, account_last_synced = await engine.run_cycle()

    assert result.created == 2
    assert personal_route.called
    assert shared_route.called
    assert db.get_tracked("acct-personal:tx-a")["ynab_budget_id"] == "budget-1"
    assert db.get_tracked("acct-shared:tx-b")["ynab_budget_id"] == "budget-2"
    assert set(account_last_synced) == {"acct-personal", "acct-shared"}


@respx.mock
async def test_run_cycle_raises_and_tracks_nothing_when_create_fails(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-1",
                        "nonUniqueId": "tx-1",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -50,
                    }
                ]
            },
        )
    )
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        with pytest.raises(httpx.HTTPStatusError):
            await engine.run_cycle()

    assert db.get_tracked("acct-1:tx-1") is None
    assert db.read_account_states() == {}


@respx.mock
async def test_run_cycle_filters_out_transactions_before_since_window(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    old_date = (datetime.now(UTC) - timedelta(days=60)).date().isoformat()
    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {"id": "tx-old", "accountKey": "acct-1", "date": old_date, "amount": -50}
                ]
            },
        )
    )
    create_route_mock = create_route("budget-1")

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()  # initial_backfill_days=30, tx is 60 days old

    assert result.created == 0
    assert not create_route_mock.called


@respx.mock
async def test_run_cycle_widens_fetch_window_for_stale_pending_transaction(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    # first_seen_at far outside the normal 72h overlap window and even
    # outside the 30-day initial_backfill_days bound - the pending-floor
    # widening must reach back this far anyway, or this transaction would
    # never be re-checked and would stay uncleared in YNAB forever.
    ancient = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    db._conn.execute(
        """
        INSERT INTO tracked_transactions (
            sb1_transaction_id, import_id, ynab_transaction_id, ynab_budget_id,
            account_key, booking_status, amount_milliunits, first_seen_at, last_checked_at
        ) VALUES (?, ?, ?, ?, ?, 'PENDING', -1000, ?, ?)
        """,
        ("tx-stale-pending", derive_import_id("tx-stale-pending"), "ynab-stale", "budget-1", "acct-1", ancient, ancient),
    )
    db._conn.commit()

    route = respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(200, json={"transactions": []})
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        await engine.run_cycle()

    sent_from_date = route.calls.last.request.url.params["fromDate"]
    assert sent_from_date <= (datetime.now(UTC) - timedelta(days=89)).date().isoformat()


@respx.mock
async def test_run_cycle_backfills_tracking_when_ynab_reports_duplicate(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)  # no local tracking - simulates state loss
    # Mappings are still present: what's being simulated is the loss of
    # tracked_transactions (the disposable layer, invariant 1), not the loss
    # of which accounts sync where.
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-orphaned",
                        "nonUniqueId": "tx-orphaned",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -50,
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-orphaned")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": [],
                    "duplicate_import_ids": [import_id],
                    "transactions": [],
                }
            },
        )
    )
    lookup_route = respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/accounts/ynab-acct-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {"id": "ynab-recovered", "import_id": import_id, "amount": -50000}
                    ]
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.duplicates == 1
    assert lookup_route.called
    tracked = db.get_tracked("acct-1:tx-orphaned")
    assert tracked is not None
    assert tracked["ynab_transaction_id"] == "ynab-recovered"


@respx.mock
async def test_run_cycle_resolves_duplicate_as_deleted_when_active_lookup_finds_nothing(
    tmp_path: Path,
):
    """Confirmed live: YNAB permanently reserves an import_id even after
    the transaction using it is deleted, and its plain (account-scoped)
    transaction list silently excludes deleted transactions - so the
    active-lookup resilience path finds nothing even though the duplicate
    report is correct. The heavier delta-based lookup (which does include
    deleted transactions) should then resolve it as permanently DELETED,
    not error forever.
    """
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-deleted",
                        "nonUniqueId": "tx-deleted",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -13,
                        "description": "Micro Kaffi AS",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-deleted")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transaction_ids": [], "duplicate_import_ids": [import_id], "transactions": []}},
        )
    )
    # Active (account-scoped) lookup finds nothing - the deleted transaction
    # is excluded from this plain list.
    active_lookup_route = respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/accounts/ynab-acct-1/transactions"
    ).mock(return_value=httpx.Response(200, json={"data": {"transactions": []}}))
    # Delta-based (budget-wide, last_knowledge_of_server=0) lookup finds it,
    # marked deleted.
    deleted_lookup_route = respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {"id": "ynab-deleted-1", "import_id": import_id, "deleted": True}
                    ]
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 0
    assert result.duplicates == 1
    assert result.resolved_deleted == 1
    assert active_lookup_route.called
    assert deleted_lookup_route.called

    tracked = db.get_tracked("acct-1:tx-deleted")
    assert tracked is not None
    assert tracked["booking_status"] == "DELETED"
    assert tracked["payee_name"] == "Micro Kaffi AS"
    assert tracked["cleared"] == "cleared"


@respx.mock
async def test_run_cycle_does_not_duplicate_when_sb1_id_changes_across_polls(tmp_path: Path):
    """Regression test for the confirmed live bug: SpareBank1's own `id`
    field was proven to change on every poll of a still-pending
    credit-card transaction, while nonUniqueId (the reservation number)
    stays stable. Tracking must key off get_tracking_key (nonUniqueId),
    not `id`, so polling the same real-world transaction twice - with two
    different `id` values, exactly as observed live - creates it in YNAB
    only once, not once per poll. Uses bookingStatus BOOKED (not the
    originally-observed PENDING) since PENDING rows are no longer imported
    at all (see providers/sparebank1/provider.py) - the `id`-instability
    hazard this guards against is a property of SpareBank1's API in
    general, not specific to the pending state that first surfaced it.
    """
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    sb1_ids = ["sb1-id-poll-1", "sb1-id-poll-2"]
    call_count = {"n": 0}

    def sb1_side_effect(request):
        n = min(call_count["n"], len(sb1_ids) - 1)
        call_count["n"] += 1
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": sb1_ids[n],
                        "nonUniqueId": "575043327",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -130,
                        "description": "Card purchase",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )

    respx.get(sb1_client.TRANSACTIONS_URL).mock(side_effect=sb1_side_effect)

    import_id = derive_import_id("acct-1:575043327")
    create_route_mock = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-booked-1"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-booked-1", "import_id": import_id, "amount": -130000}
                    ],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)

        first_result, _ = await engine.run_cycle()
        second_result, _ = await engine.run_cycle()

    assert first_result.created == 1
    assert second_result.created == 0
    assert second_result.updated == 0
    assert create_route_mock.call_count == 1

    tracked = db.get_tracked("acct-1:575043327")
    assert tracked is not None
    assert tracked["ynab_transaction_id"] == "ynab-booked-1"


@respx.mock
async def test_readd_deleted_transaction_recreates_with_fresh_import_id(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    old_import_id = derive_import_id("acct-1:tx-deleted:readd:0")
    await db.upsert_tracked(
        "acct-1:tx-deleted",
        import_id=old_import_id,
        ynab_transaction_id="ynab-old-deleted",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="DELETED",
        amount_milliunits=-13000,
        payee_name="Micro Kaffi AS",
        memo="Zettle_*Micro Kaffi AS",
        transaction_date="2026-08-20",
        ynab_account_id="ynab-acct-1",
        cleared="uncleared",
    )
    expected_new_import_id = derive_import_id("acct-1:tx-deleted:readd:1")

    route = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-readded-1"],
                    "transactions": [{"id": "ynab-readded-1", "import_id": expected_new_import_id}],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        new_id = await engine.readd_deleted_transaction("acct-1:tx-deleted")

    assert new_id == "ynab-readded-1"
    sent_body = json.loads(route.calls.last.request.content)
    sent_tx = sent_body["transactions"][0]
    assert sent_tx["import_id"] == expected_new_import_id
    assert sent_tx["import_id"] != old_import_id
    assert sent_tx["payee_name"] == "Micro Kaffi AS"
    assert sent_tx["amount"] == -13000
    assert sent_tx["cleared"] == "uncleared"

    tracked = db.get_tracked("acct-1:tx-deleted")
    assert tracked["booking_status"] == "PENDING"  # restored from cleared="uncleared"
    assert tracked["ynab_transaction_id"] == "ynab-readded-1"
    assert tracked["import_id"] == expected_new_import_id
    assert tracked["readd_count"] == 1


async def test_readd_deleted_transaction_raises_for_unknown_id(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        with pytest.raises(ValueError, match="no tracked transaction"):
            await engine.readd_deleted_transaction("acct-1:unknown")


async def test_readd_deleted_transaction_raises_when_not_deleted(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    await db.upsert_tracked(
        "acct-1:tx-active",
        import_id=derive_import_id("acct-1:tx-active"),
        ynab_transaction_id="ynab-active-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-1000,
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        with pytest.raises(ValueError, match="not marked deleted"):
            await engine.readd_deleted_transaction("acct-1:tx-active")


# --- Account-transfer detection/creation ---


def make_transfer_config():
    accounts = [
        AccountMapping(
            sparebank1_account_key="acct-out",
            ynab_account_id="ynab-out",
            ynab_budget="personal",
        ),
        AccountMapping(
            sparebank1_account_key="acct-in",
            ynab_account_id="ynab-in",
            ynab_budget="personal",
        ),
    ]
    return make_config(accounts=accounts)


def mock_sb1_transfer_fetch(out_date="2026-08-20", in_date="2026-08-20"):
    # accountNumber/remoteAccountNumber mirror the real 39722.10 kr transfer
    # pair used to design this cross-reference check (both directions
    # correctly name each other's real account number) - see
    # sync/transfers.py's find_transfer_pairs docstring.
    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-out-1",
                        "nonUniqueId": "tx-out-1",
                        "accountKey": "acct-out",
                        "date": out_date,
                        "amount": -50,
                        "bookingStatus": "BOOKED",
                        "accountNumber": {"value": "11110000001"},
                        "remoteAccountNumber": "22220000002",
                    },
                    {
                        "id": "tx-in-1",
                        "nonUniqueId": "tx-in-1",
                        "accountKey": "acct-in",
                        "date": in_date,
                        "amount": 50,
                        "bookingStatus": "BOOKED",
                        "accountNumber": {"value": "22220000002"},
                        "remoteAccountNumber": "11110000001",
                    },
                ]
            },
        )
    )


def mock_payees_route(budget_id: str):
    return respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/{budget_id}/payees"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "payees": [
                        {"id": "payee-for-out", "name": "Transfer : Out", "transfer_account_id": "ynab-out"},
                        {"id": "payee-for-in", "name": "Transfer : In", "transfer_account_id": "ynab-in"},
                    ]
                }
            },
        )
    )


@respx.mock
async def test_run_cycle_creates_linked_transfer_for_matched_pair(tmp_path: Path):
    config = make_transfer_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    mock_sb1_transfer_fetch()
    payees_route = mock_payees_route("budget-1")

    primary_import_id = derive_import_id("acct-out:tx-out-1")

    def create_side_effect(request):
        body = json.loads(request.content)
        sent = body["transactions"]
        assert len(sent) == 1, "only the primary leg should ever be submitted"
        sent_tx = sent[0]
        assert sent_tx["payee_id"] == "payee-for-in"
        assert "payee_name" not in sent_tx
        assert sent_tx["import_id"] == primary_import_id
        return httpx.Response(
            200,
            json={
                "data": {
                    # Confirmed live: YNAB lists the same id twice here for a
                    # transfer-creating submission - must not be trusted for counting.
                    "transaction_ids": ["ynab-transfer-out", "ynab-transfer-out"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-transfer-out",
                            "import_id": primary_import_id,
                            "amount": -50000,
                            "payee_name": "Transfer : In",
                            "transfer_account_id": "ynab-in",
                            "transfer_transaction_id": "ynab-transfer-in",
                        }
                    ],
                }
            },
        )

    create_route = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(side_effect=create_side_effect)
    patch_route = respx.patch(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(return_value=httpx.Response(200, json={"data": {}}))

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert payees_route.called
    assert create_route.called
    assert result.created == 2

    primary_tracked = db.get_tracked("acct-out:tx-out-1")
    assert primary_tracked is not None
    assert primary_tracked["ynab_transaction_id"] == "ynab-transfer-out"
    assert primary_tracked["import_id"] == primary_import_id

    secondary_tracked = db.get_tracked("acct-in:tx-in-1")
    assert secondary_tracked is not None
    assert secondary_tracked["ynab_transaction_id"] == "ynab-transfer-in"
    assert secondary_tracked["import_id"].startswith("LOCAL:XFER:")
    assert secondary_tracked["booking_status"] == "BOOKED"

    # Secondary leg was BOOKED per SpareBank1, so its YNAB-defaulted
    # "uncleared" state should have been corrected immediately via PATCH.
    assert patch_route.called
    patch_body = json.loads(patch_route.calls.last.request.content)
    assert patch_body["transactions"] == [
        {"id": "ynab-transfer-in", "cleared": "cleared", "amount": 50000}
    ]


@respx.mock
async def test_transfer_primary_reported_as_duplicate_does_not_crash(tmp_path: Path):
    """Regression: _match_transfers pops "payee_name" off a transfer primary's
    payload (replacing it with payee_id), but _backfill_duplicates then read
    p.ynab_tx["payee_name"] with [] rather than .get(). So a transfer primary
    coming back in duplicate_import_ids - a real possibility on a retry after
    a partial failure, or two overlapping cycles - raised an unhandled
    KeyError and took down the whole sync cycle rather than resolving the
    duplicate. Not hypothetical: the pop and the [] access are both on the
    normal path, they just had to coincide.
    """
    config = make_transfer_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    mock_sb1_transfer_fetch()
    mock_payees_route("budget-1")

    primary_import_id = derive_import_id("acct-out:tx-out-1")

    respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": [],
                    "duplicate_import_ids": [primary_import_id],
                    "transactions": [],
                }
            },
        )
    )
    # The account-scoped lookup finds the pre-existing transaction, so
    # _backfill_duplicates takes its "recover the tracking row" branch - the
    # exact path that used to raise.
    respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/accounts/ynab-out/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {"id": "ynab-existing-out", "import_id": primary_import_id, "amount": -50000}
                    ]
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.duplicates == 1

    # The primary leg was recovered into tracking despite having no
    # payee_name on its payload (it was replaced by payee_id when the pair
    # was rewired into a linked transfer).
    tracked = db.get_tracked("acct-out:tx-out-1")
    assert tracked is not None
    assert tracked["ynab_transaction_id"] == "ynab-existing-out"
    assert tracked["payee_name"] is None


@respx.mock
async def test_run_cycle_does_not_match_transfer_across_different_budgets(tmp_path: Path):
    accounts = [
        AccountMapping(
            sparebank1_account_key="acct-out",
            ynab_account_id="ynab-out",
            ynab_budget="personal",
        ),
        AccountMapping(
            sparebank1_account_key="acct-in",
            ynab_account_id="ynab-in",
            ynab_budget="shared",
        ),
    ]
    config = make_config(accounts=accounts)
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    mock_sb1_transfer_fetch()
    payees_route = mock_payees_route("budget-1")

    personal_route = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transaction_ids": ["a"], "duplicate_import_ids": [], "transactions": [
                {"id": "a", "import_id": derive_import_id("acct-out:tx-out-1"), "amount": -50000}
            ]}},
        )
    )
    shared_route = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-2/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transaction_ids": ["b"], "duplicate_import_ids": [], "transactions": [
                {"id": "b", "import_id": derive_import_id("acct-in:tx-in-1"), "amount": 50000}
            ]}},
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    # Different budgets can never be linked by YNAB's transfer mechanism -
    # both legs must import as two ordinary, independent transactions.
    assert not payees_route.called
    assert personal_route.called
    assert shared_route.called
    assert result.created == 2
    assert db.get_tracked("acct-out:tx-out-1")["ynab_budget_id"] == "budget-1"
    assert db.get_tracked("acct-in:tx-in-1")["ynab_budget_id"] == "budget-2"


@respx.mock
async def test_run_cycle_falls_back_to_normal_import_when_no_transfer_payee_found(tmp_path: Path):
    config = make_transfer_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    mock_sb1_transfer_fetch()
    # Neither payee targets our accounts - simulates a budget where the
    # transfer payee somehow isn't provisioned.
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/payees").mock(
        return_value=httpx.Response(200, json={"data": {"payees": []}})
    )

    def create_side_effect(request):
        body = json.loads(request.content)
        sent = body["transactions"]
        assert len(sent) == 2, "both legs should import independently when no transfer payee exists"
        return httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": [],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": f"ynab-{i}",
                            "import_id": tx["import_id"],
                            "amount": tx["amount"],
                        }
                        for i, tx in enumerate(sent)
                    ],
                }
            },
        )

    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        side_effect=create_side_effect
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 2
    assert db.get_tracked("acct-out:tx-out-1")["import_id"] == derive_import_id(
        "acct-out:tx-out-1"
    )
    assert db.get_tracked("acct-in:tx-in-1")["import_id"] == derive_import_id(
        "acct-in:tx-in-1"
    )


@respx.mock
async def test_run_cycle_does_not_link_coincidental_same_amount_pair(tmp_path: Path):
    """Regression test for a real false positive: a 158.48 kr salary
    deposit and an unrelated 158.48 kr grocery purchase on a different
    account, opposite sign, within the match window - but neither names
    the other's real account number. Must import as two ordinary,
    unlinked transactions, not a matched transfer.
    """
    config = make_transfer_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-salary",
                        "nonUniqueId": "tx-salary",
                        "accountKey": "acct-out",
                        "date": "2026-08-20",
                        "amount": 158.48,
                        "bookingStatus": "BOOKED",
                        "accountNumber": {"value": "11110000001"},
                        "remoteAccountNumber": "99998888777",
                    },
                    {
                        "id": "tx-grocery",
                        "nonUniqueId": "tx-grocery",
                        "accountKey": "acct-in",
                        "date": "2026-08-24",
                        "amount": -158.48,
                        "bookingStatus": "BOOKED",
                        "accountNumber": {"value": "K1861615456"},
                        "remoteAccountNumber": "-1",
                    },
                ]
            },
        )
    )
    payees_route = mock_payees_route("budget-1")

    def create_side_effect(request):
        body = json.loads(request.content)
        sent = body["transactions"]
        assert len(sent) == 2, "unrelated same-amount transactions must not be linked"
        return httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": [],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": f"ynab-{i}", "import_id": tx["import_id"], "amount": tx["amount"]}
                        for i, tx in enumerate(sent)
                    ],
                }
            },
        )

    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        side_effect=create_side_effect
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    # No cross-reference between the two legs -> never even attempted as a
    # transfer, so get_payees is never called at all.
    assert not payees_route.called
    assert result.created == 2


def make_row(row_index: int, day: int, amount_milliunits: int, payee: str, memo=None):
    from datetime import date as date_cls

    return ImportedTransactionRow(
        date=date_cls(2026, 8, day),
        amount_milliunits=amount_milliunits,
        payee_name=payee,
        memo=memo,
        row_index=row_index,
    )


async def test_import_file_rows_dry_run_classifies_without_calling_ynab(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    rows = [make_row(2, 1, -12500, "Kiwi")]

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        with respx.mock:
            # No route registered at all - if the engine made an HTTP call
            # during dry_run, respx would raise for the unmocked request.
            result = await engine.import_file_rows(
                ynab_account_id="ynab-acct-1",
                ynab_budget_id="budget-1",
                account_key="acct-1",
                rows=rows,
                dry_run=True,
            )

    assert result.committed is False
    assert result.created == 0
    assert len(result.rows) == 1
    assert result.rows[0].status == "new"
    assert db.get_tracked(get_file_tracking_key("ynab-acct-1", "anything")) is None


@respx.mock
async def test_import_file_rows_commits_and_tracks_new_rows(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    row = make_row(2, 1, -12500, "Kiwi")

    from ynab_auto_sync.sync.file_import import dedup as file_dedup

    dedup_key = file_dedup.compute_dedup_key(row.date, row.amount_milliunits, row.payee_name, row.memo)
    tracking_key = get_file_tracking_key("ynab-acct-1", dedup_key)
    import_id = derive_file_import_id(tracking_key)

    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-file-1"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-file-1", "import_id": import_id, "amount": -12500}
                    ],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result = await engine.import_file_rows(
            ynab_account_id="ynab-acct-1",
            ynab_budget_id="budget-1",
            account_key="acct-1",
            rows=[row],
            dry_run=False,
        )

    assert result.committed is True
    assert result.created == 1
    assert result.rows[0].status == "new"

    tracked = db.get_tracked(tracking_key)
    assert tracked is not None
    assert tracked["ynab_transaction_id"] == "ynab-file-1"
    assert tracked["booking_status"] == "BOOKED"
    assert tracked["cleared"] == "cleared"


@respx.mock
async def test_import_file_rows_classifies_already_tracked_row_as_duplicate(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    row = make_row(2, 1, -12500, "Kiwi")

    from ynab_auto_sync.sync.file_import import dedup as file_dedup

    dedup_key = file_dedup.compute_dedup_key(row.date, row.amount_milliunits, row.payee_name, row.memo)
    tracking_key = get_file_tracking_key("ynab-acct-1", dedup_key)
    await db.upsert_tracked(
        tracking_key,
        import_id=derive_file_import_id(tracking_key),
        ynab_transaction_id="ynab-existing",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-12500,
    )
    create_route_mock = create_route("budget-1")

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result = await engine.import_file_rows(
            ynab_account_id="ynab-acct-1",
            ynab_budget_id="budget-1",
            account_key="acct-1",
            rows=[row],
            dry_run=False,
        )

    assert result.committed is False  # no new rows to submit
    assert result.rows[0].status == "duplicate"
    assert not create_route_mock.called


@respx.mock
async def test_import_file_rows_dedup_key_never_collides_with_sparebank1_domain(tmp_path: Path):
    """Regression guard for the collision-safety invariant: a file-import
    row and a SpareBank1-derived transaction that happen to describe the
    same real-world transaction must never share a tracked_transactions
    row, since the two prefixes ("FILE:" vs "SB1:") and tracking-key
    domains ("file:..." vs "<account_key>:...") are structurally distinct.
    """
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    sb1_import_id = derive_import_id("acct-1:tx-1")
    await db.upsert_tracked(
        "acct-1:tx-1",
        import_id=sb1_import_id,
        ynab_transaction_id="ynab-sb1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-12500,
    )

    row = make_row(2, 1, -12500, "Kiwi")
    create_route_mock = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transaction_ids": ["ynab-file-1"], "duplicate_import_ids": [], "transactions": []}},
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result = await engine.import_file_rows(
            ynab_account_id="ynab-acct-1",
            ynab_budget_id="budget-1",
            account_key="acct-1",
            rows=[row],
            dry_run=False,
        )

    # Not classified as a duplicate of the SB1 row - different tracking
    # domain entirely - so it was actually submitted to YNAB.
    assert result.rows[0].status == "new"
    assert create_route_mock.called


# -- audit_events -------------------------------------------------------


@respx.mock
async def test_run_cycle_records_audit_event_for_created_transaction(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-booked-1",
                        "nonUniqueId": "tx-booked-1",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -50,
                        "description": "Coffee",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-booked-1")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-1"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-tx-1", "import_id": import_id, "amount": -50000}
                    ],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        await engine.run_cycle()

    events, total = db.list_audit_events()
    assert total == 1
    assert events[0]["event_type"] == "created"
    assert events[0]["source"] == "sparebank1"
    assert events[0]["account_key"] == "acct-1"
    assert events[0]["tracking_key"] == "acct-1:tx-booked-1"
    assert events[0]["ynab_transaction_id"] == "ynab-tx-1"
    assert events[0]["amount_milliunits"] == -50000


@respx.mock
async def test_run_cycle_records_audit_event_for_updated_transaction(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    await db.upsert_tracked(
        "acct-1:tx-transition",
        import_id=derive_import_id("acct-1:tx-transition"),
        ynab_transaction_id="ynab-transition",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="PENDING",
        amount_milliunits=-130000,
    )

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-transition",
                        "nonUniqueId": "tx-transition",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -142.5,
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    respx.patch(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(return_value=httpx.Response(200, json={"data": {"transaction_ids": ["ynab-transition"]}}))

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        await engine.run_cycle()

    events, total = db.list_audit_events()
    assert total == 1
    assert events[0]["event_type"] == "updated"
    assert events[0]["source"] == "sparebank1"
    assert events[0]["ynab_transaction_id"] == "ynab-transition"
    assert events[0]["amount_milliunits"] == -142500
    assert "pending" in events[0]["detail"].lower()


@respx.mock
async def test_run_cycle_records_audit_event_for_duplicate_recovered_via_lookup(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-orphaned",
                        "nonUniqueId": "tx-orphaned",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -50,
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-orphaned")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": [],
                    "duplicate_import_ids": [import_id],
                    "transactions": [],
                }
            },
        )
    )
    respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/accounts/ynab-acct-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {"id": "ynab-recovered", "import_id": import_id, "amount": -50000}
                    ]
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        await engine.run_cycle()

    events, total = db.list_audit_events()
    assert total == 1
    assert events[0]["event_type"] == "duplicate"
    assert events[0]["ynab_transaction_id"] == "ynab-recovered"
    assert "recovered" in events[0]["detail"]


@respx.mock
async def test_run_cycle_records_audit_event_for_duplicate_resolved_as_deleted(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-deleted",
                        "nonUniqueId": "tx-deleted",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -13,
                        "description": "Micro Kaffi AS",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-deleted")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transaction_ids": [], "duplicate_import_ids": [import_id], "transactions": []}},
        )
    )
    respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/accounts/ynab-acct-1/transactions"
    ).mock(return_value=httpx.Response(200, json={"data": {"transactions": []}}))
    respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {"id": "ynab-deleted-1", "import_id": import_id, "deleted": True}
                    ]
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        await engine.run_cycle()

    events, total = db.list_audit_events()
    assert total == 1
    assert events[0]["event_type"] == "duplicate"
    assert "resolved as previously deleted" in events[0]["detail"]


@respx.mock
async def test_submit_retry_records_duplicate_audit_event_for_already_tracked_row(
    tmp_path: Path,
):
    """The "already tracked (retry within same cycle)" branch of
    _backfill_duplicates is only reachable by resubmitting the same
    ClassifiedCycle - exactly the retry scenario scheduler.py's backoff
    handling relies on being safe (see ClassifiedCycle's docstring)."""
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-retry",
                        "nonUniqueId": "tx-retry",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -50,
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-retry")

    call_count = 0

    def create_side_effect(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "transaction_ids": ["ynab-retry-1"],
                        "duplicate_import_ids": [],
                        "transactions": [
                            {"id": "ynab-retry-1", "import_id": import_id, "amount": -50000}
                        ],
                    }
                },
            )
        # Second submit() of the SAME classified cycle: YNAB now reports it
        # as a duplicate, but it's already tracked locally from the first
        # call above - the "already tracked" no-op branch.
        return httpx.Response(
            200,
            json={"data": {"transaction_ids": [], "duplicate_import_ids": [import_id], "transactions": []}},
        )

    respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(side_effect=create_side_effect)

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        classified = await engine.fetch_and_classify()
        await engine.submit(classified)
        await engine.submit(classified)  # simulated retry of the same cycle

    events, total = db.list_audit_events()
    assert total == 2
    duplicate_events = [e for e in events if e["event_type"] == "duplicate"]
    assert len(duplicate_events) == 1
    assert "retry within same cycle" in duplicate_events[0]["detail"]


@respx.mock
async def test_run_cycle_records_audit_events_for_transfer_pair(tmp_path: Path):
    config = make_transfer_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    mock_sb1_transfer_fetch()
    mock_payees_route("budget-1")

    primary_import_id = derive_import_id("acct-out:tx-out-1")

    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-transfer-out", "ynab-transfer-out"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-transfer-out",
                            "import_id": primary_import_id,
                            "amount": -50000,
                            "payee_name": "Transfer : In",
                            "transfer_account_id": "ynab-in",
                            "transfer_transaction_id": "ynab-transfer-in",
                        }
                    ],
                }
            },
        )
    )
    respx.patch(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(return_value=httpx.Response(200, json={"data": {}}))

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        await engine.run_cycle()

    events, total = db.list_audit_events()
    assert total == 2
    created_events = [e for e in events if e["event_type"] == "created"]
    assert len(created_events) == 2
    primary = next(e for e in created_events if e["ynab_transaction_id"] == "ynab-transfer-out")
    secondary = next(e for e in created_events if e["ynab_transaction_id"] == "ynab-transfer-in")
    assert primary["detail"] == "transfer primary leg"
    assert primary["account_key"] == "acct-out"
    assert "transfer secondary leg" in secondary["detail"]
    assert secondary["account_key"] == "acct-in"


@respx.mock
async def test_import_file_rows_records_audit_event_for_duplicate_on_commit(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    row = make_row(2, 1, -12500, "Kiwi")

    from ynab_auto_sync.sync.file_import import dedup as file_dedup

    dedup_key = file_dedup.compute_dedup_key(row.date, row.amount_milliunits, row.payee_name, row.memo)
    tracking_key = get_file_tracking_key("ynab-acct-1", dedup_key)
    await db.upsert_tracked(
        tracking_key,
        import_id=derive_file_import_id(tracking_key),
        ynab_transaction_id="ynab-existing",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-12500,
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        await engine.import_file_rows(
            ynab_account_id="ynab-acct-1",
            ynab_budget_id="budget-1",
            account_key="acct-1",
            rows=[row],
            dry_run=False,
        )

    events, total = db.list_audit_events()
    assert total == 1
    assert events[0]["event_type"] == "duplicate"
    assert events[0]["source"] == "file"
    assert events[0]["tracking_key"] == tracking_key


@respx.mock
async def test_import_file_rows_dry_run_does_not_record_audit_events(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    row = make_row(2, 1, -12500, "Kiwi")

    from ynab_auto_sync.sync.file_import import dedup as file_dedup

    dedup_key = file_dedup.compute_dedup_key(row.date, row.amount_milliunits, row.payee_name, row.memo)
    tracking_key = get_file_tracking_key("ynab-acct-1", dedup_key)
    await db.upsert_tracked(
        tracking_key,
        import_id=derive_file_import_id(tracking_key),
        ynab_transaction_id="ynab-existing",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-12500,
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        with respx.mock:
            await engine.import_file_rows(
                ynab_account_id="ynab-acct-1",
                ynab_budget_id="budget-1",
                account_key="acct-1",
                rows=[row],
                dry_run=True,
            )

    assert db.list_audit_events(include_skipped=True)[1] == 0


@respx.mock
async def test_run_cycle_records_account_key_for_malformed_row_skip(tmp_path: Path):
    """A malformed row's skip audit event carries the account_key the
    provider still knew about (via SkipCallback's context dict), even
    though the row itself never became a NormalizedTransaction."""
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "accountKey": "acct-1",
                        "amount": -10,
                        "bookingStatus": "BOOKED",
                        # no date field - malformed
                    }
                ]
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        await engine.run_cycle()

    events, total = db.list_audit_events(include_skipped=True)
    assert total == 1
    assert events[0]["event_type"] == "skipped"
    assert events[0]["account_key"] == "acct-1"
    assert "date field" in events[0]["detail"]
