from __future__ import annotations

import base64
import subprocess
from typing import Dict, List
from pathlib import Path


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
	return subprocess.run(cmd, check=True, text=True, capture_output=True)


def get_replication_lag_seconds(container_name: str, postgres_user: str, postgres_db: str) -> Dict[str, float]:
	"""Compute replication lag metrics from the subscriber (replica) side.

	Returns a dict with:
	- network_lag_seconds: last_msg_receipt_time - last_msg_send_time (seconds)
	- apply_lag_seconds: now() - latest_end_time (seconds)
	If values are NULL, returns 0.0.
	"""
	# Single-row aggregate over all subscriptions
	sql = (
		"SELECT "
		" COALESCE(MAX(EXTRACT(EPOCH FROM (now() - st.latest_end_time))), 0)::text AS apply_lag_seconds,"
		" COALESCE(MAX(EXTRACT(EPOCH FROM (st.last_msg_receipt_time - st.last_msg_send_time))), 0)::text AS network_lag_seconds"
		" FROM pg_stat_subscription st;"
	)
	proc = subprocess.run(
		[
			"docker", "exec", container_name,
			"psql", "-U", postgres_user, "-d", postgres_db, "-tAc", sql,
		],
		text=True, capture_output=True, check=True,
	)
	line = (proc.stdout or "").strip()
	parts = [p for p in line.replace("|", " ").split() if p]
	if len(parts) >= 2:
		apply_lag = float(parts[0])
		network_lag = float(parts[1])
	else:
		parts = [p for p in line.split(",") if p]
		apply_lag = float(parts[0]) if parts else 0.0
		network_lag = float(parts[1]) if len(parts) > 1 else 0.0
	return {
		"network_lag_seconds": network_lag,
		"apply_lag_seconds": apply_lag,
	}


def get_initial_copy_progress(container_name: str, postgres_user: str, postgres_db: str) -> Dict:
	"""Report initial logical replication copy progress on the subscriber.

	Heuristic:
	- total_tables = count rows in pg_subscription_rel
	- finished_tables = count rows with srsubstate in ('r','s')
	- status: 'idle' if total=0; 'copying' if finished<total; 'complete' otherwise
	- active copy details from pg_subscription_rel (states not 'r') and, if available, pg_stat_progress_copy
	"""
	# Summary counts
	summary_sql = (
		"WITH rels AS (SELECT srrelid, srsubstate FROM pg_subscription_rel) "
		"SELECT COALESCE((SELECT count(*) FROM rels),0)::text AS total, "
		"COALESCE((SELECT count(*) FROM rels WHERE srsubstate IN ('r','s')),0)::text AS done;"
	)
	try:
		p = subprocess.run(
			[
				"docker", "exec", container_name,
				"psql", "-U", postgres_user, "-d", postgres_db, "-At", "-F", ",", "-c", summary_sql,
			],
			text=True, capture_output=True, check=True,
		)
		line = (p.stdout or "").strip()
		parts = [x for x in line.split(",") if x != ""]
		total = int(parts[0]) if len(parts) > 0 else 0
		done = int(parts[1]) if len(parts) > 1 else 0
	except subprocess.CalledProcessError as e:
		total = 0
		done = 0

	# Active details from pg_subscription_rel
	details: List[Dict] = []
	try:
		detail_sql = (
			"SELECT r.srsubstate, n.nspname, c.relname "
			"FROM pg_subscription_rel r "
			"JOIN pg_class c ON c.oid = r.srrelid "
			"JOIN pg_namespace n ON n.oid = c.relnamespace "
			"WHERE r.srsubstate <> 'r' "
			"ORDER BY 1,2,3;"
		)
		p2 = subprocess.run(
			[
				"docker", "exec", container_name,
				"psql", "-U", postgres_user, "-d", postgres_db, "-At", "-F", ",", "-c", detail_sql,
			],
			text=True, capture_output=True, check=True,
		)
		for ln in (p2.stdout or "").splitlines():
			ln = ln.strip()
			if not ln:
				continue
			parts = ln.split(",")
			if len(parts) >= 3:
				details.append({
					"state": parts[0],
					"schema": parts[1],
					"table": parts[2],
				})
	except subprocess.CalledProcessError:
		pass

	# Optional: bytes progress from pg_stat_progress_copy (best-effort)
	active: List[Dict] = []
	try:
		prog_sql = (
			"SELECT n.nspname, c.relname, p.bytes_processed, p.bytes_total "
			"FROM pg_stat_progress_copy p "
			"JOIN pg_class c ON c.oid = p.relid "
			"JOIN pg_namespace n ON n.oid = c.relnamespace;"
		)
		p3 = subprocess.run(
			[
				"docker", "exec", container_name,
				"psql", "-U", postgres_user, "-d", postgres_db, "-At", "-F", ",", "-c", prog_sql,
			],
			text=True, capture_output=True, check=True,
		)
		for ln in (p3.stdout or "").splitlines():
			ln = ln.strip()
			if not ln:
				continue
			parts = ln.split(",")
			if len(parts) >= 4:
				try:
					bp = int(parts[2]) if parts[2] else 0
					bt = int(parts[3]) if parts[3] else 0
					pct = (bp / bt * 100.0) if bt > 0 else None
				except ValueError:
					bp, bt, pct = 0, 0, None
				active.append({
					"schema": parts[0],
					"table": parts[1],
					"bytes_processed": bp,
					"bytes_total": bt,
					"percent": pct,
				})
	except subprocess.CalledProcessError:
		pass

	status = "idle" if total == 0 else ("copying" if done < total else "complete")
	percent = (done / total * 100.0) if total > 0 else 0.0
	return {
		"status": status,
		"total_tables": total,
		"finished_tables": done,
		"percent": percent,
		"active": active if active else None,
		"details": details if details else None,
	}


