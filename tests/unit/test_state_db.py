import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ynab_auto_sync.sync.state_db import MappingValidationError, StateDB, compute_since


def test_compute_since_with_no_prior_state_uses_initial_backfill():
    since = compute_since(None, initial_backfill_days=30, lookback_overlap_hours=72)
    expected = datetime.now(UTC) - timedelta(days=30)
    assert abs((since - expected).total_seconds()) < 5


def test_compute_since_applies_overlap_when_recent():
    last_synced = datetime.now(UTC) - timedelta(hours=1)
    state = {"last_synced_at": last_synced.isoformat()}
    since = compute_since(state, initial_backfill_days=30, lookback_overlap_hours=72)
    expected = last_synced - timedelta(hours=72)
    assert abs((since - expected).total_seconds()) < 5


def test_compute_since_never_exceeds_initial_backfill_bound():
    old_sync = datetime.now(UTC) - timedelta(days=200)
    state = {"last_synced_at": old_sync.isoformat()}
    since = compute_since(state, initial_backfill_days=30, lookback_overlap_hours=72)
    earliest_allowed = datetime.now(UTC) - timedelta(days=30)
    assert abs((since - earliest_allowed).total_seconds()) < 5


async def test_account_cursor_roundtrip(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    assert db.read_account_states() == {}
    await db.record_success(
        {"acct-1": "2026-08-20T00:00:00+00:00"}, imported=0, updated=0, duplicates=0
    )
    assert db.read_account_states() == {
        "acct-1": {"last_synced_at": "2026-08-20T00:00:00+00:00"}
    }


async def test_record_success_updates_accounts_and_run_metadata(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.record_success(
        {"acct-1": "2026-08-20T00:00:00+00:00"}, imported=2, updated=1, duplicates=1
    )
    meta = db.read_run_metadata()
    assert meta["last_error"] is None
    assert meta["auth_required"] is False
    assert meta["imported_total"] == 2
    assert meta["imported_last_run"] == 2
    assert meta["updated_last_run"] == 1
    assert meta["duplicates_last_run"] == 1
    assert db.read_account_states()["acct-1"]["last_synced_at"] == "2026-08-20T00:00:00+00:00"


async def test_record_success_persists_fetched_last_run(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.record_success({}, imported=2, updated=1, duplicates=0, fetched=7)
    assert db.read_run_metadata()["fetched_last_run"] == 7


async def test_record_failure_zeroes_fetched_last_run(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.record_success({}, imported=1, updated=0, duplicates=0, fetched=5)
    await db.record_failure("boom", auth_required=False)
    assert db.read_run_metadata()["fetched_last_run"] == 0


async def test_record_success_accumulates_imported_total(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.record_success({}, imported=3, updated=0, duplicates=0)
    await db.record_success({}, imported=2, updated=0, duplicates=0)
    assert db.read_run_metadata()["imported_total"] == 5


async def test_record_failure_does_not_touch_account_cursors(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.record_success(
        {"acct-1": "2026-08-20T00:00:00+00:00"}, imported=1, updated=0, duplicates=0
    )
    await db.record_failure("boom", auth_required=False)

    meta = db.read_run_metadata()
    assert meta["last_error"] == "boom"
    assert meta["auth_required"] is False
    # account cursor from the earlier success must survive an unrelated failure
    assert db.read_account_states()["acct-1"]["last_synced_at"] == "2026-08-20T00:00:00+00:00"


async def test_record_failure_marks_auth_required(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.record_failure("refresh token dead", auth_required=True)
    meta = db.read_run_metadata()
    assert meta["auth_required"] is True
    assert meta["imported_last_run"] == 0
    assert meta["updated_last_run"] == 0
    assert meta["duplicates_last_run"] == 0


async def test_pause_flag_roundtrip(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    assert db.read_paused() is False
    await db.set_paused(True)
    assert db.read_paused() is True


async def test_log_level_defaults_to_none_until_ever_set(tmp_path: Path):
    # None means "no runtime override yet" - the caller (webapp/routes/
    # settings.py, __main__.py) falls back to config.logging.level.
    db = StateDB(tmp_path / "state.db")
    assert db.read_run_metadata()["log_level"] is None


async def test_set_log_level_roundtrip_and_uppercases(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.set_log_level("debug")
    assert db.read_run_metadata()["log_level"] == "DEBUG"


def test_get_tracked_returns_none_for_unknown_id(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    assert db.get_tracked("does-not-exist") is None


async def test_upsert_tracked_then_get_tracked(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-1",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="PENDING",
        amount_milliunits=-15000,
    )
    tracked = db.get_tracked("sb1-1")
    assert tracked is not None
    assert tracked["sb1_transaction_id"] == "sb1-1"
    assert tracked["import_id"] == "import-1"
    assert tracked["ynab_transaction_id"] == "ynab-1"
    assert tracked["ynab_budget_id"] == "budget-1"
    assert tracked["account_key"] == "acct-1"
    assert tracked["booking_status"] == "PENDING"
    assert tracked["amount_milliunits"] == -15000
    assert tracked["first_seen_at"] is not None
    assert tracked["last_checked_at"] is not None


async def test_upsert_tracked_twice_preserves_first_seen_at(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-1",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="PENDING",
        amount_milliunits=-15000,
    )
    first = db.get_tracked("sb1-1")

    await db.upsert_tracked(
        "sb1-1",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-15500,
    )
    second = db.get_tracked("sb1-1")

    assert second["first_seen_at"] == first["first_seen_at"]
    assert second["booking_status"] == "BOOKED"
    assert second["amount_milliunits"] == -15500


async def test_mark_booked_flips_status_and_amount(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-1",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="PENDING",
        amount_milliunits=-15000,
    )
    await db.mark_booked("sb1-1", -15250)
    tracked = db.get_tracked("sb1-1")
    assert tracked["booking_status"] == "BOOKED"
    assert tracked["amount_milliunits"] == -15250


async def test_mark_booked_raises_on_unknown_id(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    with pytest.raises(ValueError, match="sb1-unknown"):
        await db.mark_booked("sb1-unknown", -100)


async def test_mark_booked_sets_cleared_so_a_later_readd_restores_booked_not_pending(
    tmp_path: Path,
):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-1",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="PENDING",
        amount_milliunits=-15000,
        cleared="uncleared",
    )
    await db.mark_booked("sb1-1", -15250)
    tracked = db.get_tracked("sb1-1")
    assert tracked["cleared"] == "cleared"


async def test_rekey_pending_to_booked_changes_pk_and_status(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-old",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-cc",
        booking_status="PENDING",
        amount_milliunits=-79000,
    )
    previous = await db.rekey_pending_to_booked("sb1-old", "sb1-new", -79100)

    assert previous is not None
    assert previous["amount_milliunits"] == -79000
    assert db.get_tracked("sb1-old") is None
    tracked = db.get_tracked("sb1-new")
    assert tracked is not None
    assert tracked["booking_status"] == "BOOKED"
    assert tracked["amount_milliunits"] == -79100
    assert tracked["cleared"] == "cleared"
    assert tracked["ynab_transaction_id"] == "ynab-1"


async def test_rekey_pending_to_booked_idempotent_retry(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-old",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-cc",
        booking_status="PENDING",
        amount_milliunits=-79000,
    )
    await db.rekey_pending_to_booked("sb1-old", "sb1-new", -79100)

    second = await db.rekey_pending_to_booked("sb1-old", "sb1-new", -79100)
    assert second is None
    tracked = db.get_tracked("sb1-new")
    assert tracked["booking_status"] == "BOOKED"
    assert tracked["amount_milliunits"] == -79100


async def test_rekey_pending_to_booked_raises_on_genuine_missing_key(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    with pytest.raises(ValueError, match="sb1-old"):
        await db.rekey_pending_to_booked("sb1-old", "sb1-new", -100)


async def test_list_pending_candidates_scoped_to_account_and_pending_status(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-a-pending",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-a",
        booking_status="PENDING",
        amount_milliunits=-10000,
        payee_name="Merchant A",
    )
    await db.upsert_tracked(
        "sb1-a-booked",
        import_id="import-2",
        ynab_transaction_id="ynab-2",
        ynab_budget_id="budget-1",
        account_key="acct-a",
        booking_status="BOOKED",
        amount_milliunits=-20000,
    )
    await db.upsert_tracked(
        "sb1-b-pending",
        import_id="import-3",
        ynab_transaction_id="ynab-3",
        ynab_budget_id="budget-1",
        account_key="acct-b",
        booking_status="PENDING",
        amount_milliunits=-30000,
    )

    candidates = db.list_pending_candidates("acct-a")
    assert len(candidates) == 1
    assert candidates[0]["sb1_transaction_id"] == "sb1-a-pending"
    assert candidates[0]["payee_name"] == "Merchant A"


async def test_get_tracked_by_ynab_transaction_id(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-1",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-10000,
    )
    tracked = db.get_tracked_by_ynab_transaction_id("ynab-1")
    assert tracked is not None
    assert tracked["sb1_transaction_id"] == "sb1-1"
    assert db.get_tracked_by_ynab_transaction_id("ynab-unknown") is None


async def test_upsert_tracked_stores_reconstruction_fields(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-1",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-15000,
        payee_name="Micro Kaffi AS",
        memo="Zettle_*Micro Kaffi AS",
        transaction_date="2026-08-20",
        ynab_account_id="ynab-acct-1",
        cleared="cleared",
    )
    tracked = db.get_tracked("sb1-1")
    assert tracked["payee_name"] == "Micro Kaffi AS"
    assert tracked["memo"] == "Zettle_*Micro Kaffi AS"
    assert tracked["transaction_date"] == "2026-08-20"
    assert tracked["ynab_account_id"] == "ynab-acct-1"
    assert tracked["cleared"] == "cleared"
    assert tracked["readd_count"] == 0


async def test_list_deleted_transactions_only_returns_deleted_status(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-booked",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-1000,
    )
    await db.upsert_tracked(
        "sb1-deleted",
        import_id="import-2",
        ynab_transaction_id="ynab-2",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="DELETED",
        amount_milliunits=-2000,
        payee_name="Some Shop",
    )
    deleted = db.list_deleted_transactions()
    assert [row["sb1_transaction_id"] for row in deleted] == ["sb1-deleted"]
    assert deleted[0]["payee_name"] == "Some Shop"


async def test_mark_readded_restores_pending_when_cleared_was_uncleared(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-1",
        import_id="import-old",
        ynab_transaction_id="ynab-old",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="DELETED",
        amount_milliunits=-1000,
        cleared="uncleared",
    )
    await db.mark_readded("sb1-1", new_ynab_transaction_id="ynab-new", new_import_id="import-new")
    tracked = db.get_tracked("sb1-1")
    assert tracked["booking_status"] == "PENDING"
    assert tracked["ynab_transaction_id"] == "ynab-new"
    assert tracked["import_id"] == "import-new"
    assert tracked["readd_count"] == 1


async def test_mark_readded_restores_booked_when_cleared_was_cleared(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-1",
        import_id="import-old",
        ynab_transaction_id="ynab-old",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="DELETED",
        amount_milliunits=-1000,
        cleared="cleared",
    )
    await db.mark_readded("sb1-1", new_ynab_transaction_id="ynab-new", new_import_id="import-new")
    assert db.get_tracked("sb1-1")["booking_status"] == "BOOKED"


async def test_mark_readded_raises_on_unknown_id(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    with pytest.raises(ValueError, match="sb1-unknown"):
        await db.mark_readded("sb1-unknown", new_ynab_transaction_id="x", new_import_id="y")


async def test_prune_booked_transactions_deletes_only_past_cutoff(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-old",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-1000,
        transaction_date="2020-01-01",
    )
    await db.upsert_tracked(
        "sb1-recent",
        import_id="import-2",
        ynab_transaction_id="ynab-2",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-2000,
        transaction_date=datetime.now(UTC).date().isoformat(),
    )

    pruned = await db.prune_booked_transactions(datetime.now(UTC).date() - timedelta(days=365))

    assert pruned == 1
    assert db.get_tracked("sb1-old") is None
    assert db.get_tracked("sb1-recent") is not None


async def test_prune_booked_transactions_never_prunes_pending(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_tracked(
        "sb1-pending",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="PENDING",
        amount_milliunits=-1000,
        transaction_date="2020-01-01",
    )

    pruned = await db.prune_booked_transactions(datetime.now(UTC).date())

    assert pruned == 0
    assert db.get_tracked("sb1-pending") is not None


async def test_maybe_vacuum_runs_first_time_and_respects_min_interval(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    assert await db.maybe_vacuum(min_interval_days=30) is True
    assert await db.maybe_vacuum(min_interval_days=30) is False


def test_opens_and_migrates_a_pre_existing_older_schema_db(tmp_path: Path):
    # Simulates a real state/sync_state.db written before the
    # resolved_deleted_last_run / reconstruction-field columns existed -
    # StateDB must add them via ALTER TABLE, not just skip via
    # CREATE TABLE IF NOT EXISTS, and must not lose existing data.
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE account_cursors (account_key TEXT PRIMARY KEY, last_synced_at TEXT);
        CREATE TABLE run_metadata (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_run_at TEXT, last_success_at TEXT, last_error TEXT,
            auth_required INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0,
            imported_total INTEGER NOT NULL DEFAULT 0, imported_last_run INTEGER NOT NULL DEFAULT 0,
            updated_last_run INTEGER NOT NULL DEFAULT 0, duplicates_last_run INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE tracked_transactions (
            sb1_transaction_id TEXT PRIMARY KEY, import_id TEXT NOT NULL,
            ynab_transaction_id TEXT NOT NULL, ynab_budget_id TEXT NOT NULL,
            account_key TEXT NOT NULL, booking_status TEXT NOT NULL,
            amount_milliunits INTEGER NOT NULL, first_seen_at TEXT NOT NULL,
            last_checked_at TEXT NOT NULL
        );
        INSERT INTO run_metadata (id, imported_total) VALUES (1, 42);
        INSERT INTO tracked_transactions VALUES (
            'sb1-old', 'import-old', 'ynab-old', 'budget-1', 'acct-1', 'BOOKED',
            -5000, '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    db = StateDB(db_path)
    assert db.read_run_metadata()["imported_total"] == 42  # pre-existing data preserved
    assert db.read_run_metadata()["resolved_deleted_last_run"] == 0
    assert db.read_run_metadata()["fetched_last_run"] == 0
    assert db.read_run_metadata()["log_level"] is None
    old_row = db.get_tracked("sb1-old")
    assert old_row["amount_milliunits"] == -5000  # pre-existing row preserved
    assert old_row["payee_name"] is None  # new column, backfilled as NULL


async def test_opens_and_migrates_and_new_functionality_works(tmp_path: Path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE account_cursors (account_key TEXT PRIMARY KEY, last_synced_at TEXT);
        CREATE TABLE run_metadata (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_run_at TEXT, last_success_at TEXT, last_error TEXT,
            auth_required INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0,
            imported_total INTEGER NOT NULL DEFAULT 0, imported_last_run INTEGER NOT NULL DEFAULT 0,
            updated_last_run INTEGER NOT NULL DEFAULT 0, duplicates_last_run INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE tracked_transactions (
            sb1_transaction_id TEXT PRIMARY KEY, import_id TEXT NOT NULL,
            ynab_transaction_id TEXT NOT NULL, ynab_budget_id TEXT NOT NULL,
            account_key TEXT NOT NULL, booking_status TEXT NOT NULL,
            amount_milliunits INTEGER NOT NULL, first_seen_at TEXT NOT NULL,
            last_checked_at TEXT NOT NULL
        );
        INSERT INTO run_metadata (id, imported_total) VALUES (1, 42);
        """
    )
    conn.commit()
    conn.close()

    db = StateDB(db_path)

    # And the new functionality works against the migrated table.
    await db.upsert_tracked(
        "sb1-new",
        import_id="import-new",
        ynab_transaction_id="ynab-new",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="DELETED",
        amount_milliunits=-1000,
        payee_name="New Shop",
    )
    assert db.list_deleted_transactions()[0]["payee_name"] == "New Shop"


# -- account_mappings ---------------------------------------------------


async def test_create_mapping_then_get_and_list_roundtrip(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    mapping_id = await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
        display_name="Everyday account",
        import_source_name="everyday",
    )
    assert isinstance(mapping_id, int)

    fetched = db.get_mapping(mapping_id)
    assert fetched is not None
    assert fetched["provider"] == "sparebank1"
    assert fetched["provider_account_id"] == "acct-key-1"
    assert fetched["ynab_budget_id"] == "budget-1"
    assert fetched["ynab_account_id"] == "ynab-acct-1"
    assert fetched["display_name"] == "Everyday account"
    assert fetched["import_source_name"] == "everyday"
    assert fetched["enabled"] is True
    assert fetched["created_at"] is not None
    assert fetched["updated_at"] is not None

    listed = db.list_mappings()
    assert [row["id"] for row in listed] == [mapping_id]


def test_get_mapping_returns_none_for_unknown_id(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    assert db.get_mapping(999) is None


async def test_update_mapping_changes_fields_and_bumps_updated_at(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    mapping_id = await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
    )
    before = db.get_mapping(mapping_id)

    await db.update_mapping(mapping_id, display_name="Renamed", enabled=False)

    after = db.get_mapping(mapping_id)
    assert after["display_name"] == "Renamed"
    assert after["enabled"] is False
    # unrelated fields untouched
    assert after["provider_account_id"] == "acct-key-1"
    assert after["updated_at"] >= before["updated_at"]


async def test_update_mapping_raises_on_unknown_id(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    with pytest.raises(ValueError, match="999"):
        await db.update_mapping(999, display_name="x")


async def test_delete_mapping_removes_row(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    mapping_id = await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
    )
    await db.delete_mapping(mapping_id)
    assert db.get_mapping(mapping_id) is None
    assert db.list_mappings() == []


async def test_delete_all_mappings_removes_every_row_and_returns_count(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
    )
    await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-2",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-2",
    )

    deleted = await db.delete_all_mappings()

    assert deleted == 2
    assert db.list_mappings() == []


async def test_delete_all_mappings_on_empty_table_returns_zero(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    assert await db.delete_all_mappings() == 0


async def test_list_mappings_enabled_only_filter(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    enabled_id = await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
    )
    await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-2",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-2",
        enabled=False,
    )

    all_mappings = db.list_mappings()
    enabled_mappings = db.list_mappings(enabled_only=True)

    assert len(all_mappings) == 2
    assert [row["id"] for row in enabled_mappings] == [enabled_id]


async def test_create_mapping_duplicate_provider_account_id_raises(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
    )
    with pytest.raises(MappingValidationError):
        await db.create_mapping(
            provider="sparebank1",
            provider_account_id="acct-key-1",
            ynab_budget_id="budget-2",
            ynab_account_id="ynab-acct-2",
        )
    # the failed attempt must not have left a partial row behind
    assert len(db.list_mappings()) == 1


async def test_update_mapping_duplicate_provider_account_id_raises(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
    )
    other_id = await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-2",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-2",
    )
    with pytest.raises(MappingValidationError):
        await db.update_mapping(other_id, provider_account_id="acct-key-1")


async def test_create_mapping_duplicate_import_source_name_raises(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
        import_source_name="everyday",
    )
    with pytest.raises(MappingValidationError):
        await db.create_mapping(
            provider="sparebank1",
            provider_account_id="acct-key-2",
            ynab_budget_id="budget-1",
            ynab_account_id="ynab-acct-2",
            import_source_name="everyday",
        )


async def test_update_mapping_duplicate_import_source_name_raises(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
        import_source_name="everyday",
    )
    other_id = await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-2",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-2",
    )
    with pytest.raises(MappingValidationError):
        await db.update_mapping(other_id, import_source_name="everyday")


async def test_blank_import_source_name_may_repeat_freely(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
    )
    # default import_source_name is "" for both - must not collide
    second_id = await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-2",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-2",
    )
    assert db.get_mapping(second_id)["import_source_name"] == ""
    assert len(db.list_mappings()) == 2


async def test_update_mapping_can_keep_import_source_name_unchanged(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    mapping_id = await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-1",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
        import_source_name="everyday",
    )
    # re-saving the same import_source_name on the same row must not
    # self-collide
    await db.update_mapping(mapping_id, import_source_name="everyday")
    assert db.get_mapping(mapping_id)["import_source_name"] == "everyday"


async def test_seed_mappings_from_config_populates_empty_table(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    rows = [
        {
            "provider": "sparebank1",
            "provider_account_id": "acct-key-1",
            "ynab_budget_id": "budget-1",
            "ynab_account_id": "ynab-acct-1",
            "display_name": "Everyday",
            "import_source_name": "everyday",
            "enabled": True,
        },
        {
            "provider": "sparebank1",
            "provider_account_id": "acct-key-2",
            "ynab_budget_id": "budget-1",
            "ynab_account_id": "ynab-acct-2",
        },
    ]
    created = await db.seed_mappings_from_config(rows)
    assert created == 2
    assert len(db.list_mappings()) == 2


async def test_seed_mappings_from_config_is_a_noop_when_table_already_has_rows(
    tmp_path: Path,
):
    db = StateDB(tmp_path / "state.db")
    await db.create_mapping(
        provider="sparebank1",
        provider_account_id="acct-key-existing",
        ynab_budget_id="budget-1",
        ynab_account_id="ynab-acct-1",
    )
    rows = [
        {
            "provider": "sparebank1",
            "provider_account_id": "acct-key-new",
            "ynab_budget_id": "budget-1",
            "ynab_account_id": "ynab-acct-2",
        }
    ]
    created = await db.seed_mappings_from_config(rows)
    assert created == 0
    listed = db.list_mappings()
    assert len(listed) == 1
    assert listed[0]["provider_account_id"] == "acct-key-existing"


async def test_seed_mappings_from_config_called_twice_only_seeds_once(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    rows = [
        {
            "provider": "sparebank1",
            "provider_account_id": "acct-key-1",
            "ynab_budget_id": "budget-1",
            "ynab_account_id": "ynab-acct-1",
        }
    ]
    first = await db.seed_mappings_from_config(rows)
    second = await db.seed_mappings_from_config(rows)
    assert first == 1
    assert second == 0
    assert len(db.list_mappings()) == 1


async def test_seed_mappings_from_config_preserves_account_id_byte_for_byte(
    tmp_path: Path,
):
    # This is the sharpest risk in the whole refactor: provider_account_id
    # must be an exact copy of the old sparebank1_account_key, because it is
    # the prefix of every existing tracked_transactions primary key and
    # feeds every import_id already burned into YNAB (CLAUDE.md invariant
    # 2). Any trimming/case-folding/whitespace change here would silently
    # break matching for a real user's entire synced history. Use a value
    # deliberately shaped to expose exactly those bugs: mixed case, leading/
    # trailing whitespace-adjacent characters, and a length that would be
    # truncated if anyone "helpfully" shortened it.
    tricky_account_key = "  ACC-0099.MixedCase-KEY_with.punct  "
    db = StateDB(tmp_path / "state.db")
    rows = [
        {
            "provider": "sparebank1",
            "provider_account_id": tricky_account_key,
            "ynab_budget_id": "budget-1",
            "ynab_account_id": "ynab-acct-1",
        }
    ]
    await db.seed_mappings_from_config(rows)
    stored = db.list_mappings()[0]["provider_account_id"]
    assert stored == tricky_account_key
    assert len(stored) == len(tricky_account_key)


async def test_count_tracked_for_account_counts_matching_rows(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    assert db.count_tracked_for_account("acct-1") == 0

    await db.upsert_tracked(
        "acct-1:tx-1",
        import_id="import-1",
        ynab_transaction_id="ynab-1",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-1000,
    )
    await db.upsert_tracked(
        "acct-1:tx-2",
        import_id="import-2",
        ynab_transaction_id="ynab-2",
        ynab_budget_id="budget-1",
        account_key="acct-1",
        booking_status="BOOKED",
        amount_milliunits=-2000,
    )
    await db.upsert_tracked(
        "acct-2:tx-1",
        import_id="import-3",
        ynab_transaction_id="ynab-3",
        ynab_budget_id="budget-1",
        account_key="acct-2",
        booking_status="BOOKED",
        amount_milliunits=-3000,
    )

    assert db.count_tracked_for_account("acct-1") == 2
    assert db.count_tracked_for_account("acct-2") == 1
    assert db.count_tracked_for_account("acct-3") == 0


def test_opens_pre_existing_db_without_account_mappings_table(tmp_path: Path):
    # A DB created before this table existed at all - CREATE TABLE IF NOT
    # EXISTS in _SCHEMA must add it (no ALTER TABLE migration needed since
    # it's a brand-new table, not new columns on an existing one).
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE account_cursors (account_key TEXT PRIMARY KEY, last_synced_at TEXT);
        CREATE TABLE run_metadata (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_run_at TEXT, last_success_at TEXT, last_error TEXT,
            auth_required INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0,
            imported_total INTEGER NOT NULL DEFAULT 0, imported_last_run INTEGER NOT NULL DEFAULT 0,
            updated_last_run INTEGER NOT NULL DEFAULT 0, duplicates_last_run INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE tracked_transactions (
            sb1_transaction_id TEXT PRIMARY KEY, import_id TEXT NOT NULL,
            ynab_transaction_id TEXT NOT NULL, ynab_budget_id TEXT NOT NULL,
            account_key TEXT NOT NULL, booking_status TEXT NOT NULL,
            amount_milliunits INTEGER NOT NULL, first_seen_at TEXT NOT NULL,
            last_checked_at TEXT NOT NULL
        );
        INSERT INTO run_metadata (id, imported_total) VALUES (1, 42);
        """
    )
    conn.commit()
    conn.close()

    db = StateDB(db_path)
    assert db.list_mappings() == []


async def test_get_payee_id_returns_none_when_no_mapping(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    assert db.get_payee_id("budget-1", "SOME MERCHANT") is None


async def test_upsert_payee_mapping_roundtrip(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_payee_mapping("budget-1", "SOME MERCHANT", "payee-abc")
    assert db.get_payee_id("budget-1", "SOME MERCHANT") == "payee-abc"


async def test_get_payee_id_is_scoped_per_budget(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_payee_mapping("budget-1", "SOME MERCHANT", "payee-abc")
    assert db.get_payee_id("budget-2", "SOME MERCHANT") is None


async def test_upsert_payee_mapping_first_write_wins(tmp_path: Path):
    # The cached payee_id is a permanent anchor to whatever the user has
    # since renamed the payee to in YNAB - a later create for the same raw
    # text must never silently overwrite it with a different payee_id.
    db = StateDB(tmp_path / "state.db")
    await db.upsert_payee_mapping("budget-1", "SOME MERCHANT", "payee-abc")
    await db.upsert_payee_mapping("budget-1", "SOME MERCHANT", "payee-xyz")
    assert db.get_payee_id("budget-1", "SOME MERCHANT") == "payee-abc"


async def test_delete_payee_mappings_for_ids_removes_matching_rows(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")
    await db.upsert_payee_mapping("budget-1", "MERCHANT B", "payee-b")

    deleted = await db.delete_payee_mappings_for_ids("budget-1", {"payee-a"})

    assert deleted == 1
    assert db.get_payee_id("budget-1", "MERCHANT A") is None
    assert db.get_payee_id("budget-1", "MERCHANT B") == "payee-b"


async def test_delete_payee_mappings_for_ids_is_noop_when_ids_empty(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")

    deleted = await db.delete_payee_mappings_for_ids("budget-1", set())

    assert deleted == 0
    assert db.get_payee_id("budget-1", "MERCHANT A") == "payee-a"


async def test_delete_payee_mappings_for_ids_is_scoped_per_budget(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")
    await db.upsert_payee_mapping("budget-2", "MERCHANT A", "payee-a")

    deleted = await db.delete_payee_mappings_for_ids("budget-1", {"payee-a"})

    assert deleted == 1
    assert db.get_payee_id("budget-1", "MERCHANT A") is None
    assert db.get_payee_id("budget-2", "MERCHANT A") == "payee-a"


async def test_list_payee_ids_returns_distinct_ids_for_budget(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.upsert_payee_mapping("budget-1", "MERCHANT A", "payee-a")
    # Two different raw texts legitimately sharing one real payee_id.
    await db.upsert_payee_mapping("budget-1", "MERCHANT A ALT SPELLING", "payee-a")
    await db.upsert_payee_mapping("budget-1", "MERCHANT B", "payee-b")
    await db.upsert_payee_mapping("budget-2", "MERCHANT C", "payee-c")

    assert sorted(db.list_payee_ids("budget-1")) == ["payee-a", "payee-b"]
    assert db.list_payee_ids("budget-2") == ["payee-c"]


async def test_list_payee_ids_empty_when_nothing_cached(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    assert db.list_payee_ids("budget-1") == []


async def test_is_payee_reconcile_due_runs_first_time_and_respects_min_interval(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    assert db.is_payee_reconcile_due(min_interval_days=1) is True
    await db.mark_payee_reconcile_done()
    assert db.is_payee_reconcile_due(min_interval_days=1) is False


# -- audit_events -----------------------------------------------------


async def test_insert_and_list_audit_events_most_recent_first(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.insert_audit_event(event_type="created", source="sparebank1", detail="first")
    await db.insert_audit_event(event_type="created", source="sparebank1", detail="second")
    events, total = db.list_audit_events()
    assert total == 2
    assert [e["detail"] for e in events] == ["second", "first"]


async def test_list_audit_events_excludes_skipped_by_default(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.insert_audit_event(event_type="created", source="sparebank1")
    await db.insert_audit_event(event_type="skipped", source="sparebank1")
    events, total = db.list_audit_events()
    assert total == 1
    assert events[0]["event_type"] == "created"


async def test_list_audit_events_include_skipped_true_returns_everything(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.insert_audit_event(event_type="created", source="sparebank1")
    await db.insert_audit_event(event_type="skipped", source="sparebank1")
    _events, total = db.list_audit_events(include_skipped=True)
    assert total == 2


async def test_list_audit_events_explicit_type_wins_over_include_skipped(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.insert_audit_event(event_type="skipped", source="sparebank1")
    events, total = db.list_audit_events(event_type="skipped", include_skipped=False)
    assert total == 1
    assert events[0]["event_type"] == "skipped"


async def test_list_audit_events_filters_by_event_type(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.insert_audit_event(event_type="created", source="sparebank1")
    await db.insert_audit_event(event_type="updated", source="sparebank1")
    events, total = db.list_audit_events(event_type="updated")
    assert total == 1
    assert events[0]["event_type"] == "updated"


async def test_list_audit_events_paginates(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    for i in range(5):
        await db.insert_audit_event(event_type="created", source="sparebank1", detail=str(i))
    page1, total = db.list_audit_events(limit=2, offset=0)
    page2, _ = db.list_audit_events(limit=2, offset=2)
    assert total == 5
    assert len(page1) == 2
    assert len(page2) == 2
    assert [e["id"] for e in page1] != [e["id"] for e in page2]


async def test_list_audit_events_filters_by_account_key(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.insert_audit_event(event_type="created", source="sparebank1", account_key="acct-1")
    await db.insert_audit_event(event_type="created", source="sparebank1", account_key="acct-2")
    events, total = db.list_audit_events(account_key="acct-1")
    assert total == 1
    assert events[0]["account_key"] == "acct-1"


async def test_list_audit_events_sorts_by_requested_column_and_direction(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.insert_audit_event(event_type="created", source="sparebank1", payee_name="Zebra")
    await db.insert_audit_event(event_type="created", source="sparebank1", payee_name="Apple")
    asc, _ = db.list_audit_events(sort_by="payee_name", sort_dir="asc")
    desc, _ = db.list_audit_events(sort_by="payee_name", sort_dir="desc")
    assert [e["payee_name"] for e in asc] == ["Apple", "Zebra"]
    assert [e["payee_name"] for e in desc] == ["Zebra", "Apple"]


async def test_list_audit_events_rejects_unsortable_column(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    with pytest.raises(ValueError, match="unsortable"):
        db.list_audit_events(sort_by="id; DROP TABLE audit_events")


async def test_list_audit_events_rejects_invalid_sort_direction(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    with pytest.raises(ValueError, match="invalid sort direction"):
        db.list_audit_events(sort_dir="sideways")


async def test_count_audit_events_by_type_is_zero_filled(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    assert db.count_audit_events_by_type() == {
        "created": 0,
        "updated": 0,
        "duplicate": 0,
        "skipped": 0,
    }


async def test_count_audit_events_by_type_counts_each_category(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.insert_audit_event(event_type="created", source="sparebank1")
    await db.insert_audit_event(event_type="created", source="sparebank1")
    await db.insert_audit_event(event_type="skipped", source="sparebank1")
    counts = db.count_audit_events_by_type()
    assert counts["created"] == 2
    assert counts["skipped"] == 1
    assert counts["updated"] == 0


async def test_prune_audit_events_deletes_rows_before_cutoff(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.insert_audit_event(event_type="created", source="sparebank1")
    # Backdate the row directly - insert_audit_event always stamps "now".
    db._conn.execute("UPDATE audit_events SET occurred_at = '2020-01-01T00:00:00+00:00'")
    db._conn.commit()
    deleted = await db.prune_audit_events(datetime.now(UTC).date())
    assert deleted == 1
    assert db.list_audit_events()[1] == 0


async def test_prune_audit_events_never_prunes_todays_rows(tmp_path: Path):
    db = StateDB(tmp_path / "state.db")
    await db.insert_audit_event(event_type="created", source="sparebank1")
    deleted = await db.prune_audit_events(datetime.now(UTC).date())
    assert deleted == 0
    assert db.list_audit_events()[1] == 1
