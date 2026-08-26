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
from ynab_auto_sync.sync.money import from_milliunits, to_milliunits
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
async def test_run_cycle_stale_cached_payee_id_self_heals(tmp_path: Path):
    # Confirmed live in production (2026-08-25): submitting a create with a
    # cached payee_id that no longer resolves to a real YNAB payee (deleted
    # or merged since it was cached) is NOT rejected - YNAB returns 200 but
    # silently drops it, coming back with payee_id AND payee_name both
    # null. The engine must notice this, heal the stale cache entry, and
    # issue a corrective PATCH so the transaction doesn't permanently lose
    # its payee.
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    await db.upsert_payee_mapping("budget-1", "Coffee", "payee-deleted-1")

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-stale-payee",
                        "nonUniqueId": "tx-stale-payee",
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
    import_id = derive_import_id("acct-1:tx-stale-payee")
    create_route_mock = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-stale"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-tx-stale",
                            "import_id": import_id,
                            "amount": -50000,
                            "payee_id": None,
                            "payee_name": None,
                        }
                    ],
                }
            },
        )
    )
    patch_route = respx.patch(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(return_value=httpx.Response(200, json={"data": {}}))

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    # The create was submitted with the (stale) cached payee_id, not the
    # raw payee_name.
    sent = json.loads(create_route_mock.calls[0].request.content)["transactions"][0]
    assert sent["payee_id"] == "payee-deleted-1"
    assert "payee_name" not in sent

    # Cache healed, so a future create for "Coffee" re-learns a fresh id.
    assert db.get_payee_id("budget-1", "Coffee") is None

    # Corrective PATCH sent to attach the real payee to the transaction
    # YNAB otherwise would have left permanently payee-less.
    assert patch_route.called
    patched = json.loads(patch_route.calls.last.request.content)["transactions"]
    assert patched == [{"id": "ynab-tx-stale", "payee_name": "Coffee"}]

    # Local tracking reflects the real payee too, not the null YNAB
    # returned.
    assert result.created == 1
    tracked = db.get_tracked("acct-1:tx-stale-payee")
    assert tracked is not None
    assert tracked["payee_name"] == "Coffee"


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
        amount=from_milliunits(-50000),
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
        amount=from_milliunits(-130000),
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
    # Bulk pre-check (submit()) confirms the target still exists before
    # attempting the PATCH - see CLAUDE.md's "Resolved: update PATCH could
    # permanently wedge the sync cycle".
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transactions": [{"id": "ynab-transition", "deleted": False}]}},
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
async def test_run_cycle_pending_update_target_deleted_no_replacement_marks_deleted(
    tmp_path: Path,
):
    """The pending->booked PATCH target was deleted directly in YNAB (e.g.
    by hand) and no plausible manually-typed replacement exists - see
    CLAUDE.md's "Resolved: update PATCH could permanently wedge the sync
    cycle". Must resolve to DELETED (for GUI re-add) rather than raising
    and wedging the whole cycle, and must NOT attempt the doomed PATCH at
    all (no patch route mocked - a PATCH attempt would fail this test with
    a routing error).
    """
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
        amount=from_milliunits(-130000),
        payee_name="Merchant One",
        transaction_date="2026-08-20",
        ynab_account_id="ynab-acct-1",
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
    # Bulk pre-check confirms the target is gone.
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transactions": [{"id": "ynab-transition", "deleted": True}]}},
        )
    )
    # No manually-typed replacement candidate exists.
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(200, json={"data": {"transactions": []}})
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.updated == 0
    assert result.created == 0
    assert result.resolved_deleted == 1

    tracked = db.get_tracked("acct-1:tx-transition")
    assert tracked["booking_status"] == "DELETED"
    assert tracked["ynab_transaction_id"] == "ynab-transition"

    events, _total = db.list_audit_events()
    matching = [e for e in events if e["tracking_key"] == "acct-1:tx-transition"]
    assert any("no replacement found" in (e["detail"] or "") for e in matching)


@respx.mock
async def test_run_cycle_pending_update_target_deleted_with_manual_replacement_resolves(
    tmp_path: Path,
):
    """Same starting point as the test above, but this time the user's
    live YNAB register already has a manually-typed replacement at the
    exact final amount (e.g. they deleted the confusing uncleared preauth
    placeholder and typed the correct one in themselves - confirmed live,
    2026-08-26). The fix must find and adopt it (via the same create +
    native-match-or-replace flow the "matched_manual" classify() outcome
    already uses) instead of blindly marking the row DELETED, which would
    have caused a real duplicate if a human later re-added it via the
    GUI.
    """
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
        amount=from_milliunits(-130000),
        payee_name="Merchant One",
        transaction_date="2026-08-20",
        ynab_account_id="ynab-acct-1",
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
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transactions": [{"id": "ynab-transition", "deleted": True}]}},
        )
    )
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {
                            "id": "manual-99",
                            "date": "2026-08-20",
                            "amount": -142500,
                            "payee_name": "Merchant One",
                            "category_id": "cat-1",
                            "memo": None,
                            "cleared": "cleared",
                            "approved": True,
                            "flag_color": None,
                            "import_id": None,
                            "deleted": False,
                            "subtransactions": [],
                        }
                    ]
                }
            },
        )
    )
    # Native matching does NOT fire in this mocked response (no
    # matched_transaction_id) - exercises the create-then-delete fallback
    # ending, the more consequential of the two to get right. The shadow's
    # import_id is derived dynamically (from the tracked row's own hash),
    # so echo back whatever was actually submitted rather than
    # hardcoding a value - _record_matched() looks up its row by this
    # exact echoed import_id.
    def _create_side_effect(request):
        submitted = json.loads(request.content)["transactions"][0]
        return httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-shadow-1"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-shadow-1",
                            "amount": -142500,
                            "import_id": submitted["import_id"],
                        }
                    ],
                }
            },
        )

    create_route = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(side_effect=_create_side_effect)
    delete_route = respx.delete(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions/manual-99"
    ).mock(return_value=httpx.Response(200, json={"data": {"transaction": {"deleted": True}}}))

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    assert result.updated == 0
    assert result.resolved_deleted == 0
    assert create_route.called
    assert delete_route.called

    tracked = db.get_tracked("acct-1:tx-transition")
    assert tracked["booking_status"] == "BOOKED"
    assert tracked["ynab_transaction_id"] == "ynab-shadow-1"
    assert tracked["amount_milliunits"] == -142500


