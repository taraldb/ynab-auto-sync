"""Shared, source-agnostic mechanics for deriving a YNAB import_id.

The *prefix* is owned by each source (SpareBank1's "SB1:", file-import's
"FILE:") and is deliberately NOT decided here - see CLAUDE.md invariant 7
for why two sources sharing a prefix would permanently merge their dedup
domains. Only the hashing/truncation, which is identical everywhere, lives
in this module, so the engine can re-derive an id for a source it doesn't
otherwise know anything about (see SyncEngine.readd_deleted_transaction)
without importing a provider-specific module.

The output format is a frozen wire contract: YNAB permanently reserves
every import_id it has ever seen, even after the transaction using it is
deleted (invariant 6), so changing the hash, its length, or the prefix
would silently re-import a source's entire history as duplicates.
"""

from __future__ import annotations

import hashlib

# YNAB caps import_id at 36 characters. The longest prefix in use is
# "FILE:" (5), so 5 + 30 = 35 leaves headroom. sha256 is used purely to fit
# an arbitrary-length source id into that budget deterministically, not for
# any security property.
IMPORT_ID_HASH_LEN = 30


def derive_import_id(prefix: str, seed: str) -> str:
    """Hash `seed` into this source's import_id namespace.

    `seed` is whatever string that source considers the transaction's
    stable identity (a SpareBank1 tracking key, a file-import content
    hash). Only the seed is hashed - the prefix is prepended afterwards and
    contributes no entropy, which is exactly why two sources must never
    share one.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:IMPORT_ID_HASH_LEN]}"


def prefix_of(import_id: str) -> str:
    """Recover the source prefix from an already-derived import_id, so a
    re-add can stay inside the dedup domain the original belonged to
    (invariant 7) rather than defaulting to whichever source the calling
    code happens to know about.

    Returns everything up to and including the first ":". Note this is only
    meaningful for real, source-derived import_ids; the locally-synthesized
    "LOCAL:XFER:..." marker written for a transfer's auto-created secondary
    leg would yield "LOCAL:", but that value is never a re-add candidate -
    only rows marked DELETED are, and those always carry a real prefix.
    """
    head, sep, _ = import_id.partition(":")
    return head + sep if sep else ""
