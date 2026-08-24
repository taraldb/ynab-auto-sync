from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ynab_auto_sync.config import AppConfig
from ynab_auto_sync.logging_setup import configure_logging
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp.deps import get_config, get_db

router = APIRouter()

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class SettingsUpdateRequest(BaseModel):
    log_level: LogLevel


def _effective_log_level(config: AppConfig, db: StateDB) -> str:
    # NULL in the DB means "no runtime override yet" - fall back to
    # config.logging.level, same fallback __main__.py applies at startup.
    return db.read_run_metadata()["log_level"] or config.logging.level.upper()


@router.get("/api/settings")
async def get_settings(
    config: AppConfig = Depends(get_config), db: StateDB = Depends(get_db)
) -> dict[str, str]:
    return {"log_level": _effective_log_level(config, db)}


@router.patch("/api/settings")
async def update_settings(
    body: SettingsUpdateRequest,
    db: StateDB = Depends(get_db),
) -> dict[str, str]:
    await db.set_log_level(body.log_level)
    # Applies to the running process immediately, no restart needed -
    # configure_logging() is idempotent (rebuilds the one stdout handler and
    # resets the root logger's level), the same call __main__.py makes once
    # at startup.
    configure_logging(body.log_level)
    return {"log_level": body.log_level}
