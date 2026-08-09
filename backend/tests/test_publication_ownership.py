"""One publication, and what it costs to keep the log table inside it.

The DDL log used to have a publication of its own so that reading someone
else's publication never meant writing to it. It rides the data publication
now, which is simpler and puts the log table inside the very object the
selection screen drops and recreates. Losing it there is silent — rows keep
arriving, DDL just stops — so the tests below hold that door shut.

The capture trigger's own refusal to add tables to a publication this install
does not own is unchanged, and is still tested here.
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
def restore_publication(conn, capture_installed, tmp_path, monkeypatch):
    """Back to the session fixture's shape: an empty table-list publication.

    Some of these narrow it for real, so putting the log table back is not
    enough — the publication itself is recreated. The choice files go to
    tmp_path for the same reason: apply_selection records what it did, and
    that record would otherwise outlive the test.
    """
    from app.services import policy as policy_svc
    from app.services import publication as publication_svc

    monkeypatch.setattr(publication_svc, "_path",
                        lambda: tmp_path / "publication_choice.json")
    monkeypatch.setattr(policy_svc, "_path",
                        lambda: tmp_path / "selection_policy.json")
    yield
    psql(f'DROP PUBLICATION IF EXISTS "{CAPTURE_LOG_PUBLICATION}";')
    psql(f"DROP PUBLICATION IF EXISTS {PUBLICATION}; CREATE PUBLICATION {PUBLICATION};")
    install_capture_triggers(conn, PUBLICATION)


def test_the_log_table_joins_the_data_publication(conn):
    before = members(PUBLICATION)
    ensure_ddl_publication(conn, PUBLICATION)

    assert members(PUBLICATION) == before | {f"public.{CAPTURE_LOG_TABLE}"}
    assert CAPTURE_LOG_PUBLICATION not in publications(), "no second publication"


def test_ensure_ddl_publication_is_idempotent(conn):
    ensure_ddl_publication(conn, PUBLICATION)
    first = members(PUBLICATION)
    ensure_ddl_publication(conn, PUBLICATION)
    assert members(PUBLICATION) == first


def test_a_broader_publication_is_left_alone(conn):
    """FOR ALL TABLES already carries it, and ADD TABLE on one is an error.

    Membership is asked of pg_publication_tables, which answers for every
    form of publication, so the broad case is a no-op rather than a failure.
    """
    psql('CREATE PUBLICATION "wide" FOR ALL TABLES;')
    try:
        result = ensure_ddl_publication(conn, "wide")
        assert result["added"] is False
        assert f"public.{CAPTURE_LOG_TABLE}" in members("wide")
    finally:
        psql('DROP PUBLICATION IF EXISTS "wide";')


def test_narrowing_the_selection_keeps_the_log_table(conn):
    """The rewrite that would silently switch DDL replication off.

    apply_selection cannot ALTER its way from FOR ALL TABLES to anything
    narrower, so it DROPs and CREATEs — and whatever is not named in the new
    statement stops being replicated. Rows would keep arriving and only DDL
    would stop, which is why this is asserted rather than trusted.
    """
    from app.services import publication as publication_svc
    from app.services.selection import all_tables, apply_selection

    ensure_ddl_publication(conn, PUBLICATION)
    publication_svc.save(PUBLICATION, ours=True)

    psql("CREATE TABLE IF NOT EXISTS narrow_me (id int PRIMARY KEY);")
    try:
        assert "public.narrow_me" in all_tables(conn)
        apply_selection(conn, PUBLICATION, ["public.narrow_me"], auto_schemas=[])

        assert members(PUBLICATION) == {
            "public.narrow_me", f"public.{CAPTURE_LOG_TABLE}",
        }
    finally:
        psql("DROP TABLE IF EXISTS narrow_me;")


def test_selecting_nothing_still_keeps_the_log_table(conn):
    from app.services import publication as publication_svc
    from app.services.selection import apply_selection

    ensure_ddl_publication(conn, PUBLICATION)
    publication_svc.save(PUBLICATION, ours=True)

    result = apply_selection(conn, PUBLICATION, [], auto_schemas=[])
    assert result["count"] == 0, "nothing was chosen"
    assert members(PUBLICATION) == {f"public.{CAPTURE_LOG_TABLE}"}


def test_log_table_is_not_offered_as_a_choice(conn):
    """It is always in the publication and never a choice.

    Listing it would let someone untick it, which stops DDL replicating while
    every screen still says it is on.
    """
    from app.services.selection import all_tables

    ensure_ddl_publication(conn, PUBLICATION)
    assert f"public.{CAPTURE_LOG_TABLE}" not in all_tables(conn)


def test_the_selection_screen_does_not_show_it_either(conn):
    """Membership reads as "someone chose this" everywhere downstream."""
    from app.services.selection import current_selection

    ensure_ddl_publication(conn, PUBLICATION)
    assert f"public.{CAPTURE_LOG_TABLE}" not in current_selection(conn, PUBLICATION)["tables"]


def test_capture_scope_refuses_to_follow_a_publication_we_do_not_own(monkeypatch):
    from app.services import policy as policy_svc
    from app.services import publication as publication_svc

    monkeypatch.setattr(
        policy_svc, "load",
        lambda: {"chosen": True, "auto_schemas": ["public"],
                 "off_schemas": ["etl"], "excluded": []},
    )

    monkeypatch.setattr(publication_svc, "may_rewrite", lambda name: True)
    assert policy_svc.capture_scope("mine") == (None, [], ["etl"])

    monkeypatch.setattr(publication_svc, "may_rewrite", lambda name: False)
    follow, excluded, unfollow = policy_svc.capture_scope("theirs")
    assert follow == [], "a publication we may not rewrite follows nothing"
    assert excluded == [], "exclusions still apply"
    assert unfollow == ["etl"], "and so do the schemas switched off"


def test_capture_scope_without_a_policy_still_refuses(monkeypatch):
    """No policy means 'derive the scope' — except on someone else's."""
    from app.services import policy as policy_svc
    from app.services import publication as publication_svc

    monkeypatch.setattr(
        policy_svc, "load",
        lambda: {"chosen": False, "auto_schemas": [], "off_schemas": [], "excluded": []},
    )
    monkeypatch.setattr(publication_svc, "may_rewrite", lambda name: True)
    assert policy_svc.capture_scope("mine") == (None, None, None)

    monkeypatch.setattr(publication_svc, "may_rewrite", lambda name: False)
    assert policy_svc.capture_scope("theirs") == ([], None, None)


