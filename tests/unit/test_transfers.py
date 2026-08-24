from datetime import date

from ynab_auto_sync.sync.transfers import TransferCandidate, find_transfer_pairs


def c(index, account_key, day, amount, account_number=None, remote_account_number=None):
    return TransferCandidate(
        index=index,
        account_key=account_key,
        date=date(2026, 8, day),
        amount_milliunits=amount,
        account_number=account_number,
        remote_account_number=remote_account_number,
    )


def test_matches_opposite_sign_same_day_different_accounts():
    candidates = [
        c(0, "acct-a", 20, -50000, account_number="a-num", remote_account_number="b-num"),
        c(1, "acct-b", 20, 50000, account_number="b-num", remote_account_number="a-num"),
    ]
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
    candidates = [
        c(0, "acct-a", 20, -50000, account_number="a-num", remote_account_number="b-num"),
        c(1, "acct-b", 23, 50000, account_number="b-num", remote_account_number="a-num"),
    ]
    assert find_transfer_pairs(candidates, match_window_days=4) == [(0, 1)]


def test_does_not_match_outside_date_window():
    candidates = [c(0, "acct-a", 20, -50000), c(1, "acct-b", 26, 50000)]
    assert find_transfer_pairs(candidates, match_window_days=4) == []


def test_ambiguous_match_skipped_on_both_sides():
    # Two candidates in account B/C could both plausibly be account A's
    # partner (both correctly cross-reference account A) - must not guess
    # either way, even though the cross-reference check alone can't
    # disambiguate them.
    candidates = [
        c(0, "acct-a", 20, -50000, account_number="a-num", remote_account_number="a-num"),
        c(1, "acct-b", 20, 50000, account_number="b-num", remote_account_number="a-num"),
        c(2, "acct-c", 20, 50000, account_number="c-num", remote_account_number="a-num"),
    ]
    assert find_transfer_pairs(candidates, match_window_days=4) == []


def test_unrelated_transactions_do_not_interfere_with_a_real_pair():
    candidates = [
        c(0, "acct-a", 20, -50000, account_number="a-num", remote_account_number="b-num"),
        c(1, "acct-b", 20, 50000, account_number="b-num", remote_account_number="a-num"),
        c(2, "acct-a", 20, -10000),
        c(3, "acct-b", 20, 999000),
    ]
    assert find_transfer_pairs(candidates, match_window_days=4) == [(0, 1)]


def test_multiple_independent_pairs_all_matched():
    candidates = [
        c(0, "acct-a", 20, -50000, account_number="a-num", remote_account_number="b-num"),
        c(1, "acct-b", 20, 50000, account_number="b-num", remote_account_number="a-num"),
        c(2, "acct-a", 21, -25000, account_number="a-num", remote_account_number="c-num"),
        c(3, "acct-c", 22, 25000, account_number="c-num", remote_account_number="a-num"),
    ]
    result = find_transfer_pairs(candidates, match_window_days=4)
    assert set(result) == {(0, 1), (2, 3)}


def test_empty_input_returns_no_pairs():
    assert find_transfer_pairs([], match_window_days=4) == []


def test_single_candidate_has_no_partner():
    assert find_transfer_pairs([c(0, "acct-a", 20, -50000)], match_window_days=4) == []


# -- account-number cross-reference (added after a real false positive) -----


def test_no_match_without_any_account_number_cross_reference():
    # Regression test for the real 158.48 kr false positive: a salary
    # deposit and an unrelated grocery purchase, same amount, opposite
    # sign, different accounts, within the window - but neither names the
    # other's real account number.
    candidates = [
        c(0, "acct-a", 20, -15848, account_number="a-num", remote_account_number="unrelated-1"),
        c(1, "acct-b", 24, 15848, account_number="b-num", remote_account_number=None),
    ]
    assert find_transfer_pairs(candidates, match_window_days=4) == []


def test_matches_when_only_one_direction_cross_references():
    # Regression test for the real credit-card-bill-payment shape: the
    # checking leg names the card issuer's clearing account (not the card
    # itself), but the card's own leg correctly names the checking account
    # back. Only ONE direction resolves - must still match.
    candidates = [
        c(0, "acct-checking", 20, -1828930, account_number="checking-num",
          remote_account_number="card-issuer-clearing-num"),
        c(1, "acct-card", 21, 1828930, account_number="card-num",
          remote_account_number="checking-num"),
    ]
    assert find_transfer_pairs(candidates, match_window_days=4) == [(0, 1)]
