"""The stretch before DDL replication is connected, and what it leaves behind.

The watermark decides which captured DDL counts as history. Seeded when the
stream is connected, it draws that line in the wrong place: the replica's
schema is the primary's as of the bootstrap's dump, so a DDL between the two
moments is missing from the schema AND below the watermark — reachable by
neither route. The first write to that table then stops the apply worker on a
column the replica has no room for.

So the line is drawn at the dump, and what earlier installs already lost is
found by comparing the two schemas rather than by replaying anything.

The pair these run against is shared and already carries deliberate drift
from the apply tests, so every assertion here is on what a change adds or
removes, never on the absolute state.
"""
from __future__ import annotations

import pytest

from app.services.replication import (
    compare_published_schemas,
    enable_ddl_apply,
    get_publisher_max_ddl_log_id,
)

from conftest import (
    E2E_SUB,
    E2E_SUBSCRIPTION,
    PG_DB,
    PG_PASSWORD,
    PG_USER,
    PUBLICATION,
    psql_conn,
    wait_until,
)


def drift(pub):
    return compare_published_schemas(
        pub, PUBLICATION, E2E_SUB, PG_USER, PG_PASSWORD, PG_DB,
    )


class TestWatermarkComesFromTheClone:
    def test_an_explicit_watermark_wins_over_the_current_max(self, pg_pair):
        """The bootstrap knows where the schema was taken; this is how it says so.

        Without the argument the seed is 'wherever the log stands now', which
        is exactly the value that skips the DDL the replica is missing.
        """
        pub = pg_pair["pub"]
        now = get_publisher_max_ddl_log_id(pub)
        assert now > 0, "the pair has captured DDL by this point"

        res = enable_ddl_apply(
            pub, PUBLICATION, E2E_SUB, PG_USER, PG_PASSWORD, PG_DB,
            E2E_SUBSCRIPTION, 0,
        )
        assert res["watermark"] == 0, "the passed value is used verbatim"

        res = enable_ddl_apply(
            pub, PUBLICATION, E2E_SUB, PG_USER, PG_PASSWORD, PG_DB,
            E2E_SUBSCRIPTION,
        )
        assert res["watermark"] == now, "omitted, it still falls back to now"


class TestDrift:
    def test_a_column_only_the_primary_has_is_the_one_that_breaks_it(self, pg_pair):
        """Precisely the shape that stops the apply worker.

        Built by letting the column replicate and then taking it off the
        subscriber, which is the state a skipped DDL leaves behind.
        """
        pub, sub = pg_pair["pub"], pg_pair["sub"]
        before = set(drift(pub)["missing_columns"])

        psql_conn(pub, "ALTER TABLE seed ADD COLUMN drift_probe text;")
        wait_until(
            lambda: "drift_probe" in psql_conn(
                sub,
                "SELECT string_agg(column_name, ',') FROM information_schema.columns "
                "WHERE table_name = 'seed';",
            ),
            timeout=30, desc="column replicated",
        )
        assert set(drift(pub)["missing_columns"]) == before, "in step, nothing new"

        psql_conn(sub, "ALTER TABLE seed DROP COLUMN drift_probe;")
        try:
            d = drift(pub)
            assert set(d["missing_columns"]) - before == {"public.seed.drift_probe"}
            assert d["breaks_replication"] is True
        finally:
            psql_conn(pub, "ALTER TABLE seed DROP COLUMN IF EXISTS drift_probe;")

    def test_the_difference_clears_when_the_replica_catches_up(self, pg_pair):
        pub, sub = pg_pair["pub"], pg_pair["sub"]
        before = set(drift(pub)["missing_columns"])
        psql_conn(pub, "ALTER TABLE seed ADD COLUMN heals text;")
        wait_until(
            lambda: "heals" in psql_conn(
                sub,
                "SELECT string_agg(column_name, ',') FROM information_schema.columns "
                "WHERE table_name = 'seed';",
            ),
            timeout=30, desc="column replicated",
        )
        psql_conn(sub, "ALTER TABLE seed DROP COLUMN heals;")
        assert "public.seed.heals" in drift(pub)["missing_columns"]

        psql_conn(sub, "ALTER TABLE seed ADD COLUMN heals text;")
        try:
            assert set(drift(pub)["missing_columns"]) == before
        finally:
            psql_conn(pub, "ALTER TABLE seed DROP COLUMN IF EXISTS heals;")

    def test_a_column_only_the_replica_has_is_reported_but_not_fatal(self, pg_pair):
        pub, sub = pg_pair["pub"], pg_pair["sub"]
        before = drift(pub)
        psql_conn(sub, "ALTER TABLE seed ADD COLUMN local_only text;")
        try:
            d = drift(pub)
            assert "public.seed.local_only" in d["extra_columns"]
            # The apply worker ignores a column the publisher never sends, so
            # this must not change the verdict either way.
            assert set(d["missing_columns"]) == set(before["missing_columns"])
            assert d["breaks_replication"] == before["breaks_replication"]
        finally:
            psql_conn(sub, "ALTER TABLE seed DROP COLUMN IF EXISTS local_only;")

    def test_a_published_table_the_replica_never_got(self, pg_pair):
        """CREATE TABLE alone publishes it here — the capture trigger follows
        the schemas the publication covers."""
        pub, sub = pg_pair["pub"], pg_pair["sub"]
        before = set(drift(pub)["missing_tables"])
        psql_conn(pub, "CREATE TABLE drift_table (id int PRIMARY KEY, v text);")
        try:
            wait_until(
                lambda: "public.drift_table" in drift(pub)["missing_tables"]
                or psql_conn(
                    sub,
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name = 'drift_table';",
                ) == "1",
                timeout=30, desc="table created or reported missing",
            )
            # Once it has replayed on the subscriber it is no longer missing;
            # take it off there to make the state a skipped CREATE leaves.
            psql_conn(sub, "DROP TABLE IF EXISTS drift_table;")
            d = drift(pub)
            assert set(d["missing_tables"]) - before == {"public.drift_table"}
            assert d["breaks_replication"] is True
            # Its columns are not each reported as missing — that would bury
            # the one line that matters under one line per column.
            assert not any(
                c.startswith("public.drift_table.") for c in d["missing_columns"]
            )
        finally:
            psql_conn(pub, "DROP TABLE IF EXISTS drift_table;")
            psql_conn(sub, "DROP TABLE IF EXISTS drift_table;")

    def test_unpublished_tables_are_none_of_its_business(self, pg_pair):
        """A table nobody replicates cannot drift in a way that matters."""
        pub, sub = pg_pair["pub"], pg_pair["sub"]
        psql_conn(pub, "CREATE TABLE not_published (id int PRIMARY KEY);")
        psql_conn(pub, f"ALTER PUBLICATION {PUBLICATION} DROP TABLE not_published;")
        psql_conn(sub, "DROP TABLE IF EXISTS not_published;")
        try:
            assert "public.not_published" not in drift(pub)["missing_tables"]
        finally:
            psql_conn(pub, "DROP TABLE IF EXISTS not_published;")
            psql_conn(sub, "DROP TABLE IF EXISTS not_published;")
