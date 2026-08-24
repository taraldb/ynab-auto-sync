import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ynab_auto_sync.alerts.base import CycleStats, EventNotifier
from ynab_auto_sync.alerts.null_notifier import NullNotifier
from ynab_auto_sync.config import (
    AccountMapping,
    AppConfig,
    MqttConfig,
    SpareBank1Config,
    SyncConfig,
    YnabConfig,
)
from ynab_auto_sync.mqtt import client as mqtt_client
from ynab_auto_sync.notifications.base import Command, NotificationSink
from ynab_auto_sync.notifications.mqtt_sink import MqttSink
from ynab_auto_sync.notifications.null_sink import NullSink
from ynab_auto_sync.providers.base import ProviderAuthRequiredError
from ynab_auto_sync.scheduler import Scheduler
from ynab_auto_sync.sync.engine import CycleResult
from ynab_auto_sync.sync.state_db import StateDB


class FakeNotifier(EventNotifier):
    """Records every notify_* call - stands in for a real EventNotifier
    (e.g. NtfySink) so scheduler tests can assert which event kind was
    chosen, and how many times, without any real HTTP traffic."""

    def __init__(self):
        self.calls: list[tuple[str, Any]] = []

    async def notify_success(self, stats: CycleStats) -> None:
        self.calls.append(("success", stats))

    async def notify_success_with_changes(self, stats: CycleStats) -> None:
        self.calls.append(("success_with_changes", stats))

    async def notify_error(self, message: str, *, auth_required: bool) -> None:
        self.calls.append(("error", (message, auth_required)))


def make_config(cron_expression: str = "0 6,8,10,12,16,20 * * *") -> AppConfig:
    return AppConfig(
        providers={"sparebank1": SpareBank1Config(
            client_id="cid", client_secret="secret", redirect_uri="http://localhost:8765/callback"
        )},
        ynab=YnabConfig(personal_access_token="pat", budgets={"personal": "budget-1"}),
        accounts=[
            AccountMapping(
                sparebank1_account_key="acct-1",
                ynab_account_id="ynab-acct-1",
                ynab_budget="personal",
            )
        ],
        sync=SyncConfig(cron_expression=cron_expression),
        mqtt=MqttConfig(host="mqtt.local"),
    )


class FakeSink(NotificationSink):
    """Records every publish call and lets a test feed in Commands on
    demand - stands in for a real MQTT connection so scheduler tests never
    need a broker."""

    def __init__(self, commands: list[Command] | None = None):
        self.published: list[tuple[str, Any]] = []
        self._commands = commands or []
        self._parked = asyncio.Event()

    async def publish_availability(self, online: bool) -> None:
        self.published.append(("availability", online))

    async def publish_sync_state(self, value: str) -> None:
        self.published.append(("sync_state", value))

    async def publish_status(self, run_metadata: dict[str, Any]) -> None:
        self.published.append(("status", run_metadata))

    async def publish_state_value(self, name: str, value: Any) -> None:
        self.published.append((f"state:{name}", value))

    async def publish_progress(self, phase: str, **context: Any) -> None:
        self.published.append(("progress", (phase, context)))

    async def commands(self):
        for command in self._commands:
            yield command
        # Then park forever, like NullSink, so the consuming task only ever
        # ends via cancellation - matches how a real connection behaves
        # (messages arrive, then nothing, until the caller shuts down).
        await self._parked.wait()


