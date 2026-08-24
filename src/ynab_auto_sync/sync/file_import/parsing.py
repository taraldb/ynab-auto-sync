from __future__ import annotations

from io import BytesIO
from pathlib import Path

import openpyxl

# This module is deliberately format-agnostic: it knows how to open a
# container file (xlsx today) and hand back a plain header row + data rows,
# but nothing about what any particular bank's columns mean. Column-level
# knowledge (which index is the date, whether a raw numeric cell secretly
# means "Excel serial-number date", etc.) belongs to each transformer in
# transformers/, not here - a future file format may have a totally
# different column layout, or even a different "what looks like a date"
# quirk, so baking one bank's fallback into this generic layer would leak
# transformer-specific assumptions into code meant to stay reusable across
# all of them. See transformers/norwegian_bank.py for where the Excel
# serial-number date fallback actually lives, and why.


def _parse_xlsx(data: bytes) -> tuple[list[str], list[tuple]]:
    workbook = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
    try:
        worksheet = workbook.active
        rows_iter = worksheet.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            return [], []
        headers = [str(cell) if cell is not None else "" for cell in header_row]
        data_rows = list(rows_iter)
        return headers, data_rows
    finally:
        workbook.close()


def parse_file(filename: str, data: bytes) -> tuple[list[str], list[tuple]]:
    """Parse a bank export file into (headers, data_rows).

    Dispatches on filename extension. Currently supports .xlsx only. A
    future .csv branch slots in here as another `elif` (e.g. using the
    stdlib csv module with csv.Sniffer for delimiter detection, matching
    ynab-converter's own precedent) without needing to restructure this
    function or its callers.
    """
    ext = Path(filename).suffix.lower()
    if ext == ".xlsx":
        return _parse_xlsx(data)
    # elif ext == ".csv":
    #     return _parse_csv(data)
    raise ValueError(
        f"Unsupported file extension {ext!r} for file {filename!r} (supported: .xlsx)"
    )
