#!/usr/bin/env python3
"""Live verification for the manual-transaction-matching feature's core
assumption (see CLAUDE.md's "Manual-transaction matching" section):

Can PATCHing `import_id` onto an existing YNAB transaction that was
created with none give that transaction the SAME import_id-dedup
protection as one created with the id from the start (invariant 1)?

This has never been confirmed against the live API - every other use of
import_id in this codebase sets it at creation time. If this doesn't hold,
the manual-match design (attach import_id via PATCH to a pre-existing
manually-typed transaction) needs to change to something heavier
(delete-and-recreate) instead.

Steps:
  1. Create (or recover, for idempotent re-runs) a test transaction with
     NO import_id at all.
  2. PATCH an import_id onto it.
  3. Read it back via find_transaction_by_import_id to confirm the PATCH
     actually took (YNAB might silently ignore an import_id in an update
     payload - unconfirmed until now).
  4. Submit a SECOND, distinct transaction dict via create_transactions
     using that SAME import_id, and confirm YNAB reports it in
     duplicate_import_ids (not a newly created transaction) - the actual
     dedup guarantee this whole feature depends on.

Safe to leave the resulting test transaction in the budget afterward, or
delete it in the YNAB app.

Usage:
    python scripts/verify_ynab_import_id_patch.py --account-id <ynab_account_id>
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

TEST_MEMO = "Safe to delete - created by scripts/verify_ynab_import_id_patch.py"
TEST_IMPORT_ID = "SB1:verify-patch-test-0000000000"


async def _find_by_memo(
    http_client: httpx.AsyncClient, pat: str, budget_id: str, account_id: str
) -> dict | None:
    """Client-side scan for a previous run's test transaction (by memo, not
    import_id - the whole point of this transaction is that it starts with
    none), so re-running this script doesn't pile up duplicates."""
    response = await http_client.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/{budget_id}/accounts/{account_id}/transactions",
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
        print("--- Step 1: creating (or recovering) a test transaction with NO import_id ---")
        existing = await _find_by_memo(http_client, pat, budget_id, account_id)
        if existing is not None:
            transaction_id = existing["id"]
            print(f"OK - reusing existing test transaction id={transaction_id!r}")
        else:
            tx = {
                "account_id": account_id,
                "date": datetime.now(UTC).date().isoformat(),
                "amount": -1000,
                "payee_name": "ynab-auto-sync import-id-patch-verification",
                "memo": TEST_MEMO,
                "cleared": "uncleared",
                "approved": False,
                # Deliberately no "import_id" key at all - this is the
                # manually-typed-transaction scenario being simulated.
            }
            created = await ynab_client.create_transactions(http_client, pat, budget_id, [tx])
            transaction_ids = created.get("transaction_ids", [])
            if not transaction_ids:
                print(f"FAIL: expected a new transaction id, got: {created!r}")
                return
            transaction_id = transaction_ids[0]
            print(f"OK - created new transaction id={transaction_id!r}")

        print(f"\n--- Step 2: PATCHing import_id={TEST_IMPORT_ID!r} onto {transaction_id!r} ---")
        try:
            update_result = await ynab_client.update_transactions(
                http_client, pat, budget_id, [{"id": transaction_id, "import_id": TEST_IMPORT_ID}]
            )
            print(f"OK - PATCH succeeded, response: {update_result!r}")
        except httpx.HTTPStatusError as e:
            print(f"FAIL: update_transactions raised {e.response.status_code}: {e.response.text}")
            return

        print("\n--- Step 3: reading it back via find_transaction_by_import_id ---")
        fetched = await ynab_client.find_transaction_by_import_id(
            http_client, pat, budget_id, account_id, TEST_IMPORT_ID
        )
        if fetched is None:
            print(
                "FAIL: find_transaction_by_import_id found nothing - the PATCH did not "
                "actually set import_id (or YNAB silently ignored it)."
            )
            return
        if fetched["id"] != transaction_id:
            print(
                f"FAIL: found a DIFFERENT transaction ({fetched['id']!r}) with this "
                f"import_id than the one PATCHed ({transaction_id!r})."
            )
            return
        print(f"OK - import_id correctly attached to {transaction_id!r}")

        print(
            "\n--- Step 4: submitting a distinct transaction with the SAME import_id, "
            "expecting a reported duplicate, not a new transaction ---"
        )
        probe_tx = {
            "account_id": account_id,
            "date": datetime.now(UTC).date().isoformat(),
            "amount": -999000,  # deliberately different from the patched tx's -1000
            "payee_name": "ynab-auto-sync import-id-patch-verification (dedup probe)",
            "memo": "Should NEVER actually be created - see verify_ynab_import_id_patch.py",
            "cleared": "cleared",
            "approved": False,
            "import_id": TEST_IMPORT_ID,
        }
        probe_result = await ynab_client.create_transactions(http_client, pat, budget_id, [probe_tx])
        print(f"Response: {probe_result!r}")

        created_new = len(probe_result.get("transaction_ids", [])) > 0
        reported_duplicate = TEST_IMPORT_ID in probe_result.get("duplicate_import_ids", [])

        if reported_duplicate and not created_new:
            print(
                "\nPASS: a PATCH-attached import_id gets the same dedup protection as one "
                "set at creation time. The manual-match design's core assumption holds."
            )
        else:
            print(
                "\nFAIL: PATCHing import_id onto an existing transaction does NOT give it "
                "creation-time dedup protection - the manual-match design needs to change "
                "(e.g. delete-and-recreate instead of patch-in-place)."
            )
            if created_new:
                print(
                    "  A second, real transaction was created despite the shared import_id "
                    "- check the budget and delete it manually."
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
