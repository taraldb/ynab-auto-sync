#!/usr/bin/env python3
"""Live verification for how YNAB's BULK payee list (GET
{RESOURCE_PATH}/{budget_id}/payees) behaves for a deleted payee.

**Already run once and CONFIRMED**: a real, human-deleted payee is OMITTED
from this bulk list entirely, rather than kept present with `deleted: true`
as originally assumed (that assumption was borrowed from this project's own
GET .../accounts, which DOES keep closed/deleted accounts in its response,
and from sibling project ../ynab-auto-bank's identical stance for payees -
both turned out not to generalize here). Independently reconfirmed via
scripts/verify_ynab_payee_get_by_id.py, which showed the same deleted
payee_id reliably 404s on a direct GET .../payees/{payee_id} lookup - a
completely different mechanism (no bulk-list scanning, no name-matching)
that corroborates the same conclusion.

Because of this, SyncEngine.reconcile_payee_mappings() does NOT use this
bulk list for detection at all - it checks each of its own cached
payee_ids individually via ynab_client.get_payee() instead (see that
engine method's docstring, StateDB.delete_payee_mappings_for_ids's
docstring, and CLAUDE.md's "Payee-mapping reconcile pass" section for the
full history). This script is kept only in case YNAB's bulk-list behavior
is ever worth re-checking (e.g. after a YNAB API change) - it is NOT a
gating check the reconcile design currently depends on.

Two phases:

  1. Automatic: fetches the target budget's real payee list and reports how
     many already carry `deleted: true` vs `false`, printing one full raw
     example of each kind found (or "none found" for deleted, which is
     expected given the confirmed behavior above).
  2. Manual: creates one throwaway transaction under a unique, timestamped
     test payee name (a fixed name previously caused several same-named
     payees to pile up across repeated runs, making them hard to tell
     apart in the YNAB UI - fixed), prints that payee's id, then asks you
     to go delete or merge that payee by hand in the YNAB web/mobile app
     (YNAB has no payee-deletion/merge endpoint reachable from this app -
     it can only be triggered by a human, never scripted). Press Enter
     once done, and the script re-fetches the bulk list to check whether
     that exact id is present/absent/flagged.

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

TEST_PAYEE_NAME_PREFIX = "ynab-auto-sync payee-deletion-verification"
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
        run_suffix = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        test_import_id = f"{TEST_IMPORT_ID_PREFIX}{run_suffix}"
        # A timestamp suffix, not a fixed name: re-running this script
        # previously created a fresh payee under the exact same name every
        # time (YNAB's exact-string payee matching only reuses an existing
        # payee for an unchanged name, but a PRIOR run's payee may already
        # be deleted by the time this one runs, so a repeat name doesn't
        # reliably collide with a still-active one anyway) - several
        # same-named payees piling up in the real payee list made it
        # genuinely hard to tell which one in the YNAB UI corresponded to
        # which run. A unique name per run removes that ambiguity entirely.
        test_payee_name = f"{TEST_PAYEE_NAME_PREFIX} {run_suffix}"
        tx = {
            "account_id": account_id,
            "date": datetime.now(UTC).date().isoformat(),
            "amount": -1000,
            "payee_name": test_payee_name,
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
        print(f"OK - created test transaction, payee_id={payee_id!r}, payee_name={test_payee_name!r}")

        print(
            "\n--- Manual step required ---\n"
            f"In the YNAB web or mobile app, delete or merge the payee "
            f"{test_payee_name!r} (payee_id={payee_id!r}) in budget alias "
            f"{budget_alias!r}. Merging it into any other payee, or deleting it "
            "outright, both count."
        )
        input("Press Enter once you've done this... ")

        print("\n--- Phase 3: re-fetching payees to check the bulk list ---")
        payees_after = await ynab_client.get_payees(http_client, pat, budget_id)
        match = next((p for p in payees_after if p.get("id") == payee_id), None)
        if match is None:
            print(
                f"CONFIRMED (again): payee_id={payee_id!r} is absent from the bulk payee "
                "list entirely, rather than present with deleted=true. This matches the "
                "already-confirmed real behavior (see scripts/verify_ynab_payee_get_by_id.py, "
                "which independently confirmed the same id 404s on a direct per-payee GET) - "
                "reconcile_payee_mappings() no longer scans this bulk list at all, precisely "
                "because it can't reliably distinguish 'deleted' from 'any other reason it's "
                "momentarily absent'. It instead checks each cached payee_id directly via "
                "ynab_client.get_payee()."
            )
            return
        if match.get("deleted"):
            print(
                f"UNEXPECTED: payee_id={payee_id!r} is present in the bulk list WITH "
                f"deleted=true ({match!r}) - this contradicts the previously-confirmed "
                "behavior (deleted payees were fully absent, not flagged). If reproduced "
                "consistently, YNAB's behavior may have changed - worth re-examining "
                "whether the bulk list can be trusted again."
            )
        else:
            print(
                f"payee_id={payee_id!r} is still present and NOT marked deleted "
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
