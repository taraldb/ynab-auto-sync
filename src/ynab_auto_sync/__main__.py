from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import httpx
import uvicorn
from pydantic import ValidationError

from ynab_auto_sync.alerts import CompositeNotifier, EventNotifier, NtfySink, NullNotifier
from ynab_auto_sync.api_response_logging import build_api_response_logger
from ynab_auto_sync.config import AppConfig, load_config
from ynab_auto_sync.logging_setup import configure_logging
from ynab_auto_sync.notifications import (
    CompositeSink,
    MqttSink,
    NotificationSink,
    NullSink,
    WebSocketSink,
)
from ynab_auto_sync.provider_setup import token_store_path
from ynab_auto_sync.providers.base import TransactionProvider
from ynab_auto_sync.providers.sparebank1.auth import TokenStore
from ynab_auto_sync.providers.sparebank1.provider import SpareBank1Provider
from ynab_auto_sync.scheduler import Scheduler
from ynab_auto_sync.state import JsonStateStore
from ynab_auto_sync.sync.engine import SyncEngine
from ynab_auto_sync.sync.state_db import StateDB
from ynab_auto_sync.webapp.app import create_app
from ynab_auto_sync.webapp.connection_manager import ConnectionManager

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config/config.yaml")
STATE_DIR = Path("state")


