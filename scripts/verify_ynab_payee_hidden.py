#!/usr/bin/env python3
"""Investigates whether YNAB payees have a "hidden" concept distinct from
`deleted` - neither `scripts/verify_ynab_payee_deletion.py` nor
`scripts/verify_ynab_payee_get_by_id.py` observed such a field in any real
payee dict printed so far (both print the full raw JSON YNAB returns, not a
narrowed subset, so if the field existed on an ordinary payee it would
already have shown up).

Three questions, in order:

  1. Does an ORDINARY payee's raw JSON (from both the bulk GET .../payees
     list and the single-payee GET .../payees/{id}) already carry a
     `hidden` key (or anything hidden-shaped) that we've simply never
     looked closely enough to notice?
  2. Can this app HIDE a payee itself, via the API? Best-effort: tries a
     PATCH .../payees/{payee_id} with {"hidden": true}. YNAB's public API
     has no documented payee-write endpoint at all (every prior
     verification in this project confirms payees are only ever mutated
     indirectly, via a transaction's payee_name/payee_id) - this is
     expected to fail, and the script must report that failure cleanly
     rather than crash, not treat it as some other kind of error.
  3. If the API attempt fails, falls back to asking you to try hiding the
     throwaway test payee by hand in the YNAB web/mobile app, if you can
     find such a control at all - this project doesn't know one exists
     yet either. Re-fetches afterward and reports the full raw dict again
     so any change (a new field appearing, a value flipping) is visible.

This is exploratory, not a gating check - unlike the two deletion scripts,
there's no existing design decision resting on this one's outcome.

Usage:
    python scripts/verify_ynab_payee_hidden.py --account-id <ynab_account_id>
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
from ynab_auto_sync.ynab.client import BASE_URL, RESOURCE_PATH


def _headers(personal_access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {personal_access_token}",
        "Content-Type": "application/json",
    }


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
        print("--- Question 1: does an ordinary payee's raw JSON carry a 'hidden' field? ---")
        payees = await ynab_client.get_payees(http_client, pat, budget_id)
        print(f"Fetched {len(payees)} payee(s) from the bulk list.")
        if payees:
            sample = payees[0]
            print(f"Full raw dict of one bulk-list payee: {sample!r}")
            print(f"Keys present: {sorted(sample.keys())}")
            hidden_keys = [k for k in sample if "hidden" in k.lower() or "visible" in k.lower()]
            print(f"Hidden-shaped keys found in bulk list: {hidden_keys or 'NONE'}")

            single = await ynab_client.get_payee(http_client, pat, budget_id, sample["id"])
            print(f"\nFull raw dict of the SAME payee via single-payee GET: {single!r}")
            if single is not None:
                print(f"Keys present: {sorted(single.keys())}")
                hidden_keys_single = [
                    k for k in single if "hidden" in k.lower() or "visible" in k.lower()
                ]
                print(f"Hidden-shaped keys found via single-payee GET: {hidden_keys_single or 'NONE'}")
        else:
            print("No payees in this budget at all - can't inspect an example.")

        print("\n--- Setting up a throwaway test payee for questions 2 and 3 ---")
        run_suffix = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        test_payee_name = f"ynab-auto-sync hidden-field-verification {run_suffix}"
        # YNAB caps import_id at 36 characters total (see e.g.
        # sync/import_ids.py's own comment on this) - "SB1:verify-hidden-"
        # (18 chars) + a 14-digit timestamp = 32, safely under the cap.
        test_import_id = f"SB1:verify-hidden-{run_suffix}"
        tx = {
            "account_id": account_id,
            "date": datetime.now(UTC).date().isoformat(),
            "amount": -1000,
            "payee_name": test_payee_name,
            "memo": "Safe to delete - created by scripts/verify_ynab_payee_hidden.py",
            "cleared": "uncleared",
            "approved": False,
            "import_id": test_import_id,
        }
        created = await ynab_client.create_transactions(http_client, pat, budget_id, [tx])
        transaction_ids = created.get("transaction_ids", [])
        if not transaction_ids:
            print(f"FAIL: create_transactions reported no new transaction id. Response: {created!r}")
            return
        payee_id = None
        for t in created.get("transactions", []):
            if t.get("id") == transaction_ids[0]:
                payee_id = t.get("payee_id")
                break
        if payee_id is None:
            print(f"FAIL: could not read payee_id off the create response: {created!r}")
            return
        print(f"OK - created test payee_id={payee_id!r}, payee_name={test_payee_name!r}")

        print("\n--- Question 2: can this app hide a payee itself, via the API? ---")
        patch_url = f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/payees/{payee_id}"
        response = await http_client.patch(
            patch_url, headers=_headers(pat), json={"payee": {"hidden": True}}
        )
        print(f"PATCH {patch_url}\nStatus: {response.status_code}\nBody: {response.text}")
        api_hide_worked = response.status_code == 200
        if api_hide_worked:
            print("UNEXPECTED: the PATCH succeeded! YNAB's API DOES support hiding a payee.")
        else:
            print(
                "As expected: no API support for hiding a payee directly - YNAB's public API "
                "has no payee-write endpoint (payees are only ever mutated indirectly through "
                "a transaction's payee_name/payee_id)."
            )

        if not api_hide_worked:
            print(
                "\n--- Question 3: manual step - can you hide this payee by hand in the app? ---\n"
                f"In the YNAB web or mobile app, look for any way to 'hide' the payee "
                f"{test_payee_name!r} (payee_id={payee_id!r}) in budget alias {budget_alias!r} - "
                "distinct from deleting or merging it. If you can't find such a control at all, "
                "just press Enter without doing anything; that's a valid, useful answer too."
            )
            input("Press Enter once you've tried (or confirmed there's no such option)... ")

        print("\n--- Re-fetching this payee to see what changed ---")
        after = await ynab_client.get_payee(http_client, pat, budget_id, payee_id)
        if after is None:
            print(
                f"payee_id={payee_id!r} is now gone (404) - whatever action was taken removed it "
                "the same way an outright delete does, not merely 'hidden'."
            )
        else:
            print(f"Full raw dict after: {after!r}")
            print(f"Keys present: {sorted(after.keys())}")
            print(f"deleted={after.get('deleted')!r}")


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
