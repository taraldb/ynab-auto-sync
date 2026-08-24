from __future__ import annotations

from dataclasses import dataclass
from datetime import date


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


def find_transfer_pairs(
    candidates: list[TransferCandidate], match_window_days: int
) -> list[tuple[int, int]]:
    """Match pairs of candidates that look like two legs of the same
    transfer: opposite sign, equal absolute amount, different accounts, and
    dates within match_window_days of each other.

    Ambiguous candidates - more than one transaction that could plausibly
    be the other leg - are skipped entirely on BOTH sides, never guessed,
    matching this project's existing "don't guess, fall back to the safe
    default" stance elsewhere (see engine.py's _backfill_duplicates). A
    coincidental same-amount, opposite-sign, same-window transaction that
    isn't really a transfer is an accepted, low-probability false-positive
    risk when it uniquely matches - the alternative (never linking anything)
    was explicitly not what was asked for.

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
            and abs((other.date - c.date).days) <= match_window_days
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
