from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_cursors (
    account_key TEXT PRIMARY KEY,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS run_metadata (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_run_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    auth_required INTEGER NOT NULL DEFAULT 0,
    paused INTEGER NOT NULL DEFAULT 0,
    imported_total INTEGER NOT NULL DEFAULT 0,
    imported_last_run INTEGER NOT NULL DEFAULT 0,
    updated_last_run INTEGER NOT NULL DEFAULT 0,
    duplicates_last_run INTEGER NOT NULL DEFAULT 0,
    resolved_deleted_last_run INTEGER NOT NULL DEFAULT 0,
    fetched_last_run INTEGER NOT NULL DEFAULT 0,
    log_level TEXT
);

CREATE TABLE IF NOT EXISTS tracked_transactions (
    sb1_transaction_id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL,
    ynab_transaction_id TEXT NOT NULL,
    ynab_budget_id TEXT NOT NULL,
    account_key TEXT NOT NULL,
    booking_status TEXT NOT NULL,
    amount_milliunits INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    payee_name TEXT,
    memo TEXT,
    transaction_date TEXT,
    ynab_account_id TEXT,
    cleared TEXT,
    readd_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS account_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    provider_account_id TEXT NOT NULL,
    ynab_budget_id TEXT NOT NULL,
    ynab_account_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    import_source_name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(provider, provider_account_id)
);

CREATE TABLE IF NOT EXISTS payee_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ynab_budget_id TEXT NOT NULL,
    raw_payee_name TEXT NOT NULL,
    ynab_payee_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(ynab_budget_id, raw_payee_name)
);