@respx.mock
async def test_run_cycle_pending_update_patch_unexpectedly_400s_recovers(tmp_path: Path):
    """The bulk pre-check reports the target as alive, but the individual
    PATCH itself still gets a 400 (e.g. deleted in the split second
    between the two calls). Must fall into the same recover-or-mark-
    deleted path rather than raising and aborting the whole cycle.
    """
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
        amount=from_milliunits(-130000),
        payee_name="Merchant One",
        transaction_date="2026-08-20",
        ynab_account_id="ynab-acct-1",
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
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transactions": [{"id": "ynab-transition", "deleted": False}]}},
        )
    )
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(200, json={"data": {"transactions": []}})
    )
    respx.patch(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            400, json={"error": {"id": "400", "name": "bad_request", "detail": "gone"}}
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.resolved_deleted == 1
    tracked = db.get_tracked("acct-1:tx-transition")
    assert tracked["booking_status"] == "DELETED"


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
            account_key, booking_status, amount_milliunits, amount_decimal,
            first_seen_at, last_checked_at
        ) VALUES (?, ?, ?, ?, ?, 'PENDING', -1000, '-1.000', ?, ?)
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
        amount=from_milliunits(-13000),
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
        amount=from_milliunits(-1000),
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


def _bulk_payees_route(budget_id: str, present_ids: list[str]):
    # Minimal representation - reconcile_payee_mappings() only checks
    # PRESENCE at this phase (never absence-as-proof, never the deleted
    # flag here - see the method's own docstring for why).
    return respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/{budget_id}/payees").mock(
        return_value=httpx.Response(
            200, json={"data": {"payees": [{"id": pid, "deleted": False} for pid in present_ids]}}
        )
    )


def _payee_route(budget_id: str, payee_id: str, *, not_found: bool = False, deleted: bool = False):
    url = f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/{budget_id}/payees/{payee_id}"
    if not_found:
        return respx.get(url).mock(
            return_value=httpx.Response(
                404, json={"error": {"id": "404.2", "name": "resource_not_found"}}
            )
        )
    return respx.get(url).mock(
        return_value=httpx.Response(200, json={"data": {"payee": {"id": payee_id, "deleted": deleted}}})
    )


@respx.mock
async def test_reconcile_payee_mappings_present_in_bulk_list_skips_per_id_lookup(tmp_path: Path):
    # The key efficiency property of the hybrid design: a payee PRESENT in
    # the cheap bulk list is trusted directly - no per-id GET at all.
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")
    _bulk_payees_route("budget-1", ["payee-a"])
    per_id_route = _payee_route("budget-1", "payee-a", not_found=True)  # must never be called

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        healed = await engine.reconcile_payee_mappings()

    assert healed == 0
    assert db.get_payee_id("budget-1", "MERCHANT A") == "payee-a"
    assert per_id_route.call_count == 0


@respx.mock
async def test_reconcile_payee_mappings_absent_from_bulk_list_confirmed_404_heals(tmp_path: Path):
    # Primary real-world case, confirmed live (scripts/verify_ynab_payee_deletion.py,
    # scripts/verify_ynab_payee_get_by_id.py): a genuinely gone payee is
    # absent from the bulk list AND 404s on a direct per-id GET.
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")
    _bulk_payees_route("budget-1", [])
    _payee_route("budget-1", "payee-a", not_found=True)

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        healed = await engine.reconcile_payee_mappings()

    assert healed == 1
    assert db.get_payee_id("budget-1", "MERCHANT A") is None


@respx.mock
async def test_reconcile_payee_mappings_absent_from_bulk_list_but_still_active_is_not_healed(
    tmp_path: Path,
):
    # THE single most important test in this file: absence from the bulk
    # list alone (a transient/incomplete response, or any other reason)
    # must never be trusted - only a per-id-confirmed 404/deleted:true
    # actually heals anything. This is the safety net the hybrid design
    # hinges on.
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")
    _bulk_payees_route("budget-1", [])  # absent from the bulk list...
    _payee_route("budget-1", "payee-a", deleted=False)  # ...but confirmed still active

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        healed = await engine.reconcile_payee_mappings()

    assert healed == 0
    assert db.get_payee_id("budget-1", "MERCHANT A") == "payee-a"


