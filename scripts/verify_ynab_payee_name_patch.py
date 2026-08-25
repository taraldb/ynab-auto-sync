#!/usr/bin/env python3
"""Live verification for the stale-cached-payee_id self-heal fix (see
CLAUDE.md's invariant 12 and "Manual-transaction matching" section history):

Confirmed live in production (2026-08-25) that submitting a create with a
cached payee_id that no longer resolves to a real payee (deleted/merged
since it was cached) does NOT error - YNAB returns 200 but silently drops
it, coming back with payee_id AND payee_name both null. The fix
(SyncEngine._record_created) reacts by PATCHing the just-created
transaction's payee_name back on. That PATCH's own effect has never been
confirmed against the live API - `update_transactions` is separately known
to silently ignore an `import_id` key (scripts/verify_ynab_import_id_patch.py),
so `payee_name` needs its own confirmation rather than assuming it behaves
differently.

Steps:
  1. Create a test transaction with payee_name set to one value.
  2. PATCH a DIFFERENT payee_name onto it.
  3. Read it back and confirm the new payee_name actually took (not
     silently ignored like import_id is).

Safe to leave the resulting test transaction in the budget afterward, or
delete it in the YNAB app.

Usage:
    python scripts/verify_ynab_payee_name_patch.py --account-id <ynab_account_id>
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

TEST_MEMO = "Safe to delete - created by scripts/verify_ynab_payee_name_patch.py"
ORIGINAL_PAYEE_NAME = "ynab-auto-sync payee-name-patch-verification (before)"
PATCHED_PAYEE_NAME = "ynab-auto-sync payee-name-patch-verification (after)"


async def _find_by_memo(
    http_client: httpx.AsyncClient, pat: str, budget_id: str, account_id: str
) -> dict | None:
    """Client-side scan for a previous run's test transaction (by memo, so
    re-running this script doesn't pile up duplicates)."""
    response = await http_client.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/{budget_id}"
        f"/accounts/{account_id}/transactions",
        headers={"Authorization": f"Bearer {pat}"},
    )
    response.raise_for_status()
    for tx in response.json()["data"]["transactions"]:
        if tx.get("memo") == TEST_MEMO and not tx.get("deleted"):
            return tx
    return None


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
        print(f"--- Step 1: creating (or recovering) a test transaction with payee_name={ORIGINAL_PAYEE_NAME!r} ---")
        existing = await _find_by_memo(http_client, pat, budget_id, account_id)
        if existing is not None:
            transaction_id = existing["id"]
            print(f"OK - reusing existing test transaction id={transaction_id!r}")
        else:
            tx = {
                "account_id": account_id,
                "date": datetime.now(UTC).date().isoformat(),
                "amount": -1000,
                "payee_name": ORIGINAL_PAYEE_NAME,
                "memo": TEST_MEMO,
                "cleared": "uncleared",
                "approved": False,
            }
            created = await ynab_client.create_transactions(http_client, pat, budget_id, [tx])
            transactions = created.get("transactions", [])
            if not transactions:
                print(f"FAIL: expected a created transaction in the response, got: {created!r}")
                return
            transaction_id = transactions[0]["id"]
            print(f"OK - created new transaction id={transaction_id!r}")

        print(f"\n--- Step 2: PATCHing payee_name={PATCHED_PAYEE_NAME!r} onto {transaction_id!r} ---")
        try:
            update_result = await ynab_client.update_transactions(
                http_client,
                pat,
                budget_id,
                [{"id": transaction_id, "payee_name": PATCHED_PAYEE_NAME}],
            )
            print(f"OK - PATCH succeeded, response: {update_result!r}")
        except httpx.HTTPStatusError as e:
            print(f"FAIL: update_transactions raised {e.response.status_code}: {e.response.text}")
            return

        print("\n--- Step 3: reading the transaction back to confirm the new payee_name took ---")
        response = await http_client.get(
            f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/{budget_id}"
            f"/transactions/{transaction_id}",
            headers={"Authorization": f"Bearer {pat}"},
        )
        response.raise_for_status()
        fetched = response.json()["data"]["transaction"]
        actual_payee_name = fetched.get("payee_name")

        if actual_payee_name == PATCHED_PAYEE_NAME:
            print(
                f"\nPASS: payee_name reads back as {actual_payee_name!r} - PATCHing "
                "payee_name onto an existing transaction actually takes effect, unlike "
                "import_id. The stale-payee_id self-heal fix's corrective PATCH is safe "
                "to rely on."
            )
        elif actual_payee_name == ORIGINAL_PAYEE_NAME:
            print(
                f"\nFAIL: payee_name still reads back as {actual_payee_name!r} - YNAB "
                "silently ignored the PATCH, the same way it does for import_id. The "
                "self-heal fix in SyncEngine._record_created needs a different approach "
                "(e.g. delete-and-recreate)."
            )
        else:
            print(
                f"\nUNEXPECTED: payee_name reads back as {actual_payee_name!r} - neither "
                f"the original ({ORIGINAL_PAYEE_NAME!r}) nor the patched "
                f"({PATCHED_PAYEE_NAME!r}) value. Investigate before trusting the fix."
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
