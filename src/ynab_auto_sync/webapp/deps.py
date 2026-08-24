from __future__ import annotations

from fastapi import Request

from ynab_auto_sync.config import AppConfig
from ynab_auto_sync.providers.base import TransactionProvider
from ynab_auto_sync.scheduler import Scheduler
from ynab_auto_sync.sync.engine import SyncEngine
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp.connection_manager import ConnectionManager
from ynab_auto_sync.ynab.client import YnabAccountsCache


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_engine(request: Request) -> SyncEngine:
    return request.app.state.engine


def get_db(request: Request) -> StateDB:
    return request.app.state.db


def get_providers(request: Request) -> dict[str, TransactionProvider]:
    """The provider map, keyed by TransactionProvider.type_name() - drives
    /api/providers. Defaults to {} for app instances that didn't wire one in
    (create_app's providers kwarg is optional so existing callers/tests
    don't all break)."""
    return request.app.state.providers


def get_scheduler(request: Request) -> Scheduler | None:
    """The running Scheduler, or None for an app instance that didn't wire
    one in (e.g. gui-only test setups) - callers that need it (sync-now)
    are responsible for turning None into a clear 503 rather than crashing.
    """
    return request.app.state.scheduler


def get_ws_manager(request: Request) -> ConnectionManager | None:
    """The shared ConnectionManager a WebSocketSink broadcasts through, or
    None for an app instance that didn't wire one in (create_app's
    ws_manager kwarg is optional, same reasoning as get_scheduler above) -
    routes/ws.py is responsible for refusing the connection rather than
    crashing when this is None."""
    return request.app.state.ws_manager


def get_ynab_accounts_cache(request: Request) -> YnabAccountsCache:
    """The process-lifetime TTL cache in front of ynab_client.get_accounts()
    - drives GET /api/ynab/accounts. Always present (create_app always
    constructs one, unlike the optional providers/scheduler/ws_manager
    above), since it needs no external resources."""
    return request.app.state.ynab_accounts_cache