def run_replication_check_sql(
    sql_file: str,
    publisher_connstr: str,
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
) -> Dict:
    """Run the check SQL on publisher and subscriber, strictly read-only.

    The SQL is statically validated (assert_read_only_sql) and then executed
    inside a `BEGIN READ ONLY; ... ROLLBACK;` wrapper so PostgreSQL itself
    rejects any write and nothing persists. Returns both sides separately.
    """
    from .sql_guard import assert_read_only_sql, wrap_read_only

    sql_path = Path(sql_file)
    if not sql_path.exists():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")

    raw_sql = sql_path.read_text(encoding="utf-8")
    assert_read_only_sql(raw_sql)            # layer 1: static validation
    wrapped = wrap_read_only(raw_sql)        # layer 2: DB-enforced READ ONLY tx

    pub_ok = False
    pub_out = ""
    pub_err = ""
    try:
        p_pub = subprocess.run(
            ["psql", publisher_connstr, "-q", "-v", "ON_ERROR_STOP=1",
             "-At", "-F", ",", "-f", "-"],
            input=wrapped, text=True, capture_output=True, check=True,
        )
        pub_ok = True
        pub_out = (p_pub.stdout or "").strip()
    except subprocess.CalledProcessError as e:
        pub_err = (e.stderr or e.stdout or "").strip()

    sub_ok = False
    sub_out = ""
    sub_err = ""
    try:
        exec_cmd: List[str] = ["docker", "exec", "-i", subscriber_container]
        if subscriber_password:
            exec_cmd += ["env", f"PGPASSWORD={subscriber_password}"]
        exec_cmd += [
            "psql", "-h", "localhost",
            "-U", subscriber_user, "-d", subscriber_db,
            "-q", "-v", "ON_ERROR_STOP=1", "-At", "-F", ",", "-f", "-",
        ]
        p_sub = subprocess.run(exec_cmd, input=wrapped, text=True,
                               capture_output=True, check=True)
        sub_ok = True
        sub_out = (p_sub.stdout or "").strip()
    except subprocess.CalledProcessError as e:
        sub_err = (e.stderr or e.stdout or "").strip()
    except Exception as e:
        sub_err = str(e)

    return {
        "publisher": {"ok": pub_ok, "output": pub_out, "error": (pub_err or None)},
        "subscriber": {"ok": sub_ok, "output": sub_out, "error": (sub_err or None)},
    }


# ── New functions for replication table management ──────────────────────


def _run_publisher_sql(connstr: str, sql: str) -> str:
    """Run SQL on publisher via direct psql connection. Returns stdout."""
    proc = subprocess.run(
        ["psql", connstr, "-At", "-F", ",", "-c", sql],
        text=True, capture_output=True, check=True,
    )
    return (proc.stdout or "").strip()


def _run_subscriber_sql(container_name: str, user: str, password: str | None, db: str, sql: str) -> str:
    """Run SQL on subscriber via docker exec psql. Returns stdout."""
    cmd: list[str] = ["docker", "exec"]
    if password:
        cmd += ["-e", f"PGPASSWORD={password}"]
    cmd += [container_name, "psql", "-U", user, "-d", db, "-At", "-F", ",", "-c", sql]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=True)
    return (proc.stdout or "").strip()


def list_replication_tables(
    publisher_connstr: str,
    publication_name: str,
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
) -> List[Dict]:
    """List all public tables from publisher with publication/subscriber status and estimated rows."""

    # 1) All public tables + estimated row count from publisher
    all_tables_sql = (
        "SELECT t.table_schema, t.table_name, COALESCE(s.n_live_tup, 0)::text "
        "FROM information_schema.tables t "
        "LEFT JOIN pg_stat_user_tables s ON s.schemaname = t.table_schema AND s.relname = t.table_name "
        "WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema') AND t.table_type = 'BASE TABLE' "
        "ORDER BY t.table_name;"
    )
    all_out = _run_publisher_sql(publisher_connstr, all_tables_sql)

    # 2) Tables currently in publication
    pub_sql = f"SELECT schemaname, tablename FROM pg_publication_tables WHERE pubname = '{publication_name}';"
    pub_out = _run_publisher_sql(publisher_connstr, pub_sql)
    pub_set = set()
    for line in pub_out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) >= 2:
            pub_set.add(f"{parts[0]}.{parts[1]}")

    # 2b) Tables individually registered (can be removed via DROP TABLE)
    #     vs schema-level (FOR TABLES IN SCHEMA) which cannot
    indiv_sql = (
        f"SELECT c.relnamespace::regnamespace || '.' || c.relname "
        f"FROM pg_publication_rel pr "
        f"JOIN pg_class c ON c.oid = pr.prrelid "
        f"JOIN pg_publication p ON p.oid = pr.prpubid "
        f"WHERE p.pubname = '{publication_name}';"
    )
    try:
        indiv_out = _run_publisher_sql(publisher_connstr, indiv_sql)
        indiv_set = {l.strip() for l in indiv_out.splitlines() if l.strip()}
    except Exception:
        indiv_set = pub_set  # fallback: treat all as individually added

    # 3) Tables on subscriber
    sub_sql = (
        "SELECT table_schema, table_name "
        "FROM information_schema.tables "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') AND table_type = 'BASE TABLE';"
    )
    try:
        sub_out = _run_subscriber_sql(subscriber_container, subscriber_user, subscriber_password, subscriber_db, sub_sql)
    except Exception:
        sub_out = ""
    sub_set = set()
    for line in sub_out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) >= 2:
            sub_set.add(f"{parts[0]}.{parts[1]}")

    # Combine
    result: List[Dict] = []
    for line in all_out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        schema = parts[0]
        table = parts[1]
        estimated_rows = int(parts[2]) if parts[2] else 0
        fqn = f"{schema}.{table}"
        in_pub = fqn in pub_set
        result.append({
            "schema": schema,
            "table": table,
            "in_publication": in_pub,
            "pub_via": ("table" if fqn in indiv_set else "schema") if in_pub else None,
            "in_subscriber": fqn in sub_set,
            "estimated_rows": estimated_rows,
        })

    return result


