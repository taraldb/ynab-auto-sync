from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ynab_auto_sync.scheduler import Scheduler
from ynab_auto_sync.webapp.deps import get_scheduler

router = APIRouter()


class PauseRequest(BaseModel):
    paused: bool


@router.post("/api/pause")
async def set_pause(
    body: PauseRequest, scheduler: Scheduler | None = Depends(get_scheduler)
) -> dict[str, bool]:
    """Pause/resume scheduled syncing from the GUI, the same way MQTT's
    pause command does - goes through Scheduler.set_paused() so both paths
    share the exact same DB write + MQTT/HA state republish, never
    drifting. Only available when this app instance was actually wired to
    a running Scheduler (create_app's scheduler kwarg is optional, e.g. for
    tests that only exercise other routes) - mirrors sync_now.py's guard."""
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="No scheduler is wired into this app instance - pause is unavailable.",
        )
    await scheduler.set_paused(body.paused)
    return {"paused": body.paused}
