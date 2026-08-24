from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ynab_auto_sync.scheduler import Scheduler
from ynab_auto_sync.webapp.deps import get_scheduler

router = APIRouter()


@router.post("/api/sync-now")
async def sync_now(scheduler: Scheduler | None = Depends(get_scheduler)) -> dict[str, str]:
    """Trigger an out-of-cycle sync from the GUI, the same way MQTT's
    sync_now command does - short-circuits the cron wait via
    Scheduler.request_sync_now(). Only available when this app instance was
    actually wired to a running Scheduler (create_app's scheduler kwarg is
    optional, e.g. for tests that only exercise other routes)."""
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="No scheduler is wired into this app instance - sync-now is unavailable.",
        )
    scheduler.request_sync_now()
    return {"status": "requested"}
