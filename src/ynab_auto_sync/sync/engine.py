from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx

from ynab_auto_sync.config import AppConfig
from ynab_auto_sync.providers.base import (
    BookingStatus,
    NormalizedTransaction,
    TransactionProvider,
)
from ynab_auto_sync.sync import transfers
from ynab_auto_sync.sync.date_window import days_between
from ynab_auto_sync.sync.file_import import dedup as file_dedup
from ynab_auto_sync.sync.file_import.base import ImportedTransactionRow
from ynab_auto_sync.sync.import_ids import derive_import_id, prefix_of
from ynab_auto_sync.sync.pending_match import (
    BookedCandidate,
    PendingCandidate,
    find_manual_match_tolerant,
    find_pending_match,
)
from ynab_auto_sync.sync.state_db import StateDB, compute_since
from ynab_auto_sync.sync.ynab_payload import build_create_payload
from ynab_auto_sync.ynab import client as ynab_client

logger = logging.getLogger(__name__)

# Optional, purely observational hook for "live in-cycle progress" (e.g. a
# GUI websocket push) - see fetch_and_classify()/submit()'s on_progress
# parameter and _report() below. Never required: every existing caller
# passes nothing and gets today's exact behavior unchanged.
ProgressCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _report(
    on_progress: ProgressCallback | None, phase: str, context: dict[str, Any]
) -> None:
    """Best-effort progress notification - mirrors every NotificationSink
    publish_*'s own contract (see notifications/base.py): must NEVER be
    allowed to affect a sync cycle's control flow or outcome, so any
    exception from the callback itself is caught and logged here, never
    raised. A broadcast bug must never turn into a failed (or worse,
    half-completed) sync cycle."""
    if on_progress is None:
        return
    try:
        await on_progress(phase, context)
    except Exception:
        logger.exception("Progress callback failed (non-fatal, cycle continues)")

# Prefix for the synthetic, LOCAL-ONLY import_id stored for the
# auto-created "secondary" leg of a matched transfer (see
# SyncEngine._match_transfers / _record_transfer_secondary). YNAB itself
# assigns that leg no import_id at all (confirmed live via
# scripts/verify_ynab_transfer.py) - this value is never sent to or
# recognized by the real YNAB API, only stored so tracked_transactions.
# import_id (NOT NULL) has something, and so it's visually unmistakable in
# the database as not a real YNAB import_id if ever inspected directly.
_TRANSFER_SECONDARY_IMPORT_ID_PREFIX = "LOCAL:XFER:"

# Stands in for a provider type on the file-import path, which has no
# TransactionProvider behind it. Only ever used to scope transfer matching
# (see _match_transfers) - file rows are never transfer-matched anyway,
# since import_file_rows doesn't call it, so this exists to keep
# _PendingCreate's shape uniform across both sources rather than making the
# field optional.
FILE_SOURCE_TYPE = "file"

# How often SyncEngine.reconcile_payee_mappings() runs its full sweep - see
# that method's docstring for why this needed a rate limit at all (one GET
# per DISTINCT cached payee_id, unlike the cheap one-GET-per-budget design
# it replaced).
PAYEE_RECONCILE_MIN_INTERVAL_DAYS = 1


@dataclass
class CycleResult:
    created: int
    updated: int
    duplicates: int
    resolved_deleted: int
    accounts_processed: int
    fetched: int


@dataclass
class _PendingCreate:
    tracking_key: str
    ynab_tx: dict[str, Any]
    booking_status: str
    # The source account's own id, as the provider reports it. Stored in
    # tracked_transactions.account_key and used to scope transfer matching.
    # For the file-import path this is the mapping's provider_account_id
    # too, so both sources populate it the same way.
    provider_account_id: str
    # Which provider produced this (or FILE_SOURCE_TYPE for a file import).
    # Only used to scope transfer matching: two different providers may
    # legitimately use the same provider_account_id string, and without
    # this they would be mistaken for the same account.
    provider_type: str
    # Carried separately from ynab_tx because _match_transfers POPS
    # "payee_name" out of the payload when rewiring a create into a linked
    # transfer (it's replaced by payee_id). _backfill_duplicates still
    # needs a payee for the tracking row it writes, and reading it back
    # out of the payload was a live KeyError crash - see the regression
    # test in tests/unit/test_engine.py.
    payee_name: str
    # Set only when this create is the "primary" (submitted) leg of a
    # matched transfer pair (see SyncEngine._match_transfers) - the other
    # leg's own _PendingCreate, which is never itself submitted since YNAB
    # auto-creates it as a linked transaction.
    transfer_secondary: _PendingCreate | None = field(default=None)
    # Real account-number cross-reference, carried through from
    # NormalizedTransaction purely for transfer-pair matching (see
    # sync/transfers.py and providers/base.py's NormalizedTransaction
    # docstring for why this is more reliable than a transaction-type
    # code). None for the file-import path, which never reaches
    # _match_transfers anyway.
    account_number: str | None = field(default=None)
    remote_account_number: str | None = field(default=None)


@dataclass
class _PendingUpdate:
    tracking_key: str
    ynab_transaction_id: str
    new_amount_milliunits: int
    # The budget the already-tracked transaction lives in, which is not
    # necessarily the mapping's current budget (a mapping can be re-pointed
    # after a transaction was already imported).
    ynab_budget_id: str
    # Which provider produced the fresh read that triggered this transition
    # (or FILE_SOURCE_TYPE) - only used to stamp the audit_events row, same
    # as _PendingCreate.provider_type.
    provider_type: str
    # The provider's own account identifier this transaction lives under -
    # only used to stamp the audit_events row's account_key column, same as
    # _PendingCreate.provider_account_id.
    account_key: str


@dataclass
class _PendingFuzzyUpdate:
    """Same real-world effect as _PendingUpdate (a pending->booked amount
    correction + cleared PATCH), but reached via sync/pending_match.py's
    fuzzy correlation instead of exact tracking-key equality - see
    _classify()'s "transitioned_fuzzy" outcome. Unlike _PendingUpdate,
    old_tracking_key and new_tracking_key genuinely differ: the booked
    observation's tracking key is NOT the same string as the pending
    placeholder's (confirmed live - a credit card's PENDING nonUniqueId
    bears no relation to its own creditCardIdentifiers.nonUniqueId once
    BOOKED), so submit() must rekey the tracked row's primary key, not just
    update it in place - see StateDB.rekey_pending_to_booked.
    """

    old_tracking_key: str
    new_tracking_key: str
    ynab_transaction_id: str
    new_amount_milliunits: int
    ynab_budget_id: str
    provider_type: str
    account_key: str


@dataclass
class _PendingManualMatch:
    """A fresh bank transaction matched against a pre-existing, manually-
    typed YNAB transaction (same account, exact amount, within
    manual_match_window_days - see _classify()'s "matched_manual" outcome
    and CLAUDE.md's "Manual-transaction matching" section).

    Confirmed live (scripts/verify_ynab_import_id_patch.py) that YNAB
    silently ignores import_id in a PATCH payload - there is no way to
    retroactively grant an existing transaction real import_id-dedup
    protection. The only way is create-then-delete: submit ynab_tx (which
    carries a fresh, real import_id plus the original's own payee_id/
    category_id/approved/flag_color/memo, and the REAL bank date/amount) as
    a brand new transaction, and only once that succeeds, delete
    original_transaction_id. Never the other order - see submit()'s
    _record_matched for why.
    """

    tracking_key: str
    ynab_tx: dict[str, Any]
    original_transaction_id: str
    booking_status: str
    provider_account_id: str
    provider_type: str


@dataclass
class ClassifiedCycle:
    """The output of fetch_and_classify() (phase 1 of a sync cycle) - every
    provider fetched, classified against local tracking, and transfer pairs
    already matched. Kept as its own object, separate from submit()'s (phase
    2's) YNAB writes, specifically so a caller (scheduler.py's retry/backoff
    handling) can retry a failed submit() call without re-hitting a
    provider's own API - the expensive/rate-limited part - on every retry.

    Safe to pass to submit() more than once, including after a prior partial
    failure (e.g. one budget's create_transactions succeeded, another's
    raised): every write submit() makes is naturally idempotent on retry -
    YNAB's import_id dedup (invariant 1) makes a resubmitted create a no-op
    duplicate, update_transactions' PATCH payloads are idempotent by
    construction, and StateDB.rekey_pending_to_booked (see
    fuzzy_updates_by_budget below) has its own explicit idempotent-retry
    return value (None on a resubmit whose rekey already landed) - so
    resubmitting the whole thing is always safe, never a double-book risk.
    """

    creates_by_budget: dict[str, list[_PendingCreate]]
    updates_by_budget: dict[str, list[_PendingUpdate]]
    matches_by_budget: dict[str, list[_PendingManualMatch]]
    # Pending->booked transitions resolved via sync/pending_match.py's fuzzy
    # correlation rather than exact tracking-key equality - see
    # _classify()'s "transitioned_fuzzy" outcome and _PendingFuzzyUpdate.
    fuzzy_updates_by_budget: dict[str, list[_PendingFuzzyUpdate]]
    account_last_synced: dict[str, str]
    mappings_count: int
    fetched_count: int


