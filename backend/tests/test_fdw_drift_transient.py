"""A blip reaching the FDW remote must not page; a persistent outage must.

The drift check runs every loop cycle (30s). A pooler reporting
``AllServersDown`` for one cycle used to reach Slack even though the next
cycle succeeded.
"""
from __future__ import annotations

import pytest

from app.services import fdw

POOL_DOWN = (
    'psql:<stdin>:42: ERROR:  could not get connection from the pool - '
    'AllServersDown\nCONTEXT:  remote SQL command: SET extra_float_digits = 3'
)


@pytest.fixture(autouse=True)
def _reset_state():
    fdw._transient_state["since"] = None
    yield
    fdw._transient_state["since"] = None


def test_transient_error_suppressed_within_grace():
    err, suppressed = fdw._gate_transient(POOL_DOWN, now=1000.0)
    assert (err, suppressed) == (None, True)
    err, suppressed = fdw._gate_transient(POOL_DOWN, now=1000.0 + 60)
    assert (err, suppressed) == (None, True)


def test_transient_error_reported_once_it_persists():
    fdw._gate_transient(POOL_DOWN, now=1000.0)
    err, suppressed = fdw._gate_transient(
        POOL_DOWN, now=1000.0 + fdw._TRANSIENT_GRACE_SECONDS
    )
    assert (err, suppressed) == (POOL_DOWN, False)


def test_success_resets_streak():
    fdw._gate_transient(POOL_DOWN, now=1000.0)
    fdw._gate_transient(None, now=1100.0)
    err, suppressed = fdw._gate_transient(
        POOL_DOWN, now=1000.0 + fdw._TRANSIENT_GRACE_SECONDS
    )
    assert (err, suppressed) == (None, True)


def test_non_transient_error_reported_immediately():
    other = 'ERROR:  relation "_snaplicator.remote_columns" does not exist'
    err, suppressed = fdw._gate_transient(other, now=1000.0)
    assert (err, suppressed) == (other, False)


def test_sync_fdw_drift_does_not_surface_blip(monkeypatch):
    monkeypatch.setattr(
        fdw, "detect_fdw_drift",
        lambda *a, **k: {"server_ok": True, "targets": 3, "drifted": [],
                         "changes": [], "error": POOL_DOWN},
    )
    cfg = fdw.FdwConfig.model_construct(tables=[], schemas=[])
    args = {"container": "c", "pg_user": "u", "pg_db": "d"}
    res = fdw.sync_fdw_drift(cfg, None, None, args)
    assert res["error"] is None
    assert res["transient_error"] == POOL_DOWN
