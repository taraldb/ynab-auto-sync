from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from croniter import croniter

from ynab_auto_sync.alerts.base import CycleStats, EventNotifier
from ynab_auto_sync.api_response_logging import prune_old_logs
from ynab_auto_sync.config import AppConfig
from ynab_auto_sync.notifications.base import NotificationSink
from ynab_auto_sync.providers.base import ProviderAuthRequiredError
from ynab_auto_sync.sync.engine import ClassifiedCycle, SyncEngine
from ynab_auto_sync.sync.state_db import StateDB

logger = logging.getLogger(__name__)

VACUUM_MIN_INTERVAL_DAYS = 30


class Scheduler:
    """Long-running loop: periodic sync on a timer, plus an on-demand
    trigger and a pause switch driven by commands from a NotificationSink
    (MQTT/Home Assistant today, nothing tomorrow if MQTT is disabled). The
    sink owns its own connection lifecycle (including any
    reconnect-with-backoff) - the scheduler only ever sees the abstract
    NotificationSink interface, never a transport-specific type.

    Separately, an EventNotifier (see alerts/) pushes one discrete message
    per finished cycle - success, success-with-changes, or error - to
    something like a phone (ntfy.sh today). This is a different concern
    from the sink above (continuous state vs. discrete events) and the two
    are deliberately independent: a NullNotifier is used when no
    notification provider is configured, just as a NullSink is used when
    neither MQTT nor the GUI is active.
    """

    def __init__(
        self,
        config: AppConfig,
        engine: SyncEngine,
        db: StateDB,
        stop_event: asyncio.Event,
        sink: NotificationSink,
        notifier: EventNotifier,
        log_dir: Path = Path("state"),
    ):
        self._config = config
        self._engine = engine
        self._db = db
        self._stop_event = stop_event
        self._sink = sink
        self._notifier = notifier
        # Base dir for API response log files (see api_response_logging.py) -
        # only ever read from when api_response_logging.enabled, in
        # _prune_tracked_transactions() below. Defaulted so existing
        # callers/tests that predate this feature don't need updating.
        self._log_dir = log_dir
        self._sync_now_event = asyncio.Event()

        # Retry/backoff state for a failed cycle - see _do_cycle() and
        # _schedule_retry(). _retry_deadline, when set, is an earlier wake
        # time than the next natural cron fire; _cached_classified holds a
        # fetch_and_classify() result to resubmit on the next attempt
        # without re-hitting a provider's API, set only when the PREVIOUS
        # failure happened during submit() (a YNAB write), never fetch().
        self._retry_attempt = 0
        self._retry_deadline: datetime | None = None
        self._cached_classified: ClassifiedCycle | None = None

        # Dedup key for error notifications (see _maybe_notify_error): the
        # exact message string of the last error an EventNotifier was told
        # about, or None if the most recent attempt (if any) succeeded.
        # Deliberately NOT reset by _clear_retry_state()/_schedule_retry() -
        # it must survive "give up retrying, wait for next cron fire" the
        # same way the underlying unresolved error does, and only clears on
        # an actual success.
        self._last_notified_error: str | None = None

    def request_sync_now(self) -> None:
        """Trigger an out-of-cycle sync, short-circuiting the cron wait.
        Source-agnostic - used by MQTT command handling today, and
        available for the web UI to call directly later."""
        self._sync_now_event.set()

    async def run(self) -> None:
        async with self._sink:
            await self._sink.publish_status(self._db.read_run_metadata())

            listener_task = asyncio.create_task(self._consume_commands())
            try:
                await self._run_loop()
            finally:
                listener_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener_task

    async def _consume_commands(self) -> None:
        async for command in self._sink.commands():
            try:
                if command.name == "sync_now":
                    logger.info("Received sync_now command")
                    self._sync_now_event.set()
                elif command.name == "pause":
                    paused = command.payload.strip().upper() == "ON"
                    logger.info("Received pause command: %s", paused)
                    await self._db.set_paused(paused)
                    await self._sink.publish_state_value("paused", "ON" if paused else "OFF")
            except Exception:
                # A single bad command must never kill this task - without
                # this, an exception here (a transient publish failure, a DB
                # error) would silently end command handling for the rest of
                # the connection's lifetime while the main sync loop keeps
                # ticking normally, with no error surfaced anywhere.
                logger.exception("Error handling command %r", command)

    def _next_cron_fire_at(self) -> datetime:
        now = datetime.now(UTC)
        return croniter(self._config.sync.cron_expression, now).get_next(datetime)

    def _seconds_until_next_fire(self) -> float:
        now = datetime.now(UTC)
        return max((self._next_cron_fire_at() - now).total_seconds(), 0)

    def _seconds_until_next_wake(self) -> float:
        """The wait timeout for _run_loop: the next natural cron fire,
        unless a backoff retry (see _schedule_retry) is due sooner."""
        seconds_to_cron = self._seconds_until_next_fire()
        if self._retry_deadline is None:
            return seconds_to_cron
        seconds_to_retry = max((self._retry_deadline - datetime.now(UTC)).total_seconds(), 0)
        return min(seconds_to_cron, seconds_to_retry)

    def _clear_retry_state(self) -> None:
        self._retry_attempt = 0
        self._retry_deadline = None
        self._cached_classified = None

    def _schedule_retry(self) -> None:
        """After a failed cycle, either schedule an earlier wake-up for a
        backoff retry, or give up and let the next scheduled cron fire
        handle it - never both. A retry racing (or landing after) the next
        cron fire would be redundant, since that fire attempts a fresh
        cycle regardless."""
        delay = min(
            self._config.sync.retry_backoff_base_seconds * (2**self._retry_attempt),
            self._config.sync.retry_backoff_max_seconds,
        )
        candidate_deadline = datetime.now(UTC) + timedelta(seconds=delay)
        if candidate_deadline < self._next_cron_fire_at():
            self._retry_deadline = candidate_deadline
            self._retry_attempt += 1
            logger.info(
                "Sync cycle failed - retrying in %.0fs (attempt %d)",
                delay,
                self._retry_attempt,
            )
        else:
            logger.info(
                "Sync cycle failed - next backoff retry would land at or after the next "
                "scheduled cron run, giving up on retrying until then"
            )
            # Not just abandoning the *timer*: any cached classified data is
            # discarded too, so the next (cron-fired) attempt re-fetches
            # fresh rather than resubmitting data that could by then be
            # hours stale.
            self._clear_retry_state()

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._db.read_paused():
                await self._do_cycle()
            else:
                logger.info("Sync paused - skipping scheduled cycle")

            self._sync_now_event.clear()
            wait_stop = asyncio.create_task(self._stop_event.wait())
            wait_sync_now = asyncio.create_task(self._sync_now_event.wait())
            _done, pending = await asyncio.wait(
                {wait_stop, wait_sync_now},
                timeout=self._seconds_until_next_wake(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    async def _do_cycle(self) -> None:
        await self._sink.publish_sync_state("running")
        try:
            await self._attempt_cycle()
        finally:
            await self._sink.publish_status(self._db.read_run_metadata())
            # Unambiguous "this attempt is over" signal, distinct from the
            # "running" published above - today's only other consumer
            # (MQTT/Home Assistant) never distinguished a finished cycle
            # from an in-progress one by this value alone, so this is
            # purely additive; the GUI websocket push uses it to know when
            # to stop showing in-cycle progress (see publish_progress).
            await self._sink.publish_sync_state("idle")
            await self._prune_tracked_transactions()

    async def _report_progress(self, phase: str, context: dict[str, Any]) -> None:
        await self._sink.publish_progress(phase, **context)

    async def _maybe_notify_error(self, message: str, *, auth_required: bool) -> None:
        """Notify on error, but only on a state change: the first failure
        of a new outage, or a message that differs from the last one we
        notified about. Repeated backoff retries of the exact same error
        stay silent - without this, an extended outage (retries up to every
        30 minutes for hours, see _schedule_retry) would push a message on
        every single retry. Known, accepted limitation: dedup is by exact
        message text, so a message that varies slightly between retries
        (e.g. embeds a timestamp) would defeat this and notify every time.
        """
        if message == self._last_notified_error:
            return
        self._last_notified_error = message
        await self._notifier.notify_error(message, auth_required=auth_required)

    async def _attempt_cycle(self) -> None:
        """One attempt at a sync cycle - whether this is the cron-scheduled
        attempt, a manual "sync now", or a backoff retry of either. Never
        raises: every failure is recorded via StateDB and, unless it's an
        auth failure, queued for a backoff retry via _schedule_retry()."""
        try:
            if self._cached_classified is not None:
                classified = self._cached_classified
            else:
                classified = await self._engine.fetch_and_classify(
                    on_progress=self._report_progress
                )
        except ProviderAuthRequiredError as e:
            logger.error("Provider authentication required: %s", e)
            await self._db.record_failure(str(e), auth_required=True)
            await self._maybe_notify_error(str(e), auth_required=True)
            # Retrying without the user re-authenticating (BankID) can't
            # succeed - don't burn backoff attempts on it, just wait for the
            # next normal cron fire like before this feature existed.
            self._clear_retry_state()
            return
        except Exception as e:
            logger.exception("Sync cycle failed (fetching from provider)")
            await self._db.record_failure(str(e), auth_required=False)
            await self._maybe_notify_error(str(e), auth_required=False)
            self._schedule_retry()
            return

        try:
            result, account_last_synced = await self._engine.submit(
                classified, on_progress=self._report_progress
            )
        except Exception as e:
            logger.exception("Sync cycle failed (submitting to YNAB)")
            await self._db.record_failure(str(e), auth_required=False)
            await self._maybe_notify_error(str(e), auth_required=False)
            # Unlike a fetch failure, cache the already-classified data so
            # the retry only re-attempts the YNAB writes, not a fresh
            # provider fetch - avoids hammering the provider's API while
            # YNAB itself is what's down.
            self._cached_classified = classified
            self._schedule_retry()
            return

        await self._db.record_success(
            account_last_synced,
            imported=result.created,
            updated=result.updated,
            duplicates=result.duplicates,
            resolved_deleted=result.resolved_deleted,
            fetched=result.fetched,
        )
        logger.info(
            "Sync cycle complete: fetched=%d created=%d updated=%d duplicates=%d "
            "resolved_deleted=%d accounts=%d",
            result.fetched,
            result.created,
            result.updated,
            result.duplicates,
            result.resolved_deleted,
            result.accounts_processed,
        )
        # A successful cycle - whether cron-fired, a manual "sync now", or
        # itself a backoff retry - always clears any retry state, including
        # one accumulated across several prior failures.
        self._clear_retry_state()
        self._last_notified_error = None
        stats = CycleStats(
            fetched=result.fetched,
            created=result.created,
            updated=result.updated,
            duplicates=result.duplicates,
            resolved_deleted=result.resolved_deleted,
        )
        if result.created > 0 or result.updated > 0:
            await self._notifier.notify_success_with_changes(stats)
        else:
            await self._notifier.notify_success(stats)

    async def _prune_tracked_transactions(self) -> None:
        # Non-fatal by design - a pruning failure must never fail (or even
        # be attributed to) the sync cycle itself, which has already been
        # recorded as success/failure above. Also prunes audit_events using
        # the same retention_days cutoff - it's an unbounded-growth table
        # like tracked_transactions, and this is a "how long do I care about
        # history" knob, not something that needs its own config setting.
        try:
            cutoff = datetime.now(UTC).date() - timedelta(days=self._config.sync.retention_days)
            pruned = await self._db.prune_booked_transactions(cutoff)
            if pruned:
                logger.info("Pruned %d stale BOOKED tracked_transactions row(s)", pruned)
            pruned_audit = await self._db.prune_audit_events(cutoff)
            if pruned_audit:
                logger.info("Pruned %d stale audit_events row(s)", pruned_audit)
            await self._db.maybe_vacuum(min_interval_days=VACUUM_MIN_INTERVAL_DAYS)

            if self._config.api_response_logging.enabled:
                log_cutoff = datetime.now(UTC) - timedelta(
                    days=self._config.api_response_logging.retention_days
                )
                pruned_logs = prune_old_logs(self._log_dir / "api_logs", log_cutoff)
                if pruned_logs:
                    logger.info("Pruned %d stale API response log file(s)", pruned_logs)
        except Exception:
            logger.exception("tracked_transactions/audit_events pruning failed (non-fatal)")