class FakeEngine:
    """Stands in for SyncEngine's two-phase fetch_and_classify()/submit()
    split (see engine.py's ClassifiedCycle) so scheduler tests can drive a
    failure at either phase independently - that distinction is exactly
    what the retry/backoff logic branches on (see scheduler.py's
    _attempt_cycle: a fetch failure re-fetches on retry, a submit failure
    reuses the cached classified data instead).
    """

    def __init__(
        self,
        result: CycleResult | None = None,
        error: Exception | None = None,
        submit_error: Exception | None = None,
    ):
        self._result = result or CycleResult(
            created=0, updated=0, duplicates=0, resolved_deleted=0, accounts_processed=0, fetched=0
        )
        # `error` is a fetch-phase failure - kept as the name pre-existing
        # tests already use for "the cycle fails."
        self._fetch_error = error
        self._submit_error = submit_error
        self.fetch_call_count = 0
        self.submit_call_count = 0
        self.progress_calls: list[tuple[str, dict]] = []

    async def fetch_and_classify(self, on_progress=None):
        self.fetch_call_count += 1
        if on_progress is not None:
            await on_progress("fetching", {})
            self.progress_calls.append(("fetching", {}))
        if self._fetch_error is not None:
            raise self._fetch_error
        return object()  # opaque - real content is never inspected by Scheduler

    async def submit(self, classified, on_progress=None):
        self.submit_call_count += 1
        if on_progress is not None:
            await on_progress("submitting", {})
            self.progress_calls.append(("submitting", {}))
        if self._submit_error is not None:
            raise self._submit_error
        return self._result, {}

    async def run_cycle(self):
        classified = await self.fetch_and_classify()
        return await self.submit(classified)

    @property
    def call_count(self) -> int:
        return self.fetch_call_count


def test_seconds_until_next_fire_matches_cron_schedule(tmp_path: Path, monkeypatch):
    config = make_config()
    db = StateDB(tmp_path / "state.db")
    scheduler = Scheduler(config, FakeEngine(), db, asyncio.Event(), NullSink(), NullNotifier())

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 24, 5, 0, 0, tzinfo=tz)

    monkeypatch.setattr("ynab_auto_sync.scheduler.datetime", FixedDatetime)

    seconds = scheduler._seconds_until_next_fire()

    assert seconds == 3600  # next fire is 06:00, one hour after the fixed 05:00 "now"


async def test_do_cycle_records_failure_without_raising_on_engine_error(tmp_path: Path):
    config = make_config()
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(error=RuntimeError("boom"))
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), NullNotifier())

    await scheduler._do_cycle()  # must not raise

    meta = db.read_run_metadata()
    assert meta["last_error"] == "boom"
    assert meta["auth_required"] is False


async def test_do_cycle_records_success_and_prunes_without_raising(tmp_path: Path):
    config = make_config()
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(
        result=CycleResult(
            created=2, updated=1, duplicates=0, resolved_deleted=0, accounts_processed=1, fetched=3
        )
    )
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), NullNotifier())

    await scheduler._do_cycle()

    meta = db.read_run_metadata()
    assert meta["last_error"] is None
    assert meta["imported_last_run"] == 2
    assert meta["updated_last_run"] == 1


async def test_do_cycle_prunes_stale_audit_events(tmp_path: Path):
    config = make_config()
    db = StateDB(tmp_path / "state.db")
    await db.insert_audit_event(event_type="created", source="sparebank1")
    db._conn.execute("UPDATE audit_events SET occurred_at = '2020-01-01T00:00:00+00:00'")
    db._conn.commit()
    engine = FakeEngine()
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), NullNotifier())

    await scheduler._do_cycle()

    assert db.list_audit_events(include_skipped=True)[1] == 0


async def test_do_cycle_prune_failure_does_not_affect_recorded_success(tmp_path: Path, monkeypatch):
    config = make_config()
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine()
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), NullNotifier())

    async def boom(*args, **kwargs):
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(db, "prune_audit_events", boom)

    await scheduler._do_cycle()  # must not raise

    meta = db.read_run_metadata()
    assert meta["last_error"] is None


