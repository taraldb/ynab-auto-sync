#!/usr/bin/env python3
"""Live verification that YNAB's since_date parameter filters correctly,
independent of other parameters like last_knowledge_of_server=0.

Confirms (in order):
1. since_date combined with last_knowledge_of_server=0 still filters by date,
   not overridden by the latter.
2. A transaction dated exactly since_date is included (boundary/inclusivity).
3. Account-scoped endpoints also respect since_date.

Run this against the real test budget before trusting the since_date-bounding
implementation in the sync cycle.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://api.ynab.com/v1"
RESOURCE_PATH = "budgets"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def verify_since_date_filtering(
    token: str, budget_id: str, account_id: str
) -> tuple[int, int]:
    """Run verification checks. Returns (passed, failed) count."""
    async with httpx.AsyncClient() as client:
        passed = 0
        failed = 0

        # Step 1: Create test transactions
        logger.info("STEP 1: Creating test transactions...")
        today = datetime.now(UTC).date()
        old_date = today - timedelta(days=35)

        test_txs = [
            {
                "account_id": account_id,
                "date": today.isoformat(),
                "amount": -10000,
                "payee_name": "ynab-auto-sync since-date-verification TODAY",
                "import_id": f"verify-since-date-today-{today.isoformat()}",
                "cleared": "cleared",
            },
            {
                "account_id": account_id,
                "date": old_date.isoformat(),
                "amount": -20000,
                "payee_name": "ynab-auto-sync since-date-verification 35DAYS",
                "import_id": f"verify-since-date-35days-{old_date.isoformat()}",
                "cleared": "cleared",
            },
        ]

        response = await client.post(
            f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/transactions",
            headers=_headers(token),
            json={"transactions": test_txs},
        )
        response.raise_for_status()
        created_ids = response.json()["data"]["transaction_ids"]
        logger.info(f"  Created {len(created_ids)} test transactions: {created_ids}")

        # Step 2: Delete the older one
        logger.info("STEP 2: Deleting the 35-day-old transaction...")
        response = await client.delete(
            f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/transactions/{created_ids[1]}",
            headers=_headers(token),
        )
        response.raise_for_status()
        logger.info(f"  Deleted: {created_ids[1]}")

        # Step 3: Budget-wide endpoint + deleted visibility check
        logger.info("STEP 3: Budget-wide endpoint + deleted visibility...")
        cutoff_5days_ago = today - timedelta(days=5)
        cutoff_40days_ago = today - timedelta(days=40)

        # Should exclude the old deleted one (after the 5-day cutoff)
        response = await client.get(
            f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/transactions",
            headers=_headers(token),
            params={"since_date": cutoff_5days_ago.isoformat(), "last_knowledge_of_server": 0},
        )
        response.raise_for_status()
        txs_5day = response.json()["data"]["transactions"]
        old_in_5day = [t for t in txs_5day if t.get("import_id") == test_txs[1]["import_id"]]
        if not old_in_5day:
            logger.info(
                f"  ✓ PASS: Old transaction excluded when since_date={cutoff_5days_ago}"
            )
            passed += 1
        else:
            logger.error(
                f"  ✗ FAIL: Old transaction was included when it should be excluded. "
                f"since_date={cutoff_5days_ago}, but old tx dated {old_date}"
            )
            failed += 1

        # Should include the old deleted one (before the 40-day cutoff)
        response = await client.get(
            f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/transactions",
            headers=_headers(token),
            params={"since_date": cutoff_40days_ago.isoformat(), "last_knowledge_of_server": 0},
        )
        response.raise_for_status()
        txs_40day = response.json()["data"]["transactions"]
        old_in_40day = [t for t in txs_40day if t.get("import_id") == test_txs[1]["import_id"]]
        if old_in_40day and old_in_40day[0].get("deleted"):
            logger.info(
                f"  ✓ PASS: Old transaction included with deleted=true when "
                f"since_date={cutoff_40days_ago}"
            )
            passed += 1
        else:
            logger.error(
                f"  ✗ FAIL: Old transaction was not found or not deleted. "
                f"since_date={cutoff_40days_ago}, import_id={test_txs[1]['import_id']}"
            )
            failed += 1

        # Step 4: Boundary/inclusivity check
        logger.info("STEP 4: Boundary/inclusivity check...")
        response = await client.get(
            f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/transactions",
            headers=_headers(token),
            params={"since_date": today.isoformat(), "last_knowledge_of_server": 0},
        )
        response.raise_for_status()
        txs_today = response.json()["data"]["transactions"]
        today_tx = [t for t in txs_today if t.get("import_id") == test_txs[0]["import_id"]]
        if today_tx:
            logger.info(
                f"  ✓ PASS: Transaction dated exactly {today} is included when "
                f"since_date={today} (boundary is inclusive)"
            )
            passed += 1
        else:
            logger.error(
                f"  ✗ FAIL: Transaction dated {today} was excluded when "
                f"since_date={today} (boundary may be exclusive)"
            )
            failed += 1

        # Step 5: Account-scoped endpoint
        logger.info("STEP 5: Account-scoped endpoint...")
        response = await client.get(
            f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/accounts/{account_id}/transactions",
            headers=_headers(token),
            params={"since_date": cutoff_5days_ago.isoformat()},
        )
        response.raise_for_status()
        acct_txs_5day = response.json()["data"]["transactions"]
        old_in_acct_5day = [t for t in acct_txs_5day if t.get("import_id") == test_txs[1]["import_id"]]
        if not old_in_acct_5day:
            logger.info(
                f"  ✓ PASS: Account-scoped endpoint also respects since_date "
                f"({cutoff_5days_ago})"
            )
            passed += 1
        else:
            logger.error(
                f"  ✗ FAIL: Account-scoped endpoint did not filter by since_date "
                f"({cutoff_5days_ago})"
            )
            failed += 1

        # Clean up: delete the today transaction
        logger.info("STEP 6: Cleanup...")
        response = await client.delete(
            f"{BASE_URL}/{RESOURCE_PATH}/{budget_id}/transactions/{created_ids[0]}",
            headers=_headers(token),
        )
        response.raise_for_status()
        logger.info("  Cleanup complete")

        return passed, failed


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--token",
        required=True,
        help="YNAB personal access token",
    )
    parser.add_argument(
        "--budget-id",
        required=True,
        help="YNAB budget ID (the test budget)",
    )
    parser.add_argument(
        "--account-id",
        required=True,
        help="YNAB account ID within the test budget to use for test transactions",
    )

    args = parser.parse_args()

    passed, failed = await verify_since_date_filtering(args.token, args.budget_id, args.account_id)

    logger.info("")
    logger.info("=== RESULTS ===")
    logger.info(f"Passed: {passed}")
    logger.info(f"Failed: {failed}")

    if failed > 0:
        logger.error(
            "VERIFICATION FAILED. Some since_date assumptions do not hold. "
            "Do not merge the since_date-bounding change until these failures are understood."
        )
        sys.exit(1)
    else:
        logger.info("VERIFICATION PASSED. All since_date assumptions confirmed.")
        sys.exit(0)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
