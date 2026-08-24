from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp.deps import get_db

router = APIRouter()

AuditEventType = Literal["created", "updated", "duplicate", "skipped"]
AuditEventSortColumn = Literal[
    "occurred_at", "event_type", "source", "account_key", "payee_name", "memo",
    "amount_milliunits", "detail",
]
AuditEventSortDir = Literal["asc", "desc"]


@router.get("/api/audit-events")
async def list_audit_events(
    event_type: AuditEventType | None = None,
    account_key: str | None = None,
    include_skipped: bool = False,
    sort_by: AuditEventSortColumn = "occurred_at",
    sort_dir: AuditEventSortDir = "desc",
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: StateDB = Depends(get_db),
) -> dict[str, Any]:
    events, total = db.list_audit_events(
        event_type=event_type,
        account_key=account_key,
        include_skipped=include_skipped,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )
    return {
        "events": events,
        "total": total,
        "counts": db.count_audit_events_by_type(),
    }