def add_tables_to_publication(
    publisher_connstr: str,
    publication_name: str,
    tables: list[str],
) -> Dict:
    """Add tables to a publication. Tables already in the publication are skipped."""
    # Get currently published tables
    pub_sql = f"SELECT schemaname || '.' || tablename FROM pg_publication_tables WHERE pubname = '{publication_name}';"
    pub_out = _run_publisher_sql(publisher_connstr, pub_sql)
    existing = {line.strip() for line in pub_out.splitlines() if line.strip()}

    to_add = [t for t in tables if t not in existing]
    skipped = [t for t in tables if t in existing]

    if not to_add:
        return {"added": [], "skipped": skipped, "message": "All tables already in publication"}

    table_list = ", ".join(to_add)
    alter_sql = f"ALTER PUBLICATION {publication_name} ADD TABLE {table_list};"
    _run_publisher_sql(publisher_connstr, alter_sql)

    return {"added": to_add, "skipped": skipped}


def remove_tables_from_publication(
    publisher_connstr: str,
    publication_name: str,
    tables: list[str],
) -> Dict:
    """Remove tables from a publication. Tables not in the publication are skipped."""
    # Get currently published tables
    pub_sql = f"SELECT schemaname || '.' || tablename FROM pg_publication_tables WHERE pubname = '{publication_name}';"
    pub_out = _run_publisher_sql(publisher_connstr, pub_sql)
    existing = {line.strip() for line in pub_out.splitlines() if line.strip()}

    # Check which tables are individually registered (can be DROP-ed)
    indiv_sql = (
        f"SELECT c.relnamespace::regnamespace || '.' || c.relname "
        f"FROM pg_publication_rel pr "
        f"JOIN pg_class c ON c.oid = pr.prrelid "
        f"JOIN pg_publication p ON p.oid = pr.prpubid "
        f"WHERE p.pubname = '{publication_name}';"
    )
    try:
        indiv_out = _run_publisher_sql(publisher_connstr, indiv_sql)
        indiv_set = {l.strip() for l in indiv_out.splitlines() if l.strip()}
    except Exception:
        indiv_set = existing  # fallback

    to_remove = [t for t in tables if t in existing and t in indiv_set]
    skipped = [t for t in tables if t not in existing]
    schema_level = [t for t in tables if t in existing and t not in indiv_set]
    skipped.extend(schema_level)

    if not to_remove:
        msg = "None of the tables are in publication"
        if schema_level:
            msg = f"Tables included via schema-level publication cannot be individually removed: {schema_level}"
        return {"removed": [], "skipped": skipped, "message": msg}

    # Remove one at a time to handle race conditions gracefully
    removed = []
    for t in to_remove:
        alter_sql = f"ALTER PUBLICATION {publication_name} DROP TABLE {t};"
        try:
            _run_publisher_sql(publisher_connstr, alter_sql)
            removed.append(t)
        except subprocess.CalledProcessError as e:
            err_msg = (e.stderr or e.stdout or str(e)).strip()
            if "is not part of the publication" in err_msg:
                skipped.append(t)
            else:
                raise

    return {"removed": removed, "skipped": skipped}



def sync_table_schemas_to_subscriber(
    publisher_connstr: str,
    tables: list[str],
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
) -> Dict:
    """Sync table schemas from publisher to subscriber for tables that don't exist on subscriber.

    Uses pg_dump --schema-only to get DDL from publisher, then applies to subscriber.
    Returns dict with synced and skipped tables.
    """
    import tempfile, os

    # Check which tables already exist on subscriber
    sub_sql = (
        "SELECT table_schema || '.' || table_name "
        "FROM information_schema.tables "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') AND table_type = 'BASE TABLE';"
    )
    try:
        sub_out = _run_subscriber_sql(subscriber_container, subscriber_user, subscriber_password, subscriber_db, sub_sql)
    except Exception:
        sub_out = ""
    existing = {line.strip() for line in sub_out.splitlines() if line.strip()}

    synced = []
    skipped = []
    errors = []

    for table in tables:
        if table in existing:
            skipped.append(table)
            continue

        # pg_dump --schema-only -t <table> from publisher
        try:
            dump_proc = subprocess.run(
                ["pg_dump", publisher_connstr, "--schema-only", "-t", table],
                text=True, capture_output=True, check=True,
            )
            ddl = dump_proc.stdout
            if not ddl.strip():
                errors.append({"table": table, "error": "Empty schema dump"})
                continue

            # Write DDL to temp file, docker cp into container, run psql
            with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as tmp:
                tmp.write(ddl)
                tmp_path = tmp.name

            try:
                # Copy into container
                subprocess.run(
                    ["docker", "cp", tmp_path, f"{subscriber_container}:/tmp/_sync_schema.sql"],
                    text=True, capture_output=True, check=True,
                )
                # Execute on subscriber
                exec_cmd = ["docker", "exec"]
                if subscriber_password:
                    exec_cmd += ["-e", f"PGPASSWORD={subscriber_password}"]
                exec_cmd += [
                    subscriber_container, "psql",
                    "-U", subscriber_user, "-d", subscriber_db,
                    "-f", "/tmp/_sync_schema.sql",
                ]
                subprocess.run(exec_cmd, text=True, capture_output=True, check=True)
                synced.append(table)
            finally:
                os.unlink(tmp_path)

        except subprocess.CalledProcessError as e:
            errors.append({"table": table, "error": (e.stderr or e.stdout or str(e)).strip()})

    return {"synced": synced, "skipped": skipped, "errors": errors}


def refresh_subscription(
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
    subscription_name: str,
) -> Dict:
    """Refresh a subscription to pick up publication changes."""
    sql = f"ALTER SUBSCRIPTION {subscription_name} REFRESH PUBLICATION;"
    _run_subscriber_sql(subscriber_container, subscriber_user, subscriber_password, subscriber_db, sql)
    return {"refreshed": True, "subscription": subscription_name}


