from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

import httpx

from ynab_auto_sync.providers.base import (
    BookingStatus,
    NormalizedTransaction,
    ProviderAccount,
    TransactionProvider,
)
from ynab_auto_sync.providers.registry import register
from ynab_auto_sync.providers.sparebank1 import client as sb1_client
from ynab_auto_sync.providers.sparebank1 import transform
from ynab_auto_sync.providers.sparebank1.auth import TokenStore
from ynab_auto_sync.providers.sparebank1.transform import MissingFieldError
from ynab_auto_sync.sync.sanitize import clean_bank_text

logger = logging.getLogger(__name__)


def _first_present(sb1_tx: dict[str, Any], *candidates: str) -> Any:
    """Local mirror of transform._get_first's "first non-None candidate"
    semantics, kept here rather than importing the private helper so this
    module only depends on transform.py's public surface. Must stay
    behaviourally identical to transform._get_first - it backs the
    payee/memo extraction, which is meant to be a faithful port of
    transform.transform_transaction / transform._extract_payee_name (see
    that function's docstring for why "SpareBank1 transaction" is the
    fallback and why only the first present candidate is used, never a
    fallback to the next one after a failed clean).
    """
    for key in candidates:
        if key in sb1_tx and sb1_tx[key] is not None:
            return sb1_tx[key]
    return None


def _extract_payee_name(sb1_tx: dict[str, Any]) -> str:
    name = _first_present(sb1_tx, *transform.PAYEE_NAME_CANDIDATES)
    cleaned = clean_bank_text(str(name)) if name is not None else None
    return cleaned or "SpareBank1 transaction"


def _extract_memo(sb1_tx: dict[str, Any]) -> str | None:
    description = _first_present(sb1_tx, *transform.DESCRIPTION_FIELD_CANDIDATES)
    return clean_bank_text(str(description)) if description is not None else None


@register
class SpareBank1Provider(TransactionProvider):
    """Wraps providers/sparebank1/client.py + transform.py behind the
    provider-agnostic TransactionProvider contract (providers/base.py).

    fetch()'s per-row logic is a deliberate, behaviour-identical port of
    what previously lived inline in SyncEngine.run_cycle() (see CLAUDE.md
    "Core invariants" 1, 2, 5, 6): three separate try/except
    MissingFieldError blocks, each with its own log message, so a single
    malformed row is skipped rather than aborting the whole batch. Unlike
    the old inline code, this method does NOT apply the tracked/untracked
    classification or the per-account date-cutoff filter - both are the
    caller's (engine's) job now, per providers/base.py's fetch() contract;
    every structurally-parseable fetched row is normalized and returned.
    """

    def __init__(self, http_client: httpx.AsyncClient, token_store: TokenStore):
        self._http_client = http_client
        self._token_store = token_store

    @staticmethod
    def type_name() -> str:
        return "sparebank1"

    async def list_accounts(self) -> list[ProviderAccount]:
        access_token = await self._token_store.ensure_valid_access_token(self._http_client)
        raw_accounts = await sb1_client.get_accounts(self._http_client, access_token)

        accounts = []
        for raw in raw_accounts:
            accounts.append(
                ProviderAccount(
                    provider_account_id=str(raw.get("accountKey") or raw.get("key") or ""),
                    display_name=str(raw.get("name") or raw.get("accountName") or ""),
                    account_type=str(raw.get("type") or raw.get("accountType") or ""),
                    currency=str(raw.get("currencyCode") or raw.get("currency") or ""),
                )
            )
        return accounts

    async def fetch(
        self, since_by_account: dict[str, datetime]
    ) -> list[NormalizedTransaction]:
        if not since_by_account:
            return []

        access_token = await self._token_store.ensure_valid_access_token(self._http_client)

        # One combined request covering every requested account, using the
        # earliest of all accounts' `since` values - confirmed live
        # (scripts/probe_multi_account_transactions.py) that SpareBank1
        # accepts a repeated accountKey param and returns the genuine union,
        # each row tagged with its own accountKey. We deliberately do NOT
        # apply any per-account since filter here - see this method's
        # docstring and providers/base.py's fetch() contract: the caller
        # re-applies the authoritative per-account cutoff itself, so
        # over-fetching is safe and expected.
        earliest_since = min(since_by_account.values())
        combined_txs = await sb1_client.get_transactions(
            self._http_client,
            access_token,
            list(since_by_account),
            earliest_since,
        )

        sb1_txs_by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sb1_tx in combined_txs:
            owning_account_key = sb1_tx.get("accountKey")
            if owning_account_key not in since_by_account:
                logger.error(
                    "Fetched transaction with unrecognized or missing accountKey "
                    "%r (not one of the configured accounts) - skipping: %r",
                    owning_account_key,
                    sb1_tx,
                )
                continue
            sb1_txs_by_account[owning_account_key].append(sb1_tx)

        results: list[NormalizedTransaction] = []
        for account_key, sb1_txs in sb1_txs_by_account.items():
            for sb1_tx in sb1_txs:
                try:
                    tx_date = transform.get_transaction_date(sb1_tx)
                except MissingFieldError:
                    logger.error(
                        "Skipping SpareBank1 transaction with no recognizable date "
                        "field: %r",
                        sb1_tx,
                    )
                    continue

                try:
                    tracking_key = transform.get_tracking_key(sb1_tx, account_key)
                except MissingFieldError:
                    logger.error(
                        "Skipping SpareBank1 transaction with no recognizable id "
                        "field: %r",
                        sb1_tx,
                    )
                    continue

                try:
                    amount_milliunits = transform.get_amount_milliunits(sb1_tx)
                except MissingFieldError:
                    logger.error(
                        "Skipping SpareBank1 transaction missing required fields: %r",
                        sb1_tx,
                    )
                    continue

                status = transform.get_booking_status(sb1_tx)
                if status == transform.BOOKING_STATUS_PENDING:
                    # SpareBank1's `amount` field is unreliable while a
                    # transaction is still PENDING (confirmed by the user
                    # against real data - a pending authorization amount can
                    # differ from what the transaction actually books at).
                    # Rather than import a value known to sometimes be wrong
                    # and rely on the pending->booked transition (engine.py's
                    # _classify "transitioned" branch) to correct it later -
                    # which itself depends on an unproven hypothesis about
                    # nonUniqueId stability across that transition, see
                    # CLAUDE.md's "Known open risk" - PENDING rows are
                    # skipped entirely and only imported once BOOKED, mirroring
                    # the sibling project's (../ynab-auto-bank) approach to the
                    # same hazard. The "transitioned" branch in engine.py is
                    # kept for any already-tracked PENDING rows from before
                    # this change; no new PENDING row will ever reach it going
                    # forward.
                    logger.debug(
                        "Skipping PENDING SpareBank1 transaction (amount not final "
                        "yet, will be imported once booked): %r",
                        sb1_tx,
                    )
                    continue

                booking_status = BookingStatus.BOOKED

                results.append(
                    NormalizedTransaction(
                        tracking_key=tracking_key,
                        import_id=transform.derive_import_id(tracking_key),
                        provider_account_id=account_key,
                        date=tx_date,
                        amount_milliunits=amount_milliunits,
                        payee_name=_extract_payee_name(sb1_tx),
                        memo=_extract_memo(sb1_tx),
                        booking_status=booking_status,
                    )
                )

        return results