async def test_do_cycle_publishes_progress_and_idle_state_in_order(tmp_path: Path):
    # Regression test for scheduler.py's _report_progress wiring and the
    # new "idle" sync_state emit - a GUI websocket consumer needs this
    # exact ordering to know when a cycle starts, what it's doing, and when
    # it's genuinely finished.
    config = make_config()
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine()
    sink = FakeSink()
    scheduler = Scheduler(config, engine, db, asyncio.Event(), sink, NullNotifier())

    await scheduler._do_cycle()

    kinds = [kind for kind, _ in sink.published]
    assert kinds.index("sync_state") < kinds.index("progress")
    assert kinds.index("progress") < kinds.index("status")
    assert kinds[-1] == "sync_state"  # the final "idle" emit
    sync_state_values = [value for kind, value in sink.published if kind == "sync_state"]
    assert sync_state_values == ["running", "idle"]
    progress_phases = {
        value[0] for kind, value in sink.published if kind == "progress"
    }
    assert progress_phases == {"fetching", "submitting"}


async def test_fetch_failure_schedules_backoff_retry(tmp_path: Path):
    # Cron fires ~a year from now, so the default 60s base backoff delay is
    # always comfortably shorter than the gap to the next fire.
    config = make_config(cron_expression="0 0 1 1 *")
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(error=RuntimeError("boom"))
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), NullNotifier())

    await scheduler._do_cycle()

    assert scheduler._retry_deadline is not None
    assert scheduler._retry_attempt == 1
    assert scheduler._cached_classified is None  # nothing to cache - fetch itself failed


async def test_submit_failure_caches_classified_and_retries_submit_only(tmp_path: Path):
    # A YNAB write failure must retry only the write, not re-hit the
    # provider's own API - see engine.py's ClassifiedCycle docstring.
    config = make_config(cron_expression="0 0 1 1 *")
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(submit_error=RuntimeError("ynab down"))
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), NullNotifier())

    await scheduler._do_cycle()
    assert engine.fetch_call_count == 1
    assert engine.submit_call_count == 1
    assert scheduler._retry_deadline is not None
    assert scheduler._cached_classified is not None

    # Simulate the scheduled retry firing.
    await scheduler._do_cycle()
    assert engine.fetch_call_count == 1  # unchanged: no re-fetch from the provider
    assert engine.submit_call_count == 2

    meta = db.read_run_metadata()
    assert meta["last_error"] == "ynab down"


async def test_successful_retry_clears_backoff_state(tmp_path: Path):
    config = make_config(cron_expression="0 0 1 1 *")
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(submit_error=RuntimeError("ynab down"))
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), NullNotifier())

    await scheduler._do_cycle()
    assert scheduler._retry_deadline is not None

    engine._submit_error = None  # YNAB recovers
    await scheduler._do_cycle()

    assert scheduler._retry_deadline is None
    assert scheduler._retry_attempt == 0
    assert scheduler._cached_classified is None
    meta = db.read_run_metadata()
    assert meta["last_error"] is None


async def test_gives_up_retrying_when_backoff_would_land_after_next_cron_fire(
    tmp_path: Path, monkeypatch
):
    config = make_config()  # default cron includes a 06:00 fire
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(error=RuntimeError("boom"))
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), NullNotifier())

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            # 30s before the 06:00 fire - shorter than the default 60s base
            # backoff delay, so a retry would land at or after that fire.
            return cls(2026, 8, 24, 5, 59, 30, tzinfo=tz)

    monkeypatch.setattr("ynab_auto_sync.scheduler.datetime", FixedDatetime)

    await scheduler._do_cycle()

    assert scheduler._retry_deadline is None
    assert scheduler._retry_attempt == 0
    assert scheduler._cached_classified is None


async def test_auth_required_failure_does_not_schedule_retry(tmp_path: Path):
    # Retrying without the user re-authenticating (BankID) can't succeed -
    # must not burn backoff attempts on it.
    config = make_config(cron_expression="0 0 1 1 *")
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(error=ProviderAuthRequiredError("token revoked"))
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), NullNotifier())

    await scheduler._do_cycle()

    meta = db.read_run_metadata()
    assert meta["auth_required"] is True
    assert scheduler._retry_deadline is None
    assert scheduler._retry_attempt == 0