@respx.mock
async def test_reconcile_payee_mappings_deletes_rows_confirmed_deleted_flag(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")
    _bulk_payees_route("budget-1", [])
    _payee_route("budget-1", "payee-a", deleted=True)

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        healed = await engine.reconcile_payee_mappings()

    assert healed == 1
    assert db.get_payee_id("budget-1", "MERCHANT A") is None


@respx.mock
async def test_reconcile_payee_mappings_is_per_budget_scoped(tmp_path: Path):
    config = make_config()  # budgets: personal->budget-1, shared->budget-2
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")
    await db.upsert_payee_mapping("budget-2", "MERCHANT A", "payee-a")
    _bulk_payees_route("budget-1", [])
    _payee_route("budget-1", "payee-a", not_found=True)
    budget_2_bulk_route = _bulk_payees_route("budget-2", ["payee-a"])  # present -> no per-id call

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        healed = await engine.reconcile_payee_mappings()

    assert healed == 1
    assert db.get_payee_id("budget-1", "MERCHANT A") is None
    assert db.get_payee_id("budget-2", "MERCHANT A") == "payee-a"
    assert budget_2_bulk_route.called


@respx.mock
async def test_reconcile_payee_mappings_skips_payee_on_lookup_failure(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")
    await db.upsert_payee_mapping("budget-1", "MERCHANT B", "payee-b")
    _bulk_payees_route("budget-1", [])
    respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/payees/payee-a"
    ).mock(return_value=httpx.Response(500))
    _payee_route("budget-1", "payee-b", not_found=True)

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        healed = await engine.reconcile_payee_mappings()  # must not raise

    assert healed == 1
    assert db.get_payee_id("budget-1", "MERCHANT A") == "payee-a"  # untouched - lookup failed
    assert db.get_payee_id("budget-1", "MERCHANT B") is None


@respx.mock
async def test_reconcile_payee_mappings_skips_budget_on_bulk_fetch_failure(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")
    await db.upsert_payee_mapping("budget-2", "MERCHANT A", "payee-a")
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/payees").mock(
        return_value=httpx.Response(500)
    )
    _bulk_payees_route("budget-2", [])
    _payee_route("budget-2", "payee-a", not_found=True)

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        healed = await engine.reconcile_payee_mappings()  # must not raise

    assert healed == 1
    assert db.get_payee_id("budget-1", "MERCHANT A") == "payee-a"  # untouched - bulk fetch failed
    assert db.get_payee_id("budget-2", "MERCHANT A") is None


@respx.mock
async def test_reconcile_payee_mappings_is_noop_when_nothing_cached(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        healed = await engine.reconcile_payee_mappings()  # no cached payee_ids -> no HTTP calls at all

    assert healed == 0


@respx.mock
async def test_reconcile_payee_mappings_is_rate_limited_within_the_same_run(tmp_path: Path):
    config = make_config()
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")
    bulk_route = _bulk_payees_route("budget-1", [])
    per_id_route = _payee_route("budget-1", "payee-a", not_found=True)

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        first = await engine.reconcile_payee_mappings()
        second = await engine.reconcile_payee_mappings()

    assert first == 1
    assert second == 0  # rate-limited - mark_payee_reconcile_done() just ran
    assert bulk_route.call_count == 1  # no second HTTP call at all
    assert per_id_route.call_count == 1


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
        amount=from_milliunits(amount_milliunits),
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

    dedup_key = file_dedup.compute_dedup_key(row.date, to_milliunits(row.amount), row.payee_name, row.memo)
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

    dedup_key = file_dedup.compute_dedup_key(row.date, to_milliunits(row.amount), row.payee_name, row.memo)
    tracking_key = get_file_tracking_key("ynab-acct-1", dedup_key)
    await db.upsert_tracked(
        tracking_key,
        import_id=derive_file_import_id(tracking_key),
        ynab_transaction_id="ynab-existing",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount=from_milliunits(-12500),
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
        amount=from_milliunits(-12500),
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
        amount=from_milliunits(-130000),
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
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transactions": [{"id": "ynab-transition", "deleted": False}]}},
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

    dedup_key = file_dedup.compute_dedup_key(row.date, to_milliunits(row.amount), row.payee_name, row.memo)
    tracking_key = get_file_tracking_key("ynab-acct-1", dedup_key)
    await db.upsert_tracked(
        tracking_key,
        import_id=derive_file_import_id(tracking_key),
        ynab_transaction_id="ynab-existing",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount=from_milliunits(-12500),
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

    dedup_key = file_dedup.compute_dedup_key(row.date, to_milliunits(row.amount), row.payee_name, row.memo)
    tracking_key = get_file_tracking_key("ynab-acct-1", dedup_key)
    await db.upsert_tracked(
        tracking_key,
        import_id=derive_file_import_id(tracking_key),
        ynab_transaction_id="ynab-existing",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount=from_milliunits(-12500),
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


# --- Manual-transaction matching (see CLAUDE.md's "Manual-transaction
# matching" section) ---------------------------------------------------


def _unimported_url(budget_id: str, ynab_account_id: str) -> str:
    return (
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/{budget_id}"
        f"/accounts/{ynab_account_id}/transactions"
    )


@respx.mock
async def test_run_cycle_matches_unambiguous_manual_transaction(tmp_path: Path):
    """Exact-amount manual match: YNAB's own native transaction-matching
    fires (matched_transaction_id set on the newly-created shadow),
    confirmed live via scripts/verify_ynab_native_match_amount_sensitivity.py.
    The original stays fully visible, untouched, holding the user's own
    categorization - the code must NOT delete it. No delete route is
    mocked at all here (same "prove it's never called" pattern the
    ambiguous-match test below already uses) - a wrongful delete attempt
    would fail this test with a routing error.
    """
    config = make_config()
    config.sync.manual_match_window_days = 3
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-mm-1",
                        "nonUniqueId": "tx-mm-1",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -463.54,
                        "description": "TELIA NORGE AS,TELIA",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {
                            "id": "manual-1",
                            "date": "2026-08-19",
                            "amount": -463540,
                            "payee_id": "payee-manual-1",
                            "payee_name": "Telia",
                            "category_id": "cat-1",
                            "memo": "user's own note",
                            "cleared": "cleared",
                            "approved": True,
                            "flag_color": "blue",
                            "import_id": None,
                            "deleted": False,
                            "subtransactions": [],
                        }
                    ]
                }
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-mm-1")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-replacement", "manual-1"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-tx-replacement",
                            "import_id": import_id,
                            "payee_name": "Telia",
                            "amount": -463540,
                            "matched_transaction_id": "manual-1",
                        },
                        {
                            # YNAB's native matching echoes the still-live
                            # original back in the same response too -
                            # approved flipped to false, matched_
                            # transaction_id set on both sides - confirmed
                            # live via scripts/verify_ynab_native_match_
                            # amount_sensitivity.py.
                            "id": "manual-1",
                            "import_id": None,
                            "payee_name": "Telia",
                            "amount": -463540,
                            "matched_transaction_id": "ynab-tx-replacement",
                            "approved": False,
                        },
                    ],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1

    tracked = db.get_tracked("acct-1:tx-mm-1")
    assert tracked is not None
    # The VISIBLE original's id, not the shadow's - see _record_matched().
    assert tracked["ynab_transaction_id"] == "manual-1"
    assert tracked["import_id"] == import_id
    assert tracked["booking_status"] == "BOOKED"

    # Audit-logged as "updated", not "created" - nothing new appeared in the
    # user's register, the pre-existing manual transaction just got a real
    # import_id anchored onto it via the hidden shadow. Confirmed against
    # real production data (2026-08-26) that "created" miscategorized this.
    events, _total = db.list_audit_events()
    matched_event = next(e for e in events if "natively linked" in e["detail"])
    assert matched_event["event_type"] == "updated"

    # Payee preference learned from the matched candidate's own payee_id,
    # so a future "Telia" transaction with no manual match resolves to the
    # same payee (invariant 12).
    assert db.get_payee_id("budget-1", "TELIA NORGE AS,TELIA") == "payee-manual-1"


@respx.mock
async def test_run_cycle_manual_match_falls_back_to_delete_recreate_when_native_match_fails(
    tmp_path: Path,
):
    """When YNAB's native transaction-matching does NOT fire (its date-gap
    sensitivity is confirmed non-monotonic/undocumented - see invariant 8's
    native-matching caveat), the code must fall back to the older
    create-then-delete behavior so a visible duplicate is never left
    behind. Same setup as test_run_cycle_matches_unambiguous_manual_
    transaction, but the mocked create response carries no
    matched_transaction_id at all.
    """
    config = make_config()
    config.sync.manual_match_window_days = 3
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-mm-1",
                        "nonUniqueId": "tx-mm-1",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -463.54,
                        "description": "TELIA NORGE AS,TELIA",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {
                            "id": "manual-1",
                            "date": "2026-08-19",
                            "amount": -463540,
                            "payee_id": "payee-manual-1",
                            "payee_name": "Telia",
                            "category_id": "cat-1",
                            "memo": "user's own note",
                            "cleared": "cleared",
                            "approved": True,
                            "flag_color": "blue",
                            "import_id": None,
                            "deleted": False,
                            "subtransactions": [],
                        }
                    ]
                }
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-mm-1")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-replacement"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-tx-replacement",
                            "import_id": import_id,
                            "payee_name": "Telia",
                            "amount": -463540,
                        }
                    ],
                }
            },
        )
    )
    delete_route = respx.delete(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions/manual-1"
    ).mock(return_value=httpx.Response(200, json={"data": {"transaction": {"id": "manual-1", "deleted": True}}}))

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    assert delete_route.called

    tracked = db.get_tracked("acct-1:tx-mm-1")
    assert tracked is not None
    assert tracked["ynab_transaction_id"] == "ynab-tx-replacement"
    assert tracked["import_id"] == import_id
    assert tracked["booking_status"] == "BOOKED"


