import json

import httpx
import respx

from ynab_auto_sync.ynab import client as ynab_client


@respx.mock
async def test_get_budgets_returns_list_under_resource_path_key():
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}").mock(
        return_value=httpx.Response(200, json={"data": {"budgets": [{"id": "b1", "name": "My Budget"}]}})
    )
    async with httpx.AsyncClient() as http_client:
        budgets = await ynab_client.get_budgets(http_client, "pat")
    assert budgets == [{"id": "b1", "name": "My Budget"}]


async def test_create_transactions_with_empty_list_skips_network_call():
    async with httpx.AsyncClient() as http_client:
        with respx.mock:  # no routes registered - any request would raise
            result = await ynab_client.create_transactions(http_client, "pat", "budget-1", [])
    assert result == {"transaction_ids": [], "transactions": [], "duplicate_import_ids": []}


@respx.mock
async def test_create_transactions_posts_and_reports_duplicates():
    route = respx.post(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["new-1"],
                    "duplicate_import_ids": ["SB1:existing"],
                }
            },
        )
    )
    tx = {"account_id": "a1", "date": "2026-08-20", "amount": -1000, "import_id": "SB1:new"}

    async with httpx.AsyncClient() as http_client:
        result = await ynab_client.create_transactions(http_client, "pat", "budget-1", [tx])

    assert result["transaction_ids"] == ["new-1"]
    assert result["duplicate_import_ids"] == ["SB1:existing"]
    sent_request = route.calls.last.request
    assert sent_request.headers["Authorization"] == "Bearer pat"


async def test_update_transactions_with_empty_list_skips_network_call():
    async with httpx.AsyncClient() as http_client:
        with respx.mock:  # no routes registered - any request would raise
            result = await ynab_client.update_transactions(http_client, "pat", "budget-1", [])
    assert result == {"transaction_ids": [], "transactions": []}


@respx.mock
async def test_update_transactions_patches_and_returns_data():
    route = respx.patch(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transaction_ids": ["existing-1"],
                    "transactions": [{"id": "existing-1", "cleared": "cleared"}],
                }
            },
        )
    )
    update = {"id": "existing-1", "cleared": "cleared", "amount": -2000}

    async with httpx.AsyncClient() as http_client:
        result = await ynab_client.update_transactions(http_client, "pat", "budget-1", [update])

    assert result["transaction_ids"] == ["existing-1"]
    assert result["transactions"] == [{"id": "existing-1", "cleared": "cleared"}]
    sent_request = route.calls.last.request
    assert sent_request.headers["Authorization"] == "Bearer pat"
    assert json.loads(sent_request.content) == {"transactions": [update]}


@respx.mock
async def test_find_transaction_by_import_id_found():
    # No dedicated YNAB endpoint for this (confirmed 404 via a live check) -
    # find_transaction_by_import_id lists the account's transactions and
    # scans for a matching import_id.
    respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/accounts/acct-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {"id": "other-1", "import_id": "SB1:other"},
                        {"id": "existing-1", "import_id": "SB1:existing", "cleared": "uncleared"},
                    ]
                }
            },
        )
    )
    async with httpx.AsyncClient() as http_client:
        result = await ynab_client.find_transaction_by_import_id(
            http_client, "pat", "budget-1", "acct-1", "SB1:existing"
        )
    assert result == {"id": "existing-1", "import_id": "SB1:existing", "cleared": "uncleared"}


@respx.mock
async def test_find_transaction_by_import_id_not_found_returns_none():
    respx.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/accounts/acct-1/transactions"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": {"transactions": [{"id": "other-1", "import_id": "SB1:other"}]}}
        )
    )
    async with httpx.AsyncClient() as http_client:
        result = await ynab_client.find_transaction_by_import_id(
            http_client, "pat", "budget-1", "acct-1", "SB1:missing"
        )
    assert result is None


@respx.mock
async def test_find_transaction_including_deleted_finds_a_deleted_transaction():
    route = respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "transactions": [
                        {"id": "other-1", "import_id": "SB1:other", "deleted": False},
                        {"id": "deleted-1", "import_id": "SB1:gone", "deleted": True},
                    ]
                }
            },
        )
    )
    async with httpx.AsyncClient() as http_client:
        result = await ynab_client.find_transaction_including_deleted(
            http_client, "pat", "budget-1", "SB1:gone"
        )
    assert result == {"id": "deleted-1", "import_id": "SB1:gone", "deleted": True}
    sent_request = route.calls.last.request
    assert sent_request.url.params["last_knowledge_of_server"] == "0"


@respx.mock
async def test_find_transaction_including_deleted_not_found_returns_none():
    respx.get(f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/budget-1/transactions").mock(
        return_value=httpx.Response(200, json={"data": {"transactions": []}})
    )
    async with httpx.AsyncClient() as http_client:
        result = await ynab_client.find_transaction_including_deleted(
            http_client, "pat", "budget-1", "SB1:missing"
        )
    assert result is None
