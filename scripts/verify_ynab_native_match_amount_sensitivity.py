#!/usr/bin/env python3
"""Live verification: does YNAB's own native transaction-matching (see
scripts/verify_ynab_delete_recreate.py's docstring, and CLAUDE.md's
"Manual-transaction matching" section) depend on the replacement's amount
being an EXACT match to the transaction it replaces?

Real-world motivation (the "dogman" incident): before PENDING-import
existed, a BOOKED manual-match replacement used the real exact amount, and
the resulting transaction visibly looked "linked"/imported in the YNAB app.
Once PENDING-import went live, the tolerant manual-match replacement uses
the PENDING transaction's preauth (rounded, non-exact) amount instead - and
the result looked like a plain, unlinked manual entry even though it
genuinely carries a real import_id. This script isolates amount exactness
as the one variable to test that theory directly, holding account and date
gap constant across both scenarios.

Creates FOUR real transactions in the budget/account you specify (two
"manual" originals, two replacements) and deletes only the two originals
(mirroring the real feature's own create-then-delete behavior). The two
replacement transactions are deliberately LEFT in the budget so they can be
visually compared in the YNAB app afterward. Not idempotent - each run
creates fresh transactions with fresh import_ids.

Usage:
    python scripts/verify_ynab_native_match_amount_sensitivity.py --account-id <ynab_account_id> [--budget-alias <alias>]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ynab_auto_sync.config import load_config
from ynab_auto_sync.ynab import client as ynab_client


def _headers(pat: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}


def _test_import_id(label: str) -> str:
    # import_id is burned forever once used (invariant 6) - unique per run
    # so re-running this script doesn't collide with a prior run's ids.
    # YNAB caps import_id at 36 chars - keep this short (a longer id
    # caused a live 400 Bad Request while building this script).
    short_label = "exact" if label.startswith("exact") else "tol"
    ms = int(datetime.now(UTC).timestamp() * 1000) % 10_000_000_000
    return f"SB1:vas-{short_label}-{ms}"


async def _get_transaction_single(
    http_client: httpx.AsyncClient, pat: str, budget_id: str, transaction_id: str
) -> dict[str, Any] | None:
    """GET .../transactions/{id} directly. Found live (building this script)
    that this can 404 for a transaction that is NOT actually deleted, when
    it carries a matched_transaction_id pointing at a partner that WAS
    since deleted - a real, previously-undocumented YNAB API quirk. Returns
    None on any HTTP error instead of raising, so callers can compare this
    against the bulk-listing source of truth below."""
    response = await http_client.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/{budget_id}/transactions/{transaction_id}",
        headers=_headers(pat),
    )
    if response.status_code != 200:
        return None
    return response.json()["data"]["transaction"]


async def _get_transaction_bulk(
    http_client: httpx.AsyncClient, pat: str, budget_id: str, transaction_id: str
) -> dict[str, Any] | None:
    """Ground truth: the budget-wide, last_knowledge_of_server=0 listing
    (same endpoint find_transaction_including_deleted uses) includes
    deleted transactions with a `deleted` field, and - confirmed live -
    does NOT 404 for a transaction whose matched partner was deleted, even
    when the single-transaction GET above does."""
    response = await http_client.get(
        f"{ynab_client.BASE_URL}/{ynab_client.RESOURCE_PATH}/{budget_id}/transactions",
        headers=_headers(pat),
        params={"last_knowledge_of_server": 0},
    )
    response.raise_for_status()
    for transaction in response.json()["data"]["transactions"]:
        if transaction.get("id") == transaction_id:
            return transaction
    return None


async def _run_scenario(
    http_client: httpx.AsyncClient,
    pat: str,
    budget_id: str,
    account_id: str,
    *,
    label: str,
    original_amount: int,
    replacement_amount: int,
) -> dict[str, Any]:
    print(
        f"\n=== Scenario: {label} "
        f"(original={original_amount / 1000:.2f} kr, replacement={replacement_amount / 1000:.2f} kr) ==="
    )
    original_date = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()

    original_tx = {
        "account_id": account_id,
        "date": original_date,
        "amount": original_amount,
        "payee_name": f"Verify amtsens {label} (manual)",
        "memo": "created by verify_ynab_native_match_amount_sensitivity.py - safe to delete",
        "cleared": "cleared",
        "approved": True,
        # No import_id - simulating a manually-typed transaction.
    }
    created_original = await ynab_client.create_transactions(http_client, pat, budget_id, [original_tx])
    original_ids = created_original.get("transaction_ids", [])
    if not original_ids:
        print(f"FAIL: expected the 'manual' original to be created, got: {created_original!r}")
        return {}
    original_id = original_ids[0]
    print(f"Created 'manual' original: id={original_id!r} amount={original_amount}")

    import_id = _test_import_id(label)
    real_date = datetime.now(UTC).date().isoformat()
    replacement_tx = {
        "account_id": account_id,
        "date": real_date,
        "amount": replacement_amount,
        "payee_name": f"Verify amtsens {label} (replacement)",
        "cleared": "cleared",
        "approved": False,
        "import_id": import_id,
    }
    response = await ynab_client.create_transactions(http_client, pat, budget_id, [replacement_tx])

    # Never index positionally - YNAB's native matching can echo the
    # still-live original back in this same response (confirmed by
    # scripts/verify_ynab_delete_recreate.py). Look up our own row by its
    # own submitted import_id, exactly like engine.py's _record_created()
    # and _record_matched() already do.
    replacement_full = next(
        (t for t in response.get("transactions", []) if t.get("import_id") == import_id), None
    )
    echoed_original = next(
        (t for t in response.get("transactions", []) if t.get("id") == original_id), None
    )

    print(
        f"Response transactions: {len(response.get('transactions', []))} row(s), "
        f"transaction_ids={response.get('transaction_ids')}"
    )
    if replacement_full is None:
        print("FAIL: could not find our own replacement row by import_id in the response!")
        print(f"  full response: {response!r}")
        return {}
    replacement_id = replacement_full["id"]
    print(f"Replacement id={replacement_id!r}")
    print(
        f"  replacement: matched_transaction_id={replacement_full.get('matched_transaction_id')!r} "
        f"approved={replacement_full.get('approved')!r}"
    )
    if echoed_original is not None:
        print("  YNAB echoed the ORIGINAL back in this same response (native-match collision detected):")
        print(
            f"    original: matched_transaction_id={echoed_original.get('matched_transaction_id')!r} "
            f"approved={echoed_original.get('approved')!r}"
        )
    else:
        print("  original was NOT echoed back in this response (no native-match collision detected)")

    print(f"Deleting original {original_id!r}...")
    await ynab_client.delete_transaction(http_client, pat, budget_id, original_id)

    print(f"Re-fetching replacement {replacement_id!r} after original's deletion...")
    single = await _get_transaction_single(http_client, pat, budget_id, replacement_id)
    bulk = await _get_transaction_bulk(http_client, pat, budget_id, replacement_id)
    print(f"  single-transaction GET: {'200 OK' if single is not None else 'non-200 (see note above)'}")
    if bulk is None:
        print("  FAIL: replacement not found even in the bulk (ground-truth) listing!")
    else:
        print(
            f"  bulk listing (ground truth): deleted={bulk.get('deleted')!r} "
            f"matched_transaction_id={bulk.get('matched_transaction_id')!r} "
            f"approved={bulk.get('approved')!r} cleared={bulk.get('cleared')!r} "
            f"import_id={bulk.get('import_id')!r}"
        )
        if single is None and bulk.get("deleted") is False:
            print(
                "  NOTE: single-transaction GET 404'd for a transaction the bulk listing "
                "confirms is NOT deleted - likely because its matched_transaction_id points "
                "at a now-deleted partner. Trust the bulk listing, not the single GET, here."
            )

    return {
        "label": label,
        "replacement_id": replacement_id,
        "native_match_detected": echoed_original is not None,
        "matched_transaction_id_at_create": replacement_full.get("matched_transaction_id"),
        "matched_transaction_id_after_cleanup": bulk.get("matched_transaction_id") if bulk else None,
        "deleted_after_cleanup": bulk.get("deleted") if bulk else None,
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
    print(f"Using budget alias {budget_alias!r} -> {budget_id}, account {account_id!r}")

    async with httpx.AsyncClient(timeout=30) as http_client:
        # Mirrors the real "dogman" gap: 47.42 kr real amount vs 47.00 kr
        # preauth. Same original amount in both scenarios, only the
        # replacement's amount varies, to isolate that one variable.
        exact = await _run_scenario(
            http_client,
            pat,
            budget_id,
            account_id,
            label="exact-amount",
            original_amount=-474200,
            replacement_amount=-474200,
        )
        tolerant = await _run_scenario(
            http_client,
            pat,
            budget_id,
            account_id,
            label="tolerant-amount",
            original_amount=-474200,
            replacement_amount=-470000,
        )

        print("\n=== Summary ===")
        for result in (exact, tolerant):
            if not result:
                continue
            print(
                f"{result['label']}: native_match_detected_at_create={result['native_match_detected']!r}, "
                f"matched_transaction_id at create={result['matched_transaction_id_at_create']!r}, "
                f"after cleanup={result['matched_transaction_id_after_cleanup']!r}, "
                f"deleted_after_cleanup={result['deleted_after_cleanup']!r}"
            )
        print(
            "\nOpen the YNAB app now and compare the two 'Verify amtsens ... (replacement)' "
            "transactions in the register for this account - do they look different "
            "(icon, badge, approval state, anything)?"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, help="A real YNAB account_id in the target budget")
    parser.add_argument(
        "--budget-alias", help="Alias from ynab.budgets to test; required if more than one is configured"
    )
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()
    asyncio.run(verify(args.account_id, args.config, args.budget_alias))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
