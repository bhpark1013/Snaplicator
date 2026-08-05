"""Two Snaplicators on one primary.

Capture used to be one trigger with one fixed name, and its body named a
publication. So the second install to start rewrote the first install's
function: from then on the first one's new tables silently stopped joining
its publication. Nothing errored — from PostgreSQL's side nothing was wrong.

The part that names a publication is now its own trigger, named after that
publication. The logging half names none, so it stays shared and every
install writes the same function. These pin that separation.
"""
from __future__ import annotations

import pytest

from app.services.replication import (
    AUTO_ADD_PREFIX,
    auto_add_name,
    install_capture_triggers,
    uninstall_capture_triggers,
    verify_capture_installed,
)

from conftest import PG_DB, PUBLICATION, connstr_for, log_count, psql

OTHER_PUB = "other_install_publication"


@pytest.fixture()
def conn(pg_container):
    return connstr_for(PG_DB)


@pytest.fixture(autouse=True)
def second_install(conn, capture_installed):
    """A second install's publication, torn down whatever the test did."""
    psql(f"DROP PUBLICATION IF EXISTS {OTHER_PUB}; CREATE PUBLICATION {OTHER_PUB};")
    yield
    uninstall_capture_triggers(conn, OTHER_PUB)
    psql(f"DROP PUBLICATION IF EXISTS {OTHER_PUB};")
    install_capture_triggers(conn, PUBLICATION)


def triggers() -> set[str]:
    out = psql("SELECT evtname FROM pg_event_trigger;")
    return {l.strip() for l in out.splitlines() if l.strip()}


def members(pubname: str) -> set[str]:
    out = psql(
        "SELECT schemaname || '.' || tablename FROM pg_publication_tables "
        f"WHERE pubname = '{pubname}';"
    )
    return {l.strip() for l in out.splitlines() if l.strip()}


class TestNames:
    def test_each_publication_gets_its_own(self):
        assert auto_add_name("a") != auto_add_name("b")
        assert auto_add_name("a").startswith(AUTO_ADD_PREFIX)

    def test_a_long_name_still_fits_and_stays_distinct(self):
        """63 bytes is the ceiling, and truncation alone would collide."""
        base = "p" * 80
        a, b = auto_add_name(base + "_one"), auto_add_name(base + "_two")
        assert len(a) <= 63 and len(b) <= 63
        assert a != b, "a shared prefix must not mean a shared trigger"

    def test_a_name_needing_quoting_does_not_produce_one(self):
        assert '"' not in auto_add_name('we"ird')


class TestCoexistence:
    def test_installing_for_one_leaves_the_other_alone(self, conn):
        install_capture_triggers(conn, PUBLICATION)
        install_capture_triggers(conn, OTHER_PUB)

        assert auto_add_name(PUBLICATION) in triggers()
        assert auto_add_name(OTHER_PUB) in triggers()
        assert verify_capture_installed(conn, PUBLICATION)
        assert verify_capture_installed(conn, OTHER_PUB)

    def test_the_first_install_keeps_auto_adding(self, conn):
        """The regression itself: a new table must join BOTH publications."""
        install_capture_triggers(conn, PUBLICATION, follow_schemas=["public"])
        install_capture_triggers(conn, OTHER_PUB, follow_schemas=["public"])

        psql("CREATE TABLE multi_install_new (id int PRIMARY KEY);")
        try:
            assert "public.multi_install_new" in members(PUBLICATION)
            assert "public.multi_install_new" in members(OTHER_PUB)
        finally:
            psql("DROP TABLE IF EXISTS multi_install_new;")

    def test_scopes_do_not_leak_into_each_other(self, conn):
        """One install following nothing must not stop the other following."""
        install_capture_triggers(conn, PUBLICATION, follow_schemas=["public"])
        install_capture_triggers(conn, OTHER_PUB, follow_schemas=[])

        psql("CREATE TABLE multi_install_scoped (id int PRIMARY KEY);")
        try:
            assert "public.multi_install_scoped" in members(PUBLICATION)
            assert "public.multi_install_scoped" not in members(OTHER_PUB)
        finally:
            psql("DROP TABLE IF EXISTS multi_install_scoped;")

    def test_logging_is_shared_and_survives_reinstalls(self, conn):
        """The half that names no publication: one copy, written identically."""
        install_capture_triggers(conn, PUBLICATION)
        before = log_count()
        install_capture_triggers(conn, OTHER_PUB)

        psql("CREATE TABLE multi_install_logged (id int);")
        try:
            assert log_count() > before, "still captured after the second install"
        finally:
            psql("DROP TABLE IF EXISTS multi_install_logged;")


class TestUninstall:
    def test_removing_one_install_leaves_the_other_capturing(self, conn):
        install_capture_triggers(conn, PUBLICATION)
        install_capture_triggers(conn, OTHER_PUB)

        uninstall_capture_triggers(conn, OTHER_PUB)

        assert auto_add_name(OTHER_PUB) not in triggers()
        assert verify_capture_installed(conn, PUBLICATION), (
            "the shared triggers belong to whoever is left"
        )

        before = log_count()
        psql("CREATE TABLE multi_install_after_partial (id int);")
        try:
            assert log_count() > before
        finally:
            psql("DROP TABLE IF EXISTS multi_install_after_partial;")

    def test_removing_the_last_install_takes_the_shared_ones(self, conn):
        install_capture_triggers(conn, PUBLICATION)
        install_capture_triggers(conn, OTHER_PUB)

        uninstall_capture_triggers(conn, OTHER_PUB)
        uninstall_capture_triggers(conn, PUBLICATION)
        try:
            assert not verify_capture_installed(conn)
            assert not [t for t in triggers() if t.startswith(AUTO_ADD_PREFIX)]

            before = log_count()
            psql("CREATE TABLE multi_install_after_all (id int);")
            psql("DROP TABLE multi_install_after_all;")
            assert log_count() == before, "nothing captured once it is all off"
        finally:
            install_capture_triggers(conn, PUBLICATION)

    def test_naming_nothing_still_removes_everything(self, conn):
        """The legacy call, kept for a publisher with one install on it."""
        install_capture_triggers(conn, PUBLICATION)
        install_capture_triggers(conn, OTHER_PUB)

        uninstall_capture_triggers(conn)
        try:
            assert not verify_capture_installed(conn)
            assert not [t for t in triggers() if t.startswith(AUTO_ADD_PREFIX)]
        finally:
            install_capture_triggers(conn, PUBLICATION)

    def test_partial_uninstall_is_idempotent(self, conn):
        install_capture_triggers(conn, PUBLICATION)
        install_capture_triggers(conn, OTHER_PUB)
        uninstall_capture_triggers(conn, OTHER_PUB)
        uninstall_capture_triggers(conn, OTHER_PUB)
        assert verify_capture_installed(conn, PUBLICATION)