@respx.mock
async def test_run_cycle_ambiguous_manual_match_falls_through_to_create(tmp_path: Path):
    config = make_config()
    config.sync.manual_match_window_days = 3
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-mm-2",
                        "nonUniqueId": "tx-mm-2",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -463.54,
                        "description": "TELIA NORGE AS,TELIA",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    # Two same-amount, in-window candidates - ambiguous, must not guess.
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {
                            "id": "manual-a",
                            "date": "2026-08-19",
                            "amount": -463540,
                            "payee_id": "payee-a",
                            "import_id": None,
                            "deleted": False,
                            "subtransactions": [],
                        },
                        {
                            "id": "manual-b",
                            "date": "2026-08-21",
                            "amount": -463540,
                            "payee_id": "payee-b",
                            "import_id": None,
                            "deleted": False,
                            "subtransactions": [],
                        },
                    ]
                }
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-mm-2")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-new"],
                    "duplicate_import_ids": [],
                    "transactions": [{"id": "ynab-tx-new", "import_id": import_id, "amount": -463540}],
                }
            },
        )
    )
    # Deliberately no DELETE route mocked - if the code wrongly tried to
    # delete either ambiguous candidate, this test would fail with a
    # routing error, proving neither was touched.

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    tracked = db.get_tracked("acct-1:tx-mm-2")
    assert tracked is not None
    assert tracked["ynab_transaction_id"] == "ynab-tx-new"


@respx.mock
async def test_run_cycle_manual_match_disabled_by_default(tmp_path: Path):
    # manual_match_window_days defaults to 0 - no manual-match lookup call
    # should ever be made, and the transaction is created normally.
    config = make_config()
    assert config.sync.manual_match_window_days == 0
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-mm-3",
                        "nonUniqueId": "tx-mm-3",
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
    # Deliberately no mock for the unimported-transactions lookup URL - if
    # the code called it despite the feature being disabled, this test
    # would fail with a routing error.
    import_id = derive_import_id("acct-1:tx-mm-3")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-3"],
                    "duplicate_import_ids": [],
                    "transactions": [{"id": "ynab-tx-3", "import_id": import_id, "amount": -50000}],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    assert db.get_tracked("acct-1:tx-mm-3") is not None


@respx.mock
async def test_run_cycle_manual_match_ignores_amount_mismatch(tmp_path: Path):
    config = make_config()
    config.sync.manual_match_window_days = 3
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-mm-4",
                        "nonUniqueId": "tx-mm-4",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -463.54,
                        "description": "TELIA NORGE AS,TELIA",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {
                            "id": "manual-diff-amount",
                            "date": "2026-08-19",
                            "amount": -100000,  # different amount - must not match
                            "payee_id": "payee-x",
                            "import_id": None,
                            "deleted": False,
                            "subtransactions": [],
                        }
                    ]
                }
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-mm-4")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-4"],
                    "duplicate_import_ids": [],
                    "transactions": [{"id": "ynab-tx-4", "import_id": import_id, "amount": -463540}],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    assert db.get_tracked("acct-1:tx-mm-4") is not None


