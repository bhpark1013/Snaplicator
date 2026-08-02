"""The auto-add half of the capture trigger, and taking the triggers back off.

Capture and auto-add share one trigger but must not share one switch. Turning
following off is a scope change, not an uninstall — DDL still has to be
captured. These pin that they move independently, and that uninstall actually
removes what install put there.
"""
from __future__ import annotations

import pytest

from app.services.replication import (
    install_capture_triggers,
    uninstall_capture_triggers,
    verify_capture_installed,
)

from conftest import PG_DB, PUBLICATION, connstr_for, log_count, psql


def published() -> set[str]:
    out = psql(
        "SELECT schemaname || '.' || tablename FROM pg_publication_tables "
        f"WHERE pubname = '{PUBLICATION}';"
    )
    return {l.strip() for l in out.splitlines() if l.strip()}


@pytest.fixture()
def conn(pg_container):
    return connstr_for(PG_DB)


@pytest.fixture(autouse=True)
def restore_default_scope(conn, capture_installed):
    """Every test here rewrites the trigger; put the session default back."""
    yield
    install_capture_triggers(conn, PUBLICATION)


def test_derived_scope_follows_a_schema_the_publication_covers(conn):
    """No policy passed — the scope comes from publication membership, as before."""
    install_capture_triggers(conn, PUBLICATION)
    psql("CREATE TABLE scope_seed (id int PRIMARY KEY);")
    psql(f"ALTER PUBLICATION {PUBLICATION} ADD TABLE scope_seed;")
    psql("CREATE TABLE scope_derived (id int PRIMARY KEY);")
    try:
        assert "public.scope_derived" in published()
    finally:
        psql("DROP TABLE IF EXISTS scope_derived;")
        psql("DROP TABLE IF EXISTS scope_seed;")


def test_derived_scope_cannot_bootstrap_an_empty_publication(conn):
    """A table-list publication covering nothing follows nothing — by construction.

    The derived scope asks which schemas the publication already covers, so an
    empty one has no schema to name and never grows on its own. This is why
    follow_schemas exists: it is the only way to say "follow public" before
    anything in public is published.
    """
    install_capture_triggers(conn, PUBLICATION)
    assert published() == set(), "fixture publication is expected to start empty"
    psql("CREATE TABLE scope_bootstrap (id int PRIMARY KEY);")
    try:
        assert published() == set(), "nothing to derive a scope from"

        install_capture_triggers(conn, PUBLICATION, follow_schemas=["public"])
        psql("CREATE TABLE scope_bootstrap2 (id int PRIMARY KEY);")
        assert "public.scope_bootstrap2" in published(), "stated scope should work"
    finally:
        psql("DROP TABLE IF EXISTS scope_bootstrap;")
        psql("DROP TABLE IF EXISTS scope_bootstrap2;")


def test_following_nothing_stops_auto_add_but_not_capture(conn):
    """An empty follow list is an answer, not a missing argument."""
    install_capture_triggers(conn, PUBLICATION, follow_schemas=[], excluded=[])
    before = log_count()
    psql("CREATE TABLE scope_none (id int PRIMARY KEY);")
    try:
        assert "public.scope_none" not in published(), "auto-add should be off"
        assert log_count() > before, "capture must keep running"
    finally:
        psql("DROP TABLE IF EXISTS scope_none;")


def test_excluded_table_is_not_re_added(conn):
    """A table someone took out stays out when it is recreated."""
    install_capture_triggers(
        conn, PUBLICATION, follow_schemas=["public"], excluded=["public.scope_excl"]
    )
    psql("CREATE TABLE scope_excl (id int PRIMARY KEY);")
    psql("CREATE TABLE scope_kept (id int PRIMARY KEY);")
    try:
        pub = published()
        assert "public.scope_excl" not in pub, "excluded table was re-added"
        assert "public.scope_kept" in pub, "non-excluded table should follow"
    finally:
        psql("DROP TABLE IF EXISTS scope_excl;")
        psql("DROP TABLE IF EXISTS scope_kept;")


def test_scope_limited_to_named_schema(conn):
    psql("CREATE SCHEMA IF NOT EXISTS scope_other;")
    install_capture_triggers(conn, PUBLICATION, follow_schemas=["public"])
    psql("CREATE TABLE scope_other.t (id int PRIMARY KEY);")
    try:
        assert "scope_other.t" not in published()
    finally:
        psql("DROP SCHEMA IF EXISTS scope_other CASCADE;")


def test_quotes_in_names_do_not_break_the_trigger(conn):
    """The array literal is built by hand; an apostrophe must not end it."""
    install_capture_triggers(
        conn, PUBLICATION, follow_schemas=["public"], excluded=["public.it's"]
    )
    assert verify_capture_installed(conn), "trigger must survive a quoted name"


def test_uninstall_removes_triggers_and_keeps_the_log(conn):
    install_capture_triggers(conn, PUBLICATION)
    assert verify_capture_installed(conn)

    psql("CREATE TABLE scope_log_keep (id int PRIMARY KEY);")
    psql("DROP TABLE scope_log_keep;")
    rows = log_count()
    assert rows > 0

    uninstall_capture_triggers(conn)
    try:
        assert not verify_capture_installed(conn), "triggers should be gone"
        assert log_count() == rows, "captured rows are data; uninstall must keep them"

        # And nothing is captured while they are off.
        psql("CREATE TABLE scope_after_uninstall (id int);")
        psql("DROP TABLE scope_after_uninstall;")
        assert log_count() == rows
    finally:
        install_capture_triggers(conn, PUBLICATION)


def test_uninstall_is_idempotent(conn):
    uninstall_capture_triggers(conn)
    uninstall_capture_triggers(conn)
    assert not verify_capture_installed(conn)
    install_capture_triggers(conn, PUBLICATION)
    assert verify_capture_installed(conn)
