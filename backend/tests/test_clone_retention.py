"""Idle clone retention: who gets announced, who gets deleted, who is spared."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

# app.core.config requires these at import; the logic under test never reads them.
os.environ.setdefault("ROOT_DATA_DIR", "/nonexistent")
os.environ.setdefault("MAIN_DATA_DIR", "replica")

from app.services import clone_retention as cr

NOW = datetime(2026, 12, 1, 3, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def days_ago(n):
    return iso(NOW - timedelta(days=n))


def run(entries, names=None, auto_delete=True):
    return cr.plan(entries, names if names is not None else list(entries), NOW, 14, 3, auto_delete)


def test_first_seen_clone_starts_its_clock_now():
    entries = {}
    cr.record_access(entries, ["a", "b"], [], NOW)
    assert entries["a"]["last_accessed_at"] == iso(NOW)
    # The day after deploy nothing is idle, however old the clone is.
    assert run(entries) == {"mark": [], "delete": [], "unmark": []}


def test_access_moves_clock_and_clears_mark():
    entries = {"a": {"last_accessed_at": days_ago(20), "delete_after": iso(NOW)}}
    cr.record_access(entries, ["a"], ["a"], NOW)
    assert entries["a"]["last_accessed_at"] == iso(NOW)
    assert "delete_after" not in entries["a"]


def test_stopped_clone_keeps_recorded_time():
    entries = {"a": {"last_accessed_at": days_ago(5)}}
    cr.record_access(entries, ["a"], [], NOW)
    assert entries["a"]["last_accessed_at"] == days_ago(5)


def test_idle_clone_is_marked_not_deleted():
    out = run({"a": {"last_accessed_at": days_ago(14)}, "b": {"last_accessed_at": days_ago(13)}})
    assert out["mark"] == [("a", NOW + timedelta(days=3))]
    assert out["delete"] == []


def test_marked_clone_deleted_only_after_grace():
    entries = {
        "due": {"last_accessed_at": days_ago(20), "delete_after": iso(NOW - timedelta(minutes=1))},
        "wait": {"last_accessed_at": days_ago(20), "delete_after": iso(NOW + timedelta(days=1))},
    }
    out = run(entries)
    assert out["delete"] == ["due"]
    assert out["mark"] == []


def test_auto_delete_off_never_deletes():
    entries = {"due": {"last_accessed_at": days_ago(20), "delete_after": days_ago(1)}}
    assert run(entries, auto_delete=False)["delete"] == []


def test_permanent_and_extended_are_spared_and_unmarked():
    entries = {
        "perm": {"last_accessed_at": days_ago(90), "permanent": True, "delete_after": days_ago(1)},
        "ext": {"last_accessed_at": days_ago(90), "keep_until": iso(NOW + timedelta(days=2)), "delete_after": days_ago(1)},
        "ext_over": {"last_accessed_at": days_ago(90), "keep_until": days_ago(1)},
    }
    out = run(entries)
    assert sorted(out["unmark"]) == ["ext", "perm"]
    assert out["delete"] == []
    assert out["mark"] == [("ext_over", NOW + timedelta(days=3))]


def test_unseen_or_ineligible_names_are_left_alone():
    entries = {"busy": {"last_accessed_at": days_ago(30), "delete_after": days_ago(1)}}
    # "busy" excluded by the caller (operation in progress); "new" has no entry.
    assert run(entries, names=["new"]) == {"mark": [], "delete": [], "unmark": []}


def test_keep_extend_permanent_and_touch():
    e = {"last_accessed_at": days_ago(20), "delete_after": iso(NOW)}
    cr.keep(e, NOW, days=7)
    assert e["keep_until"] == iso(NOW + timedelta(days=7)) and "delete_after" not in e
    cr.keep(e, NOW, permanent=True)
    assert e["permanent"] is True
    e2 = {"last_accessed_at": days_ago(20), "delete_after": iso(NOW)}
    cr.keep(e2, NOW)
    assert e2["last_accessed_at"] == iso(NOW) and "delete_after" not in e2


def test_daily_runs_once_per_day_after_hour():
    assert cr.due_daily({}, NOW)
    assert not cr.due_daily({"last_daily_run": NOW.date().isoformat()}, NOW)
    assert not cr.due_daily({}, NOW.replace(hour=0))


@pytest.mark.parametrize("last,hour,expected", [
    (None, 19, True),
    (None, 18, False),
    (6, 19, False),
    (7, 19, True),
])
def test_vacuum_due(last, hour, expected):
    now = NOW.replace(hour=hour)
    st = {} if last is None else {"replica_vacuum_last": iso(now - timedelta(days=last))}
    assert cr.vacuum_due(st, now, 168) is expected
    assert cr.vacuum_due(st, now, 0) is False


def test_state_roundtrip_and_unreadable_file(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    monkeypatch.setenv("CLONE_RETENTION_STATE_PATH", str(p))
    cr.save_state({"clones": {"a": {"last_accessed_at": iso(NOW)}}})
    assert cr.load_state()["clones"]["a"]["last_accessed_at"] == iso(NOW)
    p.write_text("{not json")
    assert cr.load_state() == {"clones": {}}