def auto_sync_new_tables(
    publisher_connstr: str,
    publication_name: str,
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
    subscription_name: str,
) -> Dict | None:
    """Connect DML flow for tables that joined the publication — pure
    subscription reconciler.

    Compares publication membership (publisher's pg_publication_tables)
    against what the subscription actually replicates (subscriber's
    pg_subscription_rel) and runs one REFRESH PUBLICATION when a published
    table is not yet connected. Table existence on the subscriber is checked
    only as a REFRESH precondition: REFRESH hard-fails on a published
    relation that is missing locally.

    Schema creation is fully delegated to in-stream DDL apply. A table whose
    CREATE has not been applied yet is reported in "waiting" and connects on
    a later cycle, once the stream materializes it. If the in-stream CREATE
    failed, that failure is already recorded in _snaplicator_ddl_failures
    for human resolution — deliberately not repaired here (same philosophy
    as the apply trigger: record loudly, never self-heal schema).

    Returns None when subscription membership already matches.
    """
    pub_sql = f"SELECT schemaname || '.' || tablename FROM pg_publication_tables WHERE pubname = '{publication_name}';"
    pub_out = _run_publisher_sql(publisher_connstr, pub_sql)
    pub_tables = {line.strip() for line in pub_out.splitlines() if line.strip()}

    # Relations the subscription already replicates. Any srsubstate counts:
    # a rel mid-COPY is connected and must not trigger another refresh.
    sub_rel_sql = (
        "SELECT n.nspname || '.' || c.relname "
        "FROM pg_subscription_rel sr "
        "JOIN pg_class c ON c.oid = sr.srrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_subscription s ON s.oid = sr.srsubid "
        f"WHERE s.subname = '{subscription_name}';"
    )
    try:
        rel_out = _run_subscriber_sql(subscriber_container, subscriber_user, subscriber_password, subscriber_db, sub_rel_sql)
    except Exception:
        # Cannot see subscription state — reconciling blind could refresh in
        # a bad moment; try again next cycle.
        return None
    sub_rels = {line.strip() for line in rel_out.splitlines() if line.strip()}

    missing = sorted(pub_tables - sub_rels)
    if not missing:
        return None

    # REFRESH precondition: the relation must exist on the subscriber.
    exists_sql = (
        "SELECT table_schema || '.' || table_name "
        "FROM information_schema.tables "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') AND table_type = 'BASE TABLE';"
    )
    try:
        sub_out = _run_subscriber_sql(subscriber_container, subscriber_user, subscriber_password, subscriber_db, exists_sql)
    except Exception:
        sub_out = ""
    sub_tables = {line.strip() for line in sub_out.splitlines() if line.strip()}

    refreshable = [t for t in missing if t in sub_tables]
    waiting = [t for t in missing if t not in sub_tables]

    refresh_ok = False
    errors = []
    if refreshable:
        try:
            sql = f"ALTER SUBSCRIPTION {subscription_name} REFRESH PUBLICATION;"
            _run_subscriber_sql(subscriber_container, subscriber_user, subscriber_password, subscriber_db, sql)
            refresh_ok = True
        except Exception as e:
            errors.append({"table": "_refresh", "error": str(e)})

    return {"synced": refreshable, "waiting": waiting, "errors": errors, "refreshed": refresh_ok}



# ── DDL Capture (wide capture-and-replicate, Phase 1) ──────────────


CAPTURE_LOG_TABLE = "_snaplicator_ddl_log"


