from __future__ import annotations

import hashlib
import json
from datetime import date

from ynab_auto_sync.sync.import_ids import derive_import_id as shared_derive_import_id

# A distinct prefix from transform.py's "SB1:" so a file-import-derived
# import_id can never collide with a SpareBank1-derived one, even for the
# same real-world transaction observed by both paths later - collision
# would silently and permanently merge two unrelated dedup domains in YNAB,
# since derive_import_id hashes only the raw pre-prefix string as its
# entropy source. "FILE:" is deliberately generic (not e.g. "XLSX:") since
# xlsx is only the first supported file format - any future format/
# transformer shares this same dedup domain via its own tracking key.
# YNAB caps import_id at 36 chars: "FILE:" (5) + 30 hex chars = 35.
IMPORT_ID_PREFIX = "FILE:"


def compute_dedup_key(
    tx_date: date, amount_milliunits: int, payee_name: str, memo: str | None
) -> str:
    """Hash of stable CONTENT fields only - date, amount, casefolded payee/
    memo - never a bank-assigned row id, matching this project's hard
    invariant (see transform.get_tracking_key) that a dedup key must be
    derived from data that cannot silently change across re-imports of the
    same real-world transaction. A file-import source has no bank-assigned
    id to begin with, so this is the only option, not just the safer one.
    """
    payload = {
        "date": tx_date.isoformat(),
        "amount_milliunits": amount_milliunits,
        "payee": payee_name.strip().casefold(),
        "memo": (memo or "").strip().casefold(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_tracking_key(ynab_account_id: str, dedup_key: str) -> str:
    """Scoped by ynab_account_id, mirroring how transform.get_tracking_key
    scopes by account_key - the same content hash landing in two different
    YNAB accounts must not collide."""
    return f"file:{ynab_account_id}:{dedup_key}"


def derive_import_id(tracking_key: str) -> str:
    """The file-import dedup domain's import_id. Delegates hashing to
    sync.import_ids (shared with every other source) while keeping the
    "FILE:" prefix a frozen literal owned here - see IMPORT_ID_PREFIX's
    comment above for why it must never collide with another source's.
    """
    return shared_derive_import_id(IMPORT_ID_PREFIX, tracking_key)
