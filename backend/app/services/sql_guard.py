"""Read-only guard for the replication-check SQL.

The replication check exists only to compare publisher vs subscriber state.
It must never mutate either database. Two independent layers enforce this:

1. assert_read_only_sql() - static validation. Rejects anything whose leading
   token per statement is not a known read-only command, and rejects a denylist
   of write keywords / mutating functions anywhere in the text (covers writable
   CTEs like `WITH x AS (INSERT ...) SELECT`).
2. wrap_read_only() - wraps the SQL in `BEGIN READ ONLY; ... ROLLBACK;` with a
   statement timeout. PostgreSQL itself then refuses any write ("cannot execute
   ... in a read-only transaction") and ROLLBACK discards everything regardless.

Layer 2 is authoritative (DB-enforced); layer 1 gives fast, clear feedback.
"""

from __future__ import annotations

import re

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# Leading token allowed to start a statement.
_ALLOWED_LEADING = {"select", "with", "show", "explain", "table", "values"}

# Write DML/DDL and mutating routines forbidden anywhere (word-boundary match).
# `_` is a word char so e.g. pg_stat_progress_copy / updated_at do NOT match.
_DENY = re.compile(
    r"\b("
    r"insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|"
    r"reindex|vacuum|cluster|refresh|call|do|comment|lock|copy|"
    r"nextval|setval|pg_drop_replication_slot|pg_create_logical_replication_slot|"
    r"pg_create_physical_replication_slot|pg_replication_slot_advance|"
    r"pg_terminate_backend|pg_promote|pg_cancel_backend|set_config|dblink_exec"
    r")\b",
    re.IGNORECASE,
)

_ANALYZE = re.compile(r"\banalyze\b", re.IGNORECASE)


def _strip_comments(sql: str) -> str:
    return _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql))


class ReadOnlyViolation(ValueError):
    """Raised when the supplied SQL is not provably read-only."""


def assert_read_only_sql(sql: str) -> None:
    """Raise ReadOnlyViolation if `sql` is not a safe read-only check query."""
    if sql is None or not sql.strip():
        raise ReadOnlyViolation("SQL is empty.")

    cleaned = _strip_comments(sql)

    deny = _DENY.search(cleaned)
    if deny:
        raise ReadOnlyViolation(
            f"Forbidden keyword '{deny.group(1).lower()}' detected. "
            "The replication check must be strictly read-only "
            "(no INSERT/UPDATE/DELETE/DDL or mutating functions)."
        )

    statements = [s.strip() for s in cleaned.split(";") if s.strip()]
    if not statements:
        raise ReadOnlyViolation("SQL is empty after removing comments.")

    for stmt in statements:
        m = re.match(r"\(*\s*([A-Za-z_][A-Za-z0-9_]*)", stmt)
        lead = (m.group(1).lower() if m else "")
        if lead not in _ALLOWED_LEADING:
            raise ReadOnlyViolation(
                f"Statement must start with one of "
                f"{sorted(_ALLOWED_LEADING)}; got '{lead or stmt[:20]}'."
            )
        if lead == "explain" and _ANALYZE.search(stmt):
            raise ReadOnlyViolation(
                "EXPLAIN ANALYZE actually executes the statement and is not allowed."
            )


def wrap_read_only(sql: str, statement_timeout: str = "15s") -> str:
    """Wrap `sql` so the DB itself enforces read-only and nothing persists."""
    body = sql.strip().rstrip(";")
    return (
        f"SET statement_timeout = '{statement_timeout}';\n"
        f"SET idle_in_transaction_session_timeout = '{statement_timeout}';\n"
        "BEGIN READ ONLY;\n"
        f"{body};\n"
        "ROLLBACK;\n"
    )