def test_seconds_until_next_wake_prefers_earlier_retry_deadline(tmp_path: Path, monkeypatch):
    config = make_config()
    db = StateDB(tmp_path / "state.db")
    scheduler = Scheduler(config, FakeEngine(), db, asyncio.Event(), NullSink(), NullNotifier())

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 24, 5, 0, 0, tzinfo=tz)

    monkeypatch.setattr("ynab_auto_sync.scheduler.datetime", FixedDatetime)

    scheduler._retry_deadline = FixedDatetime(2026, 8, 24, 5, 0, 30, tzinfo=UTC)
    assert scheduler._seconds_until_next_wake() == 30

    scheduler._retry_deadline = None
    assert scheduler._seconds_until_next_wake() == 3600  # falls back to the natural cron fire


async def test_run_loop_skips_cycle_when_paused(tmp_path: Path):
    config = make_config()
    db = StateDB(tmp_path / "state.db")
    await db.set_paused(True)
    engine = FakeEngine()
    stop_event = asyncio.Event()
    scheduler = Scheduler(config, engine, db, stop_event, NullSink(), NullNotifier())

    task = asyncio.create_task(scheduler._run_loop())
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2)

    assert engine.call_count == 0


async def test_sync_now_event_short_circuits_the_wait(tmp_path: Path):
    # A cron expression that fires once a year - if sync_now didn't
    # short-circuit the wait, this test would time out waiting for the
    # natural cron interval instead of firing immediately. Goes through the
    # public request_sync_now() entry point, since that's what a future
    # web-UI caller (and MQTT command handling) actually use.
    config = make_config(cron_expression="0 0 1 1 *")
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine()
    stop_event = asyncio.Event()
    scheduler = Scheduler(config, engine, db, stop_event, NullSink(), NullNotifier())

    task = asyncio.create_task(scheduler._run_loop())
    await asyncio.sleep(0.05)
    assert engine.call_count == 1  # the loop always runs a cycle immediately on entry

    scheduler.request_sync_now()
    await asyncio.sleep(0.05)
    assert engine.call_count == 2  # sync_now triggered a second cycle without waiting a year

    stop_event.set()
    await asyncio.wait_for(task, timeout=2)


async def test_run_uses_null_sink_end_to_end(tmp_path: Path):
    # A full run() cycle against NullSink, with no MQTT involved at all -
    # proves the scheduler doesn't secretly still depend on aiomqtt.
    config = make_config(cron_expression="0 0 1 1 *")
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine()
    stop_event = asyncio.Event()
    scheduler = Scheduler(config, engine, db, stop_event, NullSink(), NullNotifier())

    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.05)
    assert engine.call_count == 1

    stop_event.set()
    await asyncio.wait_for(task, timeout=2)


async def test_command_that_raises_does_not_kill_the_command_loop(tmp_path: Path):
    # Regression test for a real bug: one bad command must never silently
    # end command handling for the rest of the connection's lifetime. Make
    # the first command's handling raise (via a db wrapper that fails on
    # its first set_paused call), then prove a second command still gets
    # processed by the same still-running task.
    config = make_config()
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine()
    stop_event = asyncio.Event()

    commands = [Command(name="pause", payload="ON"), Command(name="sync_now", payload="")]
    sink = FakeSink(commands=commands)

    class ExplodingOnceDB:
        def __init__(self, inner: StateDB):
            self._inner = inner
            self._raised = False

        def __getattr__(self, item):
            return getattr(self._inner, item)

        async def set_paused(self, value: bool) -> None:
            if not self._raised:
                self._raised = True
                raise RuntimeError("boom - simulated failure handling the first command")
            await self._inner.set_paused(value)

    scheduler = Scheduler(config, engine, ExplodingOnceDB(db), stop_event, sink, NullNotifier())

    task = asyncio.create_task(scheduler._consume_commands())
    await asyncio.sleep(0.05)

    # The "pause" command raised inside set_paused, but the command loop
    # must still be alive to have processed the subsequent sync_now.
    assert scheduler._sync_now_event.is_set()
    assert not task.done()

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_mqtt_sink_publishes_availability_and_discovery_on_connect(monkeypatch):
    published: list[tuple[str, Any, bool]] = []
    subscribed: list[str] = []

    async def never_ending_messages():
        await asyncio.Event().wait()
        yield  # pragma: no cover - unreachable; makes this an async generator

    class FakeAiomqttClient:
        def __init__(self):
            self.messages = never_ending_messages()

        async def publish(self, topic, payload=None, qos=0, retain=False):
            published.append((topic, payload, retain))

        async def subscribe(self, topic):
            subscribed.append(topic)

    class FakeConnection:
        async def __aenter__(self):
            return FakeAiomqttClient()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mqtt_client, "make_client", lambda config: FakeConnection())

    mqtt_config = MqttConfig(host="mqtt.local")
    sink = MqttSink(mqtt_config)

    consume_task = asyncio.create_task(sink.commands().__anext__())
    await asyncio.sleep(0.05)

    published_topics = [p[0] for p in published]
    assert "ynab_auto_sync/status" in published_topics  # availability topic
    assert any(t.startswith("homeassistant/") for t in published_topics)  # discovery configs
    assert "ynab_auto_sync/command/sync_now" in subscribed
    assert "ynab_auto_sync/command/pause" in subscribed

    consume_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consume_task


