"""Pins the exact JSON field names `frontend/src/api/client.ts` reads.

These exist because a real bug shipped: `/api/providers` returned `type`
while the frontend's `ProviderInfo` declared `type_name`, so the field read
as `undefined`, `provider` was dropped from the POST body, and every attempt
to create a mapping failed with a 422. Nothing caught it - the backend tests
asserted whatever the backend happened to produce, and the frontend has no
tests at all. The two halves were built against a contract that only existed
in prose.

A round-trip test is the point: build a mapping payload out of the SAME
fields a client reads from /api/providers and /api/ynab/accounts, and POST
it. If either endpoint renames a field the create path depends on, this
fails here rather than in someone's browser.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from tests.unit.test_webapp import FakeProvider, make_client
from ynab_auto_sync.providers.base import ProviderAccount
from ynab_auto_sync.ynab import client as ynab_client

# Kept in lockstep with frontend/src/api/client.ts. Update both together.
PROVIDER_INFO_FIELDS = {"name", "type", "auth_required", "error", "accounts"}
PROVIDER_ACCOUNT_FIELDS = {
    "provider_account_id",
    "display_name",
    "account_type",
    "currency",
    "mapped",
}
YNAB_BUDGET_FIELDS = {"budget_id", "alias", "accounts"}
YNAB_ACCOUNT_FIELDS = {"id", "name", "type", "on_budget"}
MAPPING_FIELDS = {
    "id",
    "provider",
    "provider_account_id",
    "ynab_budget_id",
    "ynab_account_id",
    "display_name",
    "import_source_name",
    "enabled",
    "tracked_count",
}


def _fake(tmp_path: Path):
    provider = FakeProvider(
        accounts=[
            ProviderAccount(
                provider_account_id="4526895",
                display_name="**** **** **** 3431",
                account_type="credit_card",
                currency="NOK",
            )
        ]
    )
    return make_client(tmp_path, providers_map={"sparebank1": provider})


def test_providers_response_exposes_every_field_the_frontend_reads(tmp_path: Path):
    client, _db = _fake(tmp_path)

    [entry] = client.get("/api/providers").json()

    assert PROVIDER_INFO_FIELDS <= set(entry), (
        f"missing {PROVIDER_INFO_FIELDS - set(entry)} - frontend/src/api/client.ts's "
        "ProviderInfo reads these"
    )
    assert PROVIDER_ACCOUNT_FIELDS <= set(entry["accounts"][0])


@respx.mock
def test_ynab_accounts_response_exposes_every_field_the_frontend_reads(tmp_path: Path):
    respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/accounts"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "accounts": [
                        {
                            "id": "ynab-1",
                            "name": "KT regninger",
                            "type": "checking",
                            "on_budget": True,
                            "closed": False,
                            "deleted": False,
                        }
                    ]
                }
            },
        )
    )
    client, _db = _fake(tmp_path)

    [budget] = client.get("/api/ynab/accounts").json()

    assert YNAB_BUDGET_FIELDS <= set(budget)
    assert YNAB_ACCOUNT_FIELDS <= set(budget["accounts"][0])


def test_a_mapping_can_be_created_from_what_the_provider_endpoint_returns(tmp_path: Path):
    """The regression test for the actual shipped bug: take the identifier
    straight out of /api/providers and POST it back. The old `type` vs
    `type_name` mismatch meant the client read `undefined`, dropped
    `provider` from the body, and got a 422 on this exact round-trip."""
    client, _db = _fake(tmp_path)

    [entry] = client.get("/api/providers").json()
    account = entry["accounts"][0]

    response = client.post(
        "/api/mappings",
        json={
            "provider": entry["name"],
            "provider_account_id": account["provider_account_id"],
            "ynab_budget_id": "budget-1",
            "ynab_account_id": "ynab-acct-1",
            "display_name": account["display_name"],
        },
    )

    assert response.status_code == 201, response.text  # 201 Created
    created = response.json()
    assert MAPPING_FIELDS <= set(created)
    assert created["provider"] == "sparebank1"
    assert created["provider_account_id"] == "4526895"


def test_provider_account_is_flagged_mapped_after_being_mapped(tmp_path: Path):
    """The UI greys out already-mapped accounts using this flag, so it has to
    reflect a mapping made through the API in the same round-trip."""
    client, _db = _fake(tmp_path)
    [entry] = client.get("/api/providers").json()

    client.post(
        "/api/mappings",
        json={
            "provider": entry["name"],
            "provider_account_id": "4526895",
            "ynab_budget_id": "budget-1",
            "ynab_account_id": "ynab-acct-1",
        },
    )

    [refreshed] = client.get("/api/providers").json()
    assert refreshed["accounts"][0]["mapped"] is True
