from pathlib import Path

from ynab_auto_sync.state import JsonStateStore


def test_read_missing_file_returns_empty_dict(tmp_path: Path):
    store = JsonStateStore(tmp_path / "does_not_exist.json")
    assert store.read() == {}


def test_write_then_read_roundtrip(tmp_path: Path):
    store = JsonStateStore(tmp_path / "state.json")
    store.write({"a": 1, "b": "two"})
    assert store.read() == {"a": 1, "b": "two"}


def test_write_creates_parent_directories(tmp_path: Path):
    store = JsonStateStore(tmp_path / "nested" / "dir" / "state.json")
    store.write({"x": True})
    assert store.read() == {"x": True}


def test_write_does_not_leave_tmp_files_behind(tmp_path: Path):
    store = JsonStateStore(tmp_path / "state.json")
    store.write({"a": 1})
    leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []


def test_update_merges_into_existing_data(tmp_path: Path):
    store = JsonStateStore(tmp_path / "state.json")
    store.write({"a": 1, "b": 2})
    result = store.update(b=20, c=3)
    assert result == {"a": 1, "b": 20, "c": 3}
    assert store.read() == {"a": 1, "b": 20, "c": 3}