def install_capture_triggers(publisher_connstr: str, publication_name: str) -> Dict:
    """Install wide DDL capture on the publisher.

    Creates the _snaplicator_ddl_log outbox table plus two event triggers:
      _snaplicator_capture_ddl   ON ddl_command_end  (CREATE/ALTER, wide)
      _snaplicator_capture_drop  ON sql_drop         (DROP)

    Capture is intentionally wide — scoping decisions happen at apply time.
    Only three capture-side guards exist:
      1. recursion — DDL touching _snaplicator_* objects is never logged
      2. noise/DCL — GRANT/REVOKE/SECURITY LABEL/COMMENT are never logged
         (TRUNCATE rides native logical replication; role/password commands
         are cluster-global and never reach event triggers)
      3. dedupe   — at most one log row per (txid, query string): subcommand
         entries (serial sequences, PK indexes) and double-firing commands
         (ALTER TABLE DROP COLUMN hits both triggers) collapse to one row

    Replaces the legacy _snaplicator_auto_pub_add trigger; its behaviour
    (auto ALTER PUBLICATION ADD TABLE on CREATE TABLE) is folded into the
    capture trigger, scoped to schemas the publication already covers, and
    skipped for FOR ALL TABLES publications.

    Idempotent: safe to call repeatedly; existing log rows are preserved.

    All log-table references inside the trigger functions are schema-qualified
    (public.): the triggers run under the DDL issuer's search_path, so an
    unqualified reference would fail (and silently lose the capture) whenever
    a migration runs with SET search_path. A function-level SET search_path
    is not an option — it would corrupt the captured search_path value.
    """
    log_table_sql = f"""
CREATE TABLE IF NOT EXISTS public.{CAPTURE_LOG_TABLE} (
    id bigserial PRIMARY KEY,
    captured_at timestamptz NOT NULL DEFAULT now(),
    lsn pg_lsn NOT NULL,
    txid bigint NOT NULL,
    command_tag text NOT NULL,
    object_identity text,
    schema_name text,
    ddl_text text NOT NULL,
    search_path text
);
"""
    _run_publisher_sql(publisher_connstr, log_table_sql)

    capture_fn_sql = f"""
CREATE OR REPLACE FUNCTION _snaplicator_capture_ddl()
RETURNS event_trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    q text := current_query();
    cmd record;
    tbl record;
    allt boolean;
BEGIN
    -- Never logged: DCL noise, plus publication/subscription DDL —
    -- publisher-only infrastructure that must not replay on a subscriber
    -- (auto pub-add would otherwise log an ALTER PUBLICATION per table).
    IF tg_tag IN ('GRANT', 'REVOKE', 'SECURITY LABEL', 'COMMENT',
                  'CREATE PUBLICATION', 'ALTER PUBLICATION', 'DROP PUBLICATION',
                  'CREATE SUBSCRIPTION', 'ALTER SUBSCRIPTION', 'DROP SUBSCRIPTION') THEN
        RETURN;
    END IF;

    -- One representative entry for object_identity/schema_name. Subcommand
    -- entries (serial sequences, PK indexes, ...) share the same query
    -- string; prefer the entry whose tag matches the top-level command tag.
    -- Some commands (e.g. CREATE EXTENSION) report only their member
    -- objects here — command_tag is therefore taken from tg_tag below, not
    -- from this row.
    SELECT c.command_tag, c.object_identity, c.schema_name
      INTO cmd
      FROM pg_event_trigger_ddl_commands() c
     WHERE c.object_identity IS NULL OR c.object_identity !~ '_snaplicator_'
     ORDER BY (c.command_tag = tg_tag) DESC
     LIMIT 1;

    IF cmd.command_tag IS NOT NULL THEN
        BEGIN
            -- dedupe: one row per (txid, query string)
            IF NOT EXISTS (
                SELECT 1 FROM public.{CAPTURE_LOG_TABLE}
                 WHERE txid = txid_current() AND ddl_text = q
            ) THEN
                INSERT INTO public.{CAPTURE_LOG_TABLE}
                    (lsn, txid, command_tag, object_identity, schema_name,
                     ddl_text, search_path)
                VALUES
                    (pg_current_wal_lsn(), txid_current(), tg_tag,
                     cmd.object_identity, cmd.schema_name, q,
                     current_setting('search_path', true));
            END IF;
        EXCEPTION WHEN OTHERS THEN
            -- capture must never break the DDL that triggered it
            RAISE WARNING 'snaplicator: ddl capture failed: %', SQLERRM;
        END;
    END IF;

    -- Legacy behaviour: auto-add new tables to the publication, scoped to
    -- schemas the publication already covers (legacy hardcoded 'public';
    -- membership-derived scope needs no config). Deliberately excluded
    -- schemas stay out — e.g. FDW-managed etl tables, some without PKs:
    -- publishing one breaks publisher-side UPDATE/DELETE. FOR ALL TABLES
    -- publications include new tables automatically (and reject ALTER
    -- PUBLICATION ADD TABLE), so skip in that case.
    SELECT puballtables INTO allt
      FROM pg_publication WHERE pubname = '{publication_name}';
    IF allt IS FALSE THEN
        FOR tbl IN
            SELECT c.object_identity
              FROM pg_event_trigger_ddl_commands() c
             WHERE c.command_tag IN ('CREATE TABLE', 'CREATE TABLE AS',
                                     'SELECT INTO')
               AND c.object_type = 'table'
               AND c.object_identity !~ '_snaplicator_'
               AND c.schema_name IN (
                   SELECT pt.schemaname
                     FROM pg_publication_tables pt
                    WHERE pt.pubname = '{publication_name}')
        LOOP
            BEGIN
                EXECUTE format(
                    'ALTER PUBLICATION {publication_name} ADD TABLE %s',
                    tbl.object_identity);
            EXCEPTION WHEN OTHERS THEN
                RAISE WARNING 'snaplicator: auto pub add failed for %: %',
                    tbl.object_identity, SQLERRM;
            END;
        END LOOP;
    END IF;
END;
$fn$;
"""
    _run_publisher_sql(publisher_connstr, capture_fn_sql)

    drop_fn_sql = f"""
CREATE OR REPLACE FUNCTION _snaplicator_capture_drop()
RETURNS event_trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    q text := current_query();
    obj record;
BEGIN
    -- same noise filter as the ddl_command_end twin (e.g. ALTER PUBLICATION
    -- DROP TABLE reports 'publication relation' drops here)
    IF tg_tag IN ('GRANT', 'REVOKE', 'SECURITY LABEL', 'COMMENT',
                  'CREATE PUBLICATION', 'ALTER PUBLICATION', 'DROP PUBLICATION',
                  'CREATE SUBSCRIPTION', 'ALTER SUBSCRIPTION', 'DROP SUBSCRIPTION') THEN
        RETURN;
    END IF;

    -- one representative dropped object; prefer directly-named ones
    SELECT d.object_identity, d.schema_name
      INTO obj
      FROM pg_event_trigger_dropped_objects() d
     WHERE d.object_identity !~ '_snaplicator_'
     ORDER BY d.original DESC
     LIMIT 1;

    IF obj.object_identity IS NULL THEN
        RETURN;
    END IF;

    -- dedupe: ALTER TABLE DROP COLUMN fires sql_drop AND ddl_command_end
    IF NOT EXISTS (
        SELECT 1 FROM public.{CAPTURE_LOG_TABLE}
         WHERE txid = txid_current() AND ddl_text = q
    ) THEN
        INSERT INTO public.{CAPTURE_LOG_TABLE}
            (lsn, txid, command_tag, object_identity, schema_name,
             ddl_text, search_path)
        VALUES
            (pg_current_wal_lsn(), txid_current(), tg_tag,
             obj.object_identity, obj.schema_name, q,
             current_setting('search_path', true));
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'snaplicator: drop capture failed: %', SQLERRM;
END;
$fn$;
"""
    _run_publisher_sql(publisher_connstr, drop_fn_sql)

    # DROP + CREATE so function references stay fresh; DDL on event triggers
    # does not itself fire event triggers, so this cannot recurse.
    triggers_sql = """
DO $do$
BEGIN
    DROP EVENT TRIGGER IF EXISTS _snaplicator_auto_pub_add;
    DROP EVENT TRIGGER IF EXISTS _snaplicator_capture_ddl;
    DROP EVENT TRIGGER IF EXISTS _snaplicator_capture_drop;
    CREATE EVENT TRIGGER _snaplicator_capture_ddl
        ON ddl_command_end
        EXECUTE FUNCTION _snaplicator_capture_ddl();
    CREATE EVENT TRIGGER _snaplicator_capture_drop
        ON sql_drop
        EXECUTE FUNCTION _snaplicator_capture_drop();
END;
$do$;
"""
    _run_publisher_sql(publisher_connstr, triggers_sql)

    return {
        "installed": True,
        "publication": publication_name,
        "log_table": CAPTURE_LOG_TABLE,
    }


