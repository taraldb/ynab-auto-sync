"""Bank-agnostic construction of YNAB create-transaction payloads.

This module knows nothing about SpareBank1 or any other specific bank/import
source - it exists so that field-name/length constraints that belong to YNAB
itself (not to any one data source) live in exactly one place, shared by
every source's transform layer (today: sync/transform.py for the live
SpareBank1 poll, and sync/engine.py's import_file_rows for the file-import
path). Do not add anything bank-specific here (e.g. transform.py's
"SpareBank1 transaction" fallback payee stays in transform.py).
"""

from __future__ import annotations

from datetime import date
from typing import Any

# YNAB's own field-length caps, confirmed against the API - not specific to
# any bank or import source.
YNAB_PAYEE_NAME_MAX_LEN = 200
YNAB_MEMO_MAX_LEN = 500


def build_create_payload(
    *,
    ynab_account_id: str,
    tx_date: date,
    amount_milliunits: int,
    payee_name: str,
    memo: str | None,
    cleared: str,
    import_id: str,
    payee_id: str | None = None,
) -> dict[str, Any]:
    """Build a YNAB create-transaction payload dict from already-normalized
    values. Every parameter must already be in its final, source-specific
    form (bank-specific extraction/derivation, e.g. SpareBank1's tracking
    key or a file-import row's content-hash dedup key, is the caller's job,
    not this function's) - this function only applies YNAB's own
    field-length truncation and assembles the dict shape.

    import_id is a PURE PASSTHROUGH: this function copies it verbatim and
    never derives, hashes, re-prefixes, or otherwise modifies it. YNAB
    permanently reserves/burns an import_id once used, even after the
    transaction using it is deleted (see CLAUDE.md invariant 6) - any drift
    between what a caller derives and what actually gets submitted would
    silently re-import history as duplicates the next time this code runs
    with a "fixed" derivation.

    payee_id, when given, is submitted INSTEAD of payee_name (YNAB treats
    the two as mutually exclusive on create - confirmed live via
    scripts/verify_ynab_payee_id.py). This is the anchor that lets a repeat
    sync of the same raw bank text land on a payee the user has since
    renamed/merged in YNAB, since payee_name only ever matches by exact
    string. payee_name is still required as a parameter even when payee_id
    is set, since the caller (engine.py) needs it either way to look up or
    populate the payee_mappings cache.
    """
    payload: dict[str, Any] = {
        "account_id": ynab_account_id,
        "date": tx_date.isoformat(),
        "amount": amount_milliunits,
        "memo": (memo[:YNAB_MEMO_MAX_LEN] if memo else None),
        "cleared": cleared,
        "approved": False,
        "import_id": import_id,
    }
    if payee_id:
        payload["payee_id"] = payee_id
    else:
        payload["payee_name"] = payee_name[:YNAB_PAYEE_NAME_MAX_LEN]
    return payload
