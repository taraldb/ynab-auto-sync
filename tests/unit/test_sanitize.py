from ynab_auto_sync.sync.sanitize import (
    clean_bank_text,
    levenshtein_distance,
    normalize_payee_for_fuzzy_match,
    payee_similarity,
    payees_plausibly_match,
)


def test_replaces_star_separator_with_space():
    assert clean_bank_text("Zettle_*Micro Kaffi AS") == "Zettle_ Micro Kaffi AS"
    assert clean_bank_text("PAYPAL *JAGEX LTD") == "PAYPAL JAGEX LTD"
    assert clean_bank_text("Vipps*Odeon kino Stavange") == "Vipps Odeon kino Stavange"


def test_strips_leading_equals_signs():
    assert clean_bank_text("===Some Shop") == "Some Shop"


def test_removes_parenthesized_account_number():
    assert clean_bank_text("Transfer (1234.56.78903) to savings") == "Transfer to savings"


def test_collapses_repeated_whitespace():
    assert clean_bank_text("Too   many    spaces") == "Too many spaces"


def test_none_and_empty_input_returns_none():
    assert clean_bank_text(None) is None
    assert clean_bank_text("") is None
    assert clean_bank_text("   ") is None


def test_output_that_becomes_empty_after_cleaning_returns_none():
    assert clean_bank_text("***") is None


def test_leaves_ordinary_text_unchanged():
    assert clean_bank_text("KIWI 766 FARSUND") == "KIWI 766 FARSUND"


def test_normalize_payee_for_fuzzy_match_strips_trailing_country_suffix():
    assert normalize_payee_for_fuzzy_match("MERCHANT ONE, LOC-A, NOR") == "merchant one, loc-a"


def test_normalize_payee_for_fuzzy_match_leaves_non_country_tail_alone():
    # "loc-a" has a hyphen, so it isn't treated as a bare country code -
    # only the location text itself is preserved.
    assert normalize_payee_for_fuzzy_match("MERCHANT ONE, LOC-A") == "merchant one, loc-a"


def test_payees_plausibly_match_true_for_drifted_location():
    assert payees_plausibly_match("MERCHANT TWO, LOC-A", "MERCHANT TWO, LOC-B, XYZ")


def test_payees_plausibly_match_true_for_identical_after_normalization():
    assert payees_plausibly_match("MERCHANT ONE, LOC-A", "MERCHANT ONE, LOC-A, NOR")


def test_payees_plausibly_match_false_for_unrelated_merchants():
    assert not payees_plausibly_match("MERCHANT ONE, LOC-A", "TOTALLY DIFFERENT SHOP, LOC-B")


def test_payees_plausibly_match_true_for_identical_merchant_no_comma():
    assert payees_plausibly_match("MERCHANT-A", "MERCHANT-B", min_prefix_len=9)


def test_levenshtein_distance_known_values():
    assert levenshtein_distance("same", "same") == 0
    assert levenshtein_distance("cat", "bat") == 1
    assert levenshtein_distance("", "abc") == 3
    assert levenshtein_distance("abc", "") == 3


def test_payee_similarity_identical_strings_is_one():
    assert payee_similarity("merchant one", "merchant one") == 1.0


def test_payee_similarity_completely_different_strings_is_low():
    assert payee_similarity("aaaa", "zzzz") == 0.0


def test_payee_similarity_partial_overlap_is_between():
    score = payee_similarity("merchant one, downtown", "merchant one, airport")
    assert 0.0 < score < 1.0