@respx.mock
async def test_run_cycle_manual_match_working_days_toggle(tmp_path: Path):
    # A manual entry on Friday 2026-08-21 and the real bank charge on
    # Monday 2026-08-24 are 1 working day apart but 3 calendar days apart
    # (see tests/unit/test_date_window.py). window_days=1 with
    # match_window_unit="working_days" must match; the same window would
    # NOT match under the "calendar_days" default (3 > 1) - this proves the
    # toggle is actually wired through, not just accepted and ignored.
    config = make_config()
    config.sync.manual_match_window_days = 1
    config.sync.match_window_unit = "working_days"
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-mm-5",
                        "nonUniqueId": "tx-mm-5",
                        "accountKey": "acct-1",
                        "date": "2026-08-24",
                        "amount": -463.54,
                        "description": "TELIA NORGE AS,TELIA",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {
                            "id": "manual-friday",
                            "date": "2026-08-21",
                            "amount": -463540,
                            "payee_id": "payee-friday",
                            "import_id": None,
                            "deleted": False,
                            "subtransactions": [],
                        }
                    ]
                }
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-mm-5")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-replacement-5"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-tx-replacement-5", "import_id": import_id, "amount": -463540}
                    ],
                }
            },
        )
    )
    delete_route = respx.delete(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions/manual-friday"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": {"transaction": {"id": "manual-friday", "deleted": True}}}
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    assert delete_route.called
    tracked = db.get_tracked("acct-1:tx-mm-5")
    assert tracked is not None
    assert tracked["ynab_transaction_id"] == "ynab-tx-replacement-5"


# --- PENDING-transaction import (see CLAUDE.md's "PENDING-transaction
# import" section) --------------------------------------------------------


def _mock_creditcard_account(account_key: str = "acct-1") -> None:
    respx.get(sb1_client.ACCOUNTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"accounts": [{"accountKey": account_key, "name": "Card", "type": "CREDITCARD"}]},
        )
    )


@respx.mock
async def test_run_cycle_creates_pending_placeholder_when_enabled(tmp_path: Path):
    config = make_config()
    config.sync.pending_import_enabled = True
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    _mock_creditcard_account()
    # pending_import_enabled alone now also fetches manual-match candidates
    # (see the PENDING-vs-manual-entry decoupling fix) - this account has
    # no manually-typed duplicates, so the list is empty.
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(200, json={"data": {"transactions": []}})
    )
    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "nonUniqueId": "111111111",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -79,
                        "description": "MERCHANT ONE, LOC-A",
                        "bookingStatus": "PENDING",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:111111111")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-pending-1"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-pending-1", "import_id": import_id, "amount": -79000}
                    ],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    tracked = db.get_tracked("acct-1:111111111")
    assert tracked is not None
    assert tracked["booking_status"] == "PENDING"
    assert tracked["cleared"] == "uncleared"
    assert tracked["amount_milliunits"] == -79000


@respx.mock
async def test_run_cycle_pending_import_disabled_by_default_regression(tmp_path: Path):
    # Deliberately does NOT mock ACCOUNTS_URL - if fetch() wrongly called
    # list_accounts() with the toggle off, this test would fail with a
    # respx routing error, proving the zero-extra-calls claim.
    config = make_config()
    assert config.sync.pending_import_enabled is False
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "nonUniqueId": "111111111",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -79,
                        "description": "MERCHANT ONE, LOC-A",
                        "bookingStatus": "PENDING",
                    }
                ]
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 0
    assert db.get_tracked("acct-1:111111111") is None


