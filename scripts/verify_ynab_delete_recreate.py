#!/usr/bin/env python3
"""Live verification for the manual-transaction-matching feature's actual
design (see CLAUDE.md's "Manual-transaction matching" section): since
PATCHing import_id onto an existing transaction is a confirmed no-op
(scripts/verify_ynab_import_id_patch.py), a matched manual transaction is
instead DELETED and RECREATED with a fresh import_id, copying over the
fields the user actually chose (payee_id, category_id, approved, memo,
flag_color) so nothing about their categorization is lost.

Confirms, against the real API:
  1. DELETE .../transactions/{id} actually removes the transaction (rather
     than erroring, or being a no-op).
  2. A fresh create_transactions call, built from the deleted transaction's
     own field values plus a corrected date/amount and a real import_id,
     round-trips every copied field correctly (payee_id, category_id,
     approved, memo, flag_color).
  3. The new transaction's import_id DOES get real dedup protection this
     time (unlike the PATCH approach) - resubmitting it is reported as a
     duplicate, not a second transaction.

Real surprise found while building this: YNAB's create_transactions has its
OWN native transaction-matching, triggered whenever the submitted
transaction carries an import_id and an existing transaction on the same
account/amount is still present (regardless of payee text - confirmed it
matches even across completely different payee strings). When that fires,
the response's `transactions` array contains BOTH the pre-existing
transaction (mutated: `approved` flipped to false, `matched_transaction_id`
set) AND the genuinely new one (also `matched_transaction_id`-linked back)
- in THAT order, ahead of the new row. Never index transaction_ids[0] /
transactions[0] positionally because of this (same caution CLAUDE.md
already documents for transfers) - always look up the created row by its
own submitted import_id, exactly like engine.py's _record_created() does.
This script does that below. It's harmless for the create-THEN-delete flow
this script (and the real feature) uses, since the original gets deleted
unconditionally right after anyway - but it would have silently produced a
wrong comparison here if not accounted for.

This creates and deletes real transactions in the budget you specify. Not
idempotent by design (each run creates a fresh "original" to delete) - safe
to run repeatedly, just leaves one new tracked test transaction each time;
delete it manually afterward if you want the budget clean.

Usage:
    python scripts/verify_ynab_delete_recreate.py --account-id <ynab_account_id> --category-id <ynab_category_id>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ynab_auto_sync.config import load_config
from ynab_auto_sync.ynab import client as ynab_client


def _test_import_id() -> str:
    # import_id is burned forever once used (invariant 6) - a fixed literal
    # would make every re-run after the first fail with a false "FAIL"
    # (duplicate_import_ids, not a real problem). Unique per run instead.
    return f"SB1:verify-delrecreate-{int(datetime.now(UTC).timestamp())}"


async def verify(
    account_id: str, category_id: str, config_path: str, budget_alias: str | None
) -> None:
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
    test_import_id = _test_import_id()

    async with httpx.AsyncClient(timeout=30) as http_client:
        print("--- Step 1: creating the 'manual entry' to simulate ---")
        original_date = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        original_tx = {
            "account_id": account_id,
            "date": original_date,
            "amount": -463540,
            "payee_name": "Telia (manual verify)",
            "category_id": category_id,
            "memo": "user's own note - must survive the delete+recreate",
            "cleared": "cleared",
            "approved": True,
            "flag_color": "blue",
            # No import_id - simulating a manually-typed YNAB transaction.
        }
        created = await ynab_client.create_transactions(http_client, pat, budget_id, [original_tx])
        original_ids = created.get("transaction_ids", [])
        if not original_ids:
            print(f"FAIL: expected the 'manual' transaction to be created, got: {created!r}")
            return
        original_id = original_ids[0]
        original_full = created["transactions"][0]
        print(f"OK - created 'manual' transaction id={original_id!r}")
        print(f"  payee_id={original_full.get('payee_id')!r} category_id={original_full.get('category_id')!r} "
              f"approved={original_full.get('approved')!r} flag_color={original_full.get('flag_color')!r}")

        print("\n--- Step 2: building + creating the replacement FIRST (before any delete) ---")
        real_date = datetime.now(UTC).date().isoformat()  # simulates the real bank date
        replacement_tx = {
            "account_id": account_id,
            "date": real_date,
            "amount": original_full["amount"],  # exact match by construction of the real match rule
            "payee_id": original_full["payee_id"],
            "category_id": original_full["category_id"],
            "memo": original_full["memo"],
            "cleared": "cleared",
            "approved": original_full["approved"],
            "flag_color": original_full["flag_color"],
            "import_id": test_import_id,
        }
        replacement_result = await ynab_client.create_transactions(
            http_client, pat, budget_id, [replacement_tx]
        )
        # Never index positionally - see the module docstring's "real
        # surprise" note. Find OUR row by the import_id we submitted; YNAB's
        # own native matching may have echoed the still-live original back
        # in this same response too.
        replacement_full = next(
            (t for t in replacement_result.get("transactions", []) if t.get("import_id") == test_import_id),
            None,
        )
        if replacement_full is None:
            print(
                f"FAIL: replacement was not created (old 'manual' transaction {original_id!r} "
                f"left untouched, which is the safe failure mode): {replacement_result!r}"
            )
            return
        replacement_id = replacement_full["id"]
        print(f"OK - created replacement transaction id={replacement_id!r}")
        if len(replacement_result.get("transaction_ids", [])) > 1:
            print(
                "  (note: YNAB's native matching also echoed the still-live original "
                "transaction back in this response with matched_transaction_id set - "
                "expected, harmless, ignored here since we look up by import_id)"
            )

        fields_ok = (
            replacement_full.get("payee_id") == original_full.get("payee_id")
            and replacement_full.get("category_id") == original_full.get("category_id")
            and replacement_full.get("approved") == original_full.get("approved")
            and replacement_full.get("flag_color") == original_full.get("flag_color")
            and replacement_full.get("memo") == original_full.get("memo")
            and replacement_full.get("date") == real_date
        )
        if fields_ok:
            print("OK - every copied field round-tripped correctly, date updated to the real bank date")
        else:
            print("FAIL - one or more copied fields did not round-trip:")
            for field in ("payee_id", "category_id", "approved", "flag_color", "memo", "date"):
                print(f"  {field}: original={original_full.get(field)!r} replacement={replacement_full.get(field)!r}")

        print(f"\n--- Step 3: deleting the original 'manual' transaction {original_id!r} ---")
        try:
            delete_result = await ynab_client.delete_transaction(http_client, pat, budget_id, original_id)
            print(f"OK - DELETE succeeded: deleted={delete_result.get('transaction', {}).get('deleted')!r}")
        except httpx.HTTPStatusError as e:
            print(f"FAIL: delete_transaction raised {e.response.status_code}: {e.response.text}")
            return

        print("\n--- Step 4: confirming the replacement's import_id gets real dedup protection ---")
        probe_tx = dict(replacement_tx)
        probe_tx["payee_name"] = "Telia (manual verify) - dedup probe, should not be created"
        probe_tx.pop("payee_id", None)
        probe_result = await ynab_client.create_transactions(http_client, pat, budget_id, [probe_tx])
        created_new = len(probe_result.get("transaction_ids", [])) > 0
        reported_duplicate = test_import_id in probe_result.get("duplicate_import_ids", [])
        if reported_duplicate and not created_new:
            print("PASS: the replacement's import_id correctly protects against a future duplicate submission.")
        else:
            print(f"FAIL: expected a reported duplicate, got: {probe_result!r}")

        print(
            f"\nSummary: original={original_id!r} (deleted), replacement={replacement_id!r} "
            f"(tracked, real import_id). Safe to leave or delete manually."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, help="A real YNAB account_id in the target budget")
    parser.add_argument("--category-id", required=True, help="A real, spendable YNAB category_id in the target budget")
    parser.add_argument("--budget-alias", help="Alias from ynab.budgets to test; required if more than one is configured")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    asyncio.run(verify(args.account_id, args.category_id, args.config, args.budget_alias))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
