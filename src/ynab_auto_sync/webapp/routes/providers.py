from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from ynab_auto_sync.config import AppConfig
from ynab_auto_sync.providers.base import ProviderAuthRequiredError, TransactionProvider
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp.deps import get_config, get_db, get_providers
from ynab_auto_sync.ynab import client as ynab_client

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/providers")
async def list_providers(
    force_refresh: bool = False,
    providers: dict[str, TransactionProvider] = Depends(get_providers),
    db: StateDB = Depends(get_db),
) -> list[dict[str, Any]]:
    """One entry per configured provider, each carrying its own accounts
    (flagged `mapped` against the current account_mappings table) - the
    drag source side of the drag-drop mapping UI.

    A provider that needs re-authentication, or that fails for any other
    reason, must not take down this whole response - the UI still needs to
    render every OTHER provider's accounts, and needs to tell the user
    specifically which provider is broken and how.

    force_refresh is passed straight through to provider.list_accounts() -
    a provider that caches internally (e.g. SpareBank1Provider's TTL cache)
    uses it to bypass that cache; a provider that never caches just ignores
    it. Wired from the GUI's explicit "Refresh" button so that one action
    still always gets live data, even though the normal per-tab-visit fetch
    now serves from cache.
    """
    mapped_keys = {(m["provider"], m["provider_account_id"]) for m in db.list_mappings()}

    result: list[dict[str, Any]] = []
    for name, provider in providers.items():
        entry: dict[str, Any] = {
            # `name` is the provider's CONFIG KEY, and is exactly what
            # account_mappings.provider stores - it is what a client must
            # send back when creating a mapping. `type` is the
            # implementation type, for display only: two connections to
            # different banks in the same alliance share a type but must
            # never be conflated, so the type is not a usable identifier.
            "name": name,
            "type": provider.type_name(),
            "auth_required": False,
            "error": None,
            "accounts": [],
        }
        try:
            accounts = await provider.list_accounts(force_refresh=force_refresh)
        except ProviderAuthRequiredError:
            entry["auth_required"] = True
            result.append(entry)
            continue
        except Exception as e:  # reported per-provider on the entry, never a 500
            logger.exception("Provider %r failed to list accounts", name)
            entry["error"] = str(e)
            result.append(entry)
            continue

        entry["accounts"] = [
            {
                "provider_account_id": account.provider_account_id,
                "display_name": account.display_name,
                "account_type": account.account_type,
                "currency": account.currency,
                "mapped": (name, account.provider_account_id) in mapped_keys,
            }
            for account in accounts
        ]
        result.append(entry)
    return result


@router.get("/api/ynab/accounts")
async def list_ynab_accounts(config: AppConfig = Depends(get_config)) -> list[dict[str, Any]]:
    """Every configured YNAB budget's accounts - the drop targets the
    mapping UI offers when a provider account is dragged onto YNAB.
    """
    result: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30) as http_client:
        for alias, budget_id in config.ynab.budgets.items():
            accounts = await ynab_client.get_accounts(
                http_client, config.ynab.personal_access_token, budget_id
            )
            result.append(
                {
                    "budget_id": budget_id,
                    "alias": alias,
                    "accounts": [
                        {
                            "id": account["id"],
                            "name": account["name"],
                            "type": account["type"],
                            "on_budget": account["on_budget"],
                        }
                        for account in accounts
                    ],
                }
            )
    return result
