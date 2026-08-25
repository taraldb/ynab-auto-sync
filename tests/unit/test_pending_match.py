"""Tests for the PENDING-transaction fuzzy correlation matcher
(sync/pending_match.py) at the pure-function level. See that module's
docstring and CLAUDE.md's "PENDING-transaction import" section for how
engine.py wires this in - end-to-end wiring behavior (provider toggle,
the primary-key rekey, retry-safety) is covered separately in
test_sparebank1_provider.py/test_state_db.py/test_engine.py.

Fixture merchant/location strings below are anonymized placeholders
("MERCHANT ONE", "LOC-A", ...) - never the real Norwegian merchant/location
text pulled from the server while investigating this - while preserving the
exact structural facts that motivate each test (whole-kroner-vs-decimal
amounts, the id-mismatch pattern, the payee-drift shape, the same-day-same-
merchant ambiguity, and the branch-disambiguation case).
"""

from datetime import date

from ynab_auto_sync.providers.sparebank1 import transform
from ynab_auto_sync.sync.pending_match import (
    BookedCandidate,
    PendingCandidate,
    find_manual_match_tolerant,
    find_pending_match,
)


def p(
    index,
    tracking_key,
    account_key,
    day,
    amount,
    payee,
    ynab_transaction_id="ynab-1",
    ynab_budget_id="budget-1",
):
    return PendingCandidate(
        index=index,
        tracking_key=tracking_key,
        ynab_transaction_id=ynab_transaction_id,
        ynab_budget_id=ynab_budget_id,
        account_key=account_key,
        amount_milliunits=amount,
        date=date(2026, 8, day),
        payee=payee,
    )


def b(account_key, day, amount, payee):
    return BookedCandidate(account_key=account_key, date=date(2026, 8, day), amount_milliunits=amount, payee=payee)


# --- Regression tests locking in the real-data findings (via unmodified transform.py) ---


def test_pending_and_booked_credit_card_have_different_tracking_keys():
    pending_tx = {
        "nonUniqueId": "111111111",
        "bookingStatus": "PENDING",
        "amount": -79,
    }
    booked_tx = {
        "nonUniqueId": "2926243-222222222",
        "creditCardIdentifiers": {"partitionKey": "2926243", "nonUniqueId": "222222222"},
        "bookingStatus": "BOOKED",
        "amount": -79.0,
    }
    pending_key = transform.get_tracking_key(pending_tx, "acct-cc")
    booked_key = transform.get_tracking_key(booked_tx, "acct-cc")
    assert pending_key != booked_key


def test_pending_transfer_nonuniqueid_sentinel_collides_across_different_transfers():
    transfer_out = {"nonUniqueId": "000000000000000000", "typeCode": "R_914", "amount": -700.0}
    transfer_in = {"nonUniqueId": "000000000000000000", "typeCode": "R_914", "amount": 3000.0}
    assert transform.get_tracking_key(transfer_out, "acct-checking") == transform.get_tracking_key(
        transfer_in, "acct-checking"
    )


# --- find_pending_match (scenario 1) ---


def test_matches_despite_tracking_key_irrelevance():
    candidates = [p(0, "acct-cc:111", "acct-cc", 20, -79000, "MERCHANT ONE, LOC-A")]
    booked = b("acct-cc", 20, -79000, "MERCHANT ONE, LOC-A")
    match = find_pending_match(booked, candidates)
    assert match is not None
    assert match.candidate.index == 0
    assert match.amount_diff_milliunits == 0


def test_tolerates_rounding_drift_within_tolerance():
    candidates = [p(0, "acct-cc:1", "acct-cc", 20, -521000, "MERCHANT ONE, LOC-A")]
    booked = b("acct-cc", 20, -521100, "MERCHANT ONE, LOC-A")
    match = find_pending_match(booked, candidates)
    assert match is not None
    assert match.amount_diff_milliunits == -100


