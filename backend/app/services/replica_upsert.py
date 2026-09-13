"""Opt-in, publisher-wins INSERT conflict handling on the subscriber.

The trigger runs only in replica sessions. Matching a business key replaces
the entire row, including its primary key, so later replicated UPDATE/DELETE
messages still find the publisher's identity. It never skips a transaction.
"""
from __future__ import annotations

import hashlib
import json
import subprocess

from .replication import _quote_ident, _quote_literal, _run_subscriber_sql

TRIGGER = "_snaplicator_replica_upsert"
FUNCTION = "public._snaplicator_replica_upsert"

# Inspect columns at execution time: an in-stream ADD COLUMN must not leave
# an old handler silently omitting the new column until the next sync cycle.
FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public._snaplicator_replica_upsert()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $upsert$
DECLARE
    key_columns smallint[];
    predicate text;
    assignments text;
    affected bigint;
BEGIN
    IF TG_WHEN <> 'BEFORE' OR TG_OP <> 'INSERT'
       OR current_setting('session_replication_role') <> 'replica' THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = TG_RELID AND relkind = 'r' AND NOT relispartition)
       OR EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = TG_RELID OR inhparent = TG_RELID)
       OR NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = TG_RELID AND contype = 'p') THEN
        RAISE EXCEPTION 'replica upsert requires an ordinary, non-inherited table with a primary key';
    END IF;

    SELECT conkey INTO key_columns FROM pg_constraint
    WHERE conrelid = TG_RELID AND conname = TG_ARGV[0]
      AND contype IN ('u', 'p') AND NOT condeferrable AND convalidated;
    IF key_columns IS NULL OR EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = TG_RELID AND attnum = ANY(key_columns)
          AND (NOT attnotnull OR attgenerated <> '')
    ) THEN
        RAISE EXCEPTION 'replica upsert requires a non-null, immediate unique key: %', TG_ARGV[0];
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE contype = 'f' AND confrelid = TG_RELID) THEN
        RAISE EXCEPTION 'replica upsert cannot replace identities on a table referenced by foreign keys';
    END IF;

    SELECT string_agg(format('target.%I = ($1).%I', attname, attname), ' AND ' ORDER BY attnum)
    INTO predicate FROM pg_attribute
    WHERE attrelid = TG_RELID AND attnum = ANY(key_columns);

    SELECT string_agg(format('%I = ($1).%I', attname, attname), ', ' ORDER BY attnum)
    INTO assignments FROM pg_attribute
    WHERE attrelid = TG_RELID AND attnum > 0 AND NOT attisdropped AND attgenerated = '';

    EXECUTE format('UPDATE %I.%I AS target SET %s WHERE %s',
                   TG_TABLE_SCHEMA, TG_TABLE_NAME, assignments, predicate) USING NEW;
    GET DIAGNOSTICS affected = ROW_COUNT;
    IF affected > 0 THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END