def verify_capture_installed(publisher_connstr: str) -> bool:
    """Check that both capture event triggers exist on the publisher."""
    sql = (
        "SELECT count(*) FROM pg_event_trigger "
        "WHERE evtname IN ('_snaplicator_capture_ddl', '_snaplicator_capture_drop');"
    )
    out = _run_publisher_sql(publisher_connstr, sql)
    return out.strip() == "2"


# ── DDL Apply (subscriber side, Phase 2) ───────────────────────────


def install_ddl_apply(
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
    initial_watermark: int = 0,
) -> Dict:
    """Install the subscriber-side DDL apply infrastructure.

    The publisher's _snaplicator_ddl_log is a member of the publication, so
    its rows arrive through the normal logical replication stream in commit
    order, interleaved exactly between the surrounding DML. An ENABLE ALWAYS
    row trigger on the subscriber's copy executes each row's ddl_text at
    that exact position — no LSN gates, no polling, no ordering logic.

    Safety rules baked into the trigger:
      * id <= watermark (seed)     -> skip. The watermark is the install-time
                                     seed only and never advances: log ids are
                                     assigned at INSERT time but the stream
                                     delivers in commit order, so with two
                                     overlapping DDL transactions a lower id
                                     can arrive after a higher one — an
                                     advancing high-watermark would misread
                                     the late lower id as already-processed
                                     and silently drop its DDL.
      * id in _snaplicator_ddl_applied -> skip. Exact-id dedupe set — one row
                                     per processed log id (applied, deferred
                                     or failed), immune to arrival order.
                                     Covers clone artifacts and
                                     re-subscription re-delivery. Unbounded
                                     only in theory: one row per DDL
                                     statement ever captured.
      * ddl_text ~* CONCURRENTLY   -> queue to _snaplicator_ddl_deferred
                                     (cannot run inside the apply worker's
                                     transaction; the sync loop executes it
                                     out-of-band)
      * any execution error        -> record to _snaplicator_ddl_failures +
                                     RAISE WARNING and CONTINUE. Never
                                     re-raise: a re-raise would crash-loop
                                     the apply worker on the same
                                     transaction forever — the exact
                                     incident this system prevents.
                                     Loudness comes from monitoring the
                                     failures table, not from blocking
                                     the stream.

    initial_watermark should be the publisher's max(id) at install time,
    BEFORE the log table is added to the publication, so pre-existing rows
    are never replayed. Idempotent; an existing watermark is preserved.
    """

    def _sub_sql(sql: str) -> str:
        return _run_subscriber_sql(
            subscriber_container, subscriber_user, subscriber_password,
            subscriber_db, sql,
        )

    # same shape as the publisher's log table — receives replicated rows
    _sub_sql(f"""
CREATE TABLE IF NOT EXISTS public.{CAPTURE_LOG_TABLE} (
    id bigserial PRIMARY KEY,
    captured_at timestamptz NOT NULL DEFAULT now(),
    lsn pg_lsn NOT NULL,
    txid bigint NOT NULL,
    command_tag text NOT NULL,
    object_identity text,
    schema_name text,
    ddl_text text NOT NULL,
    search_path text
);
""")

    _sub_sql("""
CREATE TABLE IF NOT EXISTS public._snaplicator_ddl_watermark (
    id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_applied_id bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);
""")

    # exact-id dedupe set: one row per processed log id. Grows by one row
    # per DDL statement ever captured — no pruning needed at that rate.
    _sub_sql("""
CREATE TABLE IF NOT EXISTS public._snaplicator_ddl_applied (
    id bigint PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
""")

    _sub_sql("""
CREATE TABLE IF NOT EXISTS public._snaplicator_ddl_failures (
    id bigserial PRIMARY KEY,
    log_id bigint NOT NULL,
    ddl_text text NOT NULL,
    error text NOT NULL,
    search_path text,
    failed_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public._snaplicator_ddl_failures
    ADD COLUMN IF NOT EXISTS search_path text;
""")

    _sub_sql("""
CREATE TABLE IF NOT EXISTS public._snaplicator_ddl_deferred (
    id bigserial PRIMARY KEY,
    log_id bigint NOT NULL,
    ddl_text text NOT NULL,
    search_path text,
    deferred_at timestamptz NOT NULL DEFAULT now(),
    executed_at timestamptz,
    error text
);
""")

    _sub_sql(
        "INSERT INTO public._snaplicator_ddl_watermark (id, last_applied_id) "
        f"VALUES (1, {int(initial_watermark)}) ON CONFLICT (id) DO NOTHING;"
    )

    _sub_sql("""
CREATE OR REPLACE FUNCTION public._snaplicator_apply_ddl()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    wm bigint;
    saved_sp text;
BEGIN
    -- Seed skip: history that predates enable (arrives via initial COPY).
    -- The watermark never advances past the seed — ids are assigned at
    -- INSERT time but delivery is commit-ordered, so an advancing
    -- high-watermark would drop the DDL of a lower id committing late.
    wm := coalesce((SELECT last_applied_id
                      FROM public._snaplicator_ddl_watermark WHERE id = 1), 0);
    IF NEW.id <= wm THEN
        RETURN NEW;
    END IF;

    -- Exact-id dedupe: re-delivery (clone, re-subscription) skips; a late
    -- lower id from an overlapping publisher transaction does not.
    IF EXISTS (SELECT 1 FROM public._snaplicator_ddl_applied a
                WHERE a.id = NEW.id) THEN
        RETURN NEW;
    END IF;
    INSERT INTO public._snaplicator_ddl_applied (id) VALUES (NEW.id);

    IF NEW.ddl_text ~* 'CONCURRENTLY' THEN
        INSERT INTO public._snaplicator_ddl_deferred (log_id, ddl_text, search_path)
        VALUES (NEW.id, NEW.ddl_text, NEW.search_path);
    ELSE
        saved_sp := current_setting('search_path', true);
        BEGIN
            IF NEW.search_path IS NOT NULL AND NEW.search_path <> '' THEN
                PERFORM set_config('search_path', NEW.search_path, true);
            END IF;
            EXECUTE NEW.ddl_text;
        EXCEPTION WHEN OTHERS THEN
            -- loud skip: never re-raise (would crash-loop the apply worker)
            INSERT INTO public._snaplicator_ddl_failures
                (log_id, ddl_text, error, search_path)
            VALUES (NEW.id, NEW.ddl_text, SQLERRM, NEW.search_path);
            RAISE WARNING 'snaplicator: ddl apply failed for log id %: %',
                NEW.id, SQLERRM;
        END;
        PERFORM set_config('search_path', coalesce(saved_sp, 'public'), true);
    END IF;

    RETURN NEW;
END;
$fn$;
""")

    # ENABLE ALWAYS: the apply worker runs with
    # session_replication_role = replica, which suppresses ordinary triggers.
    _sub_sql(f"""
DROP TRIGGER IF EXISTS _snaplicator_ddl_apply ON public.{CAPTURE_LOG_TABLE};
CREATE TRIGGER _snaplicator_ddl_apply
    AFTER INSERT ON public.{CAPTURE_LOG_TABLE}
    FOR EACH ROW EXECUTE FUNCTION public._snaplicator_apply_ddl();
ALTER TABLE public.{CAPTURE_LOG_TABLE}
    ENABLE ALWAYS TRIGGER _snaplicator_ddl_apply;
""")

    return {
        "installed": True,
        "log_table": CAPTURE_LOG_TABLE,
        "initial_watermark": int(initial_watermark),
    }