def test_tolerates_rounding_drift_the_other_direction():
    candidates = [p(0, "acct-cc:1", "acct-cc", 20, -218000, "MERCHANT ONE, LOC-A")]
    booked = b("acct-cc", 20, -218300, "MERCHANT ONE, LOC-A")
    match = find_pending_match(booked, candidates)
    assert match is not None
    assert match.amount_diff_milliunits == -300


def test_rejects_amount_outside_tolerance():
    candidates = [p(0, "acct-cc:1", "acct-cc", 20, -218000, "MERCHANT ONE, LOC-A")]
    booked = b("acct-cc", 20, -530000, "MERCHANT ONE, LOC-A")
    assert find_pending_match(booked, candidates) is None


def test_requires_same_account():
    candidates = [p(0, "acct-cc:1", "acct-cc-a", 20, -79000, "MERCHANT ONE, LOC-A")]
    booked = b("acct-cc-b", 20, -79000, "MERCHANT ONE, LOC-A")
    assert find_pending_match(booked, candidates) is None


def test_respects_date_window():
    candidates = [p(0, "acct-cc:1", "acct-cc", 1, -79000, "MERCHANT ONE, LOC-A")]
    booked = b("acct-cc", 10, -79000, "MERCHANT ONE, LOC-A")  # 9 days later, default window is 5
    assert find_pending_match(booked, candidates) is None


def test_payee_drift_location_changed_still_matches():
    # Anonymized version of the real "...LG RD" -> "...AALGAARD, NOR" case:
    # the location token changes entirely, only the merchant prefix survives.
    candidates = [p(0, "acct-cc:1", "acct-cc", 20, -115000, "MERCHANT TWO, LOC-A")]
    booked = b("acct-cc", 20, -115000, "MERCHANT TWO, LOC-B, XYZ")
    match = find_pending_match(booked, candidates)
    assert match is not None
    assert match.payee_matched is True


def test_payee_mismatch_prevents_false_positive_even_with_amount_and_date_match():
    candidates = [p(0, "acct-cc:1", "acct-cc", 20, -79000, "MERCHANT ONE, LOC-A")]
    booked = b("acct-cc", 20, -79000, "TOTALLY UNRELATED SHOP, LOC-Z")
    assert find_pending_match(booked, candidates) is None


def test_same_day_same_merchant_different_amounts_disambiguated_by_amount():
    # Anonymized REMA-1000 counterexample: two real purchases, same day,
    # same merchant chain, different amounts - payee text alone can't tell
    # them apart, amount must.
    candidates = [
        p(0, "acct-cc:1", "acct-cc", 20, -218000, "GROCERY STORE A, LOC-A, XYZ"),
        p(1, "acct-cc:2", "acct-cc", 20, -459000, "GROCERY STORE A, LOC-A, XYZ"),
    ]
    booked = b("acct-cc", 20, -218300, "GROCERY STORE A, LOC-A, XYZ")
    match = find_pending_match(booked, candidates)
    assert match is not None
    assert match.candidate.index == 0


def test_ambiguous_amount_and_date_and_payee_falls_through_to_none():
    candidates = [
        p(0, "acct-cc:1", "acct-cc", 20, -79000, "TOTALLY UNRELATED SHOP ONE"),
        p(1, "acct-cc:2", "acct-cc", 20, -79000, "TOTALLY UNRELATED SHOP TWO"),
    ]
    booked = b("acct-cc", 20, -79000, "MERCHANT ONE, LOC-A")
    assert find_pending_match(booked, candidates) is None


def test_require_payee_match_false_allows_amount_and_date_only_match():
    candidates = [p(0, "acct-cc:1", "acct-cc", 20, -79000, "TOTALLY UNRELATED SHOP")]
    booked = b("acct-cc", 20, -79000, "MERCHANT ONE, LOC-A")
    match = find_pending_match(booked, candidates, require_payee_match=False)
    assert match is not None
    assert match.candidate.index == 0


