#!/usr/bin/env python3
"""Manual re-add for a transaction resolved as deleted-in-YNAB.

Background: SpareBank1 keeps reporting some transactions that were, at
some point, correctly created in YNAB and then deleted there (by a human,
e.g. during duplicate cleanup). YNAB permanently blocks reusing that
transaction's original import_id, so sync/engine.py's _backfill_duplicates
detects this case and marks it locally as booking_status='DELETED' -
final, never retried automatically (see CLAUDE.md's "Known open risk").

If a deletion turns out to have been a mistake, this script re-creates the
transaction in YNAB from the payee/memo/date/amount/cleared data that was
persisted at the time it was last tracked, using a fresh import_id (the
original one stays permanently burned).

This is a manual, occasional-use admin tool - no MQTT command or GUI wires
into sync.engine.SyncEngine.readd_deleted_transaction() yet (planned for
later). Since re-adding writes to the same state/sync_state.db file the
long-running service also has open, avoid running this at the exact same
moment as a real sync cycle - if you hit a "database is locked" error,
just retry.

Usage:
    python scripts/readd_deleted_transaction.py                  # list candidates
    python scripts/readd_deleted_transaction.py --tracking-key <key>    # re-add one
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ynab_auto_sync.config import load_config
from ynab_auto_sync.provider_setup import (
    resolve_sparebank1_provider,
    token_store_path,
)
from ynab_auto_sync.providers.sparebank1.auth import TokenStore
from ynab_auto_sync.providers.sparebank1.provider import SpareBank1Provider
from ynab_auto_sync.state import JsonStateStore
from ynab_auto_sync.sync.engine import SyncEngine
from ynab_auto_sync.sync.state_db import StateDB


def list_candidates(db: StateDB) -> None:
    rows = db.list_deleted_transactions()
    if not rows:
        print("No transactions are currently marked as resolved-deleted.")
        return
    print(f"{len(rows)} transaction(s) resolved as deleted-in-YNAB, most recent first:\n")
    for row in rows:
        amount_kr = row["amount_milliunits"] / 1000
        print(
            f"  tracking_key: {row['sb1_transaction_id']}\n"
            f"    payee:   {row['payee_name']}\n"
            f"    date:    {row['transaction_date']}\n"
            f"    amount:  {amount_kr:.2f} kr\n"
            f"    memo:    {row['memo']}\n"
            f"    cleared: {row['cleared']}\n"
            f"    resolved at: {row['last_checked_at']} (previously re-added {row['readd_count']}x)\n"
        )
    print(
        "Re-add one with: python scripts/readd_deleted_transaction.py "
        "--tracking-key <tracking_key>"
    )


async def readd(config_path: str, state_dir: str, tracking_key: str) -> None:
    config = load_config(config_path)
    provider_name, provider_config = resolve_sparebank1_provider(config)
    token_store = TokenStore(
        JsonStateStore(token_store_path(state_dir, provider_name)), provider_config
    )
    db = StateDB(Path(state_dir) / "sync_state.db")

    async with httpx.AsyncClient(timeout=30) as http_client:
        providers = {
            SpareBank1Provider.type_name(): SpareBank1Provider(http_client, token_store)
        }
        engine = SyncEngine(config, http_client, db, providers)
        try:
            new_id = await engine.readd_deleted_transaction(tracking_key)
        except (ValueError, RuntimeError) as e:
            print(f"FAILED: {e}")
            raise SystemExit(1) from None
        print(f"Re-added successfully - new YNAB transaction id: {new_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--tracking-key",
        help="Tracking key to re-add (the sb1_transaction_id column, which despite "
             "its name holds a tracking key from any source); omit to list candidates",
    )
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--state-dir", default="state")
    args = parser.parse_args()

    if args.tracking_key is None:
        db = StateDB(Path(args.state_dir) / "sync_state.db")
        list_candidates(db)
        return 0

    asyncio.run(readd(args.config, args.state_dir, args.tracking_key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