def verify_ddl_apply_installed(
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
) -> bool:
    """Check the apply trigger exists on the log table and is ENABLE ALWAYS."""
    sql = (
        "SELECT count(*) FROM pg_trigger t "
        "JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE t.tgname = '_snaplicator_ddl_apply' "
        f"AND c.relname = '{CAPTURE_LOG_TABLE}' "
        "AND t.tgenabled = 'A';"
    )
    out = _run_subscriber_sql(
        subscriber_container, subscriber_user, subscriber_password,
        subscriber_db, sql,
    )
    return out.strip() == "1"


def add_log_table_to_publication(
    publisher_connstr: str, publication_name: str
) -> Dict:
    """Put the DDL log table into the publication so its rows ride the
    normal replication stream. Idempotent; FOR ALL TABLES publications
    already include it (and reject ALTER PUBLICATION ADD TABLE)."""
    allt = _run_publisher_sql(
        publisher_connstr,
        f"SELECT puballtables FROM pg_publication WHERE pubname = '{publication_name}';",
    ).strip()
    if allt == "":
        return {"added": False, "reason": "publication_not_found"}
    if allt == "t":
        return {"added": False, "reason": "for_all_tables"}

    member = _run_publisher_sql(
        publisher_connstr,
        "SELECT count(*) FROM pg_publication_tables "
        f"WHERE pubname = '{publication_name}' "
        f"AND tablename = '{CAPTURE_LOG_TABLE}';",
    ).strip()
    if member == "1":
        return {"added": False, "reason": "already_member"}

    _run_publisher_sql(
        publisher_connstr,
        f"ALTER PUBLICATION {publication_name} "
        f"ADD TABLE public.{CAPTURE_LOG_TABLE};",
    )
    return {"added": True}


def check_subscription_health(
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
    subscription_name: str,
) -> Dict:
    """Subscription health snapshot for the sync loop's alerting.

    Three subscriber-side signals:
      * enabled        — pg_subscription.subenabled; false after
                         disable_on_error or a manual disable
      * worker_running — an apply worker row (relid IS NULL) with a live pid
                         in pg_stat_subscription; absent while the worker is
                         down or crash-looping between restarts
      * apply_errors / sync_errors — cumulative counters from
                         pg_stat_subscription_stats (PG15+); monotonically
                         increasing, so the caller change-detects deltas
    """
    # NB: no ::text cast on subenabled — psql -At prints bare booleans as
    # t/f, while an explicit cast yields 'true'/'false'.
    sql = (
        "SELECT s.subenabled, "
        "(SELECT count(*) FROM pg_stat_subscription st "
        " WHERE st.subname = s.subname AND st.relid IS NULL AND st.pid IS NOT NULL), "
        "coalesce(ss.apply_error_count, 0), coalesce(ss.sync_error_count, 0) "
        "FROM pg_subscription s "
        "LEFT JOIN pg_stat_subscription_stats ss ON ss.subname = s.subname "
        f"WHERE s.subname = '{subscription_name}';"
    )
    out = _run_subscriber_sql(
        subscriber_container, subscriber_user, subscriber_password,
        subscriber_db, sql,
    ).strip()
    if not out:
        return {"exists": False, "enabled": False, "worker_running": False,
                "apply_errors": 0, "sync_errors": 0}
    enabled, workers, apply_errs, sync_errs = out.split(",")
    return {
        "exists": True,
        "enabled": enabled == "t",
        "worker_running": int(workers) > 0,
        "apply_errors": int(apply_errs),
        "sync_errors": int(sync_errs),
    }