@dataclass
class FileImportRowResult:
    row_index: int
    date: str
    amount_milliunits: int
    payee_name: str
    memo: str | None
    status: str  # "new", "duplicate", or "error"


@dataclass
class FileImportResult:
    rows: list[FileImportRowResult]
    created: int
    resolved_deleted: int
    committed: bool


class SyncEngine:
    """Orchestrates one sync cycle: fetch from every configured provider ->
    classify against local tracking -> create-or-update in YNAB, grouped
    per resolved budget.

    run_cycle() is a convenience composition of two phases that are
    deliberately exposed separately - fetch_and_classify() (provider reads +
    local classification, returning a ClassifiedCycle) and submit()
    (the YNAB writes). scheduler.py calls the two phases directly so a
    failed YNAB submission can be retried, with backoff, WITHOUT re-fetching
    from the provider - see ClassifiedCycle's docstring and scheduler.py's
    _attempt_cycle().

    The engine is provider-agnostic: it knows only the TransactionProvider
    contract (providers/base.py) and consumes NormalizedTransaction values.
    It has no idea SpareBank1 exists. YNAB, by contrast, is deliberately
    hardcoded - it is the one and only target, and abstracting it would buy
    nothing.

    Both transaction sources - a live provider poll and a file import -
    converge on the SAME classify/submit/reconcile path (`_classify`, then
    `_record_created`/`_backfill_duplicates`). That is what makes the
    "no double-booking across sources" guarantee structural rather than
    something two parallel code paths have to remember to agree on.

    Two different persistence guarantees are in play here, deliberately:

    - Account cursors / run metadata (owned by the caller, via the
      (CycleResult, account_last_synced) this method returns) are only
      ever persisted by the caller after the WHOLE cycle succeeds - the
      original no-duplicates design: if anything raises, no cursor
      advances, and next cycle's overlap window retries everything.
    - `tracked_transactions` rows, by contrast, ARE written immediately
      here as each YNAB call succeeds, not deferred to end-of-cycle. This
      is safe and desirable: they record real external state (a
      transaction that genuinely now exists in YNAB), and recording them
      promptly means a later, unrelated budget's failure in the same
      cycle can't cause a retry to attempt creating an already-created
      transaction again - it'll be correctly recognized as already
      tracked next time around, cursor-advance or not.
    """

    def __init__(
        self,
        config: AppConfig,
        http_client: httpx.AsyncClient,
        db: StateDB,
        providers: dict[str, TransactionProvider],
    ):
        self._config = config
        self._http_client = http_client
        self._db = db
        self._providers = providers

    def _compute_fetch_windows(
        self, mappings: list[dict[str, Any]]
    ) -> dict[str, dict[str, datetime]]:
        """Per-provider {provider_account_id: since} fetch windows.

        This is deliberately provider-agnostic and stays in the engine: it
        depends only on the local cursor state and config, never on how a
        particular bank's API works. How to turn these windows into actual
        requests (batching several accounts into one call, etc.) is the
        provider's business - see providers/base.py's fetch() contract.
        """
        account_states = self._db.read_account_states()
        windows: dict[str, dict[str, datetime]] = defaultdict(dict)
        for mapping in mappings:
            account_id = mapping["provider_account_id"]
            since = compute_since(
                account_states.get(account_id),
                self._config.sync.initial_backfill_days,
                self._config.sync.lookback_overlap_hours,
            )
            # A pending transaction can take longer to settle than the
            # normal overlap window looks back - without this, it would
            # age out of every future fetch before ever booking, and stay
            # 'uncleared' in YNAB forever. Widen (never narrow) the window
            # to always cover the oldest transaction we're still waiting on.
            pending_floor = self._db.get_earliest_pending_first_seen(account_id)
            if pending_floor:
                since = min(since, datetime.fromisoformat(pending_floor))
            windows[mapping["provider"]][account_id] = since
        return windows

    def _find_manual_match(
        self, ntx: NormalizedTransaction, candidates: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Look for exactly one pre-existing, manually-typed YNAB
        transaction (no import_id - see ynab_client.list_unimported_
        transactions) that plausibly IS this same real-world transaction:
        exact amount match, date within manual_match_window_days (counted
        in match_window_unit - see sync/date_window.py).

        Split transactions (non-empty subtransactions) are never eligible -
        replicating a split's per-category amounts correctly is real added
        complexity this feature doesn't attempt; such a candidate is simply
        never matched, same safe-default as any other "don't guess" case
        here.

        More than one same-amount, in-window candidate is ambiguous and
        deliberately not guessed - same "don't guess" stance
        transfers.find_transfer_pairs already uses for ambiguous transfer
        pairs. Returns None in both the "no match" and "ambiguous" cases;
        the caller falls through to a normal create either way.
        """
        window_days = self._config.sync.manual_match_window_days
        unit = self._config.sync.match_window_unit
        matches = [
            c
            for c in candidates
            if not c.get("subtransactions")
            and c.get("amount") == ntx.amount_milliunits
            and days_between(date.fromisoformat(c["date"]), ntx.date, unit) <= window_days
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "Ambiguous manual-transaction match for %r (%d same-amount, in-window "
                "candidates) - importing as a new transaction instead of guessing.",
                ntx.tracking_key,
                len(matches),
            )
        return None

    def _classify(
        self,
        ntx: NormalizedTransaction,
        *,
        provider_type: str,
        ynab_account_id: str,
        ynab_budget_id: str,
        manual_candidates: list[dict[str, Any]] | None = None,
        pending_candidates: list[PendingCandidate] | None = None,
    ) -> tuple[
        str, _PendingCreate | _PendingUpdate | _PendingFuzzyUpdate | _PendingManualMatch | None
    ]:
        """Decide what one already-normalized transaction needs, by looking
        it up in local tracking. THE shared decision point for every source
        - a live provider poll and a file import both come through here, so
        neither can double-book what the other already imported.

        manual_candidates, when given and non-empty, is this transaction's
        YNAB account's own list of not-yet-imported (no import_id)
        transactions - see fetch_and_classify()'s per-account cache and
        CLAUDE.md's "Manual-transaction matching" section. A match removes
        its candidate from this list in place, so two incoming transactions
        in the same cycle can't both claim it.

        pending_candidates, when given and non-empty, is this transaction's
        provider account's own locally-tracked still-PENDING rows (see
        StateDB.list_pending_candidates and CLAUDE.md's "PENDING-transaction
        import" section) - only consulted for a BOOKED ntx, since a fuzzy
        pending->booked correlation only ever needs to happen once, when the
        real transaction settles. A match is popped from this list in place,
        same "can't be double-claimed within one cycle" contract as
        manual_candidates.

        Returns ("new", create) | ("transitioned", update) |
        ("transitioned_fuzzy", fuzzy_update) | ("matched_manual", match) |
        ("unchanged", None).
        """
        tracked = self._db.get_tracked(ntx.tracking_key)

        if tracked is None:
            if ntx.booking_status == BookingStatus.BOOKED and pending_candidates:
                fuzzy_match = find_pending_match(
                    BookedCandidate(
                        account_key=ntx.provider_account_id,
                        date=ntx.date,
                        amount_milliunits=ntx.amount_milliunits,
                        payee=ntx.payee_name,
                    ),
                    pending_candidates,
                    amount_tolerance_kroner=self._config.sync.pending_import_amount_tolerance_kroner,
                    date_window_days=self._config.sync.pending_import_date_window_days,
                    unit=self._config.sync.match_window_unit,
                )
                if fuzzy_match is not None:
                    pending_candidates.remove(fuzzy_match.candidate)
                    return "transitioned_fuzzy", _PendingFuzzyUpdate(
                        old_tracking_key=fuzzy_match.candidate.tracking_key,
                        new_tracking_key=ntx.tracking_key,
                        ynab_transaction_id=fuzzy_match.candidate.ynab_transaction_id,
                        new_amount_milliunits=ntx.amount_milliunits,
                        ynab_budget_id=fuzzy_match.candidate.ynab_budget_id,
                        provider_type=provider_type,
                        account_key=ntx.provider_account_id,
                    )

            # Each booking status has its OWN independent opt-in, not a
            # shared one - see _get_manual_candidates' fetch-gate comment.
            # A PENDING transaction colliding with something the user
            # already typed into YNAB by hand is an expected, first-class
            # scenario for pending-import specifically (confirmed live,
            # 2026-08-25 production: 3 of 9 PENDING imports duplicated
            # manually-typed entries because this used to share
            # manual_match_window_days, which was off) - so it's gated on
            # pending_import_enabled alone, never on the separate BOOKED-
            # transaction opt-in below, which keeps its own unchanged "no
            # strong signal, off by default" risk profile.
            manual_match_allowed = (
                self._config.sync.pending_import_enabled
                if ntx.booking_status == BookingStatus.PENDING
                else self._config.sync.manual_match_window_days > 0
            )
            if manual_candidates and manual_match_allowed:
                if ntx.booking_status == BookingStatus.PENDING:
                    # A PENDING transaction's amount is always whole kroner
                    # (a preauth hold), so it will almost never exactly
                    # match a hand-typed final decimal amount - use the
                    # tolerant sibling instead of the exact matcher below.
                    match = find_manual_match_tolerant(
                        ntx.amount_milliunits,
                        ntx.date,
                        manual_candidates,
                        amount_tolerance_kroner=(
                            self._config.sync.pending_import_amount_tolerance_kroner
                        ),
                        date_window_days=self._config.sync.pending_import_date_window_days,
                        unit=self._config.sync.match_window_unit,
                        pending_payee=ntx.payee_name,
                    )
                else:
                    match = self._find_manual_match(ntx, manual_candidates)
                if match is not None:
                    manual_candidates.remove(match)
                    cleared = (
                        "uncleared" if ntx.booking_status == BookingStatus.PENDING else "cleared"
                    )
                    replacement_tx: dict[str, Any] = {
                        "account_id": ynab_account_id,
                        "date": ntx.date.isoformat(),
                        "amount": ntx.amount_milliunits,
                        "category_id": match.get("category_id"),
                        "memo": match.get("memo"),
                        "cleared": cleared,
                        "approved": match.get("approved", False),
                        "flag_color": match.get("flag_color"),
                        "import_id": ntx.import_id,
                    }
                    if match.get("payee_id"):
                        replacement_tx["payee_id"] = match["payee_id"]
                    elif match.get("payee_name"):
                        replacement_tx["payee_name"] = match["payee_name"]
                    return "matched_manual", _PendingManualMatch(
                        tracking_key=ntx.tracking_key,
                        ynab_tx=replacement_tx,
                        original_transaction_id=match["id"],
                        booking_status=str(ntx.booking_status),
                        provider_account_id=ntx.provider_account_id,
                        provider_type=provider_type,
                    )

            # Anchor to a previously-cached YNAB payee_id when this exact
            # raw payee string has been created before in this budget, so
            # the submission is immune to the user having since renamed or
            # merged that payee in YNAB (payee_name only ever matches by
            # exact string - see StateDB's payee_mappings docstring and
            # scripts/verify_ynab_payee_id.py). None on a genuinely new
            # merchant, which falls back to payee_name as before.
            payee_id = self._db.get_payee_id(ynab_budget_id, ntx.payee_name)
            ynab_tx = build_create_payload(
                ynab_account_id=ynab_account_id,
                tx_date=ntx.date,
                amount_milliunits=ntx.amount_milliunits,
                payee_name=ntx.payee_name,
                memo=ntx.memo,
                cleared=(
                    "uncleared" if ntx.booking_status == BookingStatus.PENDING else "cleared"
                ),
                # Pure passthrough - the provider owns this value and its
                # dedup-domain prefix (invariants 1/6/7). Never re-derive it.
                import_id=ntx.import_id,
                payee_id=payee_id,
            )
            return "new", _PendingCreate(
                tracking_key=ntx.tracking_key,
                ynab_tx=ynab_tx,
                booking_status=str(ntx.booking_status),
                provider_account_id=ntx.provider_account_id,
                provider_type=provider_type,
                payee_name=ntx.payee_name,
                account_number=ntx.account_number,
                remote_account_number=ntx.remote_account_number,
            )

        if (
            tracked["booking_status"] == BookingStatus.PENDING
            and ntx.booking_status == BookingStatus.BOOKED
        ):
            return "transitioned", _PendingUpdate(
                tracking_key=ntx.tracking_key,
                ynab_transaction_id=tracked["ynab_transaction_id"],
                new_amount_milliunits=ntx.amount_milliunits,
                # Update where the transaction actually lives, which may
                # not be the mapping's current budget.
                ynab_budget_id=tracked["ynab_budget_id"],
                provider_type=provider_type,
                account_key=ntx.provider_account_id,
            )

        return "unchanged", None

    async def fetch_and_classify(
        self, on_progress: ProgressCallback | None = None
    ) -> ClassifiedCycle:
        """Phase 1 of a sync cycle: fetch from every configured provider,
        classify each transaction against local tracking, and match
        transfer pairs - everything that depends on a provider's own API
        (SpareBank1 today). See ClassifiedCycle's docstring for why this is
        kept separate from submit() (phase 2, the YNAB writes).

        on_progress, when given, is called once per provider right before
        that provider is fetched - see _report()'s docstring for the
        best-effort contract. Defaults to None everywhere so every existing
        caller is unaffected.
        """
        # Mappings are the source of truth for "which accounts sync where",
        # and live in SQLite so the GUI can edit them at runtime. A disabled
        # mapping is skipped entirely here; its existing PENDING tracked
        # rows are left untouched rather than pruned, so re-enabling it
        # resumes reconciliation instead of orphaning them.
        mappings = self._db.list_mappings(enabled_only=True)
        account_last_synced: dict[str, str] = {}

        creates_by_budget: dict[str, list[_PendingCreate]] = defaultdict(list)
        updates_by_budget: dict[str, list[_PendingUpdate]] = defaultdict(list)
        matches_by_budget: dict[str, list[_PendingManualMatch]] = defaultdict(list)
        fuzzy_updates_by_budget: dict[str, list[_PendingFuzzyUpdate]] = defaultdict(list)
        # Per-ynab_account_id cache of "not yet imported" YNAB transactions
        # (see ynab_client.list_unimported_transactions), populated lazily -
        # only the first time an account is touched this cycle, and never at
        # all when manual_match_window_days is 0 (the default) - so the
        # disabled/no-manual-entries case costs zero extra API calls.
        manual_candidates_cache: dict[str, list[dict[str, Any]]] = {}
        # Per-provider-account (NOT ynab_account_id - tracked_transactions.
        # account_key is the provider's own id) cache of this account's
        # still-PENDING tracked rows, populated lazily and only when
        # pending_import_enabled - so the disabled case costs zero extra DB
        # queries, mirroring _get_manual_candidates' own gate exactly.
        pending_candidates_cache: dict[str, list[PendingCandidate]] = {}

        async def _get_manual_candidates(
            ynab_account_id: str, ynab_budget_id: str
        ) -> list[dict[str, Any]]:
            # Fetched whenever EITHER toggle might need it - manual_match_
            # window_days for a BOOKED ntx's exact match below, pending_
            # import_enabled for a PENDING ntx's tolerant match. _classify()
            # gates actual usage per-status independently (see its own
            # comment) - this only controls whether the list is worth
            # fetching at all this cycle.
            if self._config.sync.manual_match_window_days <= 0 and not (
                self._config.sync.pending_import_enabled
            ):
                return []
            if ynab_account_id not in manual_candidates_cache:
                manual_candidates_cache[ynab_account_id] = (
                    await ynab_client.list_unimported_transactions(
                        self._http_client,
                        self._config.ynab.personal_access_token,
                        ynab_budget_id,
                        ynab_account_id,
                    )
                )
            return manual_candidates_cache[ynab_account_id]

        def _get_pending_candidates(provider_account_id: str) -> list[PendingCandidate]:
            if not self._config.sync.pending_import_enabled:
                return []
            if provider_account_id not in pending_candidates_cache:
                pending_candidates_cache[provider_account_id] = [
                    PendingCandidate(
                        index=i,
                        tracking_key=row["sb1_transaction_id"],
                        ynab_transaction_id=row["ynab_transaction_id"],
                        ynab_budget_id=row["ynab_budget_id"],
                        account_key=provider_account_id,
                        amount_milliunits=row["amount_milliunits"],
                        date=datetime.fromisoformat(row["first_seen_at"]).date(),
                        payee=row["payee_name"] or "",
                    )
                    for i, row in enumerate(self._db.list_pending_candidates(provider_account_id))
                ]
            return pending_candidates_cache[provider_account_id]

        mapping_by_account: dict[tuple[str, str], dict[str, Any]] = {
            (m["provider"], m["provider_account_id"]): m for m in mappings
        }
        windows_by_provider = self._compute_fetch_windows(mappings)

        fetched_count = 0
        for provider_type, since_by_account in windows_by_provider.items():
            provider = self._providers.get(provider_type)
            if provider is None:
                logger.error(
                    "%d account mapping(s) reference provider %r, which is not "
                    "configured - skipping those accounts this cycle: %s",
                    len(since_by_account),
                    provider_type,
                    sorted(since_by_account),
                )
                continue

            await _report(
                on_progress,
                "fetching",
                {"provider": provider_type, "accounts": len(since_by_account)},
            )

            async def _on_skip(
                reason: str, context: dict[str, Any], *, _provider_type: str = provider_type
            ) -> None:
                # Keeps all StateDB access inside engine.py - the provider
                # only ever calls an opaque callback, never touches the DB
                # itself (see providers/base.py's SkipCallback contract).
                # context's raw row is deliberately not parsed further here
                # - it's whatever a provider had on hand for a row it
                # already couldn't make sense of, so guessing at field names
                # (e.g. a payee) would risk surfacing wrong data as if it
                # were reliable. The reason string is the whole point.
                # account_key, when the provider included one, is the one
                # exception - it's a structural identifier, not a guess at
                # unreliable row content, and it's what the account filter
                # in the Audit Log tab needs.
                account_key = context.get("account_key") if isinstance(context, dict) else None
                await self._db.insert_audit_event(
                    event_type="skipped",
                    source=_provider_type,
                    account_key=account_key if isinstance(account_key, str) else None,
                    detail=reason,
                )

            normalized = await provider.fetch(since_by_account, on_skip=_on_skip)
            fetched_count += len(normalized)

            for ntx in normalized:
                mapping = mapping_by_account.get((provider_type, ntx.provider_account_id))
                if mapping is None:
                    logger.error(
                        "Provider %r returned a transaction for unmapped account %r "
                        "- skipping: %r",
                        provider_type,
                        ntx.provider_account_id,
                        ntx.tracking_key,
                    )
                    await self._db.insert_audit_event(
                        event_type="skipped",
                        source=provider_type,
                        account_key=ntx.provider_account_id,
                        tracking_key=ntx.tracking_key,
                        import_id=ntx.import_id,
                        payee_name=ntx.payee_name,
                        memo=ntx.memo,
                        transaction_date=ntx.date.isoformat(),
                        amount_milliunits=ntx.amount_milliunits,
                        detail=f"unmapped account: {provider_type}/{ntx.provider_account_id}",
                    )
                    continue

                # Defensive client-side cutoff. Providers deliberately
                # over-fetch (one shared window for a batched request), so
                # applying each account's OWN since is the engine's job -
                # see providers/base.py's fetch() contract.
                if ntx.date < since_by_account[ntx.provider_account_id].date():
                    await self._db.insert_audit_event(
                        event_type="skipped",
                        source=provider_type,
                        account_key=ntx.provider_account_id,
                        tracking_key=ntx.tracking_key,
                        import_id=ntx.import_id,
                        payee_name=ntx.payee_name,
                        memo=ntx.memo,
                        transaction_date=ntx.date.isoformat(),
                        amount_milliunits=ntx.amount_milliunits,
                        detail="stale: transaction date before this account's fetch window",
                    )
                    continue

                manual_candidates = await _get_manual_candidates(
                    mapping["ynab_account_id"], mapping["ynab_budget_id"]
                )
                pending_candidates = _get_pending_candidates(ntx.provider_account_id)
                outcome, pending = self._classify(
                    ntx,
                    provider_type=provider_type,
                    ynab_account_id=mapping["ynab_account_id"],
                    ynab_budget_id=mapping["ynab_budget_id"],
                    manual_candidates=manual_candidates,
                    pending_candidates=pending_candidates,
                )
                if outcome == "new":
                    creates_by_budget[mapping["ynab_budget_id"]].append(pending)
                elif outcome == "transitioned":
                    updates_by_budget[pending.ynab_budget_id].append(pending)
                elif outcome == "transitioned_fuzzy":
                    fuzzy_updates_by_budget[pending.ynab_budget_id].append(pending)
                elif outcome == "matched_manual":
                    matches_by_budget[mapping["ynab_budget_id"]].append(pending)
                # else: already tracked, status unchanged - nothing to do

            for account_id in since_by_account:
                account_last_synced[account_id] = None  # filled in below

        # Detect and rewire transfer pairs BEFORE anything is submitted -
        # only among transactions being freshly created this cycle, only
        # within one budget at a time (a transfer across two budgets can't
        # use YNAB's linked-transfer mechanism at all - see
        # scripts/verify_ynab_transfer.py). Mutates creates_by_budget in
        # place: a matched pair's "secondary" leg is removed entirely (it
        # must never be submitted - YNAB auto-creates it), and the
        # "primary" leg's payload is rewritten to create it as a real
        # linked transfer instead of an ordinary transaction.
        await self._match_transfers(creates_by_budget)

        return ClassifiedCycle(
            creates_by_budget=creates_by_budget,
            updates_by_budget=updates_by_budget,
            matches_by_budget=matches_by_budget,
            fuzzy_updates_by_budget=fuzzy_updates_by_budget,
            account_last_synced=account_last_synced,
            mappings_count=len(mappings),
            fetched_count=fetched_count,
        )

    async def submit(
        self, classified: ClassifiedCycle, on_progress: ProgressCallback | None = None
    ) -> tuple[CycleResult, dict[str, str]]:
        """Phase 2 of a sync cycle: submit a previously-classified cycle's
        creates/updates to YNAB. See ClassifiedCycle's docstring - safe to
        call more than once with the same object, including after a prior
        partial failure.

        on_progress, when given, is called once per budget right before
        that budget's creates/updates are submitted - see
        fetch_and_classify()'s matching parameter and _report()'s
        best-effort contract.
        """
        creates_by_budget = classified.creates_by_budget
        updates_by_budget = classified.updates_by_budget
        account_last_synced = classified.account_last_synced

        total_created = 0
        total_duplicates = 0
        total_resolved_deleted = 0
        for budget_id, pending_creates in creates_by_budget.items():
            await _report(
                on_progress,
                "submitting",
                {"budget_id": budget_id, "creates": len(pending_creates)},
            )
            response = await ynab_client.create_transactions(
                self._http_client,
                self._config.ynab.personal_access_token,
                budget_id,
                [p.ynab_tx for p in pending_creates],
            )
            # Counting from response["transaction_ids"] is NOT reliable here:
            # confirmed live that a transfer-creating submission returns that
            # list with the same id listed TWICE. _record_created's return
            # value (rows actually written to tracked_transactions - the
            # primary plus, for a matched transfer, its secondary) is the
            # authoritative count instead.
            total_created += await self._record_created(budget_id, pending_creates, response)
            total_duplicates += len(response.get("duplicate_import_ids", []))
            total_resolved_deleted += await self._backfill_duplicates(
                budget_id, pending_creates, response.get("duplicate_import_ids", [])
            )

        total_updated = 0
        for budget_id, pending_updates in updates_by_budget.items():
            await _report(
                on_progress,
                "submitting",
                {"budget_id": budget_id, "updates": len(pending_updates)},
            )
            updates = [
                {
                    "id": p.ynab_transaction_id,
                    "cleared": "cleared",
                    "amount": p.new_amount_milliunits,
                }
                for p in pending_updates
            ]
            await ynab_client.update_transactions(
                self._http_client, self._config.ynab.personal_access_token, budget_id, updates
            )
            for p in pending_updates:
                await self._db.mark_booked(p.tracking_key, p.new_amount_milliunits)
                await self._db.insert_audit_event(
                    event_type="updated",
                    source=p.provider_type,
                    account_key=p.account_key,
                    tracking_key=p.tracking_key,
                    ynab_transaction_id=p.ynab_transaction_id,
                    ynab_budget_id=p.ynab_budget_id,
                    amount_milliunits=p.new_amount_milliunits,
                    detail="pending → booked transition",
                )
            total_updated += len(pending_updates)

        # Same phase as the plain updates loop above (PATCH-only, no
        # create) - the only difference is StateDB.rekey_pending_to_booked
        # instead of mark_booked, since this outcome's tracking key changed
        # (see _PendingFuzzyUpdate's docstring). Folded into total_updated,
        # same "indistinguishable from an ordinary update" reasoning
        # matched_manual below already uses for total_created.
        for budget_id, pending_fuzzy in classified.fuzzy_updates_by_budget.items():
            await _report(
                on_progress,
                "submitting",
                {"budget_id": budget_id, "fuzzy_updates": len(pending_fuzzy)},
            )
            fuzzy_updates = [
                {
                    "id": p.ynab_transaction_id,
                    "cleared": "cleared",
                    "amount": p.new_amount_milliunits,
                }
                for p in pending_fuzzy
            ]
            await ynab_client.update_transactions(
                self._http_client,
                self._config.ynab.personal_access_token,
                budget_id,
                fuzzy_updates,
            )
            for p in pending_fuzzy:
                previous = await self._db.rekey_pending_to_booked(
                    p.old_tracking_key, p.new_tracking_key, p.new_amount_milliunits
                )
                if previous is None:
                    # Already applied by a prior submit() attempt on this
                    # same cached ClassifiedCycle (see rekey_pending_to_
                    # booked's own idempotent-retry docstring) - no
                    # double-count, no double audit event.
                    continue
                detail = "pending → booked (fuzzy-matched, tracking key changed)"
                if previous["amount_milliunits"] != p.new_amount_milliunits:
                    detail += (
                        f": {previous['amount_milliunits'] / 1000:.2f} kr → "
                        f"{p.new_amount_milliunits / 1000:.2f} kr"
                    )
                await self._db.insert_audit_event(
                    event_type="updated",
                    source=p.provider_type,
                    account_key=p.account_key,
                    tracking_key=p.new_tracking_key,
                    import_id=previous["import_id"],
                    ynab_transaction_id=p.ynab_transaction_id,
                    ynab_budget_id=p.ynab_budget_id,
                    payee_name=previous["payee_name"],
                    amount_milliunits=p.new_amount_milliunits,
                    detail=detail,
                )
            total_updated += len(pending_fuzzy)

        # Folded into total_created (not a separate CycleResult field): a
        # matched-and-replaced manual transaction results in exactly one
        # net-new tracked transaction with a real import_id, same as an
        # ordinary create - counting it separately would mean growing
        # CycleResult and rippling that into run_metadata columns, MQTT
        # discovery sensors, the dashboard, and ntfy message bodies for a
        # naming nicety only.
        for budget_id, pending_matches in classified.matches_by_budget.items():
            await _report(
                on_progress,
                "submitting",
                {"budget_id": budget_id, "matches": len(pending_matches)},
            )
            response = await ynab_client.create_transactions(
                self._http_client,
                self._config.ynab.personal_access_token,
                budget_id,
                [p.ynab_tx for p in pending_matches],
            )
            total_created += await self._record_matched(budget_id, pending_matches, response)

        # Only reached once every create/update call above has succeeded -
        # anything raised earlier leaves account_last_synced with None
        # placeholders that the caller must never persist.
        now_iso = datetime.now(UTC).isoformat()
        for account_key in account_last_synced:
            account_last_synced[account_key] = now_iso

        result = CycleResult(
            created=total_created,
            updated=total_updated,
            duplicates=total_duplicates,
            resolved_deleted=total_resolved_deleted,
            accounts_processed=classified.mappings_count,
            fetched=classified.fetched_count,
        )
        return result, account_last_synced

    async def run_cycle(
        self, on_progress: ProgressCallback | None = None
    ) -> tuple[CycleResult, dict[str, str]]:
        """Convenience composition of fetch_and_classify() + submit() for
        any caller that doesn't need the two phases split apart (every
        existing caller except scheduler.py's retry/backoff handling, which
        calls the two phases directly - see _do_cycle())."""
        classified = await self.fetch_and_classify(on_progress=on_progress)
        return await self.submit(classified, on_progress=on_progress)

    async def _record_created(
        self,
        budget_id: str,
        pending_creates: list[_PendingCreate],
        response: dict[str, Any],
    ) -> int:
        """Writes a tracked_transactions row for each transaction YNAB
        confirms it created, plus - for any matched transfer's primary leg
        (see _match_transfers) - a second row for its auto-created
        secondary leg. Returns the total number of rows written, which is
        the authoritative "created" count for CycleResult (NOT
        response["transaction_ids"]'s length - confirmed live that a
        transfer-creating submission lists the same id in that array
        twice).
        """
        by_import_id = {p.ynab_tx["import_id"]: p for p in pending_creates}
        tracked_count = 0
        for created in response.get("transactions", []):
            p = by_import_id.get(created.get("import_id"))
            if p is None:
                continue
            # Confirmed live (2026-08-25, production): a create submitted
            # with a cached payee_id that no longer resolves to a real YNAB
            # payee (deleted/merged since it was cached) is NOT rejected -
            # YNAB returns 200 but silently drops it, coming back with
            # payee_id AND payee_name both null. This is a stronger failure
            # than the daily reconcile pass (see StateDB.
            # delete_payee_mappings_for_ids) was built to guard against: it
            # only protects the NEXT create after its once-a-day sweep, not
            # the create that first hits a payee deleted inside that
            # window. Excludes a transfer primary leg (transfer_secondary
            # is not None) - its payee_id is YNAB's own auto-generated
            # transfer payee, never our merchant cache, and should never
            # legitimately land here.
            stale_payee_id = (
                p.transfer_secondary is None
                and bool(p.ynab_tx.get("payee_id"))
                and not created.get("payee_id")
            )
            resolved_payee_name = p.ynab_tx.get("payee_name") or created.get("payee_name")
            if stale_payee_id:
                resolved_payee_name = p.payee_name
            await self._db.upsert_tracked(
                p.tracking_key,
                import_id=p.ynab_tx["import_id"],
                ynab_transaction_id=created["id"],
                ynab_budget_id=budget_id,
                account_key=p.provider_account_id,
                booking_status=p.booking_status,
                amount_milliunits=p.ynab_tx["amount"],
                payee_name=resolved_payee_name,
                memo=p.ynab_tx["memo"],
                transaction_date=p.ynab_tx["date"],
                ynab_account_id=p.ynab_tx["account_id"],
                cleared=p.ynab_tx["cleared"],
            )
            await self._db.insert_audit_event(
                event_type="created",
                source=p.provider_type,
                account_key=p.provider_account_id,
                tracking_key=p.tracking_key,
                import_id=p.ynab_tx["import_id"],
                ynab_transaction_id=created["id"],
                ynab_budget_id=budget_id,
                ynab_account_id=p.ynab_tx["account_id"],
                payee_name=resolved_payee_name,
                memo=p.ynab_tx["memo"],
                transaction_date=p.ynab_tx["date"],
                amount_milliunits=p.ynab_tx["amount"],
                detail="transfer primary leg" if p.transfer_secondary is not None else None,
            )
            tracked_count += 1
            if stale_payee_id:
                stale_id = p.ynab_tx["payee_id"]
                logger.warning(
                    "Create for %r used cached payee_id %s, which YNAB silently "
                    "dropped (came back null) - the payee was likely deleted or "
                    "merged in YNAB since it was cached. Healing the cache and "
                    "correcting the created transaction %s's payee now.",
                    p.payee_name,
                    stale_id,
                    created["id"],
                )
                await self._db.delete_payee_mappings_for_ids(budget_id, {stale_id})
                await ynab_client.update_transactions(
                    self._http_client,
                    self._config.ynab.personal_access_token,
                    budget_id,
                    [{"id": created["id"], "payee_name": p.payee_name}],
                )
            # Only anchor a NEW payee_mappings entry when this create was
            # actually submitted with payee_name (no "payee_id" key at all
            # in the payload) - a transfer's primary leg already carries
            # YNAB's own transfer payee_id here (see _match_transfers),
            # which must never be cached under a raw merchant name, and a
            # cache hit from _classify's lookup needs no re-write (upsert
            # is INSERT OR IGNORE anyway, but skipping avoids the query).
            if p.ynab_tx.get("payee_id") is None and created.get("payee_id"):
                await self._db.upsert_payee_mapping(
                    budget_id, p.payee_name, created["payee_id"]
                )
            if p.transfer_secondary is not None:
                tracked_count += await self._record_transfer_secondary(budget_id, p, created)
        return tracked_count

    async def _record_transfer_secondary(
        self, budget_id: str, primary: _PendingCreate, created_primary: dict[str, Any]
    ) -> int:
        """Records the tracked_transactions row for the OTHER leg of a
        matched transfer (see _match_transfers) - the one YNAB auto-creates
        and that was therefore never itself submitted via
        create_transactions. Returns 1 if recorded, 0 if it couldn't be
        (logged as an error - see the missing-transfer_transaction_id case
        below).

        Known residual risk: if the process crashes between this call and
        the primary's own upsert_tracked call just above it (an extremely
        narrow window - both are local SQLite writes, no network call in
        between), the secondary leg could be left permanently untracked.
        A later cycle would then see it as a fresh "new" transaction from
        its own account's fetch and submit it independently - and since
        YNAB's auto-created secondary has no import_id for that submission
        to collide with, this specific case would create a genuine
        duplicate. Not fully solved here (would need wrapping both
        upserts in one atomic DB transaction, a larger StateDB change) -
        documented as a known, narrow gap rather than silently ignored.
        """
        secondary = primary.transfer_secondary
        assert secondary is not None
        transfer_transaction_id = created_primary.get("transfer_transaction_id")
        if not transfer_transaction_id:
            logger.error(
                "Expected transfer_transaction_id on created transfer-primary "
                "transaction %s but none was present - secondary leg %r was NOT "
                "tracked and may be misclassified as a new transaction next cycle: %r",
                created_primary.get("id"),
                secondary.tracking_key,
                created_primary,
            )
            return 0

        secondary_cleared = "uncleared" if secondary.booking_status == "PENDING" else "cleared"
        synthetic_import_id = _TRANSFER_SECONDARY_IMPORT_ID_PREFIX + hashlib.sha256(
            secondary.tracking_key.encode("utf-8")
        ).hexdigest()[:20]
        await self._db.upsert_tracked(
            secondary.tracking_key,
            import_id=synthetic_import_id,
            ynab_transaction_id=transfer_transaction_id,
            ynab_budget_id=budget_id,
            account_key=secondary.provider_account_id,
            booking_status=secondary.booking_status,
            amount_milliunits=secondary.ynab_tx["amount"],
            payee_name=created_primary.get("payee_name"),
            memo=secondary.ynab_tx["memo"],
            transaction_date=secondary.ynab_tx["date"],
            ynab_account_id=secondary.ynab_tx["account_id"],
            cleared=secondary_cleared,
        )
        await self._db.insert_audit_event(
            event_type="created",
            source=secondary.provider_type,
            account_key=secondary.provider_account_id,
            tracking_key=secondary.tracking_key,
            import_id=synthetic_import_id,
            ynab_transaction_id=transfer_transaction_id,
            ynab_budget_id=budget_id,
            ynab_account_id=secondary.ynab_tx["account_id"],
            payee_name=created_primary.get("payee_name"),
            memo=secondary.ynab_tx["memo"],
            transaction_date=secondary.ynab_tx["date"],
            amount_milliunits=secondary.ynab_tx["amount"],
            detail="transfer secondary leg (auto-created by YNAB)",
        )

        # Confirmed live: YNAB defaults the auto-created leg to uncleared
        # regardless of the primary's own cleared status. If the provider
        # already reports this leg as booked, correct YNAB's state
        # immediately via the same PATCH a normal pending->booked
        # transition uses, rather than waiting for a future poll.
        if secondary.booking_status == "BOOKED":
            await ynab_client.update_transactions(
                self._http_client,
                self._config.ynab.personal_access_token,
                budget_id,
                [
                    {
                        "id": transfer_transaction_id,
                        "cleared": "cleared",
                        "amount": secondary.ynab_tx["amount"],
                    }
                ],
            )
        return 1

    async def _record_matched(
        self,
        budget_id: str,
        pending_matches: list[_PendingManualMatch],
        response: dict[str, Any],
    ) -> int:
        """Finishes a "matched_manual" outcome (see _classify()): tracks the
        just-created replacement transaction, then deletes the original
        manual transaction it replaces. Returns the number of replacement
        rows tracked (== new transactions in YNAB), for submit()'s
        total_created.

        Never indexes response["transactions"] positionally. YNAB's
        create_transactions has its own native, undocumented transaction-
        matching: when a submitted transaction carries an import_id and an
        existing transaction on the same account/amount is still present
        (which it always is here, since the original isn't deleted until
        AFTER this call), YNAB echoes that still-live original back in this
        SAME response too (with its own approved flipped to false and
        matched_transaction_id set on both sides) - confirmed live via
        scripts/verify_ynab_delete_recreate.py. Harmless for this flow
        (the original is deleted unconditionally below regardless), but
        picking transactions[0] would sometimes grab the wrong row - the
        replacement is found by its own submitted import_id instead, same
        pattern _record_created already uses.

        Deliberately create-then-delete, never the other order: if the
        create fails, the original manual transaction is left completely
        untouched (safe). If the create succeeds but the delete fails, both
        transactions remain visible in YNAB - a manually-resolvable
        duplicate, not lost data, which is the safer of the two possible
        failure modes.

        Retry-safe: submit() may be called again with the same
        ClassifiedCycle after a prior partial failure (see its docstring).
        A resubmitted match comes back in duplicate_import_ids rather than
        transactions - handled below by re-attempting only the delete
        (idempotent: a 404 means a prior attempt's delete already
        succeeded, confirmed live, not an error).
        """
        by_import_id = {p.ynab_tx["import_id"]: p for p in pending_matches}
        tracked_count = 0

        async def _delete_original(p: _PendingManualMatch, new_ynab_transaction_id: str) -> None:
            try:
                await ynab_client.delete_transaction(
                    self._http_client,
                    self._config.ynab.personal_access_token,
                    budget_id,
                    p.original_transaction_id,
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return  # already deleted by a prior attempt - not an error
                logger.exception(
                    "Created replacement %s for manually-matched transaction %r, but "
                    "failed to delete the original manual transaction %s - both are now "
                    "visible in YNAB, manual cleanup needed.",
                    new_ynab_transaction_id,
                    p.tracking_key,
                    p.original_transaction_id,
                )
                await self._db.insert_audit_event(
                    event_type="updated",
                    source=p.provider_type,
                    account_key=p.provider_account_id,
                    tracking_key=p.tracking_key,
                    ynab_transaction_id=new_ynab_transaction_id,
                    ynab_budget_id=budget_id,
                    detail=(
                        f"WARNING: replacement created but failed to delete original manual "
                        f"transaction {p.original_transaction_id} - manual cleanup needed"
                    ),
                )

        for created in response.get("transactions", []):
            p = by_import_id.get(created.get("import_id"))
            if p is None:
                continue  # YNAB's native matching echoing back the original - not ours
            await self._db.upsert_tracked(
                p.tracking_key,
                import_id=p.ynab_tx["import_id"],
                ynab_transaction_id=created["id"],
                ynab_budget_id=budget_id,
                account_key=p.provider_account_id,
                booking_status=p.booking_status,
                amount_milliunits=p.ynab_tx["amount"],
                payee_name=created.get("payee_name"),
                memo=p.ynab_tx["memo"],
                transaction_date=p.ynab_tx["date"],
                ynab_account_id=p.ynab_tx["account_id"],
                cleared=p.ynab_tx["cleared"],
            )
            await self._db.insert_audit_event(
                event_type="created",
                source=p.provider_type,
                account_key=p.provider_account_id,
                tracking_key=p.tracking_key,
                import_id=p.ynab_tx["import_id"],
                ynab_transaction_id=created["id"],
                ynab_budget_id=budget_id,
                ynab_account_id=p.ynab_tx["account_id"],
                payee_name=created.get("payee_name"),
                memo=p.ynab_tx["memo"],
                transaction_date=p.ynab_tx["date"],
                amount_milliunits=p.ynab_tx["amount"],
                detail=(
                    f"matched & replaced pre-existing manual transaction "
                    f"{p.original_transaction_id}"
                ),
            )
            tracked_count += 1
            await _delete_original(p, created["id"])

        for import_id in response.get("duplicate_import_ids", []):
            p = by_import_id.get(import_id)
            if p is None:
                continue
            tracked = self._db.get_tracked(p.tracking_key)
            if tracked is not None:
                # Already created and tracked by a prior submit() attempt -
                # only the delete of the original might still be pending.
                await _delete_original(p, tracked["ynab_transaction_id"])
            # else: reported as a duplicate but no local tracking row - the
            # same rare "local state reset" case _backfill_duplicates
            # handles for ordinary creates. Not re-derived here (would need
            # find_transaction_by_import_id plus locating which original,
            # if any, still needs deleting) - low-stakes, since the
            # replacement is still fully protected by YNAB's own import_id
            # dedup either way, and worth documenting as a known
            # simplification rather than solving now.

        return tracked_count

    async def _match_transfers(
        self, creates_by_budget: dict[str, list[_PendingCreate]]
    ) -> None:
        """Detects pairs of freshly-classified transactions that look like
        two legs of a transfer between two of the user's own
        accounts mapped to the SAME YNAB budget, and rewires each matched
        pair to use YNAB's real linked-transfer mechanism instead of
        importing both sides as two ordinary, unlinked transactions.
        Confirmed live (scripts/verify_ynab_transfer.py) that creating one
        transaction with `payee_id` set to the other account's transfer
        payee auto-creates the linked paired transaction - the caller must
        not create both sides itself.

        Only matches within one budget's own creates list (a transfer
        across two different YNAB budgets can't use this mechanism at all)
        and only among transactions being freshly created THIS cycle - an
        already-tracked transaction is never retroactively converted into
        a transfer leg, so a pair that happens to be seen in different
        cycles simply imports as two ordinary transactions instead, same
        as before this feature existed.

        Mutates creates_by_budget in place.
        """
        for budget_id, pending_creates in creates_by_budget.items():
            candidates = [
                transfers.TransferCandidate(
                    index=i,
                    # Qualified by provider, not just the account id.
                    # find_transfer_pairs requires the two legs to be on
                    # DIFFERENT accounts; with several providers in play,
                    # two of them could legitimately use the same account-id
                    # string, and an unqualified key would make those look
                    # like one account and silently suppress a real match.
                    account_key=f"{p.provider_type}:{p.provider_account_id}",
                    date=date.fromisoformat(p.ynab_tx["date"]),
                    amount_milliunits=p.ynab_tx["amount"],
                    account_number=p.account_number,
                    remote_account_number=p.remote_account_number,
                )
                for i, p in enumerate(pending_creates)
            ]
            pairs = transfers.find_transfer_pairs(
                candidates,
                self._config.sync.transfer_match_window_days,
                unit=self._config.sync.match_window_unit,
            )
            if not pairs:
                continue

            payees: list[dict[str, Any]] | None = None
            indices_to_remove: set[int] = set()
            for i, j in pairs:
                # Deterministic choice: the outflow (negative amount) leg is
                # submitted directly; the inflow leg is the one YNAB
                # auto-creates on the other side.
                primary_idx, secondary_idx = (
                    (i, j) if pending_creates[i].ynab_tx["amount"] < 0 else (j, i)
                )
                primary = pending_creates[primary_idx]
                secondary = pending_creates[secondary_idx]

                if payees is None:
                    payees = await ynab_client.get_payees(
                        self._http_client, self._config.ynab.personal_access_token, budget_id
                    )
                transfer_payee_id = ynab_client.find_transfer_payee_id(
                    payees, secondary.ynab_tx["account_id"]
                )
                if transfer_payee_id is None:
                    logger.warning(
                        "Matched a likely transfer (%.2f kr, %s -> %s) but found no "
                        "transfer payee for account %s in budget %s - importing both "
                        "legs normally instead of linking them.",
                        abs(primary.ynab_tx["amount"]) / 1000,
                        primary.provider_account_id,
                        secondary.provider_account_id,
                        secondary.ynab_tx["account_id"],
                        budget_id,
                    )
                    continue

                logger.info(
                    "Matched transfer: %.2f kr from %s to %s - creating as a linked "
                    "YNAB transfer instead of two independent transactions.",
                    abs(primary.ynab_tx["amount"]) / 1000,
                    primary.provider_account_id,
                    secondary.provider_account_id,
                )
                primary.ynab_tx.pop("payee_name", None)
                primary.ynab_tx["payee_id"] = transfer_payee_id
                primary.transfer_secondary = secondary
                indices_to_remove.add(secondary_idx)

            if indices_to_remove:
                creates_by_budget[budget_id] = [
                    p for idx, p in enumerate(pending_creates) if idx not in indices_to_remove
                ]

    async def reconcile_payee_mappings(self) -> int:
        """Maintenance pass closing invariant 12's known gap: a cached
        payee_mappings row's ynab_payee_id can go stale if the user later
        deletes or merges that payee in YNAB (or, observed live, simply
        reassigns its one referencing transaction elsewhere, leaving it
        orphaned - YNAB appears to clean those up too, not just
        explicitly-deleted payees).

        HYBRID two-phase design, arrived at after two earlier attempts:

        1. **Bulk-list first pass** (cheap): one ynab_client.get_payees()
           call per budget. A payee_id that comes back in this list is
           trusted as still active - confirmed live, repeatedly
           (scripts/verify_ynab_payee_deletion.py, run three times against
           real deletions/merges/orphaning), that YNAB never lists a gone
           payee here at all, deleted or not - so PRESENCE is a strong,
           direct signal. ABSENCE is not: a transient or incomplete
           response could just as easily explain a momentary absence, so
           an id merely missing from this list is only ever promoted to
           "candidate," never healed on that basis alone.
        2. **Per-id confirmation, candidates only** (targeted, more
           expensive per call but rare in aggregate): each budget's
           cached payee_ids (StateDB.list_payee_ids) that DIDN'T appear in
           step 1's list get a direct ynab_client.get_payee() lookup -
           confirmed live (scripts/verify_ynab_payee_get_by_id.py) to
           reliably 404 for a genuinely gone payee. Only a 404 or an
           explicit `deleted: true` here actually heals the row
           (StateDB.delete_payee_mappings_for_ids) - a candidate that
           turns out to still be active (200, deleted: false) is left
           alone, which is exactly the safety net step 1's ambiguous
           absence needed.

        FIRST ATTEMPT (bulk-only, `deleted: true` in the list) shipped on
        an assumption borrowed from sibling project ../ynab-auto-bank and
        from this project's own GET .../accounts (which DOES keep closed
        accounts listed) - live-verified wrong: a real deleted payee was
        OMITTED from the bulk list, never flagged, making that design
        permanently inert against real deletions. SECOND ATTEMPT (per-id
        only, checking every cached payee_id individually every sweep)
        fixed the safety problem but reintroduced a cost problem: one API
        call per DISTINCT cached payee_id regardless of whether anything
        had actually changed, which could be dozens to hundreds for a
        long-lived budget. This hybrid restores the first attempt's cheap
        common-case cost (one bulk GET per budget when nothing's actually
        gone, which is the overwhelmingly common case) while keeping the
        second attempt's safety property (never heal without a targeted,
        unambiguous per-id confirmation).

        Rate-limited via StateDB.is_payee_reconcile_due() regardless - not
        because the common case is expensive anymore, but as cheap
        insurance against a burst (many payees deleted/merged/orphaned at
        once producing a large one-cycle candidate set). Marks the
        reconcile as done (StateDB.mark_payee_reconcile_done) once the
        sweep completes, regardless of whether anything was actually
        stale - the rate limit governs how often the CHECK runs, not how
        often it finds something to heal.

        Called non-fatally once per cycle from scheduler.py - a bulk-fetch
        failure skips that budget's whole sweep this cycle, and a per-id
        lookup failure for one candidate is isolated to that candidate
        only, mirroring routes/providers.py's "one broken provider can't
        break the rest" stance, applied here at both the per-budget and
        per-payee level.

        Returns the total number of payee_mappings rows healed across all
        budgets, for the caller to log.
        """
        if not self._db.is_payee_reconcile_due(PAYEE_RECONCILE_MIN_INTERVAL_DAYS):
            return 0

        total_healed = 0
        for budget_id in self._config.ynab.budgets.values():
            cached_ids = set(self._db.list_payee_ids(budget_id))
            if not cached_ids:
                continue

            try:
                payees = await ynab_client.get_payees(
                    self._http_client, self._config.ynab.personal_access_token, budget_id
                )
            except Exception:
                logger.exception(
                    "Failed to fetch payees for budget %s during payee-mapping "
                    "reconcile (non-fatal, skipping this budget this cycle)",
                    budget_id,
                )
                continue

            active_ids = {p["id"] for p in payees}
            candidates = cached_ids - active_ids

            stale_ids: set[str] = set()
            for payee_id in candidates:
                try:
                    payee = await ynab_client.get_payee(
                        self._http_client,
                        self._config.ynab.personal_access_token,
                        budget_id,
                        payee_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to look up payee %s in budget %s during payee-mapping "
                        "reconcile (non-fatal, skipping this payee this cycle)",
                        payee_id,
                        budget_id,
                    )
                    continue
                if payee is None or payee.get("deleted"):
                    stale_ids.add(payee_id)

            if not stale_ids:
                continue
            healed = await self._db.delete_payee_mappings_for_ids(budget_id, stale_ids)
            if healed:
                logger.info(
                    "Healed %d stale payee_mappings row(s) for budget %s "
                    "(payee deleted/merged in YNAB since last cached)",
                    healed,
                    budget_id,
                )
            total_healed += healed

        await self._db.mark_payee_reconcile_done()
        return total_healed

    async def _backfill_duplicates(
        self,
        budget_id: str,
        pending_creates: list[_PendingCreate],
        duplicate_import_ids: list[str],
    ) -> int:
        """Resilience path: if a submission comes back as a duplicate but we
        have no local tracking row for it (e.g. tracked_transactions was
        reset while YNAB still had the data from an earlier run), recover
        the existing YNAB transaction id via a lookup so future
        pending->booked reconciliation keeps working even after local
        state loss - matching this project's original 'local state is
        disposable, YNAB is authoritative' philosophy.

        A second case, discovered live: YNAB permanently reserves an
        import_id even after the transaction using it is deleted (e.g. a
        user manually removing a real duplicate in the YNAB app), and its
        plain transaction list silently excludes deleted transactions - so
        the lookup above finds nothing even though YNAB's "duplicate"
        response is correct. Distinguished via a second, heavier lookup
        that includes deleted transactions; if that's what happened, the
        import_id is recorded as permanently resolved (booking_status
        'DELETED') so it's never retried again, rather than erroring on
        every future cycle forever.

        Returns how many duplicates were resolved as deleted-and-final this
        call, for MQTT/log visibility.
        """
        if not duplicate_import_ids:
            return 0
        resolved_deleted = 0
        by_import_id = {p.ynab_tx["import_id"]: p for p in pending_creates}
        for import_id in duplicate_import_ids:
            p = by_import_id.get(import_id)
            if p is None:
                continue
            if self._db.get_tracked(p.tracking_key) is not None:
                await self._db.insert_audit_event(
                    event_type="duplicate",
                    source=p.provider_type,
                    account_key=p.provider_account_id,
                    tracking_key=p.tracking_key,
                    import_id=import_id,
                    ynab_budget_id=budget_id,
                    payee_name=p.ynab_tx.get("payee_name"),
                    memo=p.ynab_tx["memo"],
                    transaction_date=p.ynab_tx["date"],
                    amount_milliunits=p.ynab_tx["amount"],
                    detail="already tracked (retry within same cycle)",
                )
                continue  # already tracked (e.g. a retry within the same cycle)
            existing = await ynab_client.find_transaction_by_import_id(
                self._http_client,
                self._config.ynab.personal_access_token,
                budget_id,
                p.ynab_tx["account_id"],
                import_id,
            )
            if existing is not None:
                await self._db.upsert_tracked(
                    p.tracking_key,
                    import_id=import_id,
                    ynab_transaction_id=existing["id"],
                    ynab_budget_id=budget_id,
                    account_key=p.provider_account_id,
                    booking_status=p.booking_status,
                    amount_milliunits=existing.get("amount", p.ynab_tx["amount"]),
                    payee_name=p.ynab_tx.get("payee_name"),
                    memo=p.ynab_tx["memo"],
                    transaction_date=p.ynab_tx["date"],
                    ynab_account_id=p.ynab_tx["account_id"],
                    cleared=p.ynab_tx["cleared"],
                )
                await self._db.insert_audit_event(
                    event_type="duplicate",
                    source=p.provider_type,
                    account_key=p.provider_account_id,
                    tracking_key=p.tracking_key,
                    import_id=import_id,
                    ynab_transaction_id=existing["id"],
                    ynab_budget_id=budget_id,
                    ynab_account_id=p.ynab_tx["account_id"],
                    payee_name=p.ynab_tx.get("payee_name"),
                    memo=p.ynab_tx["memo"],
                    transaction_date=p.ynab_tx["date"],
                    amount_milliunits=existing.get("amount", p.ynab_tx["amount"]),
                    detail="recovered via find_transaction_by_import_id (local state was reset)",
                )
                continue

            deleted_tx = await ynab_client.find_transaction_including_deleted(
                self._http_client, self._config.ynab.personal_access_token, budget_id, import_id
            )
            if deleted_tx is not None and deleted_tx.get("deleted"):
                logger.info(
                    "Resolved as previously deleted in YNAB, will not retry again: "
                    "payee=%r date=%s amount=%.2f kr",
                    p.ynab_tx.get("payee_name"),
                    p.ynab_tx["date"],
                    p.ynab_tx["amount"] / 1000,
                )
                await self._db.upsert_tracked(
                    p.tracking_key,
                    import_id=import_id,
                    ynab_transaction_id=deleted_tx["id"],
                    ynab_budget_id=budget_id,
                    account_key=p.provider_account_id,
                    booking_status="DELETED",
                    amount_milliunits=p.ynab_tx["amount"],
                    payee_name=p.ynab_tx.get("payee_name"),
                    memo=p.ynab_tx["memo"],
                    transaction_date=p.ynab_tx["date"],
                    ynab_account_id=p.ynab_tx["account_id"],
                    cleared=p.ynab_tx["cleared"],
                )
                await self._db.insert_audit_event(
                    event_type="duplicate",
                    source=p.provider_type,
                    account_key=p.provider_account_id,
                    tracking_key=p.tracking_key,
                    import_id=import_id,
                    ynab_transaction_id=deleted_tx["id"],
                    ynab_budget_id=budget_id,
                    ynab_account_id=p.ynab_tx["account_id"],
                    payee_name=p.ynab_tx.get("payee_name"),
                    memo=p.ynab_tx["memo"],
                    transaction_date=p.ynab_tx["date"],
                    amount_milliunits=p.ynab_tx["amount"],
                    detail="resolved as previously deleted in YNAB",
                )
                resolved_deleted += 1
                continue

            logger.error(
                "YNAB reported import_id %s as a duplicate but it can't be found via lookup "
                "(active or deleted)",
                import_id,
            )
        return resolved_deleted

    async def readd_deleted_transaction(self, tracking_key: str) -> str:
        """Manually re-create a transaction previously resolved as
        deleted-in-YNAB (see _backfill_duplicates), using the payee/memo/
        date/amount/cleared data persisted at the time it was last
        tracked. YNAB permanently blocks reuse of the original import_id
        once its transaction is deleted, so a fresh, distinguishable
        import_id is derived for this attempt.

        Source-agnostic: reachable from both scripts/readd_deleted_transaction.py
        and the GUI's DeletedTransactions tab, for a row originating from
        any provider or from a file import (see CLAUDE.md's "Known open
        risk" section for context on why transactions end up in this
        state).

        Returns the new YNAB transaction id.
        """
        tracked = self._db.get_tracked(tracking_key)
        if tracked is None:
            raise ValueError(f"no tracked transaction for {tracking_key!r}")
        if tracked["booking_status"] != "DELETED":
            raise ValueError(
                f"{tracking_key!r} is not marked deleted "
                f"(booking_status={tracked['booking_status']!r}) - nothing to re-add"
            )

        # Re-derive inside the SAME dedup domain the original belonged to,
        # read off its stored import_id, rather than assuming any one
        # source. Hardcoding "SB1:" here would have quietly moved a
        # re-added file-imported transaction into the live-poll domain,
        # which invariant 7 exists to prevent.
        new_import_id = derive_import_id(
            prefix_of(tracked["import_id"]),
            f"{tracking_key}:readd:{tracked['readd_count'] + 1}",
        )
        new_tx = {
            "account_id": tracked["ynab_account_id"],
            "date": tracked["transaction_date"],
            "amount": tracked["amount_milliunits"],
            "payee_name": tracked["payee_name"],
            "memo": tracked["memo"],
            "cleared": tracked["cleared"],
            "approved": False,
            "import_id": new_import_id,
        }
        response = await ynab_client.create_transactions(
            self._http_client,
            self._config.ynab.personal_access_token,
            tracked["ynab_budget_id"],
            [new_tx],
        )
        transaction_ids = response.get("transaction_ids", [])
        if not transaction_ids:
            raise RuntimeError(f"YNAB did not report the re-add as created: {response!r}")

        new_ynab_transaction_id = transaction_ids[0]
        await self._db.mark_readded(
            tracking_key,
            new_ynab_transaction_id=new_ynab_transaction_id,
            new_import_id=new_import_id,
        )
        return new_ynab_transaction_id

    async def import_file_rows(
        self,
        *,
        ynab_account_id: str,
        ynab_budget_id: str,
        account_key: str,
        rows: list[ImportedTransactionRow],
        dry_run: bool,
    ) -> FileImportResult:
        """Entry point for the file-import GUI feature (an xlsx/CSV/etc.
        bank export, as opposed to a live provider poll).

        Each parsed row is normalized into exactly the same
        NormalizedTransaction shape a provider emits, then run through the
        SAME _classify() the live-poll path uses, and - when not a dry run -
        the same _record_created/_backfill_duplicates submit path. Sharing
        one classify step is what makes it structurally impossible for a
        file import to double-book something a live poll already imported,
        rather than relying on two code paths staying in agreement.

        The only thing that differs per source is where the keys come from:
        here they're sync.file_import.dedup's CONTENT hash, because a bank
        export has no bank-assigned row id to key off at all (invariant 7).

        File-imported transactions are always treated as already-settled
        (BOOKED/cleared) - a bank export only ever contains historical,
        booked transactions, never pending ones.
        """
        row_results: list[FileImportRowResult] = []
        pending_creates: list[_PendingCreate] = []
        duplicate_ntxs: list[NormalizedTransaction] = []

        for row in rows:
            dedup_key = file_dedup.compute_dedup_key(
                row.date, row.amount_milliunits, row.payee_name, row.memo
            )
            tracking_key = file_dedup.get_tracking_key(ynab_account_id, dedup_key)

            ntx = NormalizedTransaction(
                tracking_key=tracking_key,
                import_id=file_dedup.derive_import_id(tracking_key),
                provider_account_id=account_key,
                date=row.date,
                amount_milliunits=row.amount_milliunits,
                payee_name=row.payee_name,
                memo=row.memo,
                booking_status=BookingStatus.BOOKED,
            )
            # manual_candidates deliberately omitted (defaults to None) - a
            # bulk historical file import is not the scenario manual-
            # transaction matching was built for (see CLAUDE.md), and
            # wiring in the same per-account candidate cache here would add
            # real complexity to a one-shot GUI flow for a case that hasn't
            # come up. File rows always fall through to a normal "new"/
            # duplicate outcome, unaffected by manual_match_window_days.
            outcome, pending = self._classify(
                ntx,
                provider_type=FILE_SOURCE_TYPE,
                ynab_account_id=ynab_account_id,
                ynab_budget_id=ynab_budget_id,
            )
            # A file row can only ever be "new" or already-tracked: it is
            # always BOOKED, so _classify's pending->booked transition can't
            # apply. Anything already tracked is reported as a duplicate and
            # never resubmitted.
            if outcome == "new":
                pending_creates.append(pending)
            else:
                duplicate_ntxs.append(ntx)
            row_results.append(
                FileImportRowResult(
                    row_index=row.row_index,
                    date=row.date.isoformat(),
                    amount_milliunits=row.amount_milliunits,
                    payee_name=row.payee_name,
                    memo=row.memo,
                    status="new" if outcome == "new" else "duplicate",
                )
            )

        # Only a real commit (not a dry-run preview) represents something
        # that actually "happened" - _classify's "unchanged" outcome on the
        # live-poll path is deliberately never audit-logged (it would flood
        # every re-poll of an already-synced transaction), but a file-import
        # duplicate is a one-off event tied to a specific upload, not a
        # routine per-cycle occurrence, so it IS worth recording here.
        if not dry_run:
            for dup_ntx in duplicate_ntxs:
                await self._db.insert_audit_event(
                    event_type="duplicate",
                    source=FILE_SOURCE_TYPE,
                    account_key=account_key,
                    tracking_key=dup_ntx.tracking_key,
                    import_id=dup_ntx.import_id,
                    ynab_budget_id=ynab_budget_id,
                    ynab_account_id=ynab_account_id,
                    payee_name=dup_ntx.payee_name,
                    memo=dup_ntx.memo,
                    transaction_date=dup_ntx.date.isoformat(),
                    amount_milliunits=dup_ntx.amount_milliunits,
                    detail="already tracked (re-imported file row)",
                )

        if dry_run or not pending_creates:
            return FileImportResult(
                rows=row_results, created=0, resolved_deleted=0, committed=False
            )

        response = await ynab_client.create_transactions(
            self._http_client,
            self._config.ynab.personal_access_token,
            ynab_budget_id,
            [p.ynab_tx for p in pending_creates],
        )
        created = await self._record_created(ynab_budget_id, pending_creates, response)
        resolved_deleted = await self._backfill_duplicates(
            ynab_budget_id, pending_creates, response.get("duplicate_import_ids", [])
        )
        return FileImportResult(
            rows=row_results, created=created, resolved_deleted=resolved_deleted, committed=True
        )
