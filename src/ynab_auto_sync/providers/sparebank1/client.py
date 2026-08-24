from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from ynab_auto_sync.http_retry import retry_get
from ynab_auto_sync.providers.sparebank1.auth import ACCEPT_HEADER, BASE_URL

logger = logging.getLogger(__name__)

ACCOUNTS_URL = f"{BASE_URL}/personal/banking/accounts"
TRANSACTIONS_URL = f"{BASE_URL}/personal/banking/transactions"

# The developer portal's saved documentation only lists endpoint paths, not
# the expanded "try it" panel with query parameters - these names are a
# best-effort guess based on common SpareBank1/Nordic bank-API conventions
# and MUST be confirmed with scripts/probe_transactions.py against a real
# account before being trusted. Kept as constants so a wrong guess is a
# one-line fix.
TX_PARAM_ACCOUNT_KEY = "accountKey"
TX_PARAM_FROM_DATE = "fromDate"
TX_PARAM_TO_DATE = "toDate"


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Accept": ACCEPT_HEADER}


def _unwrap_list(payload: Any, *candidate_keys: str) -> list[dict[str, Any]]:
    """SpareBank1 list endpoints may return a bare JSON array or an object
    wrapping the array under a named key (their schema names - e.g.
    TransactionsDTO - suggest the latter). Handle both defensively rather
    than assuming one shape.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in candidate_keys:
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        logger.warning(
            "Unexpected response shape from SpareBank1 (expected a list or one "
            "of %s to contain a list); got keys=%s",
            candidate_keys,
            list(payload.keys()),
        )
    return []


@retry_get
async def get_accounts(http_client: httpx.AsyncClient, access_token: str) -> list[dict[str, Any]]:
    params = {"includeCreditCardAccounts": True}
    response = await http_client.get(ACCOUNTS_URL, headers=_auth_headers(access_token), params=params)
    response.raise_for_status()
    return _unwrap_list(response.json(), "accounts")


@retry_get
async def get_transactions(
    http_client: httpx.AsyncClient,
    access_token: str,
    account_keys: list[str],
    since: datetime,
) -> list[dict[str, Any]]:
    """Fetch transactions for one or more accounts since a given time, in a
    single request.

    Confirmed live via scripts/probe_multi_account_transactions.py:
    SpareBank1's API accepts a REPEATED accountKey query param
    (?accountKey=A&accountKey=B) and returns the genuine union of every
    named account's transactions (each transaction carries its own
    `accountKey` field, confirmed to correctly attribute rows back to the
    account they came from - a two-account probe returned exactly the sum
    of each account's individually-fetched count, split correctly). A
    comma-joined single value (?accountKey=A,B) is explicitly rejected with
    400 ("The account key is invalid") - do not use that form.

    Server-side date filtering via fromDate/toDate is unverified (see
    scripts/probe_transactions.py); callers MUST also apply their own
    client-side `date >= since` filter per-account on the result (since a
    single shared `since` here may be earlier than some accounts' own
    effective window when batching accounts with different cursors) and
    must not assume the server actually narrowed anything down.

    Retried up to 3 times with backoff on transient failures (connection
    errors, 429, 5xx) - safe since this is a read-only call (see
    http_retry.py). Writes (create/update in ynab/client.py) are
    deliberately NOT retried at this layer.
    """
    params = [(TX_PARAM_ACCOUNT_KEY, key) for key in account_keys]
    params.append((TX_PARAM_FROM_DATE, since.date().isoformat()))
    response = await http_client.get(
        TRANSACTIONS_URL, headers=_auth_headers(access_token), params=params
    )
    response.raise_for_status()
    return _unwrap_list(response.json(), "transactions")
