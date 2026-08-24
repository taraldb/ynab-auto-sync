#!/usr/bin/env python3
"""Live verification for the assumption src/ynab_auto_sync/sync/engine.py's
reconcile_payee_mappings() depends on: that a payee deleted or merged away
in YNAB keeps appearing in GET {RESOURCE_PATH}/{budget_id}/payees with
`deleted: true`, rather than disappearing from the list entirely.

This matters because reconcile_payee_mappings() only ever heals a cached
payee_mappings row when its ynab_payee_id shows up with `deleted: true` -
never when the id is merely absent from a fetch (a transient or incomplete
response must never be mistaken for "every payee not in this list is
gone" - see StateDB.delete_payee_mappings_for_ids's own docstring). If this
script finds that a deleted/merged payee instead vanishes from the list,
that safety assumption is wrong and reconcile_payee_mappings() needs to be
redesigned before it's trusted in the real sync path.

Two phases:

  1. Automatic: fetches the target budget's real payee list and reports how
     many already carry `deleted: true` vs `false`, printing one full raw
     example of each kind found (or "none found" for deleted, which is the
     common case on a first run).
  2. Manual: creates one throwaway transaction under a unique, obviously-
     test payee name, prints that payee's id, then asks you to go delete or
     merge that payee by hand in the YNAB web/mobile app (YNAB has no
     payee-deletion/merge endpoint reachable from this app - it can only be
     triggered by a human, never scripted). Press Enter once done, and the
     script re-fetches the payee list to confirm that exact id now reports
     `deleted: true`.

Usage:
    python scripts/verify_ynab_payee_deletion.py --account-id <ynab_account_id>
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

TEST_PAYEE_NAME = "ynab-auto-sync payee-deletion-verification"
TEST_IMPORT_ID_PREFIX = "SB1:verify-payee-del-"


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
        print("--- Phase 1: current payee list, deleted vs. active ---")
        payees = await ynab_client.get_payees(http_client, pat, budget_id)
        deleted = [p for p in payees if p.get("deleted")]
        active = [p for p in payees if not p.get("deleted")]
        print(f"Fetched {len(payees)} payee(s) total: {len(active)} active, {len(deleted)} deleted.")
        if deleted:
            print(f"Example deleted payee: {deleted[0]!r}")
        else:
            print("No deleted payees found yet in this budget (expected on a first run).")
        if active:
            print(f"Example active payee: {active[0]!r}")

        print("\n--- Phase 2: create a throwaway transaction under a unique test payee ---")
        test_import_id = f"{TEST_IMPORT_ID_PREFIX}{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
        tx = {
            "account_id": account_id,
            "date": datetime.now(UTC).date().isoformat(),
            "amount": -1000,
            "payee_name": TEST_PAYEE_NAME,
            "memo": "Safe to delete - created by scripts/verify_ynab_payee_deletion.py",
            "cleared": "uncleared",
            "approved": False,
            "import_id": test_import_id,
        }
        created = await ynab_client.create_transactions(http_client, pat, budget_id, [tx])
        transaction_ids = created.get("transaction_ids", [])
        if not transaction_ids:
            print(
                "FAIL: create_transactions reported no new transaction id (unexpected - "
                f"this test uses a fresh import_id every run). Response: {created!r}"
            )
            return
        created_transactions = created.get("transactions", [])
        payee_id = None
        for t in created_transactions:
            if t.get("id") == transaction_ids[0]:
                payee_id = t.get("payee_id")
                break
        if payee_id is None:
            print(
                "FAIL: could not read payee_id off the create response - "
                f"response: {created!r}"
            )
            return
        print(f"OK - created test transaction, payee_id={payee_id!r}, payee_name={TEST_PAYEE_NAME!r}")

        print(
            "\n--- Manual step required ---\n"
            f"In the YNAB web or mobile app, delete or merge the payee "
            f"{TEST_PAYEE_NAME!r} (payee_id={payee_id!r}) in budget alias "
            f"{budget_alias!r}. Merging it into any other payee, or deleting it "
            "outright, both count - either action removes it from the payee "
            "picker but should leave the raw record present with `deleted: "
            "true` when fetched again."
        )
        input("Press Enter once you've done this... ")

        print("\n--- Phase 3: re-fetching payees to confirm `deleted: true` ---")
        payees_after = await ynab_client.get_payees(http_client, pat, budget_id)
        match = next((p for p in payees_after if p.get("id") == payee_id), None)
        if match is None:
            print(
                f"FAIL: payee_id={payee_id!r} is no longer present in the payee list at all - "
                "the 'absence never means deletion' assumption reconcile_payee_mappings() "
                "relies on is WRONG. Do not trust deleted-only detection without redesigning "
                "that method first."
            )
            return
        if match.get("deleted"):
            print(
                f"PASS: payee_id={payee_id!r} is still present with deleted=true "
                f"({match!r}). reconcile_payee_mappings()'s explicit-deleted-only "
                "detection is safe to trust."
            )
        else:
            print(
                f"FAIL: payee_id={payee_id!r} is still present but NOT marked deleted "
                f"({match!r}) - did the manual step above actually take effect yet? "
                "YNAB may need a moment, or the merge/delete didn't go through."
            )


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
