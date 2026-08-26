from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ynab_auto_sync.http_retry import retry_get

logger = logging.getLogger(__name__)

BASE_URL = "https://api.ynab.com/v1"

# YNAB's long-established, widely-documented convention is `/budgets/{budget_id}/...`.
# One AI-summarized fetch of their OpenAPI spec surfaced `/plans/{plan_id}/...`
# instead, which could be a genuine rename or a bad summarization - this constant
# exists so that, if confirmed via scripts/verify_ynab.py to be wrong, fixing it
# is a one-line change rather than a search-and-replace across the codebase.
RESOURCE_PATH = "budgets"


def _headers(personal_access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {personal_access_token}",
        "Content-Type": "application/json",
    }


@retry_get
async def get_budgets(
    http_client: httpx.AsyncClient, personal_access_token: str
) -> list[dict[str, Any]]:
    response = await http_client.get(
        f"{BASE_URL}/{RESOURCE_PATH}", headers=_headers(personal_access_token)
    )
    response.raise_for_status()
    data = response.json()["data"]
    # YNAB wraps the list under a key matching RESOURCE_PATH ("budgets" or "plans").
    return data.get(RESOURCE_PATH, data.get("budgets", data.get("plans", [])))


@retry_get
async def get_payees(
    http_client: httpx.AsyncClient, personal_access_token: str, budget_id: str
) -> list[dict[str, Any]]:
    """List all payees in a budget, including YNAB's auto-generated
    transfer payees (one per account, named "Transfer : <account name>",
    each carrying a `transfer_account_id` field). Confirmed live via
    scripts/verify_ynab_transfer.py: creating a transaction with `payee_id`
    set to one of these (instead of `payee_name`) is what creates a real,
    linked transfer - YNAB auto-creates the paired transaction on the
    target account itself; the caller must not create both sides.
    """
    response = await http_client.get(
        f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/payees", headers=_headers(personal_access_token)
    )
    response.raise_for_status()
    return response.json()["data"]["payees"]


