#!/usr/bin/env python3
"""Live verification for YNAB's bulk transaction update endpoint and the
import_id recovery lookup.

Confirms the assumptions src/ynab_auto_sync/ynab/client.py's
update_transactions() and find_transaction_by_import_id() make, per the plan:

  1. That PATCHing {RESOURCE_PATH}/{budget_id}/transactions with a body of
     {"transactions": [{"id": ..., ...}]} actually updates an existing
     transaction in place (rather than erroring, or creating a new one).
  2. That the fields changed in the update - `cleared` and `amount` - come
     back as the new values, not the original ones, when read back.
  3. That find_transaction_by_import_id() (a client-side scan over
     GET {RESOURCE_PATH}/{budget_id}/accounts/{account_id}/transactions,
     since YNAB has no dedicated import_id lookup endpoint - an earlier
     version of this script proved that assumption wrong) correctly finds
     a transaction by import_id, both when we already know it was just
     created AND when we've lost track of it and only have the import_id
     (the resilience path: recovering after local state loss).

This creates ONE real, tiny transaction in the budget you specify: initially
cleared="uncleared" with amount -1000 milliunits, payee
"ynab-auto-sync update-verification", import_id
"SB1:verify-update-test-000000000" (deliberately distinct from
scripts/verify_ynab.py's own test import_id so the two scripts' test
transactions don't collide). It then updates that same transaction to
cleared="cleared" and amount -2000 milliunits, leaving the date untouched,
and reads it back to confirm both changes took effect.

Re-running this script is safe and idempotent: if the test transaction
already exists from a previous run (YNAB reports it as a duplicate on the
create call), the script recovers its id via find_transaction_by_import_id
instead of failing - which doubles as a live test of the resilience path
itself. Safe to leave the resulting test transaction in the budget, or
delete it afterward in the YNAB app.

Usage:
    python scripts/verify_ynab_update.py --account-id <ynab_account_id>
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

TEST_IMPORT_ID = "SB1:verify-update-test-000000000"


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
        print("--- Step 1: creating (or recovering) the test transaction ---")
        tx = {
            "account_id": account_id,
            "date": datetime.now(UTC).date().isoformat(),
            "amount": -1000,
            "payee_name": "ynab-auto-sync update-verification",
            "memo": "Safe to delete - created by scripts/verify_ynab_update.py",
            "cleared": "uncleared",
            "approved": False,
            "import_id": TEST_IMPORT_ID,
        }
        created = await ynab_client.create_transactions(http_client, pat, budget_id, [tx])
        transaction_ids = created.get("transaction_ids", [])
        if transaction_ids:
            transaction_id = transaction_ids[0]
            print(f"OK - created new transaction id={transaction_id!r}")
        else:
            print(
                "Test transaction already exists (reported as a duplicate) - "
                "recovering its id via find_transaction_by_import_id (this is "
                "exactly the resilience path being verified)..."
            )
            existing = await ynab_client.find_transaction_by_import_id(
                http_client, pat, budget_id, account_id, TEST_IMPORT_ID
            )
            if existing is None:
                print(
                    "FAIL: find_transaction_by_import_id could not locate a transaction "
                    f"YNAB itself just said already exists with import_id={TEST_IMPORT_ID!r}."
                )
                return
            transaction_id = existing["id"]
            print(f"OK - recovered existing transaction id={transaction_id!r}")

        print("\n--- Step 2: updating cleared status and amount ---")
        update = {"id": transaction_id, "cleared": "cleared", "amount": -2000}
        try:
            update_result = await ynab_client.update_transactions(
                http_client, pat, budget_id, [update]
            )
            print(f"OK - PATCH succeeded, response: {update_result!r}")
        except httpx.HTTPStatusError as e:
            print(f"FAIL: update_transactions raised {e.response.status_code}: {e.response.text}")
            return

        print("\n--- Step 3: reading the transaction back via find_transaction_by_import_id ---")
        try:
            fetched = await ynab_client.find_transaction_by_import_id(
                http_client, pat, budget_id, account_id, TEST_IMPORT_ID
            )
        except httpx.HTTPStatusError as e:
            print(
                f"FAIL: find_transaction_by_import_id raised {e.response.status_code}: "
                f"{e.response.text}"
            )
            return

        if fetched is None:
            print("FAIL: find_transaction_by_import_id returned None for a known import_id.")
            return

        print(f"Fetched transaction: {fetched!r}")

        cleared_ok = fetched.get("cleared") == "cleared"
        amount_ok = fetched.get("amount") == -2000

        if cleared_ok and amount_ok:
            print("\nPASS: the update call correctly changed cleared status and amount, "
                  "and find_transaction_by_import_id correctly located the transaction.")
        else:
            print("\nFAIL: the fetched transaction does not reflect the expected update.")
            if not cleared_ok:
                print(f"  cleared: expected 'cleared', got {fetched.get('cleared')!r}")
            if not amount_ok:
                print(f"  amount: expected -2000, got {fetched.get('amount')!r}")


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
