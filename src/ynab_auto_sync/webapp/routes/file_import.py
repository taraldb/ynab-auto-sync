from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from ynab_auto_sync.config import AppConfig
from ynab_auto_sync.sync.engine import SyncEngine
from ynab_auto_sync.sync.file_import.base import RowTransformError
from ynab_auto_sync.sync.file_import.parsing import parse_file
from ynab_auto_sync.sync.file_import.registry import detect_transformer
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp import import_messages
from ynab_auto_sync.webapp.deps import (
    get_config,
    get_db,
    get_engine,
    get_ynab_accounts_cache,
)
from ynab_auto_sync.ynab.client import YnabAccountsCache

logger = logging.getLogger(__name__)

router = APIRouter()


async def _resolve_account(
    db: StateDB,
    config: AppConfig,
    cache: YnabAccountsCache,
    transformer_name: str,
    account_key: str | None,
    ynab_budget_id: str | None,
    ynab_account_id: str | None,
) -> dict[str, Any]:
    # 1. Explicit direct account override
    if ynab_account_id and ynab_budget_id:
        if ynab_budget_id not in config.ynab.budgets.values():
            available = ", ".join(sorted(config.ynab.budgets.values())) or "(none configured)"
            raise HTTPException(
                status_code=422,
                detail=f"Budget {ynab_budget_id!r} not configured - available: {available}",
            )
        try:
            async with httpx.AsyncClient(timeout=30) as http_client:
                accounts = await cache.get_accounts(
                    http_client,
                    config.ynab.personal_access_token,
                    ynab_budget_id,
                )
            account_list = {a["id"]: a for a in accounts}
            if ynab_account_id not in account_list:
                raise HTTPException(
                    status_code=422,
                    detail=f"Account {ynab_account_id!r} not found in budget {ynab_budget_id!r}",
                )
            account_name = account_list[ynab_account_id]["name"]
            return {
                "provider_account_id": f"unmapped:{ynab_account_id}",
                "display_name": account_name,
                "ynab_account_id": ynab_account_id,
                "ynab_budget_id": ynab_budget_id,
                "provider": "unmapped",
                "enabled": True,
            }
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=e.response.status_code,
                detail=f"Failed to fetch account from YNAB: {e.response.text}",
            ) from e

    # 2. Explicit account_key (mapped account)
    if account_key:
        for mapping in db.list_mappings():
            if mapping["provider_account_id"] == account_key:
                return mapping
        raise HTTPException(
            status_code=422,
            detail=f"No configured account with key {account_key!r}.",
        )

    # 3. Auto-match by import_source_name on enabled mapping
    matches = [
        m
        for m in db.list_mappings(enabled_only=True)
        if m["import_source_name"] == transformer_name
    ]
    if len(matches) == 1:
        return matches[0]

    # 4. Transformer default account (if set)
    defaults = db.list_transformer_default_budgets()
    default = defaults.get(transformer_name)
    if default and default.get("ynab_account_id"):
        default_budget_id = default["ynab_budget_id"]
        default_account_id = default["ynab_account_id"]
        try:
            async with httpx.AsyncClient(timeout=30) as http_client:
                accounts = await cache.get_accounts(
                    http_client,
                    config.ynab.personal_access_token,
                    default_budget_id,
                )
            account_list = {a["id"]: a for a in accounts}
            if default_account_id in account_list:
                account_name = account_list[default_account_id]["name"]
                return {
                    "provider_account_id": f"transformer_default:{transformer_name}",
                    "display_name": account_name,
                    "ynab_account_id": default_account_id,
                    "ynab_budget_id": default_budget_id,
                    "provider": "file",
                    "enabled": True,
                }
        except httpx.HTTPStatusError:
            # If the default account is no longer valid, fall through to error
            pass

    raise HTTPException(
        status_code=422,
        detail={"message": import_messages.no_account_resolved(), "transformer": transformer_name},
    )


@router.post("/api/import")
async def import_file(
    file: UploadFile = File(...),
    dry_run: bool = Form(True),
    account_key: str | None = Form(None),
    ynab_budget_id: str | None = Form(None),
    ynab_account_id: str | None = Form(None),
    config: AppConfig = Depends(get_config),
    db: StateDB = Depends(get_db),
    engine: SyncEngine = Depends(get_engine),
    cache: YnabAccountsCache = Depends(get_ynab_accounts_cache),
) -> dict[str, Any]:
    """Always re-parses the uploaded file from scratch, on every call -
    dry-run preview and confirmed commit alike. There is no server-side
    session/draft state for an in-progress import: the frontend's two-phase
    preview-then-confirm flow works by simply resubmitting the same file a
    second time with dry_run=false, which keeps this route (and the whole
    upload flow) stateless and safe to retry.
    """
    data = await file.read()
    try:
        headers, raw_rows = parse_file(file.filename or "", data)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=import_messages.unrecognized_format()
        ) from None

    transformer = detect_transformer(headers)
    if transformer is None:
        raise HTTPException(status_code=422, detail=import_messages.unrecognized_format())

    account = await _resolve_account(
        db,
        config,
        cache,
        transformer.name(),
        account_key,
        ynab_budget_id,
        ynab_account_id,
    )

    try:
        rows = transformer.transform(raw_rows)
    except RowTransformError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    try:
        result = await engine.import_file_rows(
            ynab_account_id=account["ynab_account_id"],
            ynab_budget_id=account["ynab_budget_id"],
            account_key=account["provider_account_id"],
            rows=rows,
            dry_run=dry_run,
        )
    except (httpx.HTTPStatusError, httpx.TransportError):
        # Unlike the scheduled/live-poll path (see scheduler.py's
        # retry/backoff), a file import is a single user-initiated request -
        # there's no background loop to retry it in, so a YNAB-side failure
        # is reported straight to the user rather than queued for later.
        # Nothing was imported: create_transactions raises before any row
        # is recorded (see engine.py's _record_created).
        logger.exception("YNAB submission failed during file import")
        raise HTTPException(
            status_code=503, detail=import_messages.ynab_unavailable()
        ) from None

    # "errors" (plural) to match frontend/src/api/client.ts's ImportSummary -
    # this used to be "error" (singular) here, a real bug: the frontend's
    # error count always rendered undefined because the keys never matched.
    summary = {"new": 0, "duplicate": 0, "errors": 0}
    for row in result.rows:
        key = "errors" if row.status == "error" else row.status
        summary[key] = summary.get(key, 0) + 1

    return {
        "transformer": transformer.name(),
        "account": {
            "key": account["provider_account_id"],
            "display_name": account["display_name"] or account["provider_account_id"],
            "ynab_account_id": account["ynab_account_id"],
            "ynab_budget": account["ynab_budget_id"],
        },
        "rows": [
            {
                "row_index": row.row_index,
                "date": row.date,
                "amount_milliunits": row.amount_milliunits,
                "payee_name": row.payee_name,
                "memo": row.memo,
                "cleared": row.cleared,
                "status": row.status,
            }
            for row in result.rows
        ],
        "summary": summary,
        "committed": result.committed,
    }
