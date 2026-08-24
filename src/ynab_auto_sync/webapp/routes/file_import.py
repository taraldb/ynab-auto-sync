from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from ynab_auto_sync.sync.engine import SyncEngine
from ynab_auto_sync.sync.file_import.base import RowTransformError
from ynab_auto_sync.sync.file_import.parsing import parse_file
from ynab_auto_sync.sync.file_import.registry import detect_transformer
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp import import_messages
from ynab_auto_sync.webapp.deps import get_db, get_engine

logger = logging.getLogger(__name__)

router = APIRouter()


def _resolve_account(
    db: StateDB, transformer_name: str, account_key: str | None
) -> dict[str, Any]:
    if account_key:
        for mapping in db.list_mappings():
            if mapping["provider_account_id"] == account_key:
                return mapping
        raise HTTPException(
            status_code=422,
            detail=f"No configured account with key {account_key!r}.",
        )

    # Only an enabled mapping can auto-match: a disabled one shouldn't
    # silently receive an import just because it's the only mapping whose
    # import_source_name matches - the explicit account_key override above
    # is still available for that case.
    matches = [
        m
        for m in db.list_mappings(enabled_only=True)
        if m["import_source_name"] == transformer_name
    ]
    if len(matches) == 1:
        return matches[0]
    raise HTTPException(status_code=422, detail=import_messages.no_account_resolved())


@router.post("/api/import")
async def import_file(
    file: UploadFile = File(...),
    dry_run: bool = Form(True),
    account_key: str | None = Form(None),
    db: StateDB = Depends(get_db),
    engine: SyncEngine = Depends(get_engine),
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

    account = _resolve_account(db, transformer.name(), account_key)

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
                "status": row.status,
            }
            for row in result.rows
        ],
        "summary": summary,
        "committed": result.committed,
    }
