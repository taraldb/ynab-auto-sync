"""Shared test helpers.

Before account mappings moved into SQLite, `test_engine.py` and
`test_webapp.py` each carried their own near-identical `make_config` /
`make_token_store` / `make_db`. Every engine and webapp test now also has to
get its mappings into the database first, so those helpers live here rather
than being duplicated (and drifting) in two places.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from ynab_auto_sync.config import AppConfig
from ynab_auto_sync.providers.sparebank1.auth import TokenStore
from ynab_auto_sync.providers.sparebank1.provider import SpareBank1Provider
from ynab_auto_sync.sync.engine import SyncEngine
from ynab_auto_sync.sync.state_db import StateDB

SPAREBANK1 = SpareBank1Provider.type_name()


def build_providers(
    http_client: httpx.AsyncClient,
    token_store: TokenStore,
    timezone: str = "Europe/Oslo",
    pending_import_enabled: bool = False,
) -> dict[str, Any]:
    """The provider map SyncEngine takes, wired the same way __main__ wires
    the real one."""
    return {
        SPAREBANK1: SpareBank1Provider(
            http_client, token_store, timezone, pending_import_enabled=pending_import_enabled
        )
    }


def make_engine(
    config: AppConfig,
    http_client: httpx.AsyncClient,
    token_store: TokenStore,
    db: StateDB,
) -> SyncEngine:
    return SyncEngine(
        config,
        http_client,
        db,
        build_providers(
            http_client,
            token_store,
            config.sync.timezone,
            pending_import_enabled=config.sync.pending_import_enabled,
        ),
    )


def seed_mappings_sync(db_path: Any, config: AppConfig) -> None:
    """Sync seeding for the webapp tests, which drive a sync TestClient.

    Deliberately seeds through a SEPARATE StateDB instance on the same file
    rather than the one the test will go on to use: StateDB guards writes
    with an asyncio.Lock, and a lock first acquired inside this throwaway
    asyncio.run() loop would then be reused from TestClient's own loop.
    A second connection to the same file sidesteps that entirely.
    """
    seeder = StateDB(db_path)
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(seed_mappings(seeder, config))
        else:
            # Some webapp tests are themselves async, so there's already a
            # loop on this thread and asyncio.run() would refuse. Give the
            # seed its own loop on its own thread instead.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, seed_mappings(seeder, config)).result()
    finally:
        seeder._conn.close()


async def seed_mappings(db: StateDB, config: AppConfig) -> None:
    """Seed account_mappings from a test config's `accounts` list.

    Mirrors the real one-time startup seed, including the part that
    actually matters: `provider_account_id` is the old
    `sparebank1_account_key` copied byte-for-byte, since that exact string
    prefixes every tracking key and feeds every burned import_id.
    """
    await db.seed_mappings_from_config(
        [
            {
                "provider": SPAREBANK1,
                "provider_account_id": account.sparebank1_account_key,
                "ynab_budget_id": config.ynab.budgets[account.ynab_budget],
                "ynab_account_id": account.ynab_account_id,
                "display_name": account.display_name,
                "import_source_name": account.import_source_name,
            }
            for account in config.accounts
        ]
    )