def test_a_chosen_policy_still_derives_the_scope(monkeypatch):
    """The point of the exceptions model.

    A saved selection used to pin the scope to the schemas ticked at the time,
    so a schema created afterwards followed nothing — though nobody had said
    so. The scope stays derived from membership; only the departures are
    stored, and a schema nobody has spoken about is not one of them.
    """
    from app.services import policy as policy_svc
    from app.services import publication as publication_svc

    monkeypatch.setattr(publication_svc, "may_rewrite", lambda name: True)
    monkeypatch.setattr(
        policy_svc, "load",
        lambda: {"chosen": True, "auto_schemas": ["public"],
                 "off_schemas": [], "excluded": ["public.junk"]},
    )
    follow, excluded, unfollow = policy_svc.capture_scope("mine")
    assert follow is None, "derived, so a schema added later is covered too"
    assert excluded == ["public.junk"]
    assert unfollow == []


def test_a_policy_file_from_before_the_default_keeps_its_old_meaning(tmp_path, monkeypatch):
    """Upgrading must not start replicating something nobody asked for.

    The old file's list *was* the scope. Read under the new meaning its
    absent `off_schemas` says "nothing is switched off", which would put every
    covered schema back to following — including the ones deliberately left
    out of that list.
    """
    from app.services import policy as policy_svc
    from app.services import publication as publication_svc

    monkeypatch.setattr(publication_svc, "may_rewrite", lambda name: True)
    monkeypatch.setattr(policy_svc, "_path", lambda: tmp_path / "selection_policy.json")

    (tmp_path / "selection_policy.json").write_text(
        '{"auto_schemas": ["public"], "excluded": []}'  # no off_schemas key
    )
    assert policy_svc.load()["legacy"] is True
    follow, _excluded, unfollow = policy_svc.capture_scope("mine")
    assert follow == ["public"], "the stored list is still the scope"
    assert unfollow is None

    # The next save writes the key, and the new meaning takes over from there.
    policy_svc.save(["public"], [], off_schemas=["etl"])
    assert policy_svc.load()["legacy"] is False
    follow, _excluded, unfollow = policy_svc.capture_scope("mine")
    assert follow is None
    assert unfollow == ["etl"]


