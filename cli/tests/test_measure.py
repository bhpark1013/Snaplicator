"""Measurement tests: SQL construction and psql-output parsing only.

Pure functions with canned output — no database. The container-backed
integration test (known data sizes, publication over a subset) rides the
backend test infra and runs on the dev host.
"""

import pytest

from snaplicator_init.measure import (
    MeasureError,
    parse_bytes,
    parse_top_tables,
    payload_sql,
    top_tables_sql,
)


def test_default_scope_is_whole_database():
    assert "pg_database_size(current_database())" in payload_sql()


def test_tables_scope_sums_qualified_names():
    sql = payload_sql(tables=["public.orders", "sales.items"])
    assert "pg_total_relation_size" in sql
    assert "'public.orders'" in sql and "'sales.items'" in sql


def test_schemas_scope_filters_relkind():
    sql = payload_sql(schemas=["public"])
    assert "'public'" in sql
    assert "relkind IN ('r', 'p', 'm')" in sql


def test_literals_are_escaped():
    assert "''" in payload_sql(tables=["bad'name.t"])


def test_top_tables_sql_orders_and_limits():
    sql = top_tables_sql(limit=5)
    assert "ORDER BY pg_total_relation_size" in sql and "LIMIT 5" in sql


def test_parse_bytes():
    assert parse_bytes("83751862272\n") == 83751862272


def test_parse_bytes_rejects_garbage():
    with pytest.raises(MeasureError):
        parse_bytes("ERROR: whatever\n")


def test_parse_top_tables():
    out = "public.events_log|440234\npublic.orders|1024\n"
    assert parse_top_tables(out) == [
        {"name": "public.events_log", "bytes": 440234},
        {"name": "public.orders", "bytes": 1024},
    ]
