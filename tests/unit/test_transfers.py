from datetime import date

from ynab_auto_sync.sync.transfers import TransferCandidate, find_transfer_pairs


def c(index, account_key, day, amount):
    return TransferCandidate(
        index=index, account_key=account_key, date=date(2026, 8, day), amount_milliunits=amount
    )


def test_matches_opposite_sign_same_day_different_accounts():
    candidates = [c(0, "acct-a", 20, -50000), c(1, "acct-b", 20, 50000)]
    assert find_transfer_pairs(candidates, match_window_days=4) == [(0, 1)]


def test_does_not_match_same_account():
    candidates = [c(0, "acct-a", 20, -50000), c(1, "acct-a", 20, 50000)]
    assert find_transfer_pairs(candidates, match_window_days=4) == []


def test_does_not_match_same_sign():
    candidates = [c(0, "acct-a", 20, -50000), c(1, "acct-b", 20, -50000)]
    assert find_transfer_pairs(candidates, match_window_days=4) == []


def test_does_not_match_different_amount():
    candidates = [c(0, "acct-a", 20, -50000), c(1, "acct-b", 20, 40000)]
    assert find_transfer_pairs(candidates, match_window_days=4) == []


def test_matches_within_date_window():
    # credit-card next-working-day visibility: source posts day 20, card
    # payment only visible day 23 - still within a 4-day window.
    candidates = [c(0, "acct-a", 20, -50000), c(1, "acct-b", 23, 50000)]
    assert find_transfer_pairs(candidates, match_window_days=4) == [(0, 1)]


def test_does_not_match_outside_date_window():
    candidates = [c(0, "acct-a", 20, -50000), c(1, "acct-b", 26, 50000)]
    assert find_transfer_pairs(candidates, match_window_days=4) == []


def test_ambiguous_match_skipped_on_both_sides():
    # Two candidates in account B could both plausibly be account A's
    # partner - must not guess either way.
    candidates = [
        c(0, "acct-a", 20, -50000),
        c(1, "acct-b", 20, 50000),
        c(2, "acct-c", 20, 50000),
    ]
    assert find_transfer_pairs(candidates, match_window_days=4) == []


def test_unrelated_transactions_do_not_interfere_with_a_real_pair():
    candidates = [
        c(0, "acct-a", 20, -50000),
        c(1, "acct-b", 20, 50000),
        c(2, "acct-a", 20, -10000),
        c(3, "acct-b", 20, 999000),
    ]
    assert find_transfer_pairs(candidates, match_window_days=4) == [(0, 1)]


def test_multiple_independent_pairs_all_matched():
    candidates = [
        c(0, "acct-a", 20, -50000),
        c(1, "acct-b", 20, 50000),
        c(2, "acct-a", 21, -25000),
        c(3, "acct-c", 22, 25000),
    ]
    result = find_transfer_pairs(candidates, match_window_days=4)
    assert set(result) == {(0, 1), (2, 3)}


def test_empty_input_returns_no_pairs():
    assert find_transfer_pairs([], match_window_days=4) == []


def test_single_candidate_has_no_partner():
    assert find_transfer_pairs([c(0, "acct-a", 20, -50000)], match_window_days=4) == []
