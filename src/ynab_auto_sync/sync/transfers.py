from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ynab_auto_sync.sync.date_window import MatchWindowUnit, days_between


@dataclass(frozen=True)
class TransferCandidate:
    """A minimal, engine-agnostic view of one newly-classified transaction,
    just enough to decide whether it looks like one leg of a transfer to
    another of the user's own accounts. `index` refers back to the caller's
    own list (e.g. a position in _PendingCreate list) so the caller can map
    a matched pair back to its real data without this module needing to
    know anything about engine.py's internal types.
    """

    index: int
    account_key: str
    date: date
    amount_milliunits: int
    # Real account-number cross-reference (see providers/base.py's
    # NormalizedTransaction docstring) - None when the source doesn't
    # populate it, in which case this candidate can never satisfy the
    # cross-reference check below and so never matches anything.
    account_number: str | None = None
    remote_account_number: str | None = None


def find_transfer_pairs(
    candidates: list[TransferCandidate],
    match_window_days: int,
    unit: MatchWindowUnit = "calendar_days",
) -> list[tuple[int, int]]:
    """Match pairs of candidates that look like two legs of the same
    transfer: opposite sign, equal absolute amount, different accounts,
    dates within match_window_days of each other (counted in `unit` - see
    sync/date_window.py; default "calendar_days" so every existing call
    site is unaffected unless it opts into "working_days"), AND at least
    one leg's remote_account_number names the other leg's real
    account_number.

    That last condition was added after a real false positive: a 158.48 kr
    salary deposit got linked to an unrelated 158.48 kr grocery purchase 4
    days later on a different account - same amount, opposite sign, no
    other similarity. Neither transaction's remote_account_number named the
    other's account_number, so requiring the cross-reference excludes it.
    Confirmed against two genuine transfers too: a same-day account-to-
    account transfer where BOTH legs' remote_account_number correctly named
    the other's account_number, and a credit-card bill payment where only
    ONE direction resolved (the checking-account leg named the card
    issuer's own internal clearing account rather than the card itself,
    but the card's own transaction feed correctly named the checking
    account back) - which is why this checks EITHER direction, not both.

    Ambiguous candidates - more than one transaction that could plausibly
    be the other leg - are skipped entirely on BOTH sides, never guessed,
    matching this project's existing "don't guess, fall back to the safe
    default" stance elsewhere (see engine.py's _backfill_duplicates). This
    narrows, but does not eliminate, the false-positive risk: two unrelated
    genuine transfers that coincidentally net to the same amount in the
    same window, both correctly cross-referencing their own real
    counterparties, remain a (much smaller) accepted risk.

    Only call this within a single YNAB budget's own candidate set - two
    accounts in different budgets can never be linked by YNAB's transfer
    mechanism at all, so mixing budgets in would only produce nonsensical,
    unusable matches.

    Returns (index, index) pairs referencing positions in `candidates`. Does
    not mutate `candidates`; the caller decides what "matched" means for
    its own data (e.g. removing both from a normal-create list).
    """
    pairs: list[tuple[int, int]] = []
    used: set[int] = set()

    def partners_of(c: TransferCandidate, exclude: set[int]) -> list[int]:
        return [
            other.index
            for other in candidates
            if other.index not in exclude
            and other.index != c.index
            and other.account_key != c.account_key
            and other.amount_milliunits == -c.amount_milliunits
            and days_between(c.date, other.date, unit) <= match_window_days
            and (
                (c.remote_account_number is not None
                 and c.remote_account_number == other.account_number)
                or (other.remote_account_number is not None
                    and other.remote_account_number == c.account_number)
            )
        ]

    for candidate in candidates:
        if candidate.index in used:
            continue
        matches = partners_of(candidate, used | {candidate.index})
        if len(matches) != 1:
            continue
        partner = next(c for c in candidates if c.index == matches[0])
        # Symmetric check: the partner must also see exactly this candidate
        # as its own unique match, not some third transaction - otherwise a
        # three-way coincidence could pick an arbitrary pairing.
        reverse_matches = partners_of(partner, used | {partner.index})
        if reverse_matches == [candidate.index]:
            pairs.append((candidate.index, partner.index))
            used.add(candidate.index)
            used.add(partner.index)

    return pairs