@retry_get
async def get_payee(
    http_client: httpx.AsyncClient, personal_access_token: str, budget_id: str, payee_id: str
) -> dict[str, Any] | None:
    """Look up one payee directly, rather than scanning get_payees()'s bulk
    list - used by SyncEngine.reconcile_payee_mappings() to confirm
    whether a specific cached payee_id has genuinely been deleted.

    A bulk GET .../payees scan was tried first and found unsafe: live
    verification (scripts/verify_ynab_payee_deletion.py) proved a real,
    human-deleted payee is OMITTED from that list entirely rather than
    included with `deleted: true`, so "absent from the bulk list" could
    never be safely distinguished from a transient/incomplete response.
    This targeted per-id endpoint was then confirmed
    (scripts/verify_ynab_payee_get_by_id.py) to reliably 404 for a
    genuinely deleted payee instead - which IS a safe, unambiguous signal,
    since it's asking about one specific id we ourselves cached rather
    than trying to infer meaning from what a bulk list omits.

    Returns None for a 404 (confirmed-deleted) rather than raising -
    every other error status still raises via raise_for_status(), same as
    every other function here.
    """
    response = await http_client.get(
        f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/payees/{payee_id}",
        headers=_headers(personal_access_token),
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()["data"]["payee"]


@retry_get
async def get_accounts(
    http_client: httpx.AsyncClient, personal_access_token: str, budget_id: str
) -> list[dict[str, Any]]:
    """List a budget's accounts - the drop targets the GUI's mapping tab
    offers when an account from a provider is dragged onto YNAB.

    Only the on-budget/off-budget accounts a user actually sees are useful
    here, so `closed` and `deleted` accounts are filtered out: mapping a
    provider account onto a closed YNAB account would import transactions
    the user can no longer see.

    Confirmed live via scripts/verify_ynab_accounts.py before being trusted
    in the mapping UI, per this project's standing rule - a guessed YNAB
    endpoint has returned a real 404 here before (see get_budgets's comment
    on /budgets vs /plans).
    """
    response = await http_client.get(
        f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/accounts",
        headers=_headers(personal_access_token),
    )
    response.raise_for_status()
    accounts = response.json()["data"]["accounts"]
    return [a for a in accounts if not a.get("closed") and not a.get("deleted")]


@retry_get
async def list_unimported_transactions(
    http_client: httpx.AsyncClient,
    personal_access_token: str,
    budget_id: str,
    account_id: str,
) -> list[dict[str, Any]]:
    """List an account's transactions that were never imported by any
    source - no import_id, not deleted. These are the "a human typed this
    into YNAB by hand" candidates the manual-transaction-matching feature
    (engine.py's _classify()) checks a fresh bank transaction against
    before creating a new one (see CLAUDE.md's "Manual-transaction
    matching" section).

    Reuses the same GET .../accounts/{account_id}/transactions endpoint
    find_transaction_by_import_id already relies on (there is no
    import_id-filtered query param - confirmed absent from the OpenAPI spec
    - so this is a client-side filter, same as that function's own scan).
    """
    response = await http_client.get(
        f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/accounts/{account_id}/transactions",
        headers=_headers(personal_access_token),
    )
    response.raise_for_status()
    return [
        t
        for t in response.json()["data"]["transactions"]
        if t.get("import_id") is None and not t.get("deleted")
    ]


# How long a fetched budget's account list is trusted before
# YnabAccountsCache.get_accounts() hits the API again - accounts are
# added/renamed rarely, mirroring providers/sparebank1/provider.py's own
# _ACCOUNTS_CACHE_TTL/list_accounts() cache for the exact same reason (the
# Mappings tab's drop targets shouldn't re-hit a live endpoint on every tab
# visit). force_refresh=True (wired from the GUI's existing "Refresh"
# button, alongside the provider-side one it already bypasses) skips it.
ACCOUNTS_CACHE_TTL = timedelta(minutes=5)


class YnabAccountsCache:
    """Per-budget TTL cache in front of get_accounts(), for
    routes/providers.py's GET /api/ynab/accounts (the mapping UI's YNAB-side
    drop targets). Instance-level, no lock: this project runs on one asyncio
    event loop per process (see StateDB's own concurrency notes for the same
    reasoning), so no two coroutines can interleave inside a single cache
    read/write here. Meant to be constructed once per process and shared
    (see webapp/app.py's create_app), the same lifetime as
    SpareBank1Provider's own accounts cache.
    """

    def __init__(self) -> None:
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._cached_at: dict[str, datetime] = {}

    async def get_accounts(
        self,
        http_client: httpx.AsyncClient,
        personal_access_token: str,
        budget_id: str,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        cached_at = self._cached_at.get(budget_id)
        if (
            not force_refresh
            and budget_id in self._cache
            and cached_at is not None
            and datetime.now(UTC) - cached_at < ACCOUNTS_CACHE_TTL
        ):
            return list(self._cache[budget_id])

        accounts = await get_accounts(http_client, personal_access_token, budget_id)
        self._cache[budget_id] = accounts
        self._cached_at[budget_id] = datetime.now(UTC)
        return list(accounts)


def find_transfer_payee_id(payees: list[dict[str, Any]], target_account_id: str) -> str | None:
    """Find the transfer payee (from get_payees's result) targeting a given
    account, or None if the budget has no such payee for that account (e.g.
    it isn't a real account in this budget)."""
    for payee in payees:
        if payee.get("transfer_account_id") == target_account_id:
            return payee["id"]
    return None


async def create_transactions(
    http_client: httpx.AsyncClient,
    personal_access_token: str,
    budget_id: str,
    transactions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bulk-create transactions. Each transaction dict must include a
    deterministic import_id (see sync/transform.py) - YNAB reports repeats
    of an existing import_id back as duplicates rather than erroring or
    creating a second transaction, which is this project's primary
    no-duplicates guarantee.

    Returns the raw `data` object, e.g.:
        {"transaction_ids": [...], "transactions": [...],
         "duplicate_import_ids": [...], "server_knowledge": ...}
    """
    if not transactions:
        return {"transaction_ids": [], "transactions": [], "duplicate_import_ids": []}

    response = await http_client.post(
        f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/transactions",
        headers=_headers(personal_access_token),
        json={"transactions": transactions},
    )
    response.raise_for_status()
    return response.json()["data"]


async def delete_transaction(
    http_client: httpx.AsyncClient,
    personal_access_token: str,
    budget_id: str,
    transaction_id: str,
) -> dict[str, Any]:
    """Delete a single transaction. Used only by the manual-transaction-
    matching feature (engine.py's _classify()/"matched_manual" path) - see
    CLAUDE.md's "Manual-transaction matching" section for why: PATCHing
    import_id onto an existing transaction is a silent no-op (confirmed live
    via scripts/verify_ynab_import_id_patch.py - YNAB accepts the PATCH,
    returns 200, and the field stays None), so there is no way to grant a
    pre-existing transaction real import_id-dedup protection in place. The
    only way to get that protection is to delete it and create a fresh
    transaction with the import_id set from the start - see
    scripts/verify_ynab_delete_recreate.py for the live confirmation this
    endpoint behaves as expected (200, returns the deleted transaction,
    idempotent-looking on a second delete attempt was NOT assumed and is
    checked there too).

    Returns the raw `data` object: {"transaction": {...,"deleted": true}}.
    """
    response = await http_client.delete(
        f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/transactions/{transaction_id}",
        headers=_headers(personal_access_token),
    )
    response.raise_for_status()
    return response.json()["data"]


async def update_transactions(
    http_client: httpx.AsyncClient,
    personal_access_token: str,
    budget_id: str,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bulk-update existing transactions. Each dict in `updates` must include
    "id" (the YNAB transaction id) plus whichever fields are being changed,
    e.g. {"id": "...", "cleared": "cleared", "amount": -12340}.

    This PATCHes the same collection endpoint used for creation, per YNAB's
    documented pattern for bulk transaction updates - that pattern is a
    best-effort implementation, unverified against the live API (see
    RESOURCE_PATH above for the precedent on flagging this kind of
    assumption). scripts/verify_ynab_update.py exists to confirm it.

    Returns the raw `data` object, e.g.:
        {"transaction_ids": [...], "transactions": [...], "server_knowledge": ...}
    """
    if not updates:
        return {"transaction_ids": [], "transactions": []}

    response = await http_client.patch(
        f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/transactions",
        headers=_headers(personal_access_token),
        json={"transactions": updates},
    )
    response.raise_for_status()
    return response.json()["data"]


@retry_get
async def find_transaction_by_import_id(
    http_client: httpx.AsyncClient,
    personal_access_token: str,
    budget_id: str,
    account_id: str,
    import_id: str,
) -> dict[str, Any] | None:
    """Look up a transaction by its import_id within one account.

    NOTE: an earlier version of this function tried a dedicated
    `GET .../transactions/import_id/{import_id}` endpoint - live testing via
    scripts/verify_ynab_update.py confirmed that endpoint doesn't actually
    exist in the YNAB API (404, not a wrapper-key mismatch). This version
    instead lists the account's transactions (a well-established, long
    documented endpoint) and searches client-side for a matching import_id,
    since every transaction YNAB returns echoes back its own import_id.
    Used for the resilience path where local tracking state was lost and
    the code needs to recover an existing YNAB transaction's id - a rare
    path, so the extra list-and-scan cost here is fine.

    Returns None if no transaction with that import_id is found.
    """
    response = await http_client.get(
        f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/accounts/{account_id}/transactions",
        headers=_headers(personal_access_token),
    )
    response.raise_for_status()
    for transaction in response.json()["data"]["transactions"]:
        if transaction.get("import_id") == import_id:
            return transaction
    return None


@retry_get
async def list_transactions_including_deleted(
    http_client: httpx.AsyncClient,
    personal_access_token: str,
    budget_id: str,
) -> list[dict[str, Any]]:
    """The full budget-wide transaction history, including deleted ones -
    each row carries a `deleted` boolean field.

    Confirmed live: YNAB's plain (non-delta) transaction listing silently
    excludes deleted transactions - passing last_knowledge_of_server=0 to
    the budget-wide endpoint is the only way to see them, which is what
    lets a caller distinguish "really gone/lost local state, transaction
    is still active" from "this transaction was intentionally deleted,
    stop retrying it." See find_transaction_including_deleted (searches
    this same list for one import_id) and engine.py's submit(), which uses
    this directly to bulk-check a whole batch of pending->booked update
    targets before ever attempting a PATCH (see CLAUDE.md's "Resolved:
    update PATCH could permanently wedge the sync cycle").

    Budget-wide rather than account-scoped, and heavier than
    list_unimported_transactions/find_transaction_by_import_id - don't
    call this routinely, only when deleted-transaction visibility is
    actually needed.
    """
    response = await http_client.get(
        f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/transactions",
        headers=_headers(personal_access_token),
        params={"last_knowledge_of_server": 0},
    )
    response.raise_for_status()
    return response.json()["data"]["transactions"]


async def find_transaction_including_deleted(
    http_client: httpx.AsyncClient,
    personal_access_token: str,
    budget_id: str,
    import_id: str,
) -> dict[str, Any] | None:
    """Search the full budget transaction history - including deleted
    transactions - for one with the given import_id.

    Confirmed live: YNAB permanently reserves an import_id once used, even
    after the transaction using it is deleted, and will report any later
    create attempt with that import_id as a duplicate forever - but
    find_transaction_by_import_id's plain (non-delta) listing silently
    excludes deleted transactions, so it can never find the reason why.

    Only call this as a second-line check after that cheaper lookup fails,
    not routinely - see list_transactions_including_deleted (the retried
    GET this wraps) for why.
    """
    transactions = await list_transactions_including_deleted(
        http_client, personal_access_token, budget_id
    )
    for transaction in transactions:
        if transaction.get("import_id") == import_id:
            return transaction
    return None
