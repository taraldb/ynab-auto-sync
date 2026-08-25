from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ynab_auto_sync.config import AppConfig
from ynab_auto_sync.providers.base import TransactionProvider
from ynab_auto_sync.scheduler import Scheduler
from ynab_auto_sync.sync.engine import SyncEngine
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp.connection_manager import ConnectionManager
from ynab_auto_sync.webapp.routes import (
    audit,
    deleted,
    file_import,
    mappings,
    pause,
    providers,
    settings,
    status,
    sync_now,
    ws,
)
from ynab_auto_sync.ynab.client import YnabAccountsCache


def create_app(
    config: AppConfig,
    engine: SyncEngine,
    db: StateDB,
    providers_map: dict[str, TransactionProvider] | None = None,
    scheduler: Scheduler | None = None,
    ws_manager: ConnectionManager | None = None,
) -> FastAPI:
    app = FastAPI(title="ynab-auto-sync")
    app.state.config = config
    app.state.engine = engine
    app.state.db = db
    # Optional keyword args so existing callers/tests (which construct an
    # app with just config/engine/db) keep working unchanged - see
    # deps.get_providers/get_scheduler/get_ws_manager for how routes fall
    # back sanely when these weren't wired in.
    app.state.providers = providers_map or {}
    app.state.scheduler = scheduler
    app.state.ws_manager = ws_manager
    # Trivial to construct (no external resources), so unlike providers_map/
    # scheduler/ws_manager above there's no need for this to be an optional
    # kwarg - every app instance, test or real, gets its own cache.
    app.state.ynab_accounts_cache = YnabAccountsCache()

    app.include_router(status.router)
    app.include_router(audit.router)
    app.include_router(deleted.router)
    app.include_router(file_import.router)
    app.include_router(mappings.router)
    app.include_router(pause.router)
    app.include_router(providers.router)
    app.include_router(settings.router)
    app.include_router(sync_now.router)
    app.include_router(ws.router)

    # Mounted last, at the root path, so it only ever catches requests that
    # none of the /api/* routers above matched - the built SPA's static
    # assets and its index.html fallback (html=True) for client-side routes.
    static_dir = Path(config.gui.static_dir)
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

    return app
