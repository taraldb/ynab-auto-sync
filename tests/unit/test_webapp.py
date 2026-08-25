import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from tests.conftest import make_engine, seed_mappings_sync
from ynab_auto_sync.config import (
    AccountMapping,
    AppConfig,
    MqttConfig,
    SpareBank1Config,
    YnabConfig,
)
from ynab_auto_sync.providers.base import (
    ProviderAccount,
    ProviderAuthRequiredError,
    TransactionProvider,
)
from ynab_auto_sync.providers.sparebank1.auth import TokenStore
from ynab_auto_sync.providers.sparebank1.transform import derive_import_id
from ynab_auto_sync.state import JsonStateStore
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp.app import create_app
from ynab_auto_sync.ynab import client as ynab_client

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "norwegian_bank_sample.xlsx"


class FakeProvider(TransactionProvider):
    """Minimal TransactionProvider double for /api/providers tests - can be
    configured to return real accounts, raise ProviderAuthRequiredError, or
    raise an arbitrary error, so all three response branches are exercised
    without needing a real SpareBank1Provider."""

    def __init__(self, accounts=None, auth_required=False, error=None):
        self._accounts = accounts or []
        self._auth_required = auth_required
        self._error = error
        self.list_accounts_calls: list[bool] = []

    @staticmethod
    def type_name() -> str:
        return "fake"

    async def list_accounts(self, force_refresh: bool = False):
        self.list_accounts_calls.append(force_refresh)
        if self._auth_required:
            raise ProviderAuthRequiredError("re-authentication required")
        if self._error is not None:
            raise self._error
        return self._accounts

    async def fetch(self, since_by_account, on_skip=None):
        return []


class FakeScheduler:
    """Duck-typed Scheduler double - only request_sync_now() is ever called
    through the route, so a real Scheduler (which needs a NotificationSink,
    stop_event, etc.) would be unnecessary machinery for this test."""

    def __init__(self):
        self.called = False

    def request_sync_now(self) -> None:
        self.called = True


def make_config(accounts=None) -> AppConfig:
    return AppConfig(
        providers={"sparebank1": SpareBank1Config(
            client_id="cid", client_secret="secret", redirect_uri="http://localhost:8765/callback"
        )},
        ynab=YnabConfig(personal_access_token="pat", budgets={"personal": "budget-1"}),
        accounts=accounts
        if accounts is not None
        else [
            AccountMapping(
                sparebank1_account_key="acct-1",
                ynab_account_id="ynab-acct-1",
                ynab_budget="personal",
                display_name="Test Account",
                import_source_name="Norwegian Bank",
            )
        ],
        mqtt=MqttConfig(host="mqtt.local"),
    )


def make_token_store(tmp_path: Path, config: AppConfig) -> TokenStore:
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
    return TokenStore(store, config.providers["sparebank1"])


def make_client(tmp_path: Path, accounts=None, providers_map=None, scheduler=None, ws_manager=None):
    config = make_config(accounts=accounts)
    seed_mappings_sync(tmp_path / "state.db", config)
    db = StateDB(tmp_path / "state.db")
    token_store = make_token_store(tmp_path, config)
    http_client = httpx.AsyncClient()
    engine = make_engine(config, http_client, token_store, db)
    app = create_app(config, engine, db, providers_map, scheduler, ws_manager)
    return TestClient(app), db


def test_get_status_returns_run_metadata_and_accounts(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.get("/api/status")

    assert response.status_code == 200
    body = response.json()
    assert body["run_metadata"]["paused"] is False
    assert body["accounts"] == [
        {
            "key": "acct-1",
            "display_name": "Test Account",
            # Mappings store the resolved budget id, but this is rendered
            # straight into the dashboard's accounts table, so it's mapped
            # back to the human-readable config alias - a raw UUID here
            # would be a visible regression from the pre-mappings behaviour.
            "ynab_budget": "personal",
            "provider": "sparebank1",
            "enabled": True,
        }
    ]
    assert body["cron_expression"] == "0 6,8,10,12,16,20 * * *"
    assert "next_fire_at" in body


async def test_list_deleted_transactions_returns_rows(tmp_path: Path):
    client, db = make_client(tmp_path)
    await db.upsert_tracked(
        "acct-1:tx-deleted",
        import_id=derive_import_id("acct-1:tx-deleted"),
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="DELETED",
        amount_milliunits=-1000,
        payee_name="Some Shop",
    )

    response = client.get("/api/deleted-transactions")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["payee_name"] == "Some Shop"


@respx.mock
async def test_readd_deleted_transaction_success(tmp_path: Path):
    client, db = make_client(tmp_path)
    old_import_id = derive_import_id("acct-1:tx-deleted:readd:0")
    await db.upsert_tracked(
        "acct-1:tx-deleted",
        import_id=old_import_id,
        ynab_transaction_id="ynab-old",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="DELETED",
        amount_milliunits=-1000,
        payee_name="Some Shop",
        transaction_date="2026-08-20",
        ynab_account_id="ynab-acct-1",
        cleared="cleared",
    )
    new_import_id = derive_import_id("acct-1:tx-deleted:readd:1")
    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["ynab-new"],
                    "transactions": [{"id": "ynab-new", "import_id": new_import_id}],
                }
            },
        )
    )

    response = client.post("/api/deleted-transactions/acct-1:tx-deleted/readd")

    assert response.status_code == 200
    assert response.json() == {"new_ynab_transaction_id": "ynab-new"}


