#!/usr/bin/env python3
"""Live verification for YNAB's resource path and the import_id dedup guarantee.

Confirms two things this project's design depends on, per the plan:

  1. Whether YNAB's resource path is /v1/budgets/... (the long-established
     convention, and this codebase's default) or /v1/plans/... - if this
     script needs to fall back to /plans to get a 200, update
     src/ynab_auto_sync/ynab/client.py's RESOURCE_PATH constant.
  2. That submitting the same transaction (same import_id) twice results
     in a *duplicate*, not a second transaction - the core "no duplicates"
     guarantee this whole project relies on.

This creates ONE real, tiny transaction in the budget you specify (default
amount: 0 milliunits, dated today, payee "ynab-auto-sync verification" -
safe to leave, or delete it afterward in the YNAB app).

Usage:
    python scripts/verify_ynab.py --account-id <ynab_account_id>
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
        print(f"--- GET {ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH} ---")
        try:
            budgets = await ynab_client.get_budgets(http_client, pat)
            print(f"OK - resource path '{ynab_client.RESOURCE_PATH}' works.")
            print(f"Found {len(budgets)} budget(s): " + ", ".join(b.get("name", "?") for b in budgets))
            if budget_id not in {b.get("id") for b in budgets}:
                print(f"WARNING: configured budget_id={budget_id!r} not found in the list above.")
        except httpx.HTTPStatusError as e:
            print(f"FAILED with '{ynab_client.RESOURCE_PATH}': {e.response.status_code}")
            print("Try changing RESOURCE_PATH in src/ynab_auto_sync/ynab/client.py to 'plans' and re-run.")
            return

        print("\n--- Dedup test: creating the same transaction twice ---")
        tx = {
            "account_id": account_id,
            "date": datetime.now(UTC).date().isoformat(),
            "amount": 0,
            "payee_name": "ynab-auto-sync verification",
            "memo": "Safe to delete - created by scripts/verify_ynab.py",
            "cleared": "cleared",
            "approved": False,
            "import_id": "SB1:verify-dedup-test-000000000000",
        }

        first = await ynab_client.create_transactions(http_client, pat, budget_id, [tx])
        print(f"First call: created={len(first.get('transaction_ids', []))}, "
              f"duplicates={len(first.get('duplicate_import_ids', []))}")

        second = await ynab_client.create_transactions(http_client, pat, budget_id, [tx])
        print(f"Second call: created={len(second.get('transaction_ids', []))}, "
              f"duplicates={len(second.get('duplicate_import_ids', []))}")

        if second.get("duplicate_import_ids"):
            print("\nPASS: the second submission was reported as a duplicate, not created again.")
        else:
            print("\nUNEXPECTED: the second submission was not flagged as a duplicate - "
                  "investigate before relying on import_id for no-duplicates.")


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
