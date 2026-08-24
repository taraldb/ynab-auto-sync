from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ynab_auto_sync.providers.base import (
    BookingStatus,
    NormalizedTransaction,
    ProviderAccount,
    SkipCallback,
    TransactionProvider,
    report_skip,
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


# Confirmed live (GET /personal/banking/accounts, includeCreditCardAccounts=
# true): every regular checking/savings account's own `name` is already a
# real, human-chosen nickname (e.g. "Spandable Stian") - never masked, never
# touched by the logic below. A credit card account is the one exception:
# its `name` is a masked card number, e.g. "**** **** **** 3431", while its
# `description` field holds the real product name (e.g. "Mastercard Ung") -
# a genuinely nicer name than anything derivable from the mask itself.
# Matches a name that's entirely mask characters (asterisks/bullets/
# whitespace) plus a short trailing digit run - deliberately NOT anchored to
# one exact masking character, since that specific choice isn't documented
# by SpareBank1 and could plausibly differ by card product.
_MASKED_ACCOUNT_NAME_RE = re.compile(r"^[*•\s]*(\d{2,6})$")

# Real `type` values confirmed live: "USER" (regular checking-style
# accounts), "SAVING", "CREDITCARD" - notably NOT "checking"/"credit_card"
# as providers/base.py's ProviderAccount.account_type docstring used to
# guess before this was confirmed. Only used as a last-resort label when a
# masked name has no usable `description` either.
_ACCOUNT_TYPE_LABELS = {
    "CREDITCARD": "Credit Card",
    "SAVING": "Savings",
    "USER": "Account",
}


def _friendly_account_name(name: str, account_type: str, description: str) -> str:
    """Replace a masked-looking account name (confirmed live to occur only
    for credit cards) with something a human would actually want to see in
    the Mappings tab. Deliberately conservative: a name that doesn't match
    the masked pattern is returned completely untouched - this must never
    make an already-fine name (the common case) worse.
    """
    match = _MASKED_ACCOUNT_NAME_RE.match(name.strip()) if name else None
    if match is None:
        return name
    last_digits = match.group(1)[-4:]
    label = description.strip() if description and description.strip() else None
    label = label or _ACCOUNT_TYPE_LABELS.get(account_type, "Account")
    return f"{label} •{last_digits}"


# How long a fetched account list is trusted before list_accounts() hits the
# API again - accounts are added/renamed rarely, and this is a long-lived
# per-process singleton (see __main__.py's _build_providers), so a simple
# instance-level cache with no lock (single event loop, same reasoning
# StateDB's own concurrency notes already document elsewhere in this
# codebase) is enough to stop every Mappings-tab visit from re-hitting
# SpareBank1's live /accounts endpoint. force_refresh=True (wired from the
# GUI's explicit "Refresh" button) always bypasses it.
_ACCOUNTS_CACHE_TTL = timedelta(minutes=5)


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
        self._accounts_cache: list[ProviderAccount] | None = None
        self._accounts_cached_at: datetime | None = None

    @staticmethod
    def type_name() -> str:
        return "sparebank1"

    async def list_accounts(self, force_refresh: bool = False) -> list[ProviderAccount]:
        if (
            not force_refresh
            and self._accounts_cache is not None
            and self._accounts_cached_at is not None
            and datetime.now(UTC) - self._accounts_cached_at < _ACCOUNTS_CACHE_TTL
        ):
            return list(self._accounts_cache)

        access_token = await self._token_store.ensure_valid_access_token(self._http_client)
        raw_accounts = await sb1_client.get_accounts(self._http_client, access_token)

        accounts = []
        for raw in raw_accounts:
            account_type = str(raw.get("type") or raw.get("accountType") or "")
            raw_name = str(raw.get("name") or raw.get("accountName") or "")
            description = str(raw.get("description") or "")
            accounts.append(
                ProviderAccount(
                    provider_account_id=str(raw.get("accountKey") or raw.get("key") or ""),
                    display_name=_friendly_account_name(raw_name, account_type, description),
                    account_type=account_type,
                    currency=str(raw.get("currencyCode") or raw.get("currency") or ""),
                )
            )
        self._accounts_cache = accounts
        self._accounts_cached_at = datetime.now(UTC)
        return list(accounts)

    async def fetch(
        self,
        since_by_account: dict[str, datetime],
        on_skip: SkipCallback | None = None,
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
                await report_skip(
                    on_skip,
                    "malformed: unrecognized or missing accountKey",
                    {"account_key": owning_account_key, "raw": sb1_tx},
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
                    await report_skip(
                        on_skip,
                        "malformed: no recognizable date field",
                        {"account_key": account_key, "raw": sb1_tx},
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
                    await report_skip(
                        on_skip,
                        "malformed: no recognizable id field",
                        {"account_key": account_key, "raw": sb1_tx},
                    )
                    continue

                try:
                    amount_milliunits = transform.get_amount_milliunits(sb1_tx)
                except MissingFieldError:
                    logger.error(
                        "Skipping SpareBank1 transaction missing required fields: %r",
                        sb1_tx,
                    )
                    await report_skip(
                        on_skip,
                        "malformed: missing required fields",
                        {"account_key": account_key, "raw": sb1_tx},
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
                        account_number=transform.get_account_number(sb1_tx),
                        remote_account_number=transform.get_remote_account_number(sb1_tx),
                    )
                )

        return results