def test_disambiguates_multiple_plausible_candidates_via_levenshtein_distance():
    # Two branches of the same chain both pass the coarse prefix check;
    # only Levenshtein distance to the booked payee can tell them apart.
    candidates = [
        p(0, "acct-cc:1", "acct-cc", 20, -100000, "MERCHANT ONE, DOWNTOWN BRANCH"),
        p(1, "acct-cc:2", "acct-cc", 20, -100000, "MERCHANT ONE, AIRPORT BRANCH"),
    ]
    booked = b("acct-cc", 20, -100000, "MERCHANT ONE, DOWNTOWN BRANCH, XYZ")
    match = find_pending_match(booked, candidates)
    assert match is not None
    assert match.candidate.index == 0
    assert match.payee_similarity == 1.0


def test_levenshtein_too_close_to_call_falls_through_to_none():
    candidates = [
        p(0, "acct-cc:1", "acct-cc", 20, -100000, "MERCHANT ONE, LOC-B"),
        p(1, "acct-cc:2", "acct-cc", 20, -100000, "MERCHANT ONE, LOC-C"),
    ]
    booked = b("acct-cc", 20, -100000, "MERCHANT ONE, LOC-A")
    assert find_pending_match(booked, candidates) is None


def test_levenshtein_below_similarity_floor_falls_through_to_none():
    candidates = [
        p(0, "acct-cc:1", "acct-cc", 20, -100000, "MERCHXX BBBBBBBBBBBBBBBB"),
        p(1, "acct-cc:2", "acct-cc", 20, -100000, "MERCHXX CCCCCCCCCCCCCCCC"),
    ]
    booked = b("acct-cc", 20, -100000, "MERCHXX AAAAAAAAAAAAAAAA")
    assert find_pending_match(booked, candidates) is None


# --- find_manual_match_tolerant (scenario 2) ---


def _manual_candidate(amount, day, payee_name="MERCHANT ONE, LOC-A", subtransactions=None, tx_id="manual-1"):
    return {
        "id": tx_id,
        "amount": amount,
        "date": date(2026, 8, day).isoformat(),
        "payee_name": payee_name,
        "subtransactions": subtransactions or [],
    }


def test_matches_within_amount_tolerance():
    candidates = [_manual_candidate(-521100, 20)]
    match = find_manual_match_tolerant(-521000, date(2026, 8, 20), candidates, date_window_days=3)
    assert match is not None
    assert match["id"] == "manual-1"


def test_ambiguous_candidates_return_none():
    candidates = [
        _manual_candidate(-521100, 20, tx_id="manual-1"),
        _manual_candidate(-521200, 20, tx_id="manual-2"),
    ]
    assert find_manual_match_tolerant(-521000, date(2026, 8, 20), candidates, date_window_days=3) is None


def test_manual_match_rejects_amount_outside_tolerance():
    candidates = [_manual_candidate(-999000, 20)]
    assert find_manual_match_tolerant(-521000, date(2026, 8, 20), candidates, date_window_days=3) is None


def test_excludes_split_transactions():
    candidates = [_manual_candidate(-521100, 20, subtransactions=[{"amount": -521100}])]
    assert find_manual_match_tolerant(-521000, date(2026, 8, 20), candidates, date_window_days=3) is None


def test_disambiguates_multiple_amount_matches_via_levenshtein_when_pending_payee_given():
    candidates = [
        _manual_candidate(-100000, 20, payee_name="MERCHANT ONE, DOWNTOWN BRANCH", tx_id="manual-a"),
        _manual_candidate(-100000, 20, payee_name="MERCHANT ONE, AIRPORT BRANCH", tx_id="manual-b"),
    ]
    match = find_manual_match_tolerant(
        -100000,
        date(2026, 8, 20),
        candidates,
        date_window_days=3,
        pending_payee="MERCHANT ONE, DOWNTOWN BRANCH, XYZ",
    )
    assert match is not None
    assert match["id"] == "manual-a"


def test_ambiguous_manual_candidates_without_pending_payee_falls_through_to_none():
    candidates = [
        _manual_candidate(-100000, 20, tx_id="manual-a"),
        _manual_candidate(-100000, 20, tx_id="manual-b"),
    ]
    assert (
        find_manual_match_tolerant(-100000, date(2026, 8, 20), candidates, date_window_days=3, pending_payee=None)
        is None
    )
