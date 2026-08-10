"""Payload measurement: how many bytes will replication carry?

SQL construction and output parsing are pure functions (unit-tested with
canned psql output); only `measure()` talks to a server, via the psql
binary so the tool stays stdlib-only.

Scope semantics (issue #19): at install time the publication usually does
not exist yet, so "publication size" means "size of what you chose to
replicate" — the whole database by default, narrowed by --tables/--schemas.
"""

from __future__ import annotations

import subprocess
from typing import Dict, List, Optional, Sequence

TOP_TABLES_LIMIT = 10


class MeasureError(RuntimeError):
    """psql is missing, unreachable, or produced unusable output."""


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def payload_sql(
    tables: Optional[Sequence[str]] = None,
    schemas: Optional[Sequence[str]] = None,
) -> str:
    """SQL returning a single byte count for the chosen replication scope.

    `tables` entries are schema-qualified names (e.g. public.orders).
    pg_total_relation_size includes indexes and TOAST — the bytes that will
    actually land on the subscriber.
    """
    if tables:
        in_list = ", ".join(_quote_literal(t) for t in tables)
        return (
            "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname || '.' || c.relname IN ({in_list});"
        )
    if schemas:
        in_list = ", ".join(_quote_literal(s) for s in schemas)
        return (
            "SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p', 'm') "
            f"AND n.nspname IN ({in_list});"
        )
    return "SELECT pg_database_size(current_database());"


def top_tables_sql(limit: int = TOP_TABLES_LIMIT) -> str:
    """SQL listing the largest tables as 'schema.table|bytes' lines — fuel
    for the narrow-the-scope remediation hint."""
    return (
        "SELECT n.nspname || '.' || c.relname || '|' || pg_total_relation_size(c.oid) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p', 'm') "
        "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
        f"ORDER BY pg_total_relation_size(c.oid) DESC LIMIT {int(limit)};"
    )


def parse_bytes(psql_output: str) -> int:
    line = psql_output.strip().splitlines()[0].strip() if psql_output.strip() else ""
    try:
        return int(line)
    except ValueError:
        raise MeasureError(f"expected a byte count from psql, got: {line!r}")


def parse_top_tables(psql_output: str) -> List[Dict[str, int]]:
    tables: List[Dict[str, int]] = []
    for line in psql_output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, size = line.rpartition("|")
        try:
            tables.append({"name": name, "bytes": int(size)})
        except ValueError:
            raise MeasureError(f"unparseable top-tables line from psql: {line!r}")
    return tables


def _run_psql(connstr: str, sql: str) -> str:
    cmd = ["psql", connstr, "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", sql]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, check=True)
    except FileNotFoundError:
        raise MeasureError(
            "'psql' not found — install the postgresql client, or pass "
            "--payload-bytes to skip measurement"
        )
    except subprocess.CalledProcessError as e:
        raise MeasureError(
            f"psql failed against the publisher: {(e.stderr or e.stdout or '').strip()}"
        )
    return proc.stdout


def measure(
    connstr: str,
    tables: Optional[Sequence[str]] = None,
    schemas: Optional[Sequence[str]] = None,
) -> Dict[str, object]:
    """Returns {'payload_bytes': int, 'top_tables': [{'name', 'bytes'}, ...]}."""
    payload = parse_bytes(_run_psql(connstr, payload_sql(tables, schemas)))
    top = parse_top_tables(_run_psql(connstr, top_tables_sql()))
    return {"payload_bytes": payload, "top_tables": top}