async def test_list_audit_events_empty(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.get("/api/audit-events")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "events": [],
        "total": 0,
        "counts": {"created": 0, "updated": 0, "duplicate": 0, "skipped": 0},
    }


async def test_list_audit_events_excludes_skipped_by_default(tmp_path: Path):
    client, db = make_client(tmp_path)
    await db.insert_audit_event(event_type="created", source="sparebank1")
    await db.insert_audit_event(event_type="skipped", source="sparebank1")

    response = client.get("/api/audit-events")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["event_type"] == "created"
    assert body["counts"] == {"created": 1, "updated": 0, "duplicate": 0, "skipped": 1}


async def test_list_audit_events_include_skipped_true(tmp_path: Path):
    client, db = make_client(tmp_path)
    await db.insert_audit_event(event_type="created", source="sparebank1")
    await db.insert_audit_event(event_type="skipped", source="sparebank1")

    response = client.get("/api/audit-events?include_skipped=true")

    assert response.json()["total"] == 2


async def test_list_audit_events_filters_by_event_type(tmp_path: Path):
    client, db = make_client(tmp_path)
    await db.insert_audit_event(event_type="created", source="sparebank1")
    await db.insert_audit_event(event_type="updated", source="sparebank1")

    response = client.get("/api/audit-events?event_type=updated")

    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["event_type"] == "updated"


async def test_list_audit_events_pagination(tmp_path: Path):
    client, db = make_client(tmp_path)
    for i in range(3):
        await db.insert_audit_event(event_type="created", source="sparebank1", detail=str(i))

    response = client.get("/api/audit-events?limit=2&offset=0")

    body = response.json()
    assert body["total"] == 3
    assert len(body["events"]) == 2


async def test_list_audit_events_filters_by_account_key(tmp_path: Path):
    client, db = make_client(tmp_path)
    await db.insert_audit_event(event_type="created", source="sparebank1", account_key="acct-1")
    await db.insert_audit_event(event_type="created", source="sparebank1", account_key="acct-2")

    response = client.get("/api/audit-events?account_key=acct-1")

    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["account_key"] == "acct-1"


async def test_list_audit_events_sorts_by_requested_column(tmp_path: Path):
    client, db = make_client(tmp_path)
    await db.insert_audit_event(event_type="created", source="sparebank1", payee_name="Zebra")
    await db.insert_audit_event(event_type="created", source="sparebank1", payee_name="Apple")

    response = client.get("/api/audit-events?sort_by=payee_name&sort_dir=asc")

    body = response.json()
    assert [e["payee_name"] for e in body["events"]] == ["Apple", "Zebra"]


