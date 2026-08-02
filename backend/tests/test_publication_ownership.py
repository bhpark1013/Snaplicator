"""A publication this install does not own is one it may not write to.

The selection screen refuses to narrow such a publication, and that refusal
was the whole guard. Two other doors led to the same rewrite — the DDL log
table joining it, and the capture trigger adding new tables to it — so
"this install will never rewrite it" was true of one code path and false of
the deployment. These pin the doors shut.
"""
from __future__ import annotations

import pytest

from app.services.replication import (
    CAPTURE_LOG_PUBLICATION,
    CAPTURE_LOG_TABLE,
    ensure_ddl_publication,
    install_capture_triggers,
)

from conftest import PG_DB, PUBLICATION, connstr_for, psql


@pytest.fixture()
def conn(pg_container):
    return connstr_for(PG_DB)


def members(pubname: str) -> set[str]:
    out = psql(
        "SELECT schemaname || '.' || tablename FROM pg_publication_tables "
        f"WHERE pubname = '{pubname}';"
    )
    return {l.strip() for l in out.splitlines() if l.strip()}


def publications() -> set[str]:
    out = psql("SELECT pubname FROM pg_publication;")
    return {l.strip() for l in out.splitlines() if l.strip()}


@pytest.fixture(autouse=True)
def drop_log_publication(conn, capture_installed):
    yield
    psql(f'DROP PUBLICATION IF EXISTS "{CAPTURE_LOG_PUBLICATION}";')
    install_capture_triggers(conn, PUBLICATION)


def test_log_table_gets_its_own_publication(conn):
    before = members(PUBLICATION)
    ensure_ddl_publication(conn)

    assert CAPTURE_LOG_PUBLICATION in publications()
    assert members(CAPTURE_LOG_PUBLICATION) == {f"public.{CAPTURE_LOG_TABLE}"}
    assert members(PUBLICATION) == before, "the data publication must not move"


def test_ensure_ddl_publication_is_idempotent(conn):
    ensure_ddl_publication(conn)
    first = members(CAPTURE_LOG_PUBLICATION)
    ensure_ddl_publication(conn)
    assert members(CAPTURE_LOG_PUBLICATION) == first


def test_ensure_ddl_publication_repairs_an_emptied_one(conn):
    """Someone took the log table out; the next cycle puts it back."""
    ensure_ddl_publication(conn)
    psql(
        f'ALTER PUBLICATION "{CAPTURE_LOG_PUBLICATION}" '
        f"DROP TABLE public.{CAPTURE_LOG_TABLE};"
    )
    assert members(CAPTURE_LOG_PUBLICATION) == set()
    ensure_ddl_publication(conn)
    assert members(CAPTURE_LOG_PUBLICATION) == {f"public.{CAPTURE_LOG_TABLE}"}


def test_log_table_is_not_offered_as_a_choice(conn):
    """It rides its own publication; the picker must not list it.

    Listing it lets someone put it in the data publication as well — and take
    it out again, which stops DDL replicating while every screen still says
    it is on.
    """
    from app.services.selection import all_tables

    ensure_ddl_publication(conn)
    assert f"public.{CAPTURE_LOG_TABLE}" not in all_tables(conn)


def test_capture_scope_refuses_to_follow_a_publication_we_do_not_own(monkeypatch):
    from app.services import policy as policy_svc
    from app.services import publication as publication_svc

    monkeypatch.setattr(
        policy_svc, "load",
        lambda: {"chosen": True, "auto_schemas": ["public"], "excluded": []},
    )

    monkeypatch.setattr(publication_svc, "may_rewrite", lambda name: True)
    assert policy_svc.capture_scope("mine") == (["public"], [])

    monkeypatch.setattr(publication_svc, "may_rewrite", lambda name: False)
    follow, excluded = policy_svc.capture_scope("theirs")
    assert follow == [], "a publication we may not rewrite follows nothing"
    assert excluded == [], "exclusions still apply"


def test_capture_scope_without_a_policy_still_refuses(monkeypatch):
    """No policy means 'derive the scope' — except on someone else's."""
    from app.services import policy as policy_svc
    from app.services import publication as publication_svc

    monkeypatch.setattr(
        policy_svc, "load",
        lambda: {"chosen": False, "auto_schemas": [], "excluded": []},
    )
    monkeypatch.setattr(publication_svc, "may_rewrite", lambda name: True)
    assert policy_svc.capture_scope("mine") == (None, None)

    monkeypatch.setattr(publication_svc, "may_rewrite", lambda name: False)
    assert policy_svc.capture_scope("theirs") == ([], None)