@respx.mock
async def test_run_cycle_fuzzy_matches_pending_to_booked_across_different_tracking_keys(
    tmp_path: Path,
):
    config = make_config()
    config.sync.pending_import_enabled = True
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    _mock_creditcard_account()
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(200, json={"data": {"transactions": []}})
    )

    # Use a date 2 days ago so it's within the 5-day window (avoids date-rot)
    past_date = (datetime.now(UTC).date() - timedelta(days=2)).isoformat()

    call_count = {"n": 0}

    def transactions_side_effect(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "nonUniqueId": "111111111",
                            "accountKey": "acct-1",
                            "date": past_date,
                            "amount": -79,
                            "description": "MERCHANT ONE, LOC-A",
                            "bookingStatus": "PENDING",
                        }
                    ]
                },
            )
        # A different real-world observation of the SAME purchase, now
        # booked - a completely different tracking key (confirmed live
        # behavior, see CLAUDE.md), a decimal amount within tolerance, and
        # a slightly-drifted payee (a trailing country suffix appended).
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "creditCardIdentifiers": {"nonUniqueId": "222222222"},
                        "nonUniqueId": "unused-when-cc-identifiers-present",
                        "accountKey": "acct-1",
                        "date": past_date,
                        "amount": -79.1,
                        "description": "MERCHANT ONE, LOC-A, NOR",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )

    respx.get(sb1_client.TRANSACTIONS_URL).mock(side_effect=transactions_side_effect)

    pending_import_id = derive_import_id("acct-1:111111111")
    create_route = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-pending-1"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-pending-1", "import_id": pending_import_id, "amount": -79000}
                    ],
                }
            },
        )
    )
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transactions": [{"id": "ynab-pending-1", "deleted": False}]}},
        )
    )
    patch_route = respx.patch(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(return_value=httpx.Response(200, json={"data": {"transaction_ids": ["ynab-pending-1"]}}))

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        first_result, _ = await engine.run_cycle()
        second_result, _ = await engine.run_cycle()

    assert first_result.created == 1
    assert second_result.created == 0
    assert second_result.updated == 1
    assert create_route.call_count == 1

    sent_body = json.loads(patch_route.calls.last.request.content)
    assert sent_body["transactions"] == [
        {"id": "ynab-pending-1", "cleared": "cleared", "amount": -79100}
    ]

    assert db.get_tracked("acct-1:111111111") is None
    tracked = db.get_tracked("acct-1:222222222")
    assert tracked is not None
    assert tracked["booking_status"] == "BOOKED"
    assert tracked["amount_milliunits"] == -79100
    assert tracked["ynab_transaction_id"] == "ynab-pending-1"


@respx.mock
async def test_run_cycle_fuzzy_transition_retry_safe(tmp_path: Path):
    config = make_config()
    config.sync.pending_import_enabled = True
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    await db.upsert_tracked(
        "acct-1:111111111",
        import_id=derive_import_id("acct-1:111111111"),
        ynab_transaction_id="ynab-pending-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="PENDING",
        amount=from_milliunits(-79000),
        payee_name="MERCHANT ONE, LOC-A",
    )

    _mock_creditcard_account()
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(200, json={"data": {"transactions": []}})
    )

    # Use a date 2 days ago so it's within the 5-day window (avoids date-rot)
    past_date = (datetime.now(UTC).date() - timedelta(days=2)).isoformat()

    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "creditCardIdentifiers": {"nonUniqueId": "222222222"},
                        "accountKey": "acct-1",
                        "date": past_date,
                        "amount": -79.1,
                        "description": "MERCHANT ONE, LOC-A, NOR",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transactions": [{"id": "ynab-pending-1", "deleted": False}]}},
        )
    )
    respx.patch(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(200, json={"data": {"transaction_ids": ["ynab-pending-1"]}})
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        classified = await engine.fetch_and_classify()
        first_result, _ = await engine.submit(classified)
        second_result, _ = await engine.submit(classified)  # simulated retry

    assert first_result.updated == 1
    assert second_result.updated == 1  # counted per-call, but rekey itself is a no-op the 2nd time

    tracked = db.get_tracked("acct-1:222222222")
    assert tracked is not None
    assert tracked["booking_status"] == "BOOKED"
    assert tracked["amount_milliunits"] == -79100

    events, _total = db.list_audit_events()
    updated_events = [e for e in events if e["event_type"] == "updated"]
    assert len(updated_events) == 1  # not double-written on the retry


@respx.mock
async def test_run_cycle_tolerant_manual_match_replaces_with_pending_transaction(tmp_path: Path):
    """Tolerant (PENDING) manual match: the submitted amount is now the
    matched candidate's own exact amount (not the bank's rounded preauth
    amount) specifically so YNAB's native transaction-matching fires -
    confirmed live via scripts/verify_ynab_native_match_amount_sensitivity.py
    that even a 0.42 kr difference suppresses it. Same "leave both alone,
    original stays visible" outcome as the exact/BOOKED matcher - no delete
    route mocked at all, proving the original is never touched.
    """
    config = make_config()
    config.sync.pending_import_enabled = True
    config.sync.manual_match_window_days = 3
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    _mock_creditcard_account()
    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "nonUniqueId": "333333333",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -218,
                        "description": "MERCHANT ONE, LOC-A",
                        "bookingStatus": "PENDING",
                    }
                ]
            },
        )
    )
    # Hand-typed by the user with the real decimal amount - not an exact
    # match to the pending preauth's whole-kroner amount, but within
    # tolerance, which is exactly why the tolerant matcher exists.
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {
                            "id": "manual-6",
                            "date": "2026-08-19",
                            "amount": -218300,
                            "payee_id": "payee-manual-6",
                            "payee_name": "Merchant One",
                            "category_id": "cat-1",
                            "memo": None,
                            "cleared": "cleared",
                            "approved": True,
                            "flag_color": None,
                            "import_id": None,
                            "deleted": False,
                            "subtransactions": [],
                        }
                    ]
                }
            },
        )
    )
    import_id = derive_import_id("acct-1:333333333")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-replacement-6", "manual-6"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-replacement-6",
                            "import_id": import_id,
                            "amount": -218300,
                            "matched_transaction_id": "manual-6",
                        },
                        {
                            "id": "manual-6",
                            "import_id": None,
                            "payee_name": "Merchant One",
                            "amount": -218300,
                            "matched_transaction_id": "ynab-replacement-6",
                            "approved": False,
                        },
                    ],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    tracked = db.get_tracked("acct-1:333333333")
    assert tracked is not None
    # The VISIBLE original's id, not the shadow's - see _record_matched().
    assert tracked["ynab_transaction_id"] == "manual-6"
    assert tracked["booking_status"] == "PENDING"
    assert tracked["cleared"] == "uncleared"
    # The submitted (and now tracked) amount is the matched candidate's own
    # exact amount, not the bank's rounded preauth - required for native
    # matching to fire at all. Any tiny remaining difference from the real
    # settled amount self-corrects once BOOKED via the existing fuzzy
    # pending->booked pipeline (unchanged - see the tests above).
    assert tracked["amount_milliunits"] == -218300
    assert db.get_payee_id("budget-1", "MERCHANT ONE, LOC-A") == "payee-manual-6"


