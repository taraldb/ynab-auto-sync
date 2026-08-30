from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from ynab_auto_sync.sync.money import parse_provider_amount
from ynab_auto_sync.sync.sanitize import clean_bank_text

from ..base import ImportedTransactionRow, RowTransformError, TransformerBase
from ..registry import register

# Real header row, confirmed directly from the sibling ynab-converter
# project's own sample export (norwegian-bank-input.xlsx, loaded and
# inspected live) and matching its test suite (tests/test_transformers.py)
# exactly - not guessed. Column order:
#   0 TransactionDate   1 Text              2 Type
#   3 Currency Amount   4 Currency Rate     5 Currency
#   6 Amount            7 Merchant Area     8 Merchant Category
#   9 BookDate          10 ValueDate
HEADERS = (
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
)

COL_TRANSACTION_DATE = 0
COL_TEXT = 1
COL_TYPE = 2
COL_AMOUNT = 6
COL_MERCHANT_CATEGORY = 8

# Matches this project's existing YNAB field-length caps in sync/transform.py.
YNAB_PAYEE_NAME_MAX_LEN = 200
YNAB_MEMO_MAX_LEN = 500


@register
class NorwegianBankTransformer(TransformerBase):
    @staticmethod
    def can_handle(headers: list[str]) -> bool:
        # Exact match against the real, confirmed header tuple - not a
        # fuzzy/partial match. This format is distinctive enough (11 named
        # columns in a specific order) that a loose match risks silently
        # misclassifying a different bank's similarly-shaped export.
        return tuple(headers) == HEADERS

    @staticmethod
    def name() -> str:
        return "Norwegian Bank"

    def transform(self, rows: list[tuple]) -> list[ImportedTransactionRow]:
        result = []
        for i, row in enumerate(rows):
            row_index = i + 2  # +1 for 0-index, +1 to account for the header row
            result.append(self._transform_row(row, row_index))
        return result

    def _transform_row(self, row: tuple, row_index: int) -> ImportedTransactionRow:
        if len(row) <= COL_AMOUNT:
            raise RowTransformError(
                f"Row {row_index}: expected at least {COL_AMOUNT + 1} columns, "
                f"got {len(row)}: {row!r}"
            )

        tx_date = self._parse_date(row[COL_TRANSACTION_DATE], row_index, row)

        amount = row[COL_AMOUNT]
        if amount is None:
            raise RowTransformError(f"Row {row_index}: missing Amount value: {row!r}")
        try:
            # decimal_places=2 hardcoded (not threaded from
            # config.sync.currency_decimal_places) - this transformer is
            # already Norwegian-bank-specific by name and column layout, so
            # assuming NOK's 2 decimal places here is no more source-
            # specific than everything else in this file. Matches this
            # project's SpareBank1 float-NOK-to-Decimal convention
            # (sync/money.py's parse_provider_amount, used the same way by
            # providers/sparebank1/transform.py).
            amount = parse_provider_amount(amount, decimal_places=2)
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise RowTransformError(
                f"Row {row_index}: Amount value {amount!r} is not numeric: {row!r}"
            ) from exc

        text = row[COL_TEXT]
        if not text:
            raise RowTransformError(f"Row {row_index}: missing Text (payee) value: {row!r}")
        # clean_bank_text strips the merchant-processor '*' separator this
        # format is confirmed to use (e.g. "PAYPAL *JAGEX LTD",
        # "Vipps*Odeon kino Stavange") - same sanitizer applied to the live
        # SpareBank1 path in sync/transform.py, so payee formatting is
        # consistent regardless of which source a transaction came from.
        cleaned_payee = clean_bank_text(str(text))
        payee_name = (cleaned_payee or str(text))[:YNAB_PAYEE_NAME_MAX_LEN]

        # Detect uncleared transactions: when the Type column contains "Reservert"
        # (reserved/held), the transaction is uncleared. Otherwise, it's cleared.
        transaction_type = str(row[COL_TYPE]) if len(row) > COL_TYPE and row[COL_TYPE] else ""
        cleared = "uncleared" if "Reservert" in transaction_type else "cleared"

        return ImportedTransactionRow(
            date=tx_date,
            amount=amount,
            payee_name=payee_name,
            memo=None,
            cleared=cleared,
            row_index=row_index,
        )

    @staticmethod
    def _parse_date(date_val: Any, row_index: int, row: tuple):
        if isinstance(date_val, datetime):
            return date_val.date()
        if isinstance(date_val, (int, float)):
            # Defensive fallback ported from ynab-converter's
            # NorwegianBankTransformer: some rows (observed there for
            # pending/uncleared rows in real exports) arrive with
            # TransactionDate as a raw Excel serial number instead of a
            # native datetime cell, presumably because the source system
            # didn't apply a date number-format to that cell before some
            # rows settled.
            #
            # This lives here, in the transformer, rather than in
            # parsing.py's generic parse_file - only this transformer knows
            # column 0 is a date at all; parsing.py stays column-agnostic so
            # a future bank format (different columns, different quirks)
            # doesn't inherit an assumption that doesn't apply to it. No
            # second defensive layer is needed in parsing.py: parse_file
            # already returns whatever raw value openpyxl gave it for each
            # cell, so this single check is sufficient.
            # tzinfo=UTC here is a fixed-offset annotation only (UTC has no
            # DST), so it doesn't change the resulting date - added purely
            # to satisfy this project's tz-aware-datetime lint rule (DTZ001),
            # matching transform.py's own tz=UTC convention for a similar
            # epoch-anchor calculation.
            return (datetime(1899, 12, 30, tzinfo=UTC) + timedelta(days=int(date_val))).date()
        raise RowTransformError(
            f"Row {row_index}: unrecognized TransactionDate value {date_val!r}: {row!r}"
        )
