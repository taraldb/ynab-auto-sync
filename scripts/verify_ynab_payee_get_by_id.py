#!/usr/bin/env python3
"""Follow-up to scripts/verify_ynab_payee_deletion.py, which just proved a
real assumption wrong: a payee deleted via the YNAB app does NOT stay in
GET {RESOURCE_PATH}/{budget_id}/payees with `deleted: true` - it disappears
from that list entirely. That falsifies the "absence never means deletion"
detection reconcile_payee_mappings() was built around.

This script checks the one remaining candidate for a safe, targeted
detection: YNAB's documented single-payee endpoint,
GET {RESOURCE_PATH}/{budget_id}/payees/{payee_id}. If that endpoint reliably
404s (or otherwise clearly signals "gone") for a genuinely deleted payee,
reconcile_payee_mappings() can be redesigned to check each of its own
cached payee_ids directly via this endpoint - a small, bounded set specific
to what we ourselves cached - instead of scanning the full bulk payee list
and hoping deletions show up there.

Fully automatic, no manual step needed: pass the payee_id of a payee you've
ALREADY deleted (e.g. the one left over from verify_ynab_payee_deletion.py's
last run) and this just does one GET and reports exactly what comes back.

Usage:
    python scripts/verify_ynab_payee_get_by_id.py --payee-id <already-deleted-payee-id>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ynab_auto_sync.config import load_config
from ynab_auto_sync.ynab.client import BASE_URL, RESOURCE_PATH


def _headers(personal_access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {personal_access_token}",
        "Content-Type": "application/json",
    }


async def verify(payee_id: str, config_path: str, budget_alias: str | None) -> None:
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
        url = f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/payees/{payee_id}"
        print(f"GET {url}")
        response = await http_client.get(url, headers=_headers(pat))
        print(f"\nStatus: {response.status_code}")
        print(f"Body: {response.text}")

        if response.status_code == 404:
            print(
                "\nRESULT: 404 for a genuinely deleted payee - a targeted single-payee "
                "GET reliably signals 'gone' via status code. Safe to redesign "
                "reconcile_payee_mappings() around this: for each cached payee_id, GET "
                "this endpoint and treat 404 as confirmed-deleted."
            )
        elif response.status_code == 200:
            try:
                payee = response.json()["data"]["payee"]
            except (KeyError, TypeError, ValueError):
                print("\nRESULT: 200 but unexpected body shape - inspect manually.")
                return
            print(f"\nParsed payee: {payee!r}")
            if payee.get("deleted"):
                print(
                    "\nRESULT: 200 with deleted: true - the single-payee endpoint DOES "
                    "return deleted payees with the flag set, just like the bulk list "
                    "doesn't. Safe to redesign reconcile_payee_mappings() around a "
                    "per-id GET + deleted:true check (same logic as before, just "
                    "targeted per-id instead of scanning the bulk list)."
                )
            else:
                print(
                    "\nRESULT: 200 with deleted: false for a payee you deleted - "
                    "unexpected. Either the deletion didn't fully propagate, or this "
                    "endpoint doesn't reflect deletion status at all. Investigate "
                    "before trusting this detection method."
                )
        else:
            print(f"\nRESULT: unexpected status {response.status_code} - investigate.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payee-id", required=True, help="A payee_id you've already deleted in YNAB")
    parser.add_argument("--budget-alias", help="Alias from ynab.budgets to test; required if more than one is configured")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    asyncio.run(verify(args.payee_id, args.config, args.budget_alias))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