@respx.mock
async def test_run_cycle_tolerant_manual_match_falls_back_when_native_match_fails(
    tmp_path: Path,
):
    """Same scenario as test_run_cycle_tolerant_manual_match_replaces_with_
    pending_transaction, but the mocked create response carries no
    matched_transaction_id - proving the code falls back to delete-then-
    recreate (today's pre-native-match behavior) rather than leaving a
    visible duplicate when YNAB's native matching doesn't cooperate.
    """
    config = make_config()
    config.sync.pending_import_enabled = True
    config.sync.manual_match_window_days = 3
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    _mock_creditcard_account()
    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "nonUniqueId": "333333333",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -218,
                        "description": "MERCHANT ONE, LOC-A",
                        "bookingStatus": "PENDING",
                    }
                ]
            },
        )
    )
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {
                            "id": "manual-6",
                            "date": "2026-08-19",
                            "amount": -218300,
                            "payee_id": "payee-manual-6",
                            "payee_name": "Merchant One",
                            "category_id": "cat-1",
                            "memo": None,
                            "cleared": "cleared",
                            "approved": True,
                            "flag_color": None,
                            "import_id": None,
                            "deleted": False,
                            "subtransactions": [],
                        }
                    ]
                }
            },
        )
    )
    import_id = derive_import_id("acct-1:333333333")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-replacement-6"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-replacement-6", "import_id": import_id, "amount": -218300}
                    ],
                }
            },
        )
    )
    delete_route = respx.delete(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions/manual-6"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": {"transaction": {"id": "manual-6", "deleted": True}}}
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    assert delete_route.called
    tracked = db.get_tracked("acct-1:333333333")
    assert tracked is not None
    assert tracked["ynab_transaction_id"] == "ynab-replacement-6"
    assert tracked["booking_status"] == "PENDING"
    assert tracked["cleared"] == "uncleared"
    assert tracked["amount_milliunits"] == -218300


@respx.mock
async def test_run_cycle_pending_manual_match_independent_of_manual_match_window_days(
    tmp_path: Path,
):
    # Confirmed live in production (2026-08-25): with pending_import_enabled
    # on and manual_match_window_days at its default of 0, 3 of 9 PENDING
    # imports duplicated transactions the user had already typed into YNAB
    # by hand, because manual-matching used to be gated entirely behind
    # manual_match_window_days. A PENDING transaction colliding with a
    # manual entry is expected, first-class behavior for pending-import
    # specifically - it must be tried regardless of the separate,
    # BOOKED-only manual_match_window_days toggle (see the next test for
    # confirmation that toggle is unaffected for BOOKED transactions).
    config = make_config()
    config.sync.pending_import_enabled = True
    assert config.sync.manual_match_window_days == 0
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    _mock_creditcard_account()
    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "nonUniqueId": "444444444",
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -218,
                        "description": "MERCHANT ONE, LOC-A",
                        "bookingStatus": "PENDING",
                    }
                ]
            },
        )
    )
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {
                            "id": "manual-7",
                            "date": "2026-08-19",
                            "amount": -218300,
                            "payee_id": "payee-manual-7",
                            "payee_name": "Merchant One",
                            "category_id": "cat-1",
                            "memo": None,
                            "cleared": "cleared",
                            "approved": True,
                            "flag_color": None,
                            "import_id": None,
                            "deleted": False,
                            "subtransactions": [],
                        }
                    ]
                }
            },
        )
    )
    import_id = derive_import_id("acct-1:444444444")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-replacement-7"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-replacement-7", "import_id": import_id, "amount": -218000}
                    ],
                }
            },
        )
    )
    delete_route = respx.delete(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions/manual-7"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": {"transaction": {"id": "manual-7", "deleted": True}}}
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    assert delete_route.called
    tracked = db.get_tracked("acct-1:444444444")
    assert tracked is not None
    assert tracked["booking_status"] == "PENDING"


@respx.mock
async def test_run_cycle_booked_manual_match_still_requires_window_days_when_pending_import_enabled(
    tmp_path: Path,
):
    # The flip side of the previous test: pending_import_enabled must NOT
    # silently turn on manual-matching for already-BOOKED transactions too
    # - that keeps its own separate, unchanged "no strong signal, off by
    # default" opt-in (manual_match_window_days). The candidate list IS
    # still fetched (pending_import_enabled alone is enough to fetch it,
    # since a PENDING transaction elsewhere in the same account might need
    # it), but a BOOKED transaction must not be matched against it.
    config = make_config()
    config.sync.pending_import_enabled = True
    assert config.sync.manual_match_window_days == 0
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    _mock_creditcard_account()
    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "id": "tx-booked-mm",
                        "nonUniqueId": "tx-booked-mm",
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
    # An exact-amount candidate is available - if the BOOKED path wrongly
    # ignored manual_match_window_days too, this would get matched instead
    # of a fresh create.
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {
                            "id": "manual-8",
                            "date": "2026-08-20",
                            "amount": -50000,
                            "payee_id": "payee-manual-8",
                            "payee_name": "Coffee",
                            "category_id": "cat-1",
                            "memo": None,
                            "cleared": "cleared",
                            "approved": True,
                            "flag_color": None,
                            "import_id": None,
                            "deleted": False,
                            "subtransactions": [],
                        }
                    ]
                }
            },
        )
    )
    import_id = derive_import_id("acct-1:tx-booked-mm")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-tx-booked-mm"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-tx-booked-mm", "import_id": import_id, "amount": -50000}
                    ],
                }
            },
        )
    )
    # Deliberately no mock for a DELETE of manual-8 - if the code wrongly
    # matched and tried to delete it, this test would fail with a routing
    # error.

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    tracked = db.get_tracked("acct-1:tx-booked-mm")
    assert tracked is not None
    assert tracked["ynab_transaction_id"] == "ynab-tx-booked-mm"


