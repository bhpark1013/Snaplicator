"""DDL that arrived in a table's initial copy, and so was never executed.

The apply trigger fires on INSERT. A subscription's first pass over a table
is not an INSERT — PostgreSQL fills it with COPY, which row triggers do not
see. The log table is a table like any other, so whatever it already held
when its copy ran lands on the replica present, above the watermark, and
silently skipped.

Observed on a real pair: replica A had the CREATE TABLE row in its log with
no matching row in the applied set, so the table was never created; the
table then joined the publication, the publisher started sending its rows,
and the apply worker jammed on a relation that did not exist. Running that
one statement by hand cleared everything behind it.

COPY delivery is simulated the only honest way a single container allows:
the trigger is disabled for the insert, which is exactly what COPY does to
it — puts rows in the table without the trigger running.
"""
from __future__ import annotations

import pytest

from app.services.replication import (
    CAPTURE_LOG_TABLE,
    catch_up_unapplied_ddl,
    install_ddl_apply,
)

from conftest import PG_DB, PG_PASSWORD, PG_USER, TEST_PG_CONTAINER, psql


def catch_up():
    return catch_up_unapplied_ddl(TEST_PG_CONTAINER, PG_USER, PG_PASSWORD, PG_DB)


def deliver_by_copy(ddl: str, search_path: str | None = "public") -> None:
    """A log row that the trigger never sees, as the initial copy delivers it."""
    sp = "NULL" if search_path is None else f"'{search_path}'"
    psql(f"""
ALTER TABLE public.{CAPTURE_LOG_TABLE} DISABLE TRIGGER _snaplicator_ddl_apply;
INSERT INTO public.{CAPTURE_LOG_TABLE}
    (lsn, txid, command_tag, object_identity, schema_name, ddl_text, search_path)
VALUES ('0/0', 0, 'DDL', NULL, 'public', {psql_literal(ddl)}, {sp});
ALTER TABLE public.{CAPTURE_LOG_TABLE} ENABLE ALWAYS TRIGGER _snaplicator_ddl_apply;
""")


def psql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def applied_ids() -> set[int]:
    out = psql("SELECT id FROM public._snaplicator_ddl_applied ORDER BY id;")
    return {int(l) for l in out.splitlines() if l.strip()}


def table_exists(name: str) -> bool:
    return psql(
        "SELECT count(*) FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name = '{name}';"
    ).strip() == "1"


@pytest.fixture()
def apply_installed(pg_container, capture_installed):
    """Subscriber-side apply infrastructure on the same container."""
    install_ddl_apply(TEST_PG_CONTAINER, PG_USER, PG_PASSWORD, PG_DB,
                      initial_watermark=0)
    psql(f"TRUNCATE public.{CAPTURE_LOG_TABLE};")
    psql("DELETE FROM public._snaplicator_ddl_applied;")
    psql("DELETE FROM public._snaplicator_ddl_failures;")
    # Other files in this session share the container and leave rows here.
    psql("DELETE FROM public._snaplicator_ddl_deferred;")
    psql("UPDATE public._snaplicator_ddl_watermark SET last_applied_id = 0 WHERE id = 1;")
    yield
    for t in ("catchup_created", "catchup_second", "catchup_ordered"):
        psql(f"DROP TABLE IF EXISTS public.{t};")


class TestCatchUp:
    def test_a_copied_row_is_not_executed_on_arrival(self, apply_installed):
        """The defect itself, before anything is done about it."""
        deliver_by_copy("CREATE TABLE catchup_created (id int PRIMARY KEY);")
        assert not table_exists("catchup_created"), "the trigger never saw it"
        assert applied_ids() == set()

    def test_catch_up_runs_it(self, apply_installed):
        deliver_by_copy("CREATE TABLE catchup_created (id int PRIMARY KEY);")
        assert catch_up()["applied"] == 1
        assert table_exists("catchup_created")

    def test_a_second_pass_does_nothing(self, apply_installed):
        deliver_by_copy("CREATE TABLE catchup_created (id int PRIMARY KEY);")
        catch_up()
        ids = applied_ids()
        assert catch_up()["applied"] == 0, "nothing left to do"
        assert applied_ids() == ids

    def test_rows_below_the_watermark_are_history(self, apply_installed):
        """What the schema clone already carries must not be replayed."""
        deliver_by_copy("CREATE TABLE catchup_created (id int PRIMARY KEY);")
        top = psql(f"SELECT max(id) FROM public.{CAPTURE_LOG_TABLE};").strip()
        psql(f"UPDATE public._snaplicator_ddl_watermark SET last_applied_id = {top} WHERE id = 1;")
        assert catch_up()["applied"] == 0
        assert not table_exists("catchup_created")

    def test_order_is_kept(self, apply_installed):
        """A column added to a table created two rows earlier."""
        deliver_by_copy("CREATE TABLE catchup_ordered (id int PRIMARY KEY);")
        deliver_by_copy("ALTER TABLE catchup_ordered ADD COLUMN note text;")
        assert catch_up()["applied"] == 2
        assert psql(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'catchup_ordered' AND column_name = 'note';"
        ).strip() == "1"

    def test_one_failure_does_not_stop_the_rest(self, apply_installed):
        """Recorded and stepped over — the same rule the trigger follows."""
        deliver_by_copy("ALTER TABLE public.nonexistent_table ADD COLUMN x int;")
        deliver_by_copy("CREATE TABLE catchup_second (id int PRIMARY KEY);")
        assert catch_up()["applied"] == 2
        assert table_exists("catchup_second"), "the good one still ran"
        assert psql(
            "SELECT count(*) FROM public._snaplicator_ddl_failures;"
        ).strip() == "1"

    def test_concurrently_is_deferred_not_run(self, apply_installed):
        """It cannot run inside this transaction, as in the trigger."""
        deliver_by_copy("CREATE INDEX CONCURRENTLY catchup_idx ON pg_class (oid);")
        assert catch_up()["applied"] == 1
        assert psql(
            "SELECT count(*) FROM public._snaplicator_ddl_deferred;"
        ).strip() == "1"
        assert psql(
            "SELECT count(*) FROM pg_indexes WHERE indexname = 'catchup_idx';"
        ).strip() == "0"

    def test_a_row_the_trigger_did_see_is_left_alone(self, apply_installed):
        """Normal arrival still goes through the trigger, and only once."""
        psql(f"""
INSERT INTO public.{CAPTURE_LOG_TABLE}
    (lsn, txid, command_tag, object_identity, schema_name, ddl_text, search_path)
VALUES ('0/0', 0, 'DDL', NULL, 'public',
        'CREATE TABLE catchup_second (id int PRIMARY KEY);', 'public');
""")
        assert table_exists("catchup_second"), "the trigger ran it on insert"
        assert catch_up()["applied"] == 0, "and catch-up must not run it again"