def _load_config_or_exit():
    # A misconfigured budget alias or duplicate account key must fail
    # obviously and immediately on startup, not with a raw pydantic
    # traceback dumped into the container logs.
    try:
        return load_config(CONFIG_PATH)
    except FileNotFoundError as e:
        print(f"Config error: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    except ValidationError as e:
        print(f"Invalid configuration in {CONFIG_PATH}:", file=sys.stderr)
        for err in e.errors():
            print(f"  - {err['msg']}", file=sys.stderr)
        raise SystemExit(1) from None


def _build_providers(
    config: AppConfig, http_client: httpx.AsyncClient
) -> dict[str, TransactionProvider]:
    """Instantiate one provider per enabled `providers:` entry.

    The dict key is the user's chosen provider name, which is exactly what
    account_mappings.provider stores - so renaming a provider in config.yaml
    orphans its mappings until they're re-pointed. That's deliberate: the
    alternative (silently re-matching by type) would quietly re-route real
    money to the wrong account.
    """
    providers: dict[str, TransactionProvider] = {}
    for name, provider_config in config.providers.items():
        if not provider_config.enabled:
            logger.info("Provider %r is disabled in config - not started", name)
            continue
        if provider_config.type == SpareBank1Provider.type_name():
            token_store = TokenStore(
                JsonStateStore(token_store_path(STATE_DIR, name)), provider_config
            )
            providers[name] = SpareBank1Provider(http_client, token_store, config.sync.timezone)
        else:  # pragma: no cover - unreachable while the union has one member
            raise ValueError(f"Unsupported provider type {provider_config.type!r} for {name!r}")
    return providers


def _build_sink(config: AppConfig, ws_manager: ConnectionManager | None) -> NotificationSink:
    """MQTT and the GUI websocket are two independent, both-optional
    broadcast concerns - either, both, or neither may be active. Wrap them
    in a CompositeSink only when more than one is actually active, so the
    common single-sink (or no-sink) cases stay exactly as simple as before
    this feature existed."""
    sinks: list[NotificationSink] = []
    if ws_manager is not None:
        sinks.append(WebSocketSink(ws_manager))
    if config.mqtt is not None and config.mqtt.enabled:
        sinks.append(MqttSink(config.mqtt))
    else:
        logger.info("MQTT disabled - running with no MQTT notification sink")

    if not sinks:
        return NullSink()
    if len(sinks) == 1:
        return sinks[0]
    return CompositeSink(sinks)


def _build_notifier(config: AppConfig) -> EventNotifier:
    """Discrete per-cycle event notifications (success/success-with-changes/
    error) - a different, independent concern from _build_sink() above (see
    alerts/ vs notifications/). Only one provider (ntfy) exists today; a
    second slots in here the same way MQTT/websocket do in _build_sink()."""
    notifiers: list[EventNotifier] = []
    ntfy_config = config.notifications.ntfy if config.notifications is not None else None
    if ntfy_config is not None and ntfy_config.enabled:
        notifiers.append(NtfySink(ntfy_config))

    if not notifiers:
        return NullNotifier()
    if len(notifiers) == 1:
        return notifiers[0]
    return CompositeNotifier(notifiers)


async def main() -> None:
    config = _load_config_or_exit()

    db = StateDB(STATE_DIR / "sync_state.db")
    # log_level is NULL until the GUI's Settings control has ever been used
    # - config.logging.level is the fallback, every time, until a human
    # overrides it at runtime (see state_db.py's _RUN_METADATA_MIGRATED_
    # COLUMNS comment and webapp/routes/settings.py, which is what actually
    # writes a non-NULL value here).
    effective_log_level = db.read_run_metadata()["log_level"] or config.logging.level
    configure_logging(effective_log_level)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    api_response_logger = build_api_response_logger(config.api_response_logging, STATE_DIR)
    if api_response_logger is not None:
        logger.warning(
            "API response logging is enabled - raw (redacted) provider/YNAB HTTP "
            "responses will be written to %s",
            STATE_DIR / "api_logs",
        )
    event_hooks = {"response": [api_response_logger.on_response]} if api_response_logger else {}

    async with httpx.AsyncClient(timeout=30, event_hooks=event_hooks) as http_client:
        providers = _build_providers(config, http_client)

        # One-time migration of the old static config.yaml account list into
        # the account_mappings table, which is authoritative from then on so
        # the GUI can edit mappings at runtime. No-ops once the table has any
        # row, so editing config.yaml afterwards has no effect.
        #
        # The legacy `accounts:` list predates named providers, so it can
        # only have meant the single configured SpareBank1 one - attribute
        # it to that provider's actual config key rather than assuming the
        # key is literally "sparebank1", or the seeded mappings would point
        # at a provider that doesn't exist.
        legacy_provider_name = next(
            (
                name
                for name, provider_config in config.providers.items()
                if provider_config.type == SpareBank1Provider.type_name()
            ),
            None,
        )
        if config.accounts and legacy_provider_name is None:
            logger.error(
                "config.yaml still has a legacy `accounts:` list but no SpareBank1 "
                "provider is configured - cannot seed account mappings from it"
            )
        elif config.accounts:
            seeded = await db.seed_mappings_from_config(
                [
                    {
                        "provider": legacy_provider_name,
                        # BYTE-FOR-BYTE. This string prefixes every tracking
                        # key in tracked_transactions and feeds every
                        # import_id already burned into YNAB - any
                        # normalization here would re-import the user's
                        # entire history as duplicates.
                        "provider_account_id": account.sparebank1_account_key,
                        "ynab_budget_id": config.ynab.budgets[account.ynab_budget],
                        "ynab_account_id": account.ynab_account_id,
                        "display_name": account.display_name,
                        "import_source_name": account.import_source_name,
                    }
                    for account in config.accounts
                ]
            )
            if seeded:
                logger.info(
                    "Seeded %d account mapping(s) from config.yaml into the database - "
                    "mappings are managed in the GUI from now on and config.yaml's "
                    "`accounts:` list is no longer read",
                    seeded,
                )

        engine = SyncEngine(config, http_client, db, providers)
        # Built before _build_sink() since the sink (WebSocketSink) and the
        # webapp (routes/ws.py) share this one instance - the sink
        # broadcasts through it, the route registers/unregisters
        # connections in it. None when the GUI is off, so _build_sink()
        # degrades to exactly its pre-websocket behavior with zero overhead.
        ws_manager = ConnectionManager() if config.gui.enabled else None
        sink = _build_sink(config, ws_manager)
        notifier = _build_notifier(config)
        scheduler = Scheduler(config, engine, db, stop_event, sink, notifier, log_dir=STATE_DIR)
        logger.info(
            "Starting ynab-auto-sync: %d account mapping(s), %d provider(s), cron schedule '%s'",
            len(db.list_mappings(enabled_only=True)),
            len(providers),
            config.sync.cron_expression,
        )

        tasks = [asyncio.create_task(scheduler.run())]

        if config.gui.enabled:
            app = create_app(config, engine, db, providers, scheduler, ws_manager)
            uvicorn_config = uvicorn.Config(
                app, host=config.gui.host, port=config.gui.port, log_config=None
            )
            server = uvicorn.Server(uvicorn_config)
            # This process already owns SIGTERM/SIGINT via stop_event above -
            # uvicorn must not install its own competing handlers.
            server.install_signal_handlers = False
            logger.info("GUI enabled on %s:%d", config.gui.host, config.gui.port)
            tasks.append(asyncio.create_task(_serve_until_stop(server, stop_event)))

        await asyncio.gather(*tasks)

    logger.info("Shutdown complete")


async def _serve_until_stop(server: uvicorn.Server, stop_event: asyncio.Event) -> None:
    serve_task = asyncio.create_task(server.serve())
    await stop_event.wait()
    server.should_exit = True
    await serve_task


if __name__ == "__main__":
    asyncio.run(main())
