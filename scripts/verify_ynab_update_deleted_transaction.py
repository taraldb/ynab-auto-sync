#!/usr/bin/env python3
"""Live verification for the assumptions engine.py's submit() now depends
on to recover from a pending->booked PATCH target that no longer exists in
YNAB - see CLAUDE.md's "Resolved: update PATCH could permanently wedge the
sync cycle".

Confirms, against a real budget:

  1. PATCHing a deleted transaction's id returns 400 (not some other status
     or a silent no-op) - and prints the exact error body shape, since
     production's own observed shape ("transaction does not exist in this
     budget (index: N)") was reverse-engineered from one incident, not from
     YNAB's docs.
  2. In a batch PATCH containing one still-valid id alongside one deleted
     id, the WHOLE batch is rejected - including the otherwise-fine row.
     This is the exact assumption engine.py's per-row pre-check design
     depends on (a blind batched PATCH used to wedge the entire cycle over
     a single bad row).
  3. list_transactions_including_deleted (budget-wide, last_knowledge_of_
     server=0) reports deleted=true for a transaction immediately after
     it's deleted via the API - no propagation delay - since the new pre-
     check in submit() depends on this being current, not eventually
     consistent.

This creates TWO real, tiny transactions in the budget you specify, deletes
one of them, and never recreates or re-deletes anything beyond that - safe
to leave the surviving test transaction in the budget afterward, or delete
it by hand in the YNAB app.

Usage:
    python scripts/verify_ynab_update_deleted_transaction.py --account-id <ynab_account_id>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ynab_auto_sync.config import load_config
from ynab_auto_sync.ynab import client as ynab_client

IMPORT_ID_A = "SB1:verify-deleted-update-test-aaaa"
IMPORT_ID_B = "SB1:verify-deleted-update-test-bbbb"


async def verify(account_id: str, config_path: str, budget_alias: str | None) -> None:
    config = load_config(config_path)
    pat = config.ynab.personal_access_token
    if budget_alias is None:
        if len(config.ynab.budgets) != 1:
            print(
                f"Multiple budget aliases configured ({', '.join(config.ynab.budgets)}) - "
                "pass --budget-alias to pick one."
            )
            return
        budget_alias = next(iter(config.ynab.budgets))
    budget_id = config.ynab.budgets[budget_alias]
    print(f"Using budget alias {budget_alias!r} -> {budget_id}")

    async with httpx.AsyncClient(timeout=30) as http_client:
        print("--- Step 1: creating two test transactions (A: to be deleted, B: stays valid) ---")
        today = datetime.now(UTC).date().isoformat()
        tx_a = {
            "account_id": account_id,
            "date": today,
            "amount": -1000,
            "payee_name": "ynab-auto-sync deleted-update-verification A",
            "memo": "Safe to delete - created by scripts/verify_ynab_update_deleted_transaction.py",
            "cleared": "uncleared",
            "approved": False,
            "import_id": IMPORT_ID_A,
        }
        tx_b = {
            "account_id": account_id,
            "date": today,
            "amount": -2000,
            "payee_name": "ynab-auto-sync deleted-update-verification B",
            "memo": "Safe to delete - created by scripts/verify_ynab_update_deleted_transaction.py",
            "cleared": "uncleared",
            "approved": False,
            "import_id": IMPORT_ID_B,
        }
        created = await ynab_client.create_transactions(http_client, pat, budget_id, [tx_a, tx_b])
        transactions = created.get("transactions", [])
        by_import_id = {t.get("import_id"): t for t in transactions}
        id_a = by_import_id.get(IMPORT_ID_A, {}).get("id")
        id_b = by_import_id.get(IMPORT_ID_B, {}).get("id")
        if not id_a or not id_b:
            print(
                "One or both test transactions already existed (reported as duplicates) - "
                "recovering their ids via find_transaction_by_import_id..."
            )
            if not id_a:
                existing_a = await ynab_client.find_transaction_by_import_id(
                    http_client, pat, budget_id, account_id, IMPORT_ID_A
                )
                id_a = existing_a["id"] if existing_a else None
            if not id_b:
                existing_b = await ynab_client.find_transaction_by_import_id(
                    http_client, pat, budget_id, account_id, IMPORT_ID_B
                )
                id_b = existing_b["id"] if existing_b else None
        if not id_a or not id_b:
            print("FAIL: could not establish both test transaction ids - aborting.")
            return
        print(f"OK - transaction A id={id_a!r}, transaction B id={id_b!r}")

        print("\n--- Step 2: deleting transaction A ---")
        await ynab_client.delete_transaction(http_client, pat, budget_id, id_a)
        print("OK - deleted transaction A")

        print("\n--- Step 3: PATCHing the now-deleted transaction A alone ---")
        try:
            await ynab_client.update_transactions(
                http_client, pat, budget_id, [{"id": id_a, "cleared": "cleared", "amount": -1500}]
            )
            print("FAIL: expected a 400 for a PATCH referencing a deleted transaction, got success.")
        except httpx.HTTPStatusError as e:
            print(f"OK - got {e.response.status_code}: {e.response.text}")
            if e.response.status_code != 400:
                print(f"  NOTE: expected 400, got {e.response.status_code} - assumption may be wrong.")

        print(
            "\n--- Step 4: PATCHing a MIXED batch (valid B + deleted A) - "
            "does the whole batch reject? ---"
        )
        try:
            await ynab_client.update_transactions(
                http_client,
                pat,
                budget_id,
                [
                    {"id": id_b, "cleared": "cleared", "amount": -2500},
                    {"id": id_a, "cleared": "cleared", "amount": -1500},
                ],
            )
            print(
                "NOTE: mixed batch succeeded - the assumption that one bad id rejects the "
                "WHOLE batch (including valid rows) does NOT hold here. Re-check engine.py's "
                "per-row pre-check design against this."
            )
        except httpx.HTTPStatusError as e:
            print(
                f"OK - mixed batch rejected with {e.response.status_code}: {e.response.text} "
                "(confirms the whole-batch-fails assumption)"
            )

        print("\n--- Step 5: confirming list_transactions_including_deleted sees A as deleted=true immediately ---")
        all_transactions = await ynab_client.list_transactions_including_deleted(
            http_client, pat, budget_id
        )
        by_id = {t["id"]: t for t in all_transactions}
        seen_a = by_id.get(id_a)
        if seen_a is None:
            print(f"FAIL: transaction A ({id_a}) not found at all in the budget-wide listing.")
        elif seen_a.get("deleted") is True:
            print("PASS: transaction A shows deleted=true immediately, no propagation delay.")
        else:
            print(f"FAIL: transaction A's deleted field is {seen_a.get('deleted')!r}, expected True.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, help="A real YNAB account_id in the target budget")
    parser.add_argument("--budget-alias", help="Alias from ynab.budgets to test; required if more than one is configured")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    asyncio.run(verify(args.account_id, args.config, args.budget_alias))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
