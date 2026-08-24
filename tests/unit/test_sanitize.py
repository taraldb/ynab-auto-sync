from ynab_auto_sync.sync.sanitize import clean_bank_text


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