def test_list_audit_events_rejects_invalid_sort_by(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.get("/api/audit-events?sort_by=not_a_real_column")

    assert response.status_code == 422


def test_list_audit_events_rejects_invalid_event_type(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.get("/api/audit-events?event_type=not-a-real-type")

    assert response.status_code == 422


async def test_get_audit_event_not_found(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.get("/api/audit-events/999999")

    assert response.status_code == 404
    assert "audit event not found" in response.json()["detail"]


async def test_get_audit_event_no_tracking_key(tmp_path: Path):
    client, db = make_client(tmp_path)
    await db.insert_audit_event(event_type="skipped", source="sparebank1", tracking_key=None)

    response = client.get("/api/audit-events/1")

    assert response.status_code == 200
    body = response.json()
    assert body["event"]["event_type"] == "skipped"
    assert body["event"]["source"] == "sparebank1"
    assert body["event"]["tracking_key"] is None
    assert body["tracked"] is None


async def test_get_audit_event_with_tracked_join(tmp_path: Path):
    client, db = make_client(tmp_path)
    tracking_key = "acct-1:tx-123"
    await db.insert_audit_event(
        event_type="created",
        source="sparebank1",
        tracking_key=tracking_key,
        account_key="acct-1",
        payee_name="Test Shop",
        amount_milliunits=-5000,
    )
    await db.upsert_tracked(
        tracking_key,
        import_id=derive_import_id(tracking_key),
        ynab_transaction_id="ynab-123",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-5000,
        payee_name="Test Shop",
    )

    response = client.get("/api/audit-events/1")

    assert response.status_code == 200
    body = response.json()
    assert body["event"]["event_type"] == "created"
    assert body["event"]["tracking_key"] == tracking_key
    assert body["tracked"] is not None
    assert body["tracked"]["ynab_transaction_id"] == "ynab-123"
    assert body["tracked"]["payee_name"] == "Test Shop"


def test_readd_deleted_transaction_unknown_key_returns_400(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.post("/api/deleted-transactions/does-not-exist/readd")

    assert response.status_code == 400
    assert "no tracked transaction" in response.json()["detail"]


def test_import_dry_run_previews_classification(tmp_path: Path):
    client, db = make_client(tmp_path)
    data = FIXTURE_PATH.read_bytes()

    response = client.post(
        "/api/import",
        files={
            "file": (
                "sample.xlsx",
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"dry_run": "true"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["transformer"] == "Norwegian Bank"
    assert body["committed"] is False
    assert body["account"]["key"] == "acct-1"
    assert len(body["rows"]) == 5
    assert body["summary"]["new"] == 5
    assert body["summary"]["duplicate"] == 0
    # "errors" (plural) matches frontend/src/api/client.ts's ImportSummary -
    # the old "error" (singular) key meant the frontend's error count always
    # rendered undefined.
    assert body["summary"]["errors"] == 0
    # dry_run must not have written anything to local state
    assert db.list_deleted_transactions() == []


@respx.mock
def test_import_commit_creates_transactions_and_reupload_shows_duplicates(tmp_path: Path):
    client, _db = make_client(tmp_path)
    data = FIXTURE_PATH.read_bytes()

    def create_side_effect(request):
        sent = json.loads(request.content)["transactions"]
        transactions = [
            {"id": f"ynab-{i}", "import_id": tx["import_id"], "amount": tx["amount"]}
            for i, tx in enumerate(sent)
        ]
        return httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": [t["id"] for t in transactions],
                    "duplicate_import_ids": [],
                    "transactions": transactions,
                }
            },
        )

    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        side_effect=create_side_effect
    )

    first = client.post(
        "/api/import",
        files={"file": ("sample.xlsx", data)},
        data={"dry_run": "false"},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["committed"] is True
    assert first_body["summary"]["new"] == 5

    second = client.post(
        "/api/import",
        files={"file": ("sample.xlsx", data)},
        data={"dry_run": "true"},
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["summary"]["duplicate"] == 5
    assert second_body["summary"]["new"] == 0


@respx.mock
def test_import_commit_reports_ynab_unavailable_with_no_retry(tmp_path: Path):
    # File import is a single user-initiated request with no background
    # retry loop behind it (unlike the scheduled/live-poll path - see
    # scheduler.py's backoff handling) - a YNAB-side failure must surface
    # immediately as a clear error, not hang or silently retry.
    client, _db = make_client(tmp_path)
    data = FIXTURE_PATH.read_bytes()

    respx.post(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(500, json={"error": {"detail": "internal server error"}})
    )

    response = client.post(
        "/api/import",
        files={"file": ("sample.xlsx", data)},
        data={"dry_run": "false"},
    )

    assert response.status_code == 503
    assert len(response.json()["detail"]) > 0

    # Nothing was imported - the failed commit must not have recorded
    # anything as tracked, so a fresh dry-run still reports every row "new".
    preview = client.post(
        "/api/import",
        files={"file": ("sample.xlsx", data)},
        data={"dry_run": "true"},
    )
    assert preview.json()["summary"]["new"] == 5


def test_import_unrecognized_format_returns_sarcastic_422(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.post(
        "/api/import",
        files={"file": ("not-a-spreadsheet.txt", b"hello world")},
        data={"dry_run": "true"},
    )

    assert response.status_code == 422
    assert len(response.json()["detail"]) > 0


def test_import_no_account_resolved_returns_sarcastic_422(tmp_path: Path):
    accounts = [
        AccountMapping(
            sparebank1_account_key="acct-1",
            ynab_account_id="ynab-acct-1",
            ynab_budget="personal",
            # no import_source_name set - nothing auto-matches
        )
    ]
    client, _db = make_client(tmp_path, accounts=accounts)
    data = FIXTURE_PATH.read_bytes()

    response = client.post(
        "/api/import",
        files={"file": ("sample.xlsx", data)},
        data={"dry_run": "true"},
    )

    assert response.status_code == 422
    assert len(response.json()["detail"]) > 0


def test_import_explicit_account_override_takes_precedence(tmp_path: Path):
    accounts = [
        AccountMapping(
            sparebank1_account_key="acct-other",
            ynab_account_id="ynab-acct-other",
            ynab_budget="personal",
            display_name="Other Account",
            # no import_source_name - would not auto-match, but the request
            # explicitly names it, which must still be honored.
        )
    ]
    client, _db = make_client(tmp_path, accounts=accounts)
    data = FIXTURE_PATH.read_bytes()

    response = client.post(
        "/api/import",
        files={"file": ("sample.xlsx", data)},
        data={"dry_run": "true", "account_key": "acct-other"},
    )

    assert response.status_code == 200
    assert response.json()["account"]["key"] == "acct-other"


def test_import_unknown_explicit_account_override_returns_422(tmp_path: Path):
    client, _db = make_client(tmp_path)
    data = FIXTURE_PATH.read_bytes()

    response = client.post(
        "/api/import",
        files={"file": ("sample.xlsx", data)},
        data={"dry_run": "true", "account_key": "does-not-exist"},
    )

    assert response.status_code == 422


# -- /api/mappings -------------------------------------------------------


def test_create_list_update_delete_mapping_happy_path(tmp_path: Path):
    client, _db = make_client(tmp_path)

    create_response = client.post(
        "/api/mappings",
        json={
            "provider": "sparebank1",
            "provider_account_id": "acct-2",
            "ynab_budget_id": "budget-1",
            "ynab_account_id": "ynab-acct-2",
            "display_name": "Second Account",
            "import_source_name": "",
        },
    )
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["provider_account_id"] == "acct-2"
    assert created["enabled"] is True
    assert created["tracked_count"] == 0
    mapping_id = created["id"]

    list_response = client.get("/api/mappings")
    assert list_response.status_code == 200
    ids = {m["id"]: m for m in list_response.json()}
    assert mapping_id in ids
    assert ids[mapping_id]["tracked_count"] == 0

    patch_response = client.patch(f"/api/mappings/{mapping_id}", json={"enabled": False})
    assert patch_response.status_code == 200
    assert patch_response.json()["enabled"] is False
    # Untouched fields survive a partial update.
    assert patch_response.json()["display_name"] == "Second Account"

    delete_response = client.delete(f"/api/mappings/{mapping_id}")
    assert delete_response.status_code == 204

    final_list = client.get("/api/mappings").json()
    assert mapping_id not in {m["id"] for m in final_list}


def test_create_mapping_duplicate_provider_account_returns_422(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.post(
        "/api/mappings",
        json={
            "provider": "sparebank1",
            "provider_account_id": "acct-1",  # already seeded
            "ynab_budget_id": "budget-1",
            "ynab_account_id": "ynab-acct-other",
        },
    )

    assert response.status_code == 422
    assert len(response.json()["detail"]) > 0


def test_create_mapping_duplicate_import_source_name_returns_422(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.post(
        "/api/mappings",
        json={
            "provider": "sparebank1",
            "provider_account_id": "acct-2",
            "ynab_budget_id": "budget-1",
            "ynab_account_id": "ynab-acct-2",
            "import_source_name": "Norwegian Bank",  # already used by acct-1
        },
    )

    assert response.status_code == 422
    assert len(response.json()["detail"]) > 0


def test_create_mapping_unknown_budget_id_returns_422(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.post(
        "/api/mappings",
        json={
            "provider": "sparebank1",
            "provider_account_id": "acct-2",
            "ynab_budget_id": "does-not-exist",
            "ynab_account_id": "ynab-acct-2",
        },
    )

    assert response.status_code == 422
    assert "ynab_budget_id" in response.json()["detail"]


def test_update_mapping_unknown_budget_id_returns_422(tmp_path: Path):
    client, _db = make_client(tmp_path)
    mapping_id = client.get("/api/mappings").json()[0]["id"]

    response = client.patch(
        f"/api/mappings/{mapping_id}", json={"ynab_budget_id": "does-not-exist"}
    )

    assert response.status_code == 422


def test_update_unknown_mapping_id_returns_404(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.patch("/api/mappings/999999", json={"enabled": False})

    assert response.status_code == 404


def test_delete_unknown_mapping_id_returns_404(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.delete("/api/mappings/999999")

    assert response.status_code == 404


def test_clear_all_mappings_removes_every_row(tmp_path: Path):
    client, _db = make_client(tmp_path)
    client.post(
        "/api/mappings",
        json={
            "provider": "sparebank1",
            "provider_account_id": "acct-2",
            "ynab_budget_id": "budget-1",
            "ynab_account_id": "ynab-acct-2",
        },
    )
    assert len(client.get("/api/mappings").json()) == 2

    response = client.delete("/api/mappings")

    assert response.status_code == 200
    assert response.json() == {"deleted": 2}
    assert client.get("/api/mappings").json() == []


def test_clear_all_mappings_on_empty_table_deletes_nothing(tmp_path: Path):
    client, _db = make_client(tmp_path, accounts=[])

    response = client.delete("/api/mappings")

    assert response.status_code == 200
    assert response.json() == {"deleted": 0}


async def test_list_mappings_reports_tracked_count(tmp_path: Path):
    client, db = make_client(tmp_path)
    await db.upsert_tracked(
        "acct-1:tx-1",
        import_id=derive_import_id("acct-1:tx-1"),
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-1000,
    )

    response = client.get("/api/mappings")

    assert response.status_code == 200
    [mapping] = [m for m in response.json() if m["provider_account_id"] == "acct-1"]
    assert mapping["tracked_count"] == 1


# -- /api/providers and /api/ynab/accounts --------------------------------


def test_list_providers_reports_accounts_with_mapped_flag(tmp_path: Path):
    fake = FakeProvider(
        accounts=[
            ProviderAccount(
                provider_account_id="acct-1",  # already mapped, per make_config
                display_name="Checking",
                account_type="checking",
                currency="NOK",
            ),
            ProviderAccount(
                provider_account_id="acct-unmapped",
                display_name="Savings",
                account_type="savings",
                currency="NOK",
            ),
        ]
    )
    client, _db = make_client(tmp_path, providers_map={"sparebank1": fake})

    response = client.get("/api/providers")

    assert response.status_code == 200
    [entry] = response.json()
    # `name` is the config key (what a mapping stores); `type` is the
    # implementation type. They are different things - see the
    # two-providers-same-type test below.
    assert entry["name"] == "sparebank1"
    assert entry["type"] == "fake"
    assert entry["auth_required"] is False
    assert entry["error"] is None
    accounts_by_id = {a["provider_account_id"]: a for a in entry["accounts"]}
    assert accounts_by_id["acct-1"]["mapped"] is True
    assert accounts_by_id["acct-unmapped"]["mapped"] is False


def test_list_providers_passes_force_refresh_through(tmp_path: Path):
    fake = FakeProvider(accounts=[])
    client, _db = make_client(tmp_path, providers_map={"sparebank1": fake})

    client.get("/api/providers")
    client.get("/api/providers?force_refresh=true")

    assert fake.list_accounts_calls == [False, True]


def test_list_providers_reports_auth_required_without_failing_response(tmp_path: Path):
    fake = FakeProvider(auth_required=True)
    client, _db = make_client(tmp_path, providers_map={"sparebank1": fake})

    response = client.get("/api/providers")

    assert response.status_code == 200
    [entry] = response.json()
    assert entry["auth_required"] is True
    assert entry["accounts"] == []


def test_list_providers_reports_other_errors_without_failing_response(tmp_path: Path):
    fake = FakeProvider(error=RuntimeError("upstream exploded"))
    client, _db = make_client(tmp_path, providers_map={"sparebank1": fake})

    response = client.get("/api/providers")

    assert response.status_code == 200
    [entry] = response.json()
    assert entry["auth_required"] is False
    assert entry["error"] == "upstream exploded"
    assert entry["accounts"] == []


def test_list_providers_with_multiple_providers_isolates_failures(tmp_path: Path):
    healthy = FakeProvider(
        accounts=[
            ProviderAccount(
                provider_account_id="acct-9",
                display_name="Other bank",
                account_type="checking",
                currency="NOK",
            )
        ]
    )
    broken = FakeProvider(auth_required=True)
    client, _db = make_client(
        tmp_path, providers_map={"sparebank1": healthy, "otherbank": broken}
    )

    response = client.get("/api/providers")

    assert response.status_code == 200
    # Keyed by NAME, not type: both fakes report the same type_name, so
    # keying by type would collapse them into one entry - which is
    # exactly the real-world case of two banks in the same alliance.
    by_name = {e["name"]: e for e in response.json()}
    assert len(by_name["sparebank1"]["accounts"]) == 1
    assert by_name["otherbank"]["auth_required"] is True
    assert by_name["sparebank1"]["type"] == by_name["otherbank"]["type"] == "fake"


@respx.mock
def test_list_ynab_accounts_returns_accounts_per_budget(tmp_path: Path):
    client, _db = make_client(tmp_path)
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "accounts": [
                        {
                            "id": "ynab-acct-1",
                            "name": "Checking",
                            "type": "checking",
                            "on_budget": True,
                            "closed": False,
                            "deleted": False,
                        },
                        {
                            "id": "ynab-acct-closed",
                            "name": "Old",
                            "type": "checking",
                            "on_budget": True,
                            "closed": True,
                            "deleted": False,
                        },
                    ]
                }
            },
        )
    )

    response = client.get("/api/ynab/accounts")

    assert response.status_code == 200
    [entry] = response.json()
    assert entry["budget_id"] == "budget-1"
    assert entry["alias"] == "personal"
    assert [a["id"] for a in entry["accounts"]] == ["ynab-acct-1"]


@respx.mock
def test_list_ynab_accounts_caches_between_requests(tmp_path: Path):
    client, _db = make_client(tmp_path)
    route = respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/accounts").mock(
        return_value=httpx.Response(200, json={"data": {"accounts": []}})
    )

    client.get("/api/ynab/accounts")
    client.get("/api/ynab/accounts")

    assert route.call_count == 1


@respx.mock
def test_list_ynab_accounts_force_refresh_bypasses_cache(tmp_path: Path):
    client, _db = make_client(tmp_path)
    route = respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/accounts").mock(
        return_value=httpx.Response(200, json={"data": {"accounts": []}})
    )

    client.get("/api/ynab/accounts")
    client.get("/api/ynab/accounts?force_refresh=true")

    assert route.call_count == 2


# -- /api/sync-now ---------------------------------------------------------


def test_sync_now_with_scheduler_triggers_request(tmp_path: Path):
    scheduler = FakeScheduler()
    client, _db = make_client(tmp_path, scheduler=scheduler)

    response = client.post("/api/sync-now")

    assert response.status_code == 200
    assert scheduler.called is True


def test_sync_now_without_scheduler_returns_503(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.post("/api/sync-now")

    assert response.status_code == 503
    assert len(response.json()["detail"]) > 0


# -- /api/settings -----------------------------------------------------------


def test_get_settings_falls_back_to_config_before_any_override(tmp_path: Path):
    # make_config() doesn't set logging.level explicitly, so this exercises
    # LoggingConfig's own default ("INFO").
    client, _db = make_client(tmp_path)

    response = client.get("/api/settings")

    assert response.status_code == 200
    assert response.json() == {"log_level": "INFO"}


def test_patch_settings_updates_log_level_and_persists(tmp_path: Path):
    client, db = make_client(tmp_path)

    response = client.patch("/api/settings", json={"log_level": "DEBUG"})

    assert response.status_code == 200
    assert response.json() == {"log_level": "DEBUG"}
    assert db.read_run_metadata()["log_level"] == "DEBUG"
    # A second GET reflects the persisted override, not just the response
    # from the PATCH that made it.
    assert client.get("/api/settings").json() == {"log_level": "DEBUG"}


def test_patch_settings_applies_to_the_running_root_logger(tmp_path: Path):
    import logging

    client, _db = make_client(tmp_path)

    client.patch("/api/settings", json={"log_level": "WARNING"})

    assert logging.getLogger().level == logging.WARNING


def test_patch_settings_rejects_unknown_log_level(tmp_path: Path):
    client, _db = make_client(tmp_path)

    response = client.patch("/api/settings", json={"log_level": "NOT_A_LEVEL"})

    assert response.status_code == 422
