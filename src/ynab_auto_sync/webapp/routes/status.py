from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends

from ynab_auto_sync.config import AppConfig
from ynab_auto_sync.cron import next_fire_at as compute_next_fire_at
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp.deps import get_config, get_db

router = APIRouter()

# Set by the Dockerfile's ARG/ENV APP_VERSION, itself set by
# .github/workflows/docker-publish.yml from the pushed git tag. Read once at
# import time - it's fixed for the life of the process, same as everything
# else this module treats as a constant. "dev" is what a non-Docker/local
# run gets, since there's no build step to stamp a version into.
_APP_VERSION = os.environ.get("APP_VERSION", "dev")
_BUILD_TIMESTAMP = os.environ.get("BUILD_TIMESTAMP", "unknown")


def build_status_payload(config: AppConfig, db: StateDB) -> dict[str, Any]:
    """The full GET /api/status shape - factored out so webapp/routes/ws.py
    can send the exact same payload as one `status_snapshot` message right
    after a websocket connects, without duplicating this assembly logic."""
    next_fire_at = compute_next_fire_at(config.sync.cron_expression, config.sync.timezone)

    # Mappings store the RESOLVED budget id, but this field is rendered
    # straight into the dashboard's accounts table, where a raw UUID is
    # useless to a human. Map it back to the config alias it came from,
    # falling back to the id itself if the alias has since been renamed or
    # removed from config.yaml (better a UUID than a blank cell).
    alias_by_budget_id = {
        budget_id: alias for alias, budget_id in config.ynab.budgets.items()
    }

    return {
        "run_metadata": db.read_run_metadata(),
        # Sourced from the account_mappings table now that mappings are
        # runtime-editable, not config.accounts - but the response key/shape
        # (accounts / key / display_name) is unchanged so
        # frontend/src/pages/Dashboard.tsx keeps working without a rewrite.
        "accounts": [
            {
                "key": mapping["provider_account_id"],
                "display_name": mapping["display_name"] or mapping["provider_account_id"],
                "ynab_budget": alias_by_budget_id.get(
                    mapping["ynab_budget_id"], mapping["ynab_budget_id"]
                ),
                "provider": mapping["provider"],
                "enabled": mapping["enabled"],
            }
            for mapping in db.list_mappings()
        ],
        "cron_expression": config.sync.cron_expression,
        "next_fire_at": next_fire_at.isoformat(),
        "version": _APP_VERSION,
        "build_timestamp": _BUILD_TIMESTAMP,
    }


@router.get("/api/status")
async def get_status(
    config: AppConfig = Depends(get_config), db: StateDB = Depends(get_db)
) -> dict[str, Any]:
    return build_status_payload(config, db)
