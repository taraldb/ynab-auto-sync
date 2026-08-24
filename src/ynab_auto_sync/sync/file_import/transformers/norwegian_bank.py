from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

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
            # Matches this project's existing SpareBank1 float-NOK-to-
            # milliunits convention (transform.py's _to_milliunits).
            amount_milliunits = round(float(amount) * 1000)
        except (TypeError, ValueError) as exc:
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

        # Judgment call: Merchant Category (col 8) carries real,
        # payee-distinct information in actual exports - e.g. Text=
        # "PAYPAL *JAGEX LTD" alongside Merchant Category=
        # "VIDEO GAME ARCADES/ESTABLISH", or Text="KIWI 766 FARSUND"
        # alongside "GROCERY STORES/SUPERMARKETS" - confirmed by inspecting
        # ynab-converter's real sample file. That's more useful as a memo
        # than always leaving memo empty (the sibling CLI project's own
        # choice, made when its output format had no memo-worthy use for
        # it). Blank for the one observed row type without a merchant (an
        # incoming transfer, "Innbetaling"), where the raw value is "".
        memo = None
        if len(row) > COL_MERCHANT_CATEGORY:
            merchant_category = row[COL_MERCHANT_CATEGORY]
            if merchant_category:
                cleaned_memo = clean_bank_text(str(merchant_category))
                memo = (cleaned_memo or str(merchant_category))[:YNAB_MEMO_MAX_LEN]

        return ImportedTransactionRow(
            date=tx_date,
            amount_milliunits=amount_milliunits,
            payee_name=payee_name,
            memo=memo,
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
