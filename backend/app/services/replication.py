from __future__ import annotations

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
    """Run the same SQL file on publisher (host) and subscriber (inside container).

    Returns status and outputs separately. Always returns 200-level result to allow FE to show both sides,
    with ok flags and error messages included.
    """
    sql_path = Path(sql_file)
    if not sql_path.exists():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")

    # Publisher: direct psql with libpq connstr
    pub_ok = False
    pub_out = ""
    pub_err = ""
    try:
        p_pub = subprocess.run(
            [
                "psql", publisher_connstr,
                "-v", "ON_ERROR_STOP=1",
                "-At", "-F", ",",
                "-f", str(sql_path),
            ],
            text=True, capture_output=True, check=True,
        )
        pub_ok = True
        pub_out = (p_pub.stdout or "").strip()
    except subprocess.CalledProcessError as e:  # noqa: PERF203
        pub_err = (e.stderr or e.stdout or "").strip()

    # Subscriber: copy file into container and run psql locally
    sub_ok = False
    sub_out = ""
    sub_err = ""
    try:
        # Copy SQL into container
        cp = subprocess.run(["docker", "cp", str(sql_path), f"{subscriber_container}:/tmp/replication_check.sql"], text=True, capture_output=True)
        if cp.returncode != 0:
            raise RuntimeError((cp.stderr or cp.stdout or "").strip())

        # Build exec command with PGPASSWORD if provided
        exec_cmd: List[str] = [
            "docker", "exec", subscriber_container,
        ]
        if subscriber_password:
            exec_cmd += ["env", f"PGPASSWORD={subscriber_password}"]
        exec_cmd += [
            "psql", "-h", "localhost",
            "-U", subscriber_user, "-d", subscriber_db,
            "-v", "ON_ERROR_STOP=1",
            "-At", "-F", ",",
            "-f", "/tmp/replication_check.sql",
        ]
        p_sub = subprocess.run(exec_cmd, text=True, capture_output=True, check=True)
        sub_ok = True
        sub_out = (p_sub.stdout or "").strip()
    except subprocess.CalledProcessError as e:  # noqa: PERF203
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
        "WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
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

    # 3) Tables on subscriber
    sub_sql = (
        "SELECT table_schema, table_name "
        "FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';"
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
        result.append({
            "schema": schema,
            "table": table,
            "in_publication": fqn in pub_set,
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

    to_remove = [t for t in tables if t in existing]
    skipped = [t for t in tables if t not in existing]

    if not to_remove:
        return {"removed": [], "skipped": skipped, "message": "None of the tables are in publication"}

    table_list = ", ".join(to_remove)
    alter_sql = f"ALTER PUBLICATION {publication_name} DROP TABLE {table_list};"
    _run_publisher_sql(publisher_connstr, alter_sql)

    return {"removed": to_remove, "skipped": skipped}



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
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';"
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
    """Detect tables in publication but not on subscriber, sync schema and refresh.

    Returns None if nothing to sync, otherwise dict with sync details.
    """
    # Tables in publication
    pub_sql = f"SELECT schemaname || '.' || tablename FROM pg_publication_tables WHERE pubname = '{publication_name}';"
    pub_out = _run_publisher_sql(publisher_connstr, pub_sql)
    pub_tables = {line.strip() for line in pub_out.splitlines() if line.strip()}

    # Tables on subscriber
    sub_sql = (
        "SELECT table_schema || '.' || table_name "
        "FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';"
    )
    try:
        sub_out = _run_subscriber_sql(subscriber_container, subscriber_user, subscriber_password, subscriber_db, sub_sql)
    except Exception:
        sub_out = ""
    sub_tables = {line.strip() for line in sub_out.splitlines() if line.strip()}

    # Find tables in publication but missing from subscriber
    missing = [t for t in pub_tables if t not in sub_tables]
    if not missing:
        return None

    # Sync schemas
    import tempfile, os
    synced = []
    errors = []
    for table in missing:
        try:
            dump_proc = subprocess.run(
                ["pg_dump", publisher_connstr, "--schema-only", "-t", table],
                text=True, capture_output=True, check=True,
            )
            ddl = dump_proc.stdout
            if not ddl.strip():
                errors.append({"table": table, "error": "Empty schema dump"})
                continue

            with tempfile.NamedTemporaryFile(mode='w', suffix='.sql', delete=False) as tmp:
                tmp.write(ddl)
                tmp_path = tmp.name

            try:
                subprocess.run(
                    ["docker", "cp", tmp_path, f"{subscriber_container}:/tmp/_auto_sync.sql"],
                    text=True, capture_output=True, check=True,
                )
                exec_cmd = ["docker", "exec"]
                if subscriber_password:
                    exec_cmd += ["-e", f"PGPASSWORD={subscriber_password}"]
                exec_cmd += [subscriber_container, "psql", "-U", subscriber_user, "-d", subscriber_db, "-f", "/tmp/_auto_sync.sql"]
                subprocess.run(exec_cmd, text=True, capture_output=True, check=True)
                synced.append(table)
            finally:
                os.unlink(tmp_path)
        except subprocess.CalledProcessError as e:
            errors.append({"table": table, "error": (e.stderr or e.stdout or str(e)).strip()})

    # Refresh subscription if any tables were synced
    refresh_ok = False
    if synced:
        try:
            sql = f"ALTER SUBSCRIPTION {subscription_name} REFRESH PUBLICATION;"
            _run_subscriber_sql(subscriber_container, subscriber_user, subscriber_password, subscriber_db, sql)
            refresh_ok = True
        except Exception as e:
            errors.append({"table": "_refresh", "error": str(e)})

    return {"synced": synced, "errors": errors, "refreshed": refresh_ok}



def sync_column_changes(
    publisher_connstr: str,
    publication_name: str,
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
) -> Dict | None:
    """Compare column definitions between publisher and subscriber for published tables.

    Detects missing columns on subscriber and applies ALTER TABLE ADD COLUMN.
    Returns None if no changes, otherwise dict with details.
    """
    # Get published tables
    pub_tables_sql = f"SELECT schemaname || '.' || tablename FROM pg_publication_tables WHERE pubname = '{publication_name}';"
    pub_out = _run_publisher_sql(publisher_connstr, pub_tables_sql)
    pub_tables = [line.strip() for line in pub_out.splitlines() if line.strip()]

    if not pub_tables:
        return None

    table_names = [t.split(".")[-1] for t in pub_tables]

    # Query columns from publisher - use pg_catalog to avoid quoting issues
    pub_cols_sql = (
        "SELECT n.nspname, c.relname, a.attname, a.attnum::text, "
        "t.typname, a.attnotnull::text, "
        "COALESCE(pg_get_expr(d.adbin, d.adrelid), '') "
        "FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid "
        "JOIN pg_type t ON t.oid = a.atttypid "
        "LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum "
        "WHERE n.nspname = 'public' "
        "AND a.attnum > 0 AND NOT a.attisdropped "
        f"AND c.relname IN ({','.join(repr(t) for t in table_names)}) "
        "ORDER BY c.relname, a.attnum;"
    )
    try:
        pub_cols_out = _run_publisher_sql(publisher_connstr, pub_cols_sql)
    except Exception:
        return None

    # Parse: {schema.table: {col_name: {typname, notnull, default}}}
    pub_columns: dict = {}
    for line in pub_cols_out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 7:
            continue
        schema, table, col, pos = parts[0], parts[1], parts[2], parts[3]
        typname, notnull = parts[4], parts[5]
        default = ",".join(parts[6:])
        fqn = f"{schema}.{table}"
        if fqn not in pub_columns:
            pub_columns[fqn] = {}
        pub_columns[fqn][col] = {
            "typname": typname,
            "notnull": notnull == "true",
            "default": default,
            "ordinal": int(pos),
        }

    # Query columns from subscriber using same query
    try:
        sub_cols_out = _run_subscriber_sql(
            subscriber_container, subscriber_user, subscriber_password, subscriber_db,
            pub_cols_sql,
        )
    except Exception:
        return None

    sub_columns: dict = {}
    for line in sub_cols_out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 7:
            continue
        schema, table, col = parts[0], parts[1], parts[2]
        fqn = f"{schema}.{table}"
        if fqn not in sub_columns:
            sub_columns[fqn] = {}
        sub_columns[fqn][col] = True

    # Compare and apply
    added = []
    errors = []

    TYPE_MAP = {
        "int4": "integer", "int8": "bigint", "int2": "smallint",
        "float4": "real", "float8": "double precision",
        "bool": "boolean", "varchar": "character varying",
        "timestamptz": "timestamptz", "timestamp": "timestamp without time zone",
        "text": "text", "jsonb": "jsonb", "json": "json",
        "uuid": "uuid", "numeric": "numeric", "bytea": "bytea",
        "date": "date", "time": "time", "timetz": "time with time zone",
    }

    for fqn, pub_cols in pub_columns.items():
        sub_cols = sub_columns.get(fqn, {})
        if not sub_cols:
            continue  # Table missing entirely; handled by auto_sync_new_tables

        for col_name, pub_info in pub_cols.items():
            if col_name not in sub_cols:
                sql_type = TYPE_MAP.get(pub_info["typname"], pub_info["typname"])

                alter_parts = [f'ALTER TABLE {fqn} ADD COLUMN "{col_name}" {sql_type}']
                if pub_info["default"]:
                    alter_parts.append(f"DEFAULT {pub_info['default']}")
                alter_sql = " ".join(alter_parts) + ";"

                try:
                    _run_subscriber_sql(
                        subscriber_container, subscriber_user, subscriber_password, subscriber_db,
                        alter_sql,
                    )
                    added.append({"table": fqn, "column": col_name, "type": sql_type})
                except subprocess.CalledProcessError as e:
                    errors.append({"table": fqn, "column": col_name, "error": (e.stderr or e.stdout or str(e)).strip()})

    if not added and not errors:
        return None

    return {"columns_added": added, "errors": errors}


# ── Event Trigger Management ──────────────────────


def install_auto_add_trigger(publisher_connstr: str, publication_name: str) -> Dict:
    """Install or update the event trigger on publisher that auto-adds new public tables to publication.

    Idempotent: safe to call multiple times.
    """
    # Create or replace the trigger function with current publication name
    func_sql = f"""
CREATE OR REPLACE FUNCTION _snaplicator_auto_add_to_pub()
RETURNS event_trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    obj record;
BEGIN
    FOR obj IN SELECT * FROM pg_event_trigger_ddl_commands()
    WHERE command_tag = 'CREATE TABLE'
      AND schema_name = 'public'
    LOOP
        EXECUTE format('ALTER PUBLICATION {publication_name} ADD TABLE %s', obj.object_identity);
        RAISE NOTICE 'snaplicator: auto-added % to publication {publication_name}', obj.object_identity;
    END LOOP;
END;
$fn$;
"""
    _run_publisher_sql(publisher_connstr, func_sql)

    # Create event trigger if not exists (PG14+)
    # DROP + CREATE to ensure function reference is fresh
    trigger_sql = """
DO $do$
BEGIN
    DROP EVENT TRIGGER IF EXISTS _snaplicator_auto_pub_add;
    CREATE EVENT TRIGGER _snaplicator_auto_pub_add
    ON ddl_command_end
    WHEN TAG IN ('CREATE TABLE')
    EXECUTE FUNCTION _snaplicator_auto_add_to_pub();
END;
$do$;
"""
    _run_publisher_sql(publisher_connstr, trigger_sql)

    return {"installed": True, "publication": publication_name}


def verify_trigger_installed(publisher_connstr: str) -> bool:
    """Check if the auto-add event trigger exists on the publisher."""
    sql = "SELECT 1 FROM pg_event_trigger WHERE evtname = '_snaplicator_auto_pub_add';"
    out = _run_publisher_sql(publisher_connstr, sql)
    return out.strip() == "1"