CREATE TABLE IF NOT EXISTS transformer_default_budgets (
    transformer_name TEXT PRIMARY KEY,
    ynab_budget_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    account_key TEXT,
    tracking_key TEXT,
    import_id TEXT,
    ynab_transaction_id TEXT,
    ynab_budget_id TEXT,
    ynab_account_id TEXT,
    payee_name TEXT,
    memo TEXT,
    transaction_date TEXT,
    amount_milliunits INTEGER,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_events_type_time
    ON audit_events (event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_time
    ON audit_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_account_key
    ON audit_events (account_key);
"""

# Columns added after the original schema - kept here so _migrate() can add
# them to an already-existing tracked_transactions table (see the
# resolved_deleted_last_run precedent on run_metadata for why
# CREATE TABLE IF NOT EXISTS alone isn't enough). Payee/memo/date/account/
# cleared are what's needed to fully reconstruct a create payload for a
# transaction resolved as deleted-in-YNAB (see sync/engine.py's
# readd_deleted_transaction) - persisted for every tracked transaction, not
# just deleted ones, since it's the same data already in hand at write time.
_TRACKED_TRANSACTIONS_MIGRATED_COLUMNS = (
    ("payee_name", "TEXT"),
    ("memo", "TEXT"),
    ("transaction_date", "TEXT"),
    ("ynab_account_id", "TEXT"),
    ("cleared", "TEXT"),
    ("readd_count", "INTEGER NOT NULL DEFAULT 0"),
)

# last_vacuum_at tracks when prune_booked_transactions last ran a VACUUM, so
# maybe_vacuum() can rate-limit it (DELETEs alone don't shrink the on-disk
# file - only VACUUM reclaims space, and it's too expensive to run every
# cycle). fetched_last_run is the raw pre-dedup/classification count a cycle
# fetched from its provider(s) (ClassifiedCycle.fetched_count / CycleResult
# .fetched in engine.py) - added after the fact, same as
# resolved_deleted_last_run before it, hence the migration entry rather than
# relying on _SCHEMA alone.
# log_level is NULL until the GUI's Settings control is ever used - NULL
# means "no runtime override yet, fall back to config.logging.level" (see
# __main__.py's startup logic and webapp/routes/settings.py). Deliberately
# NOT seeded from config at startup the way account_mappings seeds from
# accounts: - unlike that one-time migration, config.logging.level should
# keep taking effect on every fresh process start until a human explicitly
# overrides it via the GUI, not just once. last_payee_reconcile_at mirrors
# last_vacuum_at's own rate-limiting role, for is_payee_reconcile_due() -
# see that method's docstring and the "Payee-mapping reconcile pass"
# section in CLAUDE.md for why this needed a rate limit at all.
_RUN_METADATA_MIGRATED_COLUMNS = (
    ("last_vacuum_at", "TEXT"),
    ("fetched_last_run", "INTEGER NOT NULL DEFAULT 0"),
    ("log_level", "TEXT"),
    ("last_payee_reconcile_at", "TEXT"),
)


class MappingValidationError(ValueError):
    """Raised by create_mapping/update_mapping for a write that would violate
    an invariant config.py's _validate_account_references used to guarantee
    at startup for the (now-retired) static config.yaml account list: no two
    mappings may share a (provider, provider_account_id), and no two may
    share a non-empty import_source_name. Moving mappings into a mutable
    table means this has to be re-checked on every write instead of once at
    load time - never let a raw sqlite3.IntegrityError escape this layer
    instead.
    """


def compute_since(
    account_state: dict[str, Any] | None,
    initial_backfill_days: int,
    lookback_overlap_hours: int,
) -> datetime:
    """Fetch window start for one account. Re-fetches lookback_overlap_hours
    before the last successful sync every cycle as a safety margin against
    late-settling transactions and clock drift - safe to over-fetch because
    YNAB's import_id dedup makes re-submission a no-op. Never looks back
    further than initial_backfill_days, even on the very first run.
    """
    now = datetime.now(UTC)
    earliest_allowed = now - timedelta(days=initial_backfill_days)
    if account_state and account_state.get("last_synced_at"):
        last_synced = datetime.fromisoformat(account_state["last_synced_at"])
        candidate = last_synced - timedelta(hours=lookback_overlap_hours)
        return max(candidate, earliest_allowed)
    return earliest_allowed


class StateDB:
    """SQLite-backed replacement for CursorStore/JsonStateStore: per-account
    fetch cursors, run-level metadata (used for MQTT state publishing), and
    tracked_transactions - the pending-to-booked reconciliation table that a
    flat JSON file couldn't reasonably support. Deliberately a separate
    database from sparebank1/auth.py's TokenStore (state/tokens.json) - this
    data is pure observability/dedup state, safe to lose or reset, unlike
    the OAuth tokens.
    """

    def __init__(self, db_path: Path | str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: actual concurrency safety comes from the
        # asyncio.Lock below (for writes) and WAL mode (for reads), not from
        # sqlite3's default same-thread affinity - which would otherwise
        # raise if an ASGI server ever dispatches a request onto a different
        # thread than the one StateDB was constructed on (observed with
        # Starlette's TestClient, which runs the app in a worker thread via
        # an anyio portal even for a fully async app).
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL lets readers (e.g. a GUI status request) proceed without
        # blocking on a concurrent writer, unlike the default rollback-
        # journal mode. busy_timeout makes a writer that finds the DB
        # locked by another process (e.g. scripts/readd_deleted_transaction.py
        # run manually) wait and retry for up to 5s instead of raising
        # immediately - now that the GUI makes concurrent access routine
        # rather than rare, "just retry manually" is no longer good enough.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.execute("INSERT OR IGNORE INTO run_metadata (id) VALUES (1)")
        self._conn.commit()
        # Guards every mutating method below. The scheduler and the webapp
        # run as two coroutines on one event loop/thread (see __main__.py),
        # so sqlite3's own single-threaded connection is never touched from
        # two OS threads - but a mutating method that awaits mid-transaction
        # (see _backfill_duplicates' network calls between a read and a
        # write on the same tracking key) could otherwise interleave with a
        # concurrent caller's own read-then-write. This lock serializes the
        # body of each mutating call so that can't happen.
        self._lock = asyncio.Lock()

    def _migrate(self) -> None:
        # CREATE TABLE IF NOT EXISTS in _SCHEMA doesn't add new columns to an
        # already-existing table (e.g. an older state/sync_state.db from
        # before this column existed) - add it by hand, guarded so this is
        # safe to run against a fresh DB too.
        run_metadata_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(run_metadata)")
        }
        if "resolved_deleted_last_run" not in run_metadata_columns:
            self._conn.execute(
                "ALTER TABLE run_metadata ADD COLUMN resolved_deleted_last_run "
                "INTEGER NOT NULL DEFAULT 0"
            )
        for name, sql_type in _RUN_METADATA_MIGRATED_COLUMNS:
            if name not in run_metadata_columns:
                self._conn.execute(f"ALTER TABLE run_metadata ADD COLUMN {name} {sql_type}")

        tracked_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(tracked_transactions)")
        }
        for name, sql_type in _TRACKED_TRANSACTIONS_MIGRATED_COLUMNS:
            if name not in tracked_columns:
                self._conn.execute(
                    f"ALTER TABLE tracked_transactions ADD COLUMN {name} {sql_type}"
                )

    def read_account_states(self) -> dict[str, dict[str, Any]]:
        rows = self._conn.execute("SELECT account_key, last_synced_at FROM account_cursors")
        return {row["account_key"]: {"last_synced_at": row["last_synced_at"]} for row in rows}

    def read_run_metadata(self) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM run_metadata WHERE id = 1").fetchone()
        return {
            "last_run_at": row["last_run_at"],
            "last_success_at": row["last_success_at"],
            "last_error": row["last_error"],
            "auth_required": bool(row["auth_required"]),
            "paused": bool(row["paused"]),
            "imported_total": row["imported_total"],
            "imported_last_run": row["imported_last_run"],
            "updated_last_run": row["updated_last_run"],
            "duplicates_last_run": row["duplicates_last_run"],
            "resolved_deleted_last_run": row["resolved_deleted_last_run"],
            "fetched_last_run": row["fetched_last_run"],
            "log_level": row["log_level"],
        }

    def read_paused(self) -> bool:
        row = self._conn.execute("SELECT paused FROM run_metadata WHERE id = 1").fetchone()
        return bool(row["paused"])

    async def set_paused(self, paused: bool) -> None:
        async with self._lock:
            self._conn.execute(
                "UPDATE run_metadata SET paused = ? WHERE id = 1", (int(paused),)
            )
            self._conn.commit()

    async def set_log_level(self, level: str) -> None:
        # Persists the GUI's runtime override so it survives a process
        # restart (see _RUN_METADATA_MIGRATED_COLUMNS' comment on
        # log_level). Applying it to the actual running logger is the
        # caller's job (webapp/routes/settings.py calls
        # logging_setup.configure_logging() right after this) - StateDB
        # only owns persistence, same separation as every other setting
        # here.
        async with self._lock:
            self._conn.execute(
                "UPDATE run_metadata SET log_level = ? WHERE id = 1", (level.upper(),)
            )
            self._conn.commit()

    async def record_success(
        self,
        account_last_synced: dict[str, str],
        *,
        imported: int,
        updated: int,
        duplicates: int,
        resolved_deleted: int = 0,
        fetched: int = 0,
    ) -> None:
        async with self._lock:
            now_iso = datetime.now(UTC).isoformat()
            for account_key, last_synced_at in account_last_synced.items():
                self._conn.execute(
                    """
                    INSERT INTO account_cursors (account_key, last_synced_at)
                    VALUES (?, ?)
                    ON CONFLICT(account_key) DO UPDATE SET last_synced_at = excluded.last_synced_at
                    """,
                    (account_key, last_synced_at),
                )
            self._conn.execute(
                """
                UPDATE run_metadata
                SET last_run_at = ?,
                    last_success_at = ?,
                    last_error = NULL,
                    auth_required = 0,
                    imported_total = imported_total + ?,
                    imported_last_run = ?,
                    updated_last_run = ?,
                    duplicates_last_run = ?,
                    resolved_deleted_last_run = ?,
                    fetched_last_run = ?
                WHERE id = 1
                """,
                (
                    now_iso,
                    now_iso,
                    imported,
                    imported,
                    updated,
                    duplicates,
                    resolved_deleted,
                    fetched,
                ),
            )
            self._conn.commit()

    async def record_failure(self, error: str, *, auth_required: bool) -> None:
        # Deliberately does not touch account_cursors - a failed cycle must
        # never advance any account's cursor, so next cycle's overlap window
        # naturally retries everything that wasn't confirmed synced.
        async with self._lock:
            now_iso = datetime.now(UTC).isoformat()
            self._conn.execute(
                """
                UPDATE run_metadata
                SET last_run_at = ?,
                    last_error = ?,
                    auth_required = ?,
                    imported_last_run = 0,
                    updated_last_run = 0,
                    duplicates_last_run = 0,
                    resolved_deleted_last_run = 0,
                    fetched_last_run = 0
                WHERE id = 1
                """,
                (now_iso, error, int(auth_required)),
            )
            self._conn.commit()

    def get_tracked(self, sb1_transaction_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM tracked_transactions WHERE sb1_transaction_id = ?",
            (sb1_transaction_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_tracked_by_ynab_transaction_id(self, ynab_transaction_id: str) -> dict[str, Any] | None:
        """Fallback lookup for a row whose primary key (sb1_transaction_id)
        may have changed since an older audit_events row was written - see
        rekey_pending_to_booked below, the one place a tracked row's PK
        changes mid-lifecycle. ynab_transaction_id never changes across
        that rekey, so it's the stable key to fall back to. Used by
        webapp/routes/audit.py when db.get_tracked(event["tracking_key"])
        misses on a pre-rekey tracking_key still referenced by an older
        event.
        """
        row = self._conn.execute(
            "SELECT * FROM tracked_transactions WHERE ynab_transaction_id = ? LIMIT 1",
            (ynab_transaction_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    async def upsert_tracked(
        self,
        sb1_transaction_id: str,
        *,
        import_id: str,
        ynab_transaction_id: str,
        ynab_budget_id: str,
        account_key: str,
        booking_status: str,
        amount_milliunits: int,
        payee_name: str | None = None,
        memo: str | None = None,
        transaction_date: str | None = None,
        ynab_account_id: str | None = None,
        cleared: str | None = None,
    ) -> None:
        """payee_name/memo/transaction_date/ynab_account_id/cleared exist so
        a transaction resolved as deleted-in-YNAB can later be fully
        reconstructed and re-added (see sync/engine.py's
        readd_deleted_transaction) - always pass them when the caller has
        the data (i.e. from the same transform_transaction() payload just
        submitted to YNAB), even for non-deleted rows, since it's free at
        write time and gives every tracked row the same reconstructibility.
        """
        async with self._lock:
            now_iso = datetime.now(UTC).isoformat()
            self._conn.execute(
                """
                INSERT INTO tracked_transactions (
                    sb1_transaction_id, import_id, ynab_transaction_id, ynab_budget_id,
                    account_key, booking_status, amount_milliunits, first_seen_at,
                    last_checked_at, payee_name, memo, transaction_date, ynab_account_id,
                    cleared
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sb1_transaction_id) DO UPDATE SET
                    import_id = excluded.import_id,
                    ynab_transaction_id = excluded.ynab_transaction_id,
                    ynab_budget_id = excluded.ynab_budget_id,
                    account_key = excluded.account_key,
                    booking_status = excluded.booking_status,
                    amount_milliunits = excluded.amount_milliunits,
                    last_checked_at = excluded.last_checked_at,
                    payee_name = excluded.payee_name,
                    memo = excluded.memo,
                    transaction_date = excluded.transaction_date,
                    ynab_account_id = excluded.ynab_account_id,
                    cleared = excluded.cleared
                """,
                (
                    sb1_transaction_id,
                    import_id,
                    ynab_transaction_id,
                    ynab_budget_id,
                    account_key,
                    booking_status,
                    amount_milliunits,
                    now_iso,
                    now_iso,
                    payee_name,
                    memo,
                    transaction_date,
                    ynab_account_id,
                    cleared,
                ),
            )
            self._conn.commit()

    def list_deleted_transactions(self) -> list[dict[str, Any]]:
        """All transactions resolved as permanently deleted-in-YNAB (see
        sync/engine.py's _backfill_duplicates), most recently resolved
        first - the candidate list for a future re-add command/GUI.
        """
        rows = self._conn.execute(
            "SELECT * FROM tracked_transactions WHERE booking_status = 'DELETED' "
            "ORDER BY last_checked_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    async def mark_readded(
        self, sb1_transaction_id: str, *, new_ynab_transaction_id: str, new_import_id: str
    ) -> None:
        """Re-integrate a re-added transaction into normal tracking: restores
        booking_status from the persisted `cleared` value (so a later
        pending->booked transition can still fire normally on the new
        transaction), points at the new YNAB id/import_id (the old
        import_id stays permanently burned by YNAB), and bumps readd_count
        so a further re-add attempt gets a distinct import_id too.
        """
        async with self._lock:
            row = self._conn.execute(
                "SELECT cleared FROM tracked_transactions WHERE sb1_transaction_id = ?",
                (sb1_transaction_id,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"no tracked transaction for sb1_transaction_id={sb1_transaction_id!r}"
                )
            new_booking_status = "PENDING" if row["cleared"] == "uncleared" else "BOOKED"
            now_iso = datetime.now(UTC).isoformat()
            self._conn.execute(
                """
                UPDATE tracked_transactions
                SET ynab_transaction_id = ?,
                    import_id = ?,
                    booking_status = ?,
                    readd_count = readd_count + 1,
                    last_checked_at = ?
                WHERE sb1_transaction_id = ?
                """,
                (
                    new_ynab_transaction_id,
                    new_import_id,
                    new_booking_status,
                    now_iso,
                    sb1_transaction_id,
                ),
            )
            self._conn.commit()

    def get_earliest_pending_first_seen(self, account_key: str) -> str | None:
        """Earliest first_seen_at among still-PENDING tracked transactions
        for one account, or None if none are pending. Used to widen a
        cycle's fetch window when needed - the normal overlap-window
        calculation alone can let a slow-to-settle pending transaction age
        out of every future fetch before it ever books, permanently
        stranding it as 'uncleared' in YNAB.
        """
        row = self._conn.execute(
            """
            SELECT MIN(first_seen_at) AS earliest FROM tracked_transactions
            WHERE account_key = ? AND booking_status = 'PENDING'
            """,
            (account_key,),
        ).fetchone()
        return row["earliest"] if row else None

    async def mark_booked(self, sb1_transaction_id: str, new_amount_milliunits: int) -> None:
        async with self._lock:
            now_iso = datetime.now(UTC).isoformat()
            cursor = self._conn.execute(
                """
                UPDATE tracked_transactions
                SET booking_status = 'BOOKED', amount_milliunits = ?, cleared = 'cleared',
                    last_checked_at = ?
                WHERE sb1_transaction_id = ?
                """,
                (new_amount_milliunits, now_iso, sb1_transaction_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(
                    f"no tracked transaction for sb1_transaction_id={sb1_transaction_id!r}"
                )
            self._conn.commit()

    def list_pending_candidates(self, account_key: str) -> list[dict[str, Any]]:
        """Every still-PENDING tracked row for one account - the candidate
        pool sync/pending_match.py::find_pending_match() correlates a
        freshly-fetched BOOKED transaction against, since its tracking key
        won't match a PENDING row's own key (see rekey_pending_to_booked
        below for why). Scoped by account_key only, same as
        get_earliest_pending_first_seen above - tracked_transactions has
        never been provider-qualified beyond that.
        """
        rows = self._conn.execute(
            """
            SELECT sb1_transaction_id, ynab_transaction_id, ynab_budget_id,
                   amount_milliunits, first_seen_at, payee_name
            FROM tracked_transactions
            WHERE account_key = ? AND booking_status = 'PENDING'
            """,
            (account_key,),
        ).fetchall()
        return [dict(row) for row in rows]

    async def rekey_pending_to_booked(
        self, old_tracking_key: str, new_tracking_key: str, new_amount_milliunits: int
    ) -> dict[str, Any] | None:
        """Fuzzy-matched pending->booked transition (sync/pending_match.py).
        Unlike mark_booked above (same primary key, updated in place), this
        changes the PRIMARY KEY itself: a fuzzy-matched booked observation
        has a DIFFERENT tracking key than its pending observation (confirmed
        live - a credit card's bare nonUniqueId while PENDING bears no
        relation to creditCardIdentifiers.nonUniqueId once BOOKED). Without
        this rekey, every future poll of the same real transaction would
        compute the new key, find nothing tracked under it, and eventually
        create a genuine duplicate once it's no longer a pending-candidate
        match either - see CLAUDE.md's "PENDING-transaction import" section.

        No FK constraints reference sb1_transaction_id (confirmed - this
        project doesn't use SQLite foreign keys at all), so a PK-value
        UPDATE is schema-safe. ynab_transaction_id is deliberately never
        touched here - it's the same already-created YNAB transaction,
        just getting an amount PATCH, exactly like mark_booked's own path.

        Returns the row's full pre-update state (for the caller's audit-log
        detail message - old amount/import_id/payee_name), or None if
        old_tracking_key no longer exists but new_tracking_key already
        reflects a completed BOOKED transition - an idempotent retry
        (submit() may be called twice with the same cached ClassifiedCycle
        after a prior partial failure, per ClassifiedCycle's own
        docstring). Raises ValueError if neither condition holds (a genuine
        bug, not a retry), mirroring mark_booked's own convention.
        """
        async with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracked_transactions WHERE sb1_transaction_id = ?",
                (old_tracking_key,),
            ).fetchone()
            if row is None:
                already = self._conn.execute(
                    "SELECT 1 FROM tracked_transactions "
                    "WHERE sb1_transaction_id = ? AND booking_status = 'BOOKED'",
                    (new_tracking_key,),
                ).fetchone()
                if already is not None:
                    return None
                raise ValueError(
                    f"no tracked transaction for sb1_transaction_id={old_tracking_key!r}"
                )
            previous = dict(row)
            now_iso = datetime.now(UTC).isoformat()
            self._conn.execute(
                """
                UPDATE tracked_transactions
                SET sb1_transaction_id = ?, booking_status = 'BOOKED',
                    amount_milliunits = ?, cleared = 'cleared', last_checked_at = ?
                WHERE sb1_transaction_id = ?
                """,
                (new_tracking_key, new_amount_milliunits, now_iso, old_tracking_key),
            )
            self._conn.commit()
            return previous

    async def prune_booked_transactions(self, cutoff_date: date) -> int:
        """Delete BOOKED tracked_transactions rows older than cutoff_date.
        Never touches PENDING rows regardless of age - their whole purpose
        is to be found again later so a pending->booked transition can be
        detected; pruning one would recreate the same "one duplicate at the
        transition" risk this project has already fixed once (see CLAUDE.md).
        The caller (scheduler) is responsible for choosing a cutoff safely
        past the fetch horizon (initial_backfill_days + lookback_overlap_hours)
        - config.py's retention_days validator enforces that at load time.
        Returns the number of rows deleted.
        """
        async with self._lock:
            cursor = self._conn.execute(
                """
                DELETE FROM tracked_transactions
                WHERE booking_status = 'BOOKED' AND transaction_date < ?
                """,
                (cutoff_date.isoformat(),),
            )
            self._conn.commit()
            return cursor.rowcount

    async def maybe_vacuum(self, min_interval_days: int = 30) -> bool:
        """Runs VACUUM at most once per min_interval_days (tracked via
        run_metadata.last_vacuum_at) - DELETEs alone don't shrink the
        on-disk file, only VACUUM reclaims space, and it's too expensive to
        run every cycle. Returns whether a VACUUM actually ran.
        """
        async with self._lock:
            row = self._conn.execute(
                "SELECT last_vacuum_at FROM run_metadata WHERE id = 1"
            ).fetchone()
            last_vacuum_at = row["last_vacuum_at"]
            now = datetime.now(UTC)
            if last_vacuum_at is not None:
                elapsed = now - datetime.fromisoformat(last_vacuum_at)
                if elapsed < timedelta(days=min_interval_days):
                    return False

            # VACUUM cannot run inside a transaction and reads WAL contents
            # into the main file itself, so commit first.
            self._conn.commit()
            self._conn.execute("VACUUM")
            self._conn.execute(
                "UPDATE run_metadata SET last_vacuum_at = ? WHERE id = 1", (now.isoformat(),)
            )
            self._conn.commit()
            return True

    # -- account_mappings -----------------------------------------------
    #
    # Storage layer only: this table is not yet read by config.py, engine.py,
    # or the webapp. Budget-alias resolution and any other config-level
    # validation stay the caller's responsibility - this layer only enforces
    # the two invariants config.py's _validate_account_references used to
    # guarantee at startup for the old static list (see MappingValidationError).

    _MAPPING_UPDATABLE_FIELDS = (
        "provider",
        "provider_account_id",
        "ynab_budget_id",
        "ynab_account_id",
        "display_name",
        "import_source_name",
        "enabled",
    )

    @staticmethod
    def _mapping_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        return d

    def _check_import_source_name_unique(
        self, import_source_name: str, *, exclude_id: int | None
    ) -> None:
        # Blank import_source_name may repeat freely - only opts an account
        # into file-import auto-matching when non-empty, same as config.py's
        # original rule.
        if not import_source_name:
            return
        query = "SELECT id FROM account_mappings WHERE import_source_name = ?"
        params: list[Any] = [import_source_name]
        if exclude_id is not None:
            query += " AND id != ?"
            params.append(exclude_id)
        if self._conn.execute(query, params).fetchone() is not None:
            raise MappingValidationError(
                f"import_source_name={import_source_name!r} is already used by another mapping"
            )

    def list_mappings(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM account_mappings"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY id"
        rows = self._conn.execute(query).fetchall()
        return [self._mapping_row_to_dict(row) for row in rows]

    def get_mapping(self, mapping_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM account_mappings WHERE id = ?", (mapping_id,)
        ).fetchone()
        return self._mapping_row_to_dict(row) if row is not None else None

    async def create_mapping(
        self,
        *,
        provider: str,
        provider_account_id: str,
        ynab_budget_id: str,
        ynab_account_id: str,
        display_name: str = "",
        import_source_name: str = "",
        enabled: bool = True,
    ) -> int:
        async with self._lock:
            # Checked explicitly (not just left to the UNIQUE constraint)
            # because import_source_name has no UNIQUE index of its own -
            # blank values must be allowed to repeat, which a plain UNIQUE
            # constraint can't express (NULL would work but empty string
            # doesn't get that SQLite special-case).
            self._check_import_source_name_unique(import_source_name, exclude_id=None)
            now_iso = datetime.now(UTC).isoformat()
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO account_mappings (
                        provider, provider_account_id, ynab_budget_id, ynab_account_id,
                        display_name, import_source_name, enabled, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        provider,
                        provider_account_id,
                        ynab_budget_id,
                        ynab_account_id,
                        display_name,
                        import_source_name,
                        int(enabled),
                        now_iso,
                        now_iso,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise MappingValidationError(
                    f"a mapping for provider={provider!r} "
                    f"provider_account_id={provider_account_id!r} already exists"
                ) from exc
            self._conn.commit()
            return cursor.lastrowid

    async def update_mapping(self, mapping_id: int, **fields: Any) -> None:
        unknown = set(fields) - set(self._MAPPING_UPDATABLE_FIELDS)
        if unknown:
            raise ValueError(f"unknown account_mapping field(s): {sorted(unknown)}")
        if not fields:
            return
        async with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM account_mappings WHERE id = ?", (mapping_id,)
            ).fetchone()
            if existing is None:
                raise ValueError(f"no account_mapping with id={mapping_id!r}")
            if "import_source_name" in fields:
                self._check_import_source_name_unique(
                    fields["import_source_name"], exclude_id=mapping_id
                )
            set_clauses = []
            params: list[Any] = []
            for name, value in fields.items():
                set_clauses.append(f"{name} = ?")
                params.append(int(value) if name == "enabled" else value)
            set_clauses.append("updated_at = ?")
            params.append(datetime.now(UTC).isoformat())
            params.append(mapping_id)
            try:
                self._conn.execute(
                    f"UPDATE account_mappings SET {', '.join(set_clauses)} WHERE id = ?",
                    params,
                )
            except sqlite3.IntegrityError as exc:
                raise MappingValidationError(
                    "updating this mapping would duplicate an existing "
                    "(provider, provider_account_id) pair"
                ) from exc
            self._conn.commit()

    async def delete_mapping(self, mapping_id: int) -> None:
        async with self._lock:
            self._conn.execute("DELETE FROM account_mappings WHERE id = ?", (mapping_id,))
            self._conn.commit()

    async def delete_all_mappings(self) -> int:
        """Bulk "clear all" for the GUI's Mappings tab. Only removes the
        mapping rows themselves - tracked_transactions and every burned YNAB
        import_id are untouched (invariant 11: unmapping never re-imports or
        loses history), so this is "stop syncing everything and start
        remapping from scratch", not a data-loss operation. Returns the
        number of rows deleted, for the caller to report back.
        """
        async with self._lock:
            cursor = self._conn.execute("DELETE FROM account_mappings")
            self._conn.commit()
            return cursor.rowcount

    async def seed_mappings_from_config(self, rows: list[dict[str, Any]]) -> int:
        """One-time migration path from the old static config.yaml account
        list into this table. Seeds ONLY when account_mappings is completely
        empty - if any row already exists (from a prior seed, or from a user
        editing mappings via the future web UI) this is a permanent no-op
        returning 0, since config.yaml stops being authoritative the moment
        this table has content and must never overwrite what's there.

        Each row's "provider_account_id" MUST be copied byte-for-byte from
        the old AccountMapping.sparebank1_account_key - no trimming,
        lowercasing, or namespacing. That exact string is the prefix of
        every tracking_key already used as tracked_transactions' primary key
        and feeds every import_id already burned into YNAB (CLAUDE.md
        invariant 2 / invariant 7's "provider_account_id" naming here is new,
        but the underlying string must be identical to the old key or every
        already-synced transaction stops matching and gets re-imported as a
        duplicate on the next poll).
        """
        async with self._lock:
            row_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM account_mappings"
            ).fetchone()["n"]
            if row_count > 0:
                return 0
            now_iso = datetime.now(UTC).isoformat()
            created = 0
            for row in rows:
                self._conn.execute(
                    """
                    INSERT INTO account_mappings (
                        provider, provider_account_id, ynab_budget_id, ynab_account_id,
                        display_name, import_source_name, enabled, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["provider"],
                        row["provider_account_id"],
                        row["ynab_budget_id"],
                        row["ynab_account_id"],
                        row.get("display_name", ""),
                        row.get("import_source_name", ""),
                        int(row.get("enabled", True)),
                        now_iso,
                        now_iso,
                    ),
                )
                created += 1
            self._conn.commit()
            return created

    def count_tracked_for_account(self, provider_account_id: str) -> int:
        """Number of tracked_transactions rows already recorded under this
        account key - used by the (future) UI to warn before re-pointing an
        already-synced account at a different YNAB account/budget.
        tracked_transactions.account_key is exactly the old
        sparebank1_account_key / this table's provider_account_id (see
        transform.get_tracking_key), so a direct equality match is correct,
        not an approximation.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM tracked_transactions WHERE account_key = ?",
            (provider_account_id,),
        ).fetchone()
        return row["n"]

    # -- payee_mappings ---------------------------------------------------
    #
    # Anchors a raw bank-derived payee string to the YNAB payee_id it first
    # created, per budget, so a later sync of the same raw text can submit
    # payee_id instead of payee_name - immune to the user renaming/merging
    # that payee in YNAB afterward (payee_name matching is exact-string-only
    # and would otherwise recreate the pre-rename name as a fresh payee;
    # confirmed live via scripts/verify_ynab_payee_id.py). First write wins
    # deliberately (INSERT OR IGNORE): the cached id is meant to be a
    # permanent anchor to whatever the user has since renamed it to, so a
    # later create must never overwrite it with a different payee_id.

    def get_payee_id(self, ynab_budget_id: str, raw_payee_name: str) -> str | None:
        row = self._conn.execute(
            "SELECT ynab_payee_id FROM payee_mappings WHERE ynab_budget_id = ? "
            "AND raw_payee_name = ?",
            (ynab_budget_id, raw_payee_name),
        ).fetchone()
        return row["ynab_payee_id"] if row is not None else None

    def list_payee_ids(self, ynab_budget_id: str) -> list[str]:
        """Every DISTINCT ynab_payee_id currently cached for this budget -
        the bounded candidate set SyncEngine.reconcile_payee_mappings()
        checks individually via ynab_client.get_payee() (a targeted
        single-payee GET). DISTINCT because several raw_payee_name rows can
        legitimately share one payee_id (the same real merchant reached
        under two different raw bank-text spellings)."""
        rows = self._conn.execute(
            "SELECT DISTINCT ynab_payee_id FROM payee_mappings WHERE ynab_budget_id = ?",
            (ynab_budget_id,),
        ).fetchall()
        return [row["ynab_payee_id"] for row in rows]

    def is_payee_reconcile_due(self, min_interval_days: int) -> bool:
        """Rate-limit gate for SyncEngine.reconcile_payee_mappings(),
        mirroring maybe_vacuum's own last_vacuum_at pattern. Needed because
        that method costs one YNAB API call per DISTINCT cached payee_id
        (see list_payee_ids) rather than one call per budget - a
        long-lived budget can accumulate dozens to hundreds of distinct
        payees, and running the full sweep every cycle (up to 6x/day) risks
        bursting a large fraction of YNAB's hourly rate limit in one go.
        Read-only: does not itself mark anything as done - see
        mark_payee_reconcile_done()."""
        row = self._conn.execute(
            "SELECT last_payee_reconcile_at FROM run_metadata WHERE id = 1"
        ).fetchone()
        last = row["last_payee_reconcile_at"]
        if last is None:
            return True
        elapsed = datetime.now(UTC) - datetime.fromisoformat(last)
        return elapsed >= timedelta(days=min_interval_days)

    async def mark_payee_reconcile_done(self) -> None:
        async with self._lock:
            self._conn.execute(
                "UPDATE run_metadata SET last_payee_reconcile_at = ? WHERE id = 1",
                (datetime.now(UTC).isoformat(),),
            )
            self._conn.commit()

    async def upsert_payee_mapping(
        self, ynab_budget_id: str, raw_payee_name: str, ynab_payee_id: str
    ) -> None:
        async with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO payee_mappings (
                    ynab_budget_id, raw_payee_name, ynab_payee_id, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (ynab_budget_id, raw_payee_name, ynab_payee_id, datetime.now(UTC).isoformat()),
            )
            self._conn.commit()

    async def delete_payee_mappings_for_ids(
        self, ynab_budget_id: str, ynab_payee_ids: set[str]
    ) -> int:
        """Heals invariant 12's known gap: a cached ynab_payee_id whose real
        YNAB payee has since been deleted/merged away.

        The caller (SyncEngine.reconcile_payee_mappings) passes only ids
        it confirmed individually via a targeted GET .../payees/{payee_id}
        (ynab_client.get_payee) - either a 404, or a 200 with
        `deleted: true`. An EARLIER version of this design instead scanned
        the bulk GET .../payees list and looked for `deleted: true` there,
        on the assumption (borrowed from sibling project
        ../ynab-auto-bank's identical stance, and from this project's own
        GET .../accounts, which DOES keep closed/deleted accounts in its
        response) that YNAB does the same for payees. Live verification
        (scripts/verify_ynab_payee_deletion.py) proved that assumption
        wrong for a real, human-deleted payee: it was OMITTED from the
        bulk list entirely, not flagged - which made "absent from a bulk
        fetch" fundamentally unsafe to treat as deletion (a transient or
        incomplete response could just as easily explain an absence, and
        would have mass-invalidated a whole budget's cache). The targeted
        per-id GET this design uses instead was then confirmed
        (scripts/verify_ynab_payee_get_by_id.py) to reliably 404 for a
        genuinely deleted payee, which is what makes per-id confirmation
        safe where bulk-list absence was not.

        Deliberately does not attempt to guess what a deleted payee was
        merged into (YNAB's payee list gives no such linkage) - the next
        create for that raw payee text simply re-learns a fresh payee_id
        through the normal _classify()/_record_created() path.

        Returns the number of rows actually deleted; a no-op (no query run)
        for an empty ynab_payee_ids, which is the common case most times
        this runs.
        """
        if not ynab_payee_ids:
            return 0
        async with self._lock:
            placeholders = ",".join("?" for _ in ynab_payee_ids)
            cursor = self._conn.execute(
                f"DELETE FROM payee_mappings WHERE ynab_budget_id = ? "
                f"AND ynab_payee_id IN ({placeholders})",
                (ynab_budget_id, *ynab_payee_ids),
            )
            self._conn.commit()
            return cursor.rowcount

    # -- transformer_default_budgets ----------------------------------
    #
    # Per-transformer default YNAB budget setting, settable via the GUI.

    def list_transformer_default_budgets(self) -> dict[str, str]:
        """transformer_name -> ynab_budget_id for every transformer that has a
        configured default. Missing key means no default set."""
        rows = self._conn.execute(
            "SELECT transformer_name, ynab_budget_id FROM transformer_default_budgets"
        ).fetchall()
        return {row["transformer_name"]: row["ynab_budget_id"] for row in rows}

    async def set_transformer_default_budget(
        self, transformer_name: str, ynab_budget_id: str
    ) -> None:
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO transformer_default_budgets (transformer_name, ynab_budget_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(transformer_name) DO UPDATE SET
                    ynab_budget_id = excluded.ynab_budget_id,
                    updated_at = excluded.updated_at
                """,
                (transformer_name, ynab_budget_id, datetime.now(UTC).isoformat()),
            )
            self._conn.commit()

    async def clear_transformer_default_budget(self, transformer_name: str) -> None:
        async with self._lock:
            self._conn.execute(
                "DELETE FROM transformer_default_budgets WHERE transformer_name = ?",
                (transformer_name,),
            )
            self._conn.commit()

    # -- audit_events -------------------------------------------------
    #
    # Append-only per-transaction event log (created/updated/duplicate/
    # skipped) - purely observational, backs the GUI's Audit Log tab.
    # Unlike tracked_transactions (a current-state table, upserted in
    # place), a row here is never mutated once written. See engine.py's
    # call sites for exactly which outcome writes which event_type - this
    # layer only stores what it's given, it makes no classification
    # decisions of its own.

    AUDIT_EVENT_TYPES = ("created", "updated", "duplicate", "skipped")

    # Whitelisted sort columns for list_audit_events - never interpolate a
    # caller-supplied column name directly into SQL, even though today's
    # only caller (webapp/routes/audit.py) already constrains sort_by via
    # its own Literal type. Belt and suspenders against a future caller.
    AUDIT_EVENT_SORT_COLUMNS = (
        "occurred_at",
        "event_type",
        "source",
        "account_key",
        "payee_name",
        "memo",
        "amount_milliunits",
        "detail",
    )

    async def insert_audit_event(
        self,
        *,
        event_type: str,
        source: str,
        account_key: str | None = None,
        tracking_key: str | None = None,
        import_id: str | None = None,
        ynab_transaction_id: str | None = None,
        ynab_budget_id: str | None = None,
        ynab_account_id: str | None = None,
        payee_name: str | None = None,
        memo: str | None = None,
        transaction_date: str | None = None,
        amount_milliunits: int | None = None,
        detail: str | None = None,
    ) -> None:
        async with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_events (
                    occurred_at, event_type, source, account_key, tracking_key,
                    import_id, ynab_transaction_id, ynab_budget_id, ynab_account_id,
                    payee_name, memo, transaction_date, amount_milliunits, detail
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    event_type,
                    source,
                    account_key,
                    tracking_key,
                    import_id,
                    ynab_transaction_id,
                    ynab_budget_id,
                    ynab_account_id,
                    payee_name,
                    memo,
                    transaction_date,
                    amount_milliunits,
                    detail,
                ),
            )
            self._conn.commit()

    def get_audit_event(self, event_id: int) -> dict[str, Any] | None:
        """Fetch a single audit event by id, or None if not found."""
        row = self._conn.execute(
            "SELECT * FROM audit_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_audit_events(
        self,
        *,
        event_type: str | None = None,
        account_key: str | None = None,
        include_skipped: bool = False,
        sort_by: str = "occurred_at",
        sort_dir: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Most-recent-first (by default) page of audit events plus the
        total count matching the same filter (for pagination). An explicit
        event_type always wins over include_skipped - e.g. ?event_type=skipped
        works regardless of include_skipped's value, matching the most
        intuitive reading of "I asked for exactly this category".
        """
        if sort_by not in self.AUDIT_EVENT_SORT_COLUMNS:
            raise ValueError(f"unsortable audit_events column: {sort_by!r}")
        if sort_dir not in ("asc", "desc"):
            raise ValueError(f"invalid sort direction: {sort_dir!r}")

        where_clauses: list[str] = []
        params: list[Any] = []
        if event_type is not None:
            where_clauses.append("event_type = ?")
            params.append(event_type)
        elif not include_skipped:
            where_clauses.append("event_type != 'skipped'")
        if account_key is not None:
            where_clauses.append("account_key = ?")
            params.append(account_key)
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        total = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM audit_events {where_sql}", params
        ).fetchone()["n"]
        # id as a tiebreaker keeps paging stable when many rows share the
        # same sort value (e.g. every row sorted by event_type).
        order_sql = f"{sort_by} {sort_dir.upper()}, id {sort_dir.upper()}"
        rows = self._conn.execute(
            f"SELECT * FROM audit_events {where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows], total

    def count_audit_events_by_type(self) -> dict[str, int]:
        """Cheap per-category totals for the GUI's summary strip - zero-
        filled for every known event_type so the frontend never has to
        special-case a missing key.
        """
        rows = self._conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM audit_events GROUP BY event_type"
        ).fetchall()
        counts = dict.fromkeys(self.AUDIT_EVENT_TYPES, 0)
        counts.update({row["event_type"]: row["n"] for row in rows})
        return counts

    async def prune_audit_events(self, cutoff_date: date) -> int:
        """Delete audit_events rows from strictly before cutoff_date, same
        retention_days knob prune_booked_transactions already uses - this
        table has no PENDING-style "must survive to be matched later" rows
        (every event here already represents something that finished
        happening), so a plain date cutoff is safe with no carve-out.
        Compares on date(occurred_at) rather than the raw datetime string
        so today's rows are never pruned regardless of what time "now" is.
        """
        async with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM audit_events WHERE date(occurred_at) < ?",
                (cutoff_date.isoformat(),),
            )
            self._conn.commit()
            return cursor.rowcount
