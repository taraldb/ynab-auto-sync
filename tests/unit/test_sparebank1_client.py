from datetime import UTC, datetime

import httpx
import respx

from ynab_auto_sync.providers.sparebank1 import client as sb1_client


@respx.mock
async def test_get_accounts_handles_bare_list_response():
    respx.get(sb1_client.ACCOUNTS_URL).mock(
        return_value=httpx.Response(200, json=[{"accountKey": "a1"}])
    )
    async with httpx.AsyncClient() as http_client:
        accounts = await sb1_client.get_accounts(http_client, "token")
    assert accounts == [{"accountKey": "a1"}]


@respx.mock
async def test_get_accounts_handles_wrapped_response():
    respx.get(sb1_client.ACCOUNTS_URL).mock(
        return_value=httpx.Response(200, json={"accounts": [{"accountKey": "a1"}]})
    )
    async with httpx.AsyncClient() as http_client:
        accounts = await sb1_client.get_accounts(http_client, "token")
    assert accounts == [{"accountKey": "a1"}]


@respx.mock
async def test_get_accounts_unexpected_shape_returns_empty_list():
    respx.get(sb1_client.ACCOUNTS_URL).mock(return_value=httpx.Response(200, json={"foo": "bar"}))
    async with httpx.AsyncClient() as http_client:
        accounts = await sb1_client.get_accounts(http_client, "token")
    assert accounts == []


@respx.mock
async def test_get_transactions_sends_account_and_date_params():
    route = respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(200, json={"transactions": [{"id": "tx-1"}]})
    )
    since = datetime(2026, 8, 1, tzinfo=UTC)
    async with httpx.AsyncClient() as http_client:
        result = await sb1_client.get_transactions(http_client, "token", ["acct-1"], since)

    assert result == [{"id": "tx-1"}]
    sent_request = route.calls.last.request
    assert sent_request.url.params["accountKey"] == "acct-1"
    assert sent_request.url.params["fromDate"] == "2026-08-01"
    assert sent_request.headers["Authorization"] == "Bearer token"


@respx.mock
async def test_get_transactions_sends_repeated_account_key_param_for_multiple_accounts():
    # Confirmed live (scripts/probe_multi_account_transactions.py) that
    # SpareBank1 accepts a REPEATED accountKey param and rejects a
    # comma-joined single value with 400 - must not regress to the latter.
    route = respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(200, json={"transactions": []})
    )
    since = datetime(2026, 8, 1, tzinfo=UTC)
    async with httpx.AsyncClient() as http_client:
        await sb1_client.get_transactions(http_client, "token", ["acct-1", "acct-2"], since)

    sent_url = route.calls.last.request.url
    assert sent_url.params.get_list("accountKey") == ["acct-1", "acct-2"]
    assert "acct-1,acct-2" not in str(sent_url)
