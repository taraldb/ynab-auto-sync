from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass
class ImportedTransactionRow:
    date: date
    amount_milliunits: int
    payee_name: str
    memo: str | None
    row_index: int  # 1-based, including the header row, for error reporting


class RowTransformError(Exception):
    """Raised when a single data row can't be turned into an
    ImportedTransactionRow (missing/unparseable date, amount, or payee).

    Mirrors this project's existing house style for financial-data parsing
    (see sync/transform.py's MissingFieldError): a specific, named exception
    carrying enough context (row_index, the raw row) to identify exactly
    which row failed. A file import is user-initiated and the user expects
    every row they see in their export to show up in YNAB - silently
    skipping a bad row would be a worse failure mode than a loud error that
    stops the whole import and says which row and why.
    """


class TransformerBase(ABC):
    @staticmethod
    @abstractmethod
    def can_handle(headers: list[str]) -> bool: ...

    @staticmethod
    @abstractmethod
    def name() -> str: ...

    @abstractmethod
    def transform(self, rows: list[tuple]) -> list[ImportedTransactionRow]:
        """Transform parsed data rows (header row excluded) into
        ImportedTransactionRow objects, in the same order. Must raise
        RowTransformError (not skip) for any row it can't parse."""
        ...
