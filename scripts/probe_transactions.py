#!/usr/bin/env python3
"""Live diagnostic probe for the SpareBank1 transactions endpoint.

SpareBank1's saved developer-portal docs list the /transactions endpoints
but not their query parameters, pagination scheme, or exact response field
names. Run this once against a real account (after scripts/auth_setup.py
has produced valid tokens) and read the printed output to confirm:

  1. Does fromDate/toDate actually narrow results server-side, or does the
     API return everything regardless?
  2. What is the real, stable per-transaction identifier field (needed for
     import_id derivation in sync/transform.py)?
  3. Is there any pagination signal (a "next" link, a page/size echo, a
     total count) when a date range returns many rows?

Update src/ynab_auto_sync/providers/sparebank1/client.py's TX_PARAM_* constants and
sync/transform.py's id-field lookup based on what you find here, then
delete or ignore this script - it's a one-time discovery tool, not part of
the running service.

Usage:
    python scripts/probe_transactions.py --account-key <key> [--days 30]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ynab_auto_sync.config import load_config
from ynab_auto_sync.provider_setup import (
    resolve_sparebank1_provider,
    token_store_path,
)
from ynab_auto_sync.providers.sparebank1 import auth as sb1_auth
from ynab_auto_sync.providers.sparebank1 import client as sb1_client
from ynab_auto_sync.state import JsonStateStore


async def probe(
    account_key: str, days: int, config_path: str, state_dir: str, provider: str | None
) -> None:
    config = load_config(config_path)
    provider_name, provider_config = resolve_sparebank1_provider(config, provider)
    token_store = JsonStateStore(token_store_path(state_dir, provider_name))
    tokens = token_store.read()
    if not tokens:
        print("No tokens found - run scripts/auth_setup.py first.")
        return

    async with httpx.AsyncClient(timeout=30) as http_client:
        from ynab_auto_sync.providers.sparebank1.auth import TokenStore

        store = TokenStore(token_store, provider_config)
        access_token = await store.ensure_valid_access_token(http_client)

        since = datetime.now(UTC) - timedelta(days=days)

        print(f"--- Accounts (GET {sb1_client.ACCOUNTS_URL}) ---")
        accounts = await sb1_client.get_accounts(http_client, access_token)
        print(json.dumps(accounts, indent=2))
        print(f"({len(accounts)} account(s) total)\n")

        print(f"--- Transactions with fromDate param (since {since.date()}) ---")
        params_with_date = {
            sb1_client.TX_PARAM_ACCOUNT_KEY: account_key,
            sb1_client.TX_PARAM_FROM_DATE: since.date().isoformat(),
        }
        r1 = await http_client.get(
            sb1_client.TRANSACTIONS_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": sb1_auth.ACCEPT_HEADER,
            },
            params=params_with_date,
        )
        print(f"status={r1.status_code} url={r1.url}")
        body1 = r1.json() if r1.status_code == 200 else r1.text
        print(json.dumps(body1, indent=2)[:4000])

        print("\n--- Transactions with NO date params (compare row count) ---")
        r2 = await http_client.get(
            sb1_client.TRANSACTIONS_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": sb1_auth.ACCEPT_HEADER,
            },
            params={sb1_client.TX_PARAM_ACCOUNT_KEY: account_key},
        )
        print(f"status={r2.status_code} url={r2.url}")
        body2 = r2.json() if r2.status_code == 200 else r2.text
        count1 = len(sb1_client._unwrap_list(body1, "transactions")) if isinstance(body1, (list, dict)) else None
        count2 = len(sb1_client._unwrap_list(body2, "transactions")) if isinstance(body2, (list, dict)) else None
        print(f"row count with fromDate={count1}, row count without={count2}")
        if count1 == count2:
            print(
                "NOTE: identical counts - fromDate may not be filtering server-side. "
                "Client-side filtering in sync/engine.py remains mandatory regardless."
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-key", required=True)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument(
        "--provider",
        default=None,
        help="Which configured SpareBank1 provider to use "
             "(required only if several are configured)",
    )
    args = parser.parse_args()
    asyncio.run(probe(args.account_key, args.days, args.config, args.state_dir, args.provider))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