@respx.mock
async def test_run_cycle_pending_manual_match_row_later_fuzzy_resolves_when_booked(
    tmp_path: Path,
):
    """Chains the previous test's scenario into a second cycle to prove the
    scenario-1 (self-created placeholder) / scenario-2 (tolerant-manual-
    match placeholder) convergence actually works end-to-end: both kinds of
    PENDING placeholder are found by the exact same list_pending_candidates
    query and resolved by the exact same fuzzy matcher.
    """
    config = make_config()
    config.sync.pending_import_enabled = True
    config.sync.manual_match_window_days = 3
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)

    _mock_creditcard_account()

    # Use relative dates within the 5-day window (avoids date-rot)
    pending_date = (datetime.now(UTC).date() - timedelta(days=2)).isoformat()
    manual_date = (datetime.now(UTC).date() - timedelta(days=3)).isoformat()

    tx_call_count = {"n": 0}

    def transactions_side_effect(request):
        tx_call_count["n"] += 1
        if tx_call_count["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "transactions": [
                        {
                            "nonUniqueId": "444444444",
                            "accountKey": "acct-1",
                            "date": pending_date,
                            "amount": -218,
                            "description": "MERCHANT ONE, LOC-A",
                            "bookingStatus": "PENDING",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "creditCardIdentifiers": {"nonUniqueId": "555555555"},
                        "accountKey": "acct-1",
                        "date": pending_date,
                        "amount": -218.3,
                        "description": "MERCHANT ONE, LOC-A, NOR",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )

    respx.get(sb1_client.TRANSACTIONS_URL).mock(side_effect=transactions_side_effect)

    manual_call_count = {"n": 0}

    def manual_side_effect(request):
        manual_call_count["n"] += 1
        if manual_call_count["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "transactions": [
                            {
                                "id": "manual-7",
                                "date": manual_date,
                                "amount": -218300,
                                "payee_id": "payee-manual-7",
                                "payee_name": "Merchant One",
                                "category_id": "cat-1",
                                "memo": None,
                                "cleared": "cleared",
                                "approved": True,
                                "flag_color": None,
                                "import_id": None,
                                "deleted": False,
                                "subtransactions": [],
                            }
                        ]
                    }
                },
            )
        # Already deleted last cycle - a real second poll would see nothing.
        return httpx.Response(200, json={"data": {"transactions": []}})

    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(side_effect=manual_side_effect)

    import_id = derive_import_id("acct-1:444444444")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-replacement-7"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {
                            "id": "ynab-replacement-7",
                            "import_id": import_id,
                            "amount": -218000,
                            # YNAB resolves the submitted payee_id and echoes
                            # its name back - the tracked row's payee_name
                            # comes from THIS, not the request payload (see
                            # _record_matched). Needed for cycle 2's fuzzy
                            # payee-similarity check to have anything to
                            # compare against.
                            "payee_name": "MERCHANT ONE, LOC-A",
                        }
                    ],
                }
            },
        )
    )
    respx.delete(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions/manual-7"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": {"transaction": {"id": "manual-7", "deleted": True}}}
        )
    )
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"transactions": [{"id": "ynab-replacement-7", "deleted": False}]}},
        )
    )
    respx.patch(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200, json={"data": {"transaction_ids": ["ynab-replacement-7"]}}
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        first_result, _ = await engine.run_cycle()
        second_result, _ = await engine.run_cycle()

    assert first_result.created == 1
    assert second_result.updated == 1
    assert second_result.created == 0

    assert db.get_tracked("acct-1:444444444") is None
    tracked = db.get_tracked("acct-1:555555555")
    assert tracked is not None
    assert tracked["booking_status"] == "BOOKED"
    assert tracked["ynab_transaction_id"] == "ynab-replacement-7"
    assert tracked["amount_milliunits"] == -218300


@respx.mock
async def test_run_cycle_ambiguous_fuzzy_match_falls_through_to_new_create(tmp_path: Path):
    config = make_config()
    config.sync.pending_import_enabled = True
    token_store = make_token_store(tmp_path)
    db = make_db(tmp_path)
    await seed_mappings(db, config)
    # Two pending placeholders, same account, identical (normalized) payee
    # text - both plausible, and payee similarity can't break the tie
    # (both score 1.0 against the booked payee) - must not guess, matching
    # this project's established "don't guess on ambiguity" stance.
    await db.upsert_tracked(
        "acct-1:aaa",
        import_id=derive_import_id("acct-1:aaa"),
        ynab_transaction_id="ynab-aaa",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="PENDING",
        amount=from_milliunits(-100000),
        payee_name="MERCHANT ONE, LOC-A",
    )
    await db.upsert_tracked(
        "acct-1:bbb",
        import_id=derive_import_id("acct-1:bbb"),
        ynab_transaction_id="ynab-bbb",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="PENDING",
        amount=from_milliunits(-101000),
        payee_name="MERCHANT ONE, LOC-A",
    )

    _mock_creditcard_account()
    respx.get(_unimported_url("budget-1", "ynab-acct-1")).mock(
        return_value=httpx.Response(200, json={"data": {"transactions": []}})
    )
    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {
                        "creditCardIdentifiers": {"nonUniqueId": "ccc"},
                        "accountKey": "acct-1",
                        "date": "2026-08-20",
                        "amount": -100.5,
                        "description": "MERCHANT ONE, LOC-A",
                        "bookingStatus": "BOOKED",
                    }
                ]
            },
        )
    )
    import_id = derive_import_id("acct-1:ccc")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-new-amb"],
                    "duplicate_import_ids": [],
                    "transactions": [
                        {"id": "ynab-new-amb", "import_id": import_id, "amount": -100500}
                    ],
                }
            },
        )
    )

    async with httpx.AsyncClient() as http_client:
        engine = make_engine(config, http_client, token_store, db)
        result, _ = await engine.run_cycle()

    assert result.created == 1
    assert result.updated == 0

    # Neither stale placeholder was touched - a stuck, visually-obvious
    # uncleared entry, not a silently-wrong resolution.
    assert db.get_tracked("acct-1:aaa")["booking_status"] == "PENDING"
    assert db.get_tracked("acct-1:bbb")["booking_status"] == "PENDING"
    tracked = db.get_tracked("acct-1:ccc")
    assert tracked is not None
    assert tracked["booking_status"] == "BOOKED"
