from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from ynab_auto_sync.sync.file_import.base import ImportedTransactionRow, RowTransformError
from ynab_auto_sync.sync.file_import.parsing import parse_file
from ynab_auto_sync.sync.file_import.registry import detect_transformer

# Importing the transformers package triggers @register on each
# transformer module, populating registry.REGISTRY - matching the real
# import path a production caller (e.g. the future file-import HTTP
# endpoint) would use.
from ynab_auto_sync.sync.file_import.transformers import norwegian_bank  # noqa: F401
from ynab_auto_sync.sync.file_import.transformers.norwegian_bank import (
    NorwegianBankTransformer,
)
from ynab_auto_sync.sync.money import from_milliunits

FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "norwegian_bank_sample.xlsx"

# Real header row, confirmed live from the sibling ynab-converter project's
# own sample export (norwegian-bank-input.xlsx) and its test suite.
NORWEGIAN_BANK_HEADERS = [
    "TransactionDate",
    "Text",
    "Type",
    "Currency Amount",
    "Currency Rate",
    "Currency",
    "Amount",
    "Merchant Area",
    "Merchant Category",
    "BookDate",
    "ValueDate",
]


def make_row(
    date_val,
    text="SOME STORE",
    amount=-100.0,
    merchant_category="GROCERY STORES/SUPERMARKETS",
    transaction_type=None,
):
    row = [None] * 11
    row[0] = date_val
    row[1] = text
    row[2] = transaction_type
    row[6] = amount
    row[8] = merchant_category
    return tuple(row)


# --- detect_transformer ---


def test_detect_transformer_picks_norwegian_bank_for_real_headers():
    transformer = detect_transformer(NORWEGIAN_BANK_HEADERS)
    assert isinstance(transformer, NorwegianBankTransformer)


def test_detect_transformer_returns_none_for_unrelated_headers():
    assert detect_transformer(["Foo", "Bar", "Baz"]) is None


def test_detect_transformer_requires_exact_header_match():
    # A subset/reordering of the real headers must not match - can_handle
    # is deliberately an exact match, not fuzzy.
    partial = NORWEGIAN_BANK_HEADERS[:5]
    assert detect_transformer(partial) is None


# --- NorwegianBankTransformer.transform ---


def test_transform_produces_expected_row_values():
    row = make_row(
        datetime(2025, 10, 29, tzinfo=UTC),
        text="PAYPAL *JAGEX LTD",
        amount=-119.9,
        merchant_category="VIDEO GAME ARCADES/ESTABLISH",
    )
    result = NorwegianBankTransformer().transform([row])

    assert result == [
        ImportedTransactionRow(
            date=date(2025, 10, 29),
            amount=from_milliunits(-119900),
            payee_name="PAYPAL JAGEX LTD",  # sanitized: '*' separator replaced with a space
            memo=None,  # memo is no longer populated from Merchant Category
            cleared="cleared",
            row_index=2,
        )
    ]


def test_transform_handles_positive_amount_and_missing_merchant_category():
    row = make_row(
        datetime(2025, 11, 15, tzinfo=UTC),
        text="Fra 32091049847",
        amount=20099.92,
        merchant_category="",
    )
    result = NorwegianBankTransformer().transform([row])

    assert result[0].amount == from_milliunits(20099920)
    assert result[0].memo is None
    assert result[0].cleared == "cleared"


def test_transform_row_index_accounts_for_header_row():
    rows = [
        make_row(datetime(2025, 1, 1, tzinfo=UTC)),
        make_row(datetime(2025, 1, 2, tzinfo=UTC)),
        make_row(datetime(2025, 1, 3, tzinfo=UTC)),
    ]
    result = NorwegianBankTransformer().transform(rows)
    assert [r.row_index for r in result] == [2, 3, 4]


def test_transform_detects_uncleared_transactions():
    # Transactions with "Reservert" in the Type column (column 2) are uncleared.
    row = make_row(
        datetime(2025, 11, 20, tzinfo=UTC),
        text="VISA CHARGE",
        amount=-500.0,
        transaction_type="Reservert",
    )
    result = NorwegianBankTransformer().transform([row])

    assert result[0].cleared == "uncleared"


def test_transform_detects_cleared_transactions():
    # Transactions without "Reservert" in the Type column are cleared.
    row = make_row(
        datetime(2025, 11, 20, tzinfo=UTC),
        text="STORE PURCHASE",
        amount=-150.0,
        transaction_type="Normal",
    )
    result = NorwegianBankTransformer().transform([row])

    assert result[0].cleared == "cleared"


def test_transform_serial_date_fallback():
    # Some rows in real exports arrive with TransactionDate as a raw Excel
    # serial number instead of a native datetime cell (observed for
    # pending/uncleared rows).
    serial = (datetime(2025, 11, 20, tzinfo=UTC) - datetime(1899, 12, 30, tzinfo=UTC)).days
    row = make_row(serial, text="Vipps*Odeon kino Stavange", amount=-304.3)

    result = NorwegianBankTransformer().transform([row])

    assert result[0].date == date(2025, 11, 20)


def test_transform_raises_on_unrecognized_date_value():
    row = make_row("not a date")
    with pytest.raises(RowTransformError):
        NorwegianBankTransformer().transform([row])


def test_transform_raises_on_missing_amount():
    row = list(make_row(datetime(2025, 1, 1, tzinfo=UTC)))
    row[6] = None
    with pytest.raises(RowTransformError):
        NorwegianBankTransformer().transform([tuple(row)])


def test_transform_raises_on_missing_payee():
    row = list(make_row(datetime(2025, 1, 1, tzinfo=UTC)))
    row[1] = None
    with pytest.raises(RowTransformError):
        NorwegianBankTransformer().transform([tuple(row)])


def test_transform_raises_on_row_too_short():
    with pytest.raises(RowTransformError):
        NorwegianBankTransformer().transform([(datetime(2025, 1, 1, tzinfo=UTC), "X")])


# --- parse_file ---


def test_parse_file_round_trips_real_xlsx_fixture():
    data = FIXTURE_PATH.read_bytes()
    headers, rows = parse_file("norwegian_bank_sample.xlsx", data)

    assert headers == NORWEGIAN_BANK_HEADERS
    assert len(rows) == 5

    transformer = detect_transformer(headers)
    assert isinstance(transformer, NorwegianBankTransformer)

    transformed = transformer.transform(rows)
    assert len(transformed) == 5
    # Row 3 (index 4, 0-based) is the serial-number-date pending row.
    assert transformed[3].date == date(2025, 11, 20)
    assert transformed[3].amount == from_milliunits(-304300)
    # Row 2 has no Merchant Category value in the fixture.
    assert transformed[2].memo is None


def test_parse_file_raises_for_unsupported_extension():
    with pytest.raises(ValueError):
        parse_file("statement.pdf", b"not really a pdf")