$upsert$;
"""
VERSION = hashlib.sha256(FUNCTION_SQL.encode()).hexdigest()


def _run_sql(container_name: str, user: str, password: str | None, db: str, sql: str) -> str:
    try:
        return _run_subscriber_sql(container_name, user, password, db, sql)
    except subprocess.CalledProcessError as exc:
        # CalledProcessError.__str__ includes the docker command and its
        # PGPASSWORD argument. Report PostgreSQL's error, never the command.
        errors = [line for line in (exc.stderr or "").splitlines() if line.startswith("ERROR:")]
        raise RuntimeError(errors[0] if errors else "Replica upsert SQL failed") from None


def _target(schema: str, table: str) -> str:
    if not schema or not table or "\x00" in schema + table:
        raise ValueError("A schema and table name are required")
    return f"{_quote_ident(schema)}.{_quote_ident(table)}"


def install_replica_upsert(
    container_name: str, user: str, password: str | None, db: str,
    schema: str, table: str, constraint: str,
) -> bool:
    """Install/repair a durable handler. Returns False if already installed.

    Only subscriber SQL is executed. Other constraints still fail normally;
    ambiguous conflicts must not discard unrelated rows or transactions.
    """
    target = _target(schema, table)
    if not constraint or "\x00" in constraint:
        raise ValueError("A unique constraint name is required")
    relation = _quote_literal(target)
    key = _quote_literal(constraint)

    def run(sql: str) -> str:
        return _run_sql(container_name, user, password, db, sql)

    # Avoid repeatedly taking table DDL locks during the manager's sync loop.
    current = run(f"""
        SELECT EXISTS (
            SELECT 1 FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
            JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_class c ON c.oid = t.tgrelid
            WHERE t.tgrelid = to_regclass({relation}) AND t.tgname = '{TRIGGER}'
              AND t.tgenabled = 'R' AND t.tgtype = 7 AND t.tgnargs = 1
              AND t.tgargs = convert_to({key}, current_setting('server_encoding')) || decode('00', 'hex')
              AND n.nspname = 'public' AND p.proname = '{TRIGGER}'
              AND obj_description(p.oid, 'pg_proc') = '{VERSION}'
              AND c.relkind = 'r' AND NOT c.relispartition
              AND NOT EXISTS (SELECT 1 FROM pg_inherits WHERE inhrelid = c.oid OR inhparent = c.oid)
              AND EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = c.oid AND contype = 'p')
              AND NOT EXISTS (SELECT 1 FROM pg_constraint WHERE contype = 'f' AND confrelid = c.oid)
              AND EXISTS (
                  SELECT 1 FROM pg_constraint k
                  WHERE k.conrelid = c.oid AND k.conname = {key}
                    AND k.contype IN ('u', 'p') AND NOT k.condeferrable AND k.convalidated
                    AND NOT EXISTS (
                        SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                          AND a.attnum = ANY(k.conkey) AND (NOT a.attnotnull OR a.attgenerated <> '')
                    )
              )
        );
    """)
    if current == "t":
        return False

    # Validation and installation share a transaction/table lock so a schema
    # change cannot invalidate the checks between reading and installing.
    run(f"""
        BEGIN;
        SET LOCAL lock_timeout = '2s';
        SET LOCAL statement_timeout = '15s';
        LOCK TABLE {target} IN SHARE ROW EXCLUSIVE MODE;
        DO $validate$
        DECLARE keys smallint[];
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_class WHERE oid = {relation}::regclass
                           AND relkind = 'r' AND NOT relispartition)
               OR EXISTS (SELECT 1 FROM pg_inherits
                          WHERE inhrelid = {relation}::regclass OR inhparent = {relation}::regclass) THEN
                RAISE EXCEPTION 'replica upsert supports ordinary, non-inherited tables only';
            END IF;
            SELECT conkey INTO keys FROM pg_constraint
            WHERE conrelid = {relation}::regclass AND conname = {key}
              AND contype IN ('u', 'p') AND NOT condeferrable AND convalidated;
            IF keys IS NULL OR EXISTS (
                SELECT 1 FROM pg_attribute WHERE attrelid = {relation}::regclass
                  AND attnum = ANY(keys) AND (NOT attnotnull OR attgenerated <> '')
            ) THEN
                RAISE EXCEPTION 'replica upsert requires a non-null, immediate unique key';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = {relation}::regclass AND contype = 'p') THEN
                RAISE EXCEPTION 'replica upsert requires a primary key';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_constraint WHERE contype = 'f' AND confrelid = {relation}::regclass) THEN
                RAISE EXCEPTION 'replica upsert cannot replace identities on a table referenced by foreign keys';
            END IF;
        END
        $validate$;
        {FUNCTION_SQL}
        COMMENT ON FUNCTION {FUNCTION}() IS '{VERSION}';
        CREATE OR REPLACE TRIGGER {TRIGGER} BEFORE INSERT ON {target}
            FOR EACH ROW EXECUTE FUNCTION {FUNCTION}({key});
        ALTER TABLE {target} ENABLE REPLICA TRIGGER {TRIGGER};
        COMMIT;
    """)
    return True


def remove_replica_upsert(
    container_name: str, user: str, password: str | None, db: str,
    schema: str, table: str,
) -> None:
    """Remove the handler; already-applied data is not reverted."""
    _run_sql(container_name, user, password, db, f"""
        BEGIN;
        SET LOCAL lock_timeout = '2s';
        DROP TRIGGER IF EXISTS {TRIGGER} ON {_target(schema, table)};
        COMMIT;
    """)


def ensure_replica_upserts(
    container_name: str, user: str, password: str | None, db: str,
    tables: dict[str, str],
) -> dict:
    """Reconcile configured table -> constraint policies on the main replica."""
    desired = {}
    for name, constraint in tables.items():
        parts = name.split(".")
        if len(parts) != 2 or not all(parts):
            raise ValueError("REPLICA_UPSERT_TABLES keys must be schema.table")
        desired[tuple(parts)] = constraint

    out = _run_sql(container_name, user, password, db, f"""
        SELECT coalesce(json_agg(json_build_array(n.nspname, c.relname)), '[]')
        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_proc p ON p.oid = t.tgfoid
        JOIN pg_namespace fn ON fn.oid = p.pronamespace
        WHERE t.tgname = '{TRIGGER}' AND p.proname = '{TRIGGER}' AND fn.nspname = 'public';
    """)
    result: dict = {"installed": [], "removed": [], "errors": []}
    for schema, table in json.loads(out):
        if (schema, table) not in desired:
            try:
                remove_replica_upsert(container_name, user, password, db, schema, table)
                result["removed"].append(f"{schema}.{table}")
            except Exception:
                result["errors"].append(f"Could not remove replica upsert from {schema}.{table}")
    for (schema, table), constraint in desired.items():
        try:
            if install_replica_upsert(container_name, user, password, db, schema, table, constraint):
                result["installed"].append(f"{schema}.{table}")
        except (ValueError, RuntimeError) as exc:
            # Missing/recreated tables and short lock conflicts retry next
            # cycle. Keep errors visible without leaking subprocess DSNs.
            result["errors"].append(f"Could not install replica upsert on {schema}.{table}: {exc}")
    return result