async def test_success_with_no_changes_notifies_plain_success(tmp_path: Path):
    config = make_config()
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(
        result=CycleResult(
            created=0, updated=0, duplicates=1, resolved_deleted=0, accounts_processed=1, fetched=1
        )
    )
    notifier = FakeNotifier()
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), notifier)

    await scheduler._do_cycle()

    assert [kind for kind, _ in notifier.calls] == ["success"]


async def test_success_with_changes_notifies_success_with_changes(tmp_path: Path):
    config = make_config()
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(
        result=CycleResult(
            created=2, updated=1, duplicates=0, resolved_deleted=0, accounts_processed=1, fetched=3
        )
    )
    notifier = FakeNotifier()
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), notifier)

    await scheduler._do_cycle()

    assert [kind for kind, _ in notifier.calls] == ["success_with_changes"]
    stats = notifier.calls[0][1]
    assert stats == CycleStats(fetched=3, created=2, updated=1, duplicates=0, resolved_deleted=0)


async def test_repeated_identical_failure_only_notifies_once(tmp_path: Path):
    # See _maybe_notify_error - an extended outage retries every cycle but
    # must not push a notification for every single retry of the same
    # error.
    config = make_config(cron_expression="0 0 1 1 *")
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(error=RuntimeError("boom"))
    notifier = FakeNotifier()
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), notifier)

    await scheduler._do_cycle()
    await scheduler._do_cycle()
    await scheduler._do_cycle()

    assert [kind for kind, _ in notifier.calls] == ["error"]


async def test_changed_error_message_notifies_again(tmp_path: Path):
    config = make_config(cron_expression="0 0 1 1 *")
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(error=RuntimeError("boom"))
    notifier = FakeNotifier()
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), notifier)

    await scheduler._do_cycle()
    engine._fetch_error = RuntimeError("a different failure")
    await scheduler._do_cycle()

    assert [kind for kind, _ in notifier.calls] == ["error", "error"]
    messages = [payload[0] for kind, payload in notifier.calls]
    assert messages == ["boom", "a different failure"]


async def test_success_after_failure_resets_error_dedup(tmp_path: Path):
    config = make_config(cron_expression="0 0 1 1 *")
    db = StateDB(tmp_path / "state.db")
    engine = FakeEngine(error=RuntimeError("boom"))
    notifier = FakeNotifier()
    scheduler = Scheduler(config, engine, db, asyncio.Event(), NullSink(), notifier)

    await scheduler._do_cycle()
    engine._fetch_error = None
    await scheduler._do_cycle()  # recovers
    engine._fetch_error = RuntimeError("boom")  # same message as before the recovery
    await scheduler._do_cycle()

    kinds = [kind for kind, _ in notifier.calls]
    assert kinds == ["error", "success", "error"]
