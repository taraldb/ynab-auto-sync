from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ynab_auto_sync.sync.engine import SyncEngine
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp.deps import get_db, get_engine

router = APIRouter()


@router.get("/api/deleted-transactions")
async def list_deleted_transactions(db: StateDB = Depends(get_db)) -> list[dict[str, Any]]:
    return db.list_deleted_transactions()


@router.post("/api/deleted-transactions/{tracking_key}/readd")
async def readd_deleted_transaction(
    tracking_key: str, engine: SyncEngine = Depends(get_engine)
) -> dict[str, str]:
    try:
        new_id = await engine.readd_deleted_transaction(tracking_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"new_ynab_transaction_id": new_id}
