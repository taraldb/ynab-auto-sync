"""Correlates a self-created "pending" transaction with its later-booked
real-world self, and (separately) with an existing hand-typed YNAB manual
transaction, without relying on exact tracking-key equality.

Wired into engine.py::_classify() - a BOOKED ntx with nothing tracked under
its own key is tried against find_pending_match() before the manual-match
check, and a PENDING ntx's manual-match branch uses
find_manual_match_tolerant() instead of the exact matcher. See CLAUDE.md's
"PENDING-transaction import (opt-in, credit-card only)" for the full
write-up, including why exact tracking-key matching can't be reused here.

Why exact tracking-key matching can't be reused here (confirmed against
real SpareBank1 fetch data, not assumed):

- A PENDING credit-card row's raw `nonUniqueId` bears no relation to the
  same real purchase's `creditCardIdentifiers.nonUniqueId` once BOOKED -
  confirmed on 4 separate real transactions, completely different numbers,
  not a formatting quirk (distinct from the already-documented
  RECENT/HISTORIC prefix-drift issue in transform.py::get_tracking_key).
- PENDING bank-transfer rows (typeCode "R_914") have `nonUniqueId`
  hardcoded to a dummy sentinel ("000000000000000000"), identical across
  every pending transfer observed - unusable as any kind of key.
- The `date` field floats while PENDING (reflects "today"/processing date,
  moves forward on every poll) and only fixes to the real purchase date
  once BOOKED, which can differ by several days.
- PENDING amounts are always whole kroner (no fractional-kroner component)
  while the BOOKED amount has decimals - sometimes exact, sometimes off by
  up to ~1.1 kr in observed real data (a preauth hold, not a prediction of
  the final settled amount).
- Payee text drifts slightly between PENDING and BOOKED observations of the
  same purchase (casing, a trailing country/location suffix) - and cannot
  be trusted alone: two different same-day purchases at the same merchant
  chain can produce byte-identical cleaned descriptions with different
  amounts.

Correlation here is therefore multi-signal (account + tolerant amount +
date window + payee similarity), mirroring sync/transfers.py's transfer-pair
matching and engine.py's manual-transaction matching, both of which already
established this project's "combine several signals, never guess on
ambiguity" stance. When more than one candidate survives the account/
amount/date filters, this module ranks survivors by Levenshtein-distance-
based payee similarity and accepts a winner only if it clears both a
similarity floor and a minimum margin over the runner-up - otherwise it
still refuses to guess, same as sync/transfers.py::find_transfer_pairs and
engine.py::_find_manual_match.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ynab_auto_sync.sync.date_window import MatchWindowUnit, days_between
from ynab_auto_sync.sync.money import from_milliunits
from ynab_auto_sync.sync.sanitize import (
    normalize_payee_for_fuzzy_match,
    payee_similarity,
    payees_plausibly_match,
)

DEFAULT_AMOUNT_TOLERANCE_KRONER = Decimal("2.0")
DEFAULT_DATE_WINDOW_DAYS = 5
DEFAULT_MIN_PAYEE_PREFIX_LEN = 6
# Levenshtein-based similarity floor a top-ranked candidate must clear
# before it's even considered as a possible winner.
DEFAULT_MIN_SIMILARITY_TO_DISAMBIGUATE = 0.6
# Required gap over the runner-up's score to accept the top-ranked
# candidate rather than treat the tie as genuinely ambiguous.
DEFAULT_MIN_SIMILARITY_MARGIN = 0.15


@dataclass(frozen=True)
class PendingCandidate:
    """A minimal, engine-agnostic view of one locally-tracked PENDING
    transaction - just enough to decide whether a freshly-fetched BOOKED
    transaction is the same real-world purchase now settled. `index` refers
    back to the caller's own list, mirroring sync/transfers.py's
    TransferCandidate, so this module never needs to know about engine.py's
    internal types.

    `date` is deliberately the FIRST-SEEN pending date, never re-derived on
    a later still-pending poll - SpareBank1's PENDING `date` field floats
    forward on every poll (confirmed live), so using anything but the first
    observation would let the date window drift away from the true
    purchase date over time. This mirrors the existing
    tracked_transactions.first_seen_at / StateDB.get_earliest_pending_first_seen
    precedent already in this codebase for exactly this reason.

    `amount` is always a whole-kroner value while PENDING - SpareBank1's
    PENDING amount has no fractional-kroner component, confirmed live.
    `payee` is expected to already have been run through
    sanitize.clean_bank_text, matching every other payee value in this
    codebase.

    `ynab_budget_id` is the tracked row's own budget, which is not
    necessarily the mapping's CURRENT budget (a mapping can be re-pointed
    after a transaction was already imported) - same reasoning
    engine.py's _PendingUpdate.ynab_budget_id documents.
    """

    index: int
    tracking_key: str
    ynab_transaction_id: str
    ynab_budget_id: str
    account_key: str
    amount: Decimal
    date: date
    payee: str


@dataclass(frozen=True)
class BookedCandidate:
    """The freshly-fetched BOOKED transaction being classified, reduced to
    plain values so this module stays independent of providers/base.py's
    NormalizedTransaction (same reasoning as TransferCandidate)."""

    account_key: str
    amount: Decimal
    date: date
    payee: str


@dataclass(frozen=True)
class PendingMatch:
    """Result of a successful correlation - carries the matched candidate
    plus the signals that justified it, for logging/audit purposes if this
    is ever wired into a live path."""

    candidate: PendingCandidate
    amount_diff: Decimal
    date_diff_days: int
    payee_matched: bool
    # Levenshtein-based score that decided it; 1.0 when there was only one
    # survivor after the account/amount/date/payees_plausibly_match filters
    # (no ranking was needed to pick it).
    payee_similarity: float


def _amount_within_tolerance(a: Decimal, b: Decimal, tolerance_kroner: Decimal) -> bool:
    return abs(a - b) <= tolerance_kroner


def find_pending_match(
    booked: BookedCandidate,
    candidates: list[PendingCandidate],
    *,
    amount_tolerance_kroner: Decimal = DEFAULT_AMOUNT_TOLERANCE_KRONER,
    date_window_days: int = DEFAULT_DATE_WINDOW_DAYS,
    unit: MatchWindowUnit = "calendar_days",
    require_payee_match: bool = True,
    min_payee_prefix_len: int = DEFAULT_MIN_PAYEE_PREFIX_LEN,
    min_similarity_to_disambiguate: float = DEFAULT_MIN_SIMILARITY_TO_DISAMBIGUATE,
    min_similarity_margin: float = DEFAULT_MIN_SIMILARITY_MARGIN,
) -> PendingMatch | None:
    """Find the PendingCandidate that `booked` is most plausibly the settled
    version of.

    Hard filters (never relaxed): same account_key, amount within
    amount_tolerance_kroner, date within date_window_days (via
    sync.date_window.days_between). If require_payee_match, candidates
    additionally must pass sanitize.payees_plausibly_match against
    `booked.payee`.

    Disambiguation: zero survivors -> None. Exactly one -> returned
    directly (payee_similarity=1.0, no ranking needed). More than one ->
    survivors are ranked by sanitize.payee_similarity (Levenshtein-distance
    based) between their normalized payees and `booked`'s. The top-ranked
    candidate is accepted only if its score clears
    min_similarity_to_disambiguate AND beats the runner-up by at least
    min_similarity_margin - otherwise this returns None, same "don't guess"
    stance as sync/transfers.py::find_transfer_pairs and
    engine.py::_find_manual_match, just applied after trying to break the
    tie with a finer-grained signal instead of giving up immediately on any
    multi-candidate case.
    """
    pool = [
        c
        for c in candidates
        if c.account_key == booked.account_key
        and _amount_within_tolerance(c.amount, booked.amount, amount_tolerance_kroner)
        and days_between(c.date, booked.date, unit) <= date_window_days
    ]
    if require_payee_match:
        pool = [
            c
            for c in pool
            if payees_plausibly_match(c.payee, booked.payee, min_prefix_len=min_payee_prefix_len)
        ]

    if not pool:
        return None

    def _build_match(candidate: PendingCandidate, similarity: float) -> PendingMatch:
        return PendingMatch(
            candidate=candidate,
            amount_diff=booked.amount - candidate.amount,
            date_diff_days=days_between(candidate.date, booked.date, unit),
            payee_matched=payees_plausibly_match(candidate.payee, booked.payee, min_prefix_len=min_payee_prefix_len),
            payee_similarity=similarity,
        )

    if len(pool) == 1:
        return _build_match(pool[0], 1.0)

    norm_booked = normalize_payee_for_fuzzy_match(booked.payee)
    ranked = sorted(
        (
            (payee_similarity(normalize_payee_for_fuzzy_match(c.payee), norm_booked), c)
            for c in pool
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best_candidate = ranked[0]
    runner_up_score = ranked[1][0]
    if best_score >= min_similarity_to_disambiguate and (best_score - runner_up_score) >= min_similarity_margin:
        return _build_match(best_candidate, best_score)
    return None


def find_manual_match_tolerant(
    pending_amount: Decimal,
    pending_date: date,
    candidates: list[dict[str, Any]],
    *,
    amount_tolerance_kroner: Decimal = DEFAULT_AMOUNT_TOLERANCE_KRONER,
    date_window_days: int,
    unit: MatchWindowUnit = "calendar_days",
    pending_payee: str | None = None,
    min_similarity_to_disambiguate: float = DEFAULT_MIN_SIMILARITY_TO_DISAMBIGUATE,
    min_similarity_margin: float = DEFAULT_MIN_SIMILARITY_MARGIN,
) -> dict[str, Any] | None:
    """Tolerant sibling of engine.py::_find_manual_match() - that function
    is READ here for its dict-shape contract, never modified: candidates
    come from ynab_client.list_unimported_transactions(), each a plain dict
    with at least `amount` (milliunits int), `date` (ISO string),
    `subtransactions`, and (for this function) `payee_name`.

    Differs from _find_manual_match in exactly one respect beyond the new
    payee tie-breaker below: amount is compared with amount_tolerance_kroner
    instead of requiring bit-for-bit equality, since a PENDING credit-card
    amount is always a whole-kroner preauth value that will almost never
    exactly equal a hand-typed final decimal amount.

    Same subtransactions exclusion and "don't guess" ambiguity handling as
    _find_manual_match. When more than one candidate survives amount+date
    filtering, this applies the same Levenshtein-based disambiguation as
    find_pending_match against each candidate's `payee_name`, but only if
    `pending_payee` was supplied - if it's None, ambiguity still falls
    straight to None, preserving _find_manual_match's existing no-guessing
    behavior when no payee signal is available at all.
    """
    pool = [
        c
        for c in candidates
        if not c.get("subtransactions")
        and _amount_within_tolerance(from_milliunits(c["amount"]), pending_amount, amount_tolerance_kroner)
        and days_between(date.fromisoformat(c["date"]), pending_date, unit) <= date_window_days
    ]

    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    if pending_payee is None:
        return None

    norm_pending = normalize_payee_for_fuzzy_match(pending_payee)
    ranked = sorted(
        (
            (payee_similarity(normalize_payee_for_fuzzy_match(c.get("payee_name") or ""), norm_pending), c)
            for c in pool
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best_candidate = ranked[0]
    runner_up_score = ranked[1][0]
    if best_score >= min_similarity_to_disambiguate and (best_score - runner_up_score) >= min_similarity_margin:
        return best_candidate
    return None
