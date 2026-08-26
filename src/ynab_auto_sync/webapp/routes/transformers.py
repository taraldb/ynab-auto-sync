from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ynab_auto_sync.config import AppConfig
from ynab_auto_sync.sync.file_import.registry import list_transformer_names
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp.deps import get_config, get_db, get_ynab_accounts_cache
from ynab_auto_sync.ynab.client import YnabAccountsCache

router = APIRouter()


class TransformerDefaultBudgetUpdate(BaseModel):
    default_ynab_budget_id: str | None = None
    default_ynab_account_id: str | None = None


def _to_response(
    config: AppConfig, name: str, defaults: dict[str, dict[str, str | None]]
) -> dict[str, Any]:
    default = defaults.get(name)
    budget_id = default["ynab_budget_id"] if default else None
    account_id = default["ynab_account_id"] if default else None
    alias = None
    if budget_id is not None:
        for a, bid in config.ynab.budgets.items():
            if bid == budget_id:
                alias = a
                break
    return {
        "name": name,
        "default_ynab_budget_id": budget_id,
        "default_ynab_budget_alias": alias,
        "default_ynab_account_id": account_id,
    }


@router.get("/api/transformers")
async def list_transformers(
    config: AppConfig = Depends(get_config),
    db: StateDB = Depends(get_db),
) -> list[dict[str, Any]]:
    defaults = db.list_transformer_default_budgets()
    return [_to_response(config, name, defaults) for name in list_transformer_names()]


@router.patch("/api/transformers/{name}")
async def update_transformer_default_budget(
    name: str,
    body: TransformerDefaultBudgetUpdate,
    config: AppConfig = Depends(get_config),
    db: StateDB = Depends(get_db),
    cache: YnabAccountsCache = Depends(get_ynab_accounts_cache),
) -> dict[str, Any]:
    if name not in list_transformer_names():
        raise HTTPException(status_code=404, detail=f"no transformer named {name!r}")

    if body.default_ynab_budget_id is None:
        await db.clear_transformer_default_budget(name)
    else:
        if body.default_ynab_budget_id not in config.ynab.budgets.values():
            available = ", ".join(sorted(config.ynab.budgets.values())) or "(none configured)"
            raise HTTPException(
                status_code=422,
                detail=(
                    f"ynab_budget_id {body.default_ynab_budget_id!r} is not one of the "
                    f"configured YNAB budgets - available: {available}"
                ),
            )
        # If an account is being set, validate it belongs to the given budget
        if body.default_ynab_account_id is not None:
            try:
                async with httpx.AsyncClient(timeout=30) as http_client:
                    accounts = await cache.get_accounts(
                        http_client,
                        config.ynab.personal_access_token,
                        body.default_ynab_budget_id,
                    )
                account_ids = {a["id"] for a in accounts}
                if body.default_ynab_account_id not in account_ids:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"ynab_account_id {body.default_ynab_account_id!r} does not "
                            f"belong to budget {body.default_ynab_budget_id!r}"
                        ),
                    )
            except httpx.HTTPStatusError as e:
                raise HTTPException(
                    status_code=e.response.status_code,
                    detail=f"Failed to validate account against YNAB: {e.response.text}",
                ) from e
        await db.set_transformer_default_budget(
            name, body.default_ynab_budget_id, body.default_ynab_account_id
        )

    defaults = db.list_transformer_default_budgets()
    return _to_response(config, name, defaults)