def test_the_chooser_is_not_offered_a_leftover_ddl_publication(conn):
    """The screen asks which publication belongs to someone else.

    Nothing creates this one any more, but a primary an older install touched
    still carries it, covering exactly one table — the log. Listing it invites
    the reader to arbitrate between us and us, and picking it would point the
    replica at the outbox instead of at any data.
    """
    from app.services import publication as publication_svc

    psql(f'CREATE PUBLICATION "{CAPTURE_LOG_PUBLICATION}" '
         f"FOR TABLE public.{CAPTURE_LOG_TABLE};")
    assert CAPTURE_LOG_PUBLICATION in publications(), "it is on the primary"

    offered = {p["name"] for p in publication_svc.list_existing(conn)}
    assert CAPTURE_LOG_PUBLICATION not in offered
    assert PUBLICATION in offered, "and the real ones are still offered"

    # Still reachable for anything that has to reason about every publication
    # on the server rather than about what to put in front of a person.
    everything = {p["name"] for p in publication_svc.list_existing(conn, include_internal=True)}
    assert CAPTURE_LOG_PUBLICATION in everything


class TestSuggestedName:
    """What "create a new one" starts out saying.

    Versioned rather than dated or random: the reason to make a second one is
    almost always that the first covers the wrong tables, and someone reading
    pg_publication on the primary later should be able to tell the order.
    """

    def test_the_proposed_name_when_nothing_holds_it(self, conn):
        from app.services import publication as publication_svc

        assert publication_svc.suggest_name(conn, "unused_publication") == "unused_publication"

    def test_v2_when_the_base_is_taken(self, conn):
        from app.services import publication as publication_svc

        assert publication_svc.suggest_name(conn, PUBLICATION) == f"{PUBLICATION}_v2"

    def test_it_keeps_counting(self, conn):
        from app.services import publication as publication_svc

        psql(f"CREATE PUBLICATION {PUBLICATION}_v2;")
        psql(f"CREATE PUBLICATION {PUBLICATION}_v3;")
        try:
            assert publication_svc.suggest_name(conn, PUBLICATION) == f"{PUBLICATION}_v4"
        finally:
            psql(f"DROP PUBLICATION IF EXISTS {PUBLICATION}_v2;")
            psql(f"DROP PUBLICATION IF EXISTS {PUBLICATION}_v3;")

    def test_a_gap_is_filled_rather_than_skipped(self, conn):
        """_v2 free with _v3 taken means _v2 — the count is of what is free."""
        from app.services import publication as publication_svc

        psql(f"CREATE PUBLICATION {PUBLICATION}_v3;")
        try:
            assert publication_svc.suggest_name(conn, PUBLICATION) == f"{PUBLICATION}_v2"
        finally:
            psql(f"DROP PUBLICATION IF EXISTS {PUBLICATION}_v3;")

    def test_the_suggestion_is_one_create_accepts(self, conn):
        """It has to survive the name rules, not just be unused."""
        from app.services import publication as publication_svc

        name = publication_svc.suggest_name(conn, PUBLICATION)
        publication_svc.create(conn, name)
        try:
            assert name in publications()
        finally:
            psql(f"DROP PUBLICATION IF EXISTS {name};")