def get_ddl_apply_status(
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
) -> Dict:
    """Apply progress + failure/deferred counts, for the sync loop and
    monitoring. "watermark" is the highest processed log id — the frozen
    seed or the max of the exact-id applied set, whichever is higher (the
    stored watermark row itself never advances past the seed)."""
    sql = (
        "SELECT GREATEST("
        "coalesce((SELECT last_applied_id FROM public._snaplicator_ddl_watermark WHERE id = 1), 0), "
        "coalesce((SELECT max(id) FROM public._snaplicator_ddl_applied), 0)) "
        "|| ',' || (SELECT count(*) FROM public._snaplicator_ddl_failures) "
        "|| ',' || (SELECT count(*) FROM public._snaplicator_ddl_deferred WHERE executed_at IS NULL);"
    )
    out = _run_subscriber_sql(
        subscriber_container, subscriber_user, subscriber_password,
        subscriber_db, sql,
    ).strip()
    wm, failures, deferred = out.split(",")
    return {
        "watermark": int(wm),
        "failures": int(failures),
        "deferred_pending": int(deferred),
    }


def _run_subscriber_sql_cmds(
    container_name: str, user: str, password: str | None, db: str,
    sqls: List[str],
) -> str:
    """Like _run_subscriber_sql but one psql invocation with multiple -c:
    same session, one implicit transaction per command — required for
    statements that refuse transaction blocks (CREATE INDEX CONCURRENTLY)
    while still letting an earlier session-level SET reach them."""
    cmd: list[str] = ["docker", "exec"]
    if password:
        cmd += ["-e", f"PGPASSWORD={password}"]
    cmd += [container_name, "psql", "-U", user, "-d", db, "-At",
            "-v", "ON_ERROR_STOP=1"]
    for sql in sqls:
        cmd += ["-c", sql]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=True)
    return (proc.stdout or "").strip()


def get_publisher_max_ddl_log_id(publisher_connstr: str) -> int:
    """Watermark seed: log rows at or below this id must never replay."""
    out = _run_publisher_sql(
        publisher_connstr,
        f"SELECT coalesce(max(id), 0) FROM public.{CAPTURE_LOG_TABLE};",
    ).strip()
    return int(out or 0)


def enable_ddl_apply(
    publisher_connstr: str,
    publication_name: str,
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
    subscription_name: str,
) -> Dict:
    """Connect the DDL stream, idempotently. Order matters: the watermark is
    seeded from the publisher's current max(id) BEFORE the log table joins
    the publication, so history arriving via the initial COPY is never
    executed. REFRESH runs only when the subscriber is not yet pulling the
    log table, so steady-state calls are read-only no-ops."""
    wm = get_publisher_max_ddl_log_id(publisher_connstr)
    install_ddl_apply(
        subscriber_container, subscriber_user, subscriber_password,
        subscriber_db, initial_watermark=wm,
    )
    added = add_log_table_to_publication(publisher_connstr, publication_name)
    subscribed = _run_subscriber_sql(
        subscriber_container, subscriber_user, subscriber_password,
        subscriber_db,
        "SELECT count(*) FROM pg_subscription_rel sr "
        "JOIN pg_class c ON c.oid = sr.srrelid "
        f"WHERE c.relname = '{CAPTURE_LOG_TABLE}';",
    ).strip()
    if subscribed == "0":
        _run_subscriber_sql(
            subscriber_container, subscriber_user, subscriber_password,
            subscriber_db,
            f"ALTER SUBSCRIPTION {subscription_name} REFRESH PUBLICATION;",
        )
    return {"watermark": wm, "refreshed": subscribed == "0", **added}


def run_deferred_ddl(
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
) -> Dict:
    """One-shot out-of-band executor for deferred DDL (CONCURRENTLY): each
    row is attempted exactly once — success stamps executed_at, failure
    stamps error and the row is never retried (human resolution, same
    philosophy as _snaplicator_ddl_failures). A failed CREATE INDEX
    CONCURRENTLY may leave an INVALID index behind; that cleanup is part of
    the human resolution."""
    raw = _run_subscriber_sql(
        subscriber_container, subscriber_user, subscriber_password,
        subscriber_db,
        "SELECT id::text || '|' || coalesce(search_path, '') || '|' || "
        "replace(encode(convert_to(ddl_text, 'UTF8'), 'base64'), chr(10), '') "
        "FROM public._snaplicator_ddl_deferred "
        "WHERE executed_at IS NULL AND error IS NULL ORDER BY id;",
    )
    executed: List[int] = []
    errors: List[Dict] = []
    for line in [ln for ln in raw.splitlines() if ln.strip()]:
        row_id, sp, b64 = line.split("|", 2)
        ddl = base64.b64decode(b64).decode("utf-8")
        try:
            # Separate -c per statement: CONCURRENTLY refuses transaction
            # blocks; the session-level SET still reaches the DDL.
            cmds = [f"SET search_path TO {sp};"] if sp.strip() else []
            _run_subscriber_sql_cmds(
                subscriber_container, subscriber_user, subscriber_password,
                subscriber_db, cmds + [ddl],
            )
            _run_subscriber_sql(
                subscriber_container, subscriber_user, subscriber_password,
                subscriber_db,
                "UPDATE public._snaplicator_ddl_deferred "
                f"SET executed_at = now() WHERE id = {int(row_id)};",
            )
            executed.append(int(row_id))
        except subprocess.CalledProcessError as e:
            msg = ((e.stderr or e.stdout or "") or str(e)).strip()[:300]
            _run_subscriber_sql(
                subscriber_container, subscriber_user, subscriber_password,
                subscriber_db,
                "UPDATE public._snaplicator_ddl_deferred "
                f"SET error = '{msg.replace(chr(39), chr(39) * 2)}' "
                f"WHERE id = {int(row_id)};",
            )
            errors.append({"id": int(row_id), "error": msg})
    return {"executed": executed, "errors": errors}
