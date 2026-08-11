from __future__ import annotations

import base64
import hashlib
import re
import subprocess
from typing import Dict, Iterable, List, Optional
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


def get_initial_copy_progress(
	container_name: str,
	postgres_user: str,
	postgres_db: str,
	publisher_connstr: str | None = None,
) -> Dict:
	"""Report initial logical replication copy progress on the subscriber.

	Heuristic:
	- total_tables = count rows in pg_subscription_rel
	- finished_tables = count rows with srsubstate in ('r','s')
	- status: 'idle' if total=0; 'copying' if finished<total; 'complete' otherwise
	- active copy details from pg_subscription_rel (states not 'r') and, if available, pg_stat_progress_copy

	`publisher_connstr` buys the denominator. Everything else here is measured
	on the subscriber, which can only report what has already landed: a table
	that has not started copying is zero bytes there, so summing the subscriber
	gives "how much so far" and can never give "out of how much". The size the
	copy is heading towards is a fact about the primary, and only the primary
	can be asked for it.
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
		# Every table, not only the unfinished ones. What is left is only half
		# the picture — "3 of 8" says nothing about which three, and a copy
		# that has been running for an hour is judged by what it has got
		# through, not by what it is holding.
		detail_sql = (
			"SELECT r.srsubstate, n.nspname, c.relname, "
			"       pg_total_relation_size(c.oid) "
			"FROM pg_subscription_rel r "
			"JOIN pg_class c ON c.oid = r.srrelid "
			"JOIN pg_namespace n ON n.oid = c.relnamespace "
			"ORDER BY 2,3;"
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
				try:
					size = int(parts[3]) if len(parts) > 3 and parts[3] else 0
				except ValueError:
					size = 0
				details.append({
					"state": parts[0],
					"schema": parts[1],
					"table": parts[2],
					# The subscriber's copy of it: what has landed so far,
					# which is what makes "how far along" answerable for a
					# table PostgreSQL reports no byte progress for.
					"size_bytes": size,
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

	# What the copy is heading towards, asked of the primary.
	#
	# Only for the tables actually in the copy set: a publication covering a
	# tenth of the database would otherwise be measured against the whole of
	# it, and the bar would crawl to 10% and stop looking like it worked.
	#
	# pg_total_relation_size on both sides — heap, TOAST and indexes — because
	# the numerator is the subscriber's pg_total_relation_size and comparing
	# two different measures is worse than comparing one imperfectly. It is
	# imperfect: the subscriber rebuilds indexes rather than copying them and
	# carries none of the primary's bloat, so the same rows commonly land
	# smaller. Expect the total to be reached early rather than late.
	expected_bytes: int | None = None
	if publisher_connstr and details:
		# Named explicitly rather than filtered afterwards. This runs every
		# three seconds for as long as the copy lasts, against a primary that
		# is usually production and may hold hundreds of relations —
		# pg_total_relation_size stats every fork of every one of them, and
		# asking for 587 to keep 3 is a cost paid on someone's live database
		# once per poll.
		wanted = sorted({(d["schema"], d["table"]) for d in details})
		try:
			pairs = ", ".join(
				f"({_quote_literal(s)}, {_quote_literal(t)})" for s, t in wanted
			)
			rows = _run_publisher_sql(
				publisher_connstr,
				"SELECT COALESCE(sum(pg_total_relation_size(c.oid)), 0)::text "
				"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
				f"WHERE c.relkind IN ('r','p') AND (n.nspname, c.relname) IN ({pairs});",
			)
			acc = int((rows.strip().splitlines() or ["0"])[0] or 0)
			expected_bytes = acc or None
		except Exception:
			expected_bytes = None

	status = "idle" if total == 0 else ("copying" if done < total else "complete")
	percent = (done / total * 100.0) if total > 0 else 0.0
	return {
		"status": status,
		"total_tables": total,
		"finished_tables": done,
		"percent": percent,
		"expected_bytes": expected_bytes,
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

    # 1) All public tables + estimated row count and size from publisher
    #
    # Rows and bytes answer different questions and neither substitutes for
    # the other: 40 million narrow rows and 400 thousand rows of JSON are not
    # comparable by count, and the copy is paid for in bytes while the screen
    # was only ever showing counts. Both, labelled, or the reader supplies the
    # missing unit themselves — and reads 2.5M as megabytes.
    all_tables_sql = (
        "SELECT t.table_schema, t.table_name, COALESCE(s.n_live_tup, 0)::text, "
        "COALESCE(pg_total_relation_size(c.oid), 0)::text "
        "FROM information_schema.tables t "
        "LEFT JOIN pg_namespace n ON n.nspname = t.table_schema "
        "LEFT JOIN pg_class c ON c.relname = t.table_name AND c.relnamespace = n.oid "
        "LEFT JOIN pg_stat_user_tables s ON s.schemaname = t.table_schema AND s.relname = t.table_name "
        "WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema') AND t.table_type = 'BASE TABLE' "
        # Snaplicator's own outbox rides its own publication; showing it as a
        # choice here only lets someone stop DDL replicating while every
        # screen still says it is on.
        f"AND t.table_name <> '{CAPTURE_LOG_TABLE}' "
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
        try:
            size_bytes = int(parts[3]) if len(parts) > 3 and parts[3] else 0
        except ValueError:
            size_bytes = 0
        fqn = f"{schema}.{table}"
        in_pub = fqn in pub_set
        result.append({
            "schema": schema,
            "table": table,
            "in_publication": in_pub,
            "pub_via": ("table" if fqn in indiv_set else "schema") if in_pub else None,
            "in_subscriber": fqn in sub_set,
            "estimated_rows": estimated_rows,
            "size_bytes": size_bytes,
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
# The log table used to have a publication of its own. It rides the data
# publication now; the name survives only so the chooser can recognise and
# hide the leftover on a primary an older install touched.
CAPTURE_LOG_PUBLICATION = "_snaplicator_ddl"


AUTO_ADD_PREFIX = "_snaplicator_auto_add_"


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def auto_add_name(publication_name: str) -> str:
    """The auto-add trigger's name for one publication.

    Per publication rather than one for the server, because this is the only
    part of capture that differs between installs: it names a publication and
    is scoped to what that publication covers. A single fixed name meant the
    last install to start owned it, and the other one's new tables quietly
    stopped being added — no error, because from PostgreSQL's side nothing is
    wrong.

    Names are capped at 63 bytes, so a long publication name is truncated and
    given a digest of the whole thing: two publications that share a prefix
    must not share a trigger.
    """
    room = 63 - len(AUTO_ADD_PREFIX)
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", publication_name)
    if len(safe) > room:
        digest = hashlib.sha1(publication_name.encode()).hexdigest()[:8]
        safe = safe[: room - 9] + "_" + digest
    return AUTO_ADD_PREFIX + safe


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_text_array(values: Optional[Iterable[str]]) -> str:
    """A text[] literal for embedding in generated PL/pgSQL."""
    items = ", ".join("'" + str(v).replace("'", "''") + "'" for v in (values or []))
    return f"ARRAY[{items}]::text[]"


def install_capture_triggers(
    publisher_connstr: str,
    publication_name: str,
    follow_schemas: Optional[List[str]] = None,
    excluded: Optional[List[str]] = None,
    unfollow_schemas: Optional[List[str]] = None,
) -> Dict:
    """Install wide DDL capture on the publisher.

    `follow_schemas` scopes the auto-add half. Left None it is derived from the
    publication's own membership, which is what it has always done and what the
    tests pin. Passed explicitly it is obeyed instead — including when empty,
    which is how "follow nothing at all" is expressed for a publication this
    install may not rewrite. Capture keeps running either way: the two halves
    share a trigger but not a switch.

    `unfollow_schemas` takes schemas back out of whichever scope the above
    produced. Following is the default for a replicated schema, so a person
    turning it off for one schema is recorded as the exception it is rather
    than by replacing the scope with a list — a list would also have to be
    right about every schema that does not exist yet.

    `excluded` names tables that must not be added back. A table someone took
    out is not a table to re-add the next time it is recreated.

    Creates the _snaplicator_ddl_log outbox table plus three event triggers,
    split by whether they depend on a publication:
      _snaplicator_capture_ddl    ON ddl_command_end  (CREATE/ALTER, wide)
      _snaplicator_capture_drop   ON sql_drop         (DROP)
      _snaplicator_auto_add_<pub> ON ddl_command_end  (new tables -> pub)

    The first two log DDL and name no publication, so every install writes
    the same function and they are shared. The third names one and is scoped
    to what it covers, so it is per publication — which is what lets a second
    install attach to the same primary without taking the first one's
    auto-add away. See auto_add_name().

    Capture is intentionally wide — scoping decisions happen at apply time.
    Only three capture-side guards exist:
      1. recursion — DDL touching _snaplicator_* objects is never logged
      2. noise/DCL — GRANT/REVOKE/SECURITY LABEL/COMMENT are never logged
         (TRUNCATE rides native logical replication; role/password commands
         are cluster-global and never reach event triggers)
      3. dedupe   — at most one log row per (txid, query string): subcommand
         entries (serial sequences, PK indexes) and double-firing commands
         (ALTER TABLE DROP COLUMN hits both triggers) collapse to one row

    Replaces the legacy _snaplicator_auto_pub_add trigger, which had one fixed
    name for the whole server.

    Idempotent: safe to call repeatedly; existing log rows are preserved.

    All log-table references inside the trigger functions are schema-qualified
    (public.): the triggers run under the DDL issuer's search_path, so an
    unqualified reference would fail (and silently lose the capture) whenever
    a migration runs with SET search_path. A function-level SET search_path
    is not an option — it would corrupt the captured search_path value.
    """
    # First, before anything that runs DDL: retire the legacy auto-add event
    # trigger. It adds every new table to the publication unconditionally, so
    # against a FOR ALL TABLES publication it raises and takes the CREATE
    # TABLE with it — including the CREATE TABLE below, which would leave
    # install unable to bootstrap itself. The trigger set this installs is
    # dropped and recreated at the end as before.
    _run_publisher_sql(
        publisher_connstr,
        "DROP EVENT TRIGGER IF EXISTS _snaplicator_auto_pub_add;",
    )

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

    # Membership is the scope, and it is what makes following the default: a
    # schema the publication already covers keeps taking its new tables
    # without anyone having listed it anywhere. An empty follow_schemas is a
    # real answer — "follow nothing" — and has to survive as an array that
    # matches no schema rather than fall back to the derived scope.
    if follow_schemas is None:
        scope_clause = (
            "c.schema_name IN (SELECT pt.schemaname FROM pg_publication_tables pt "
            f"WHERE pt.pubname = '{publication_name}')"
        )
    else:
        scope_clause = f"c.schema_name = ANY({_sql_text_array(follow_schemas)})"

    # The exceptions, applied to whichever scope that was.
    if unfollow_schemas:
        scope_clause = (
            f"({scope_clause}) AND NOT "
            f"(c.schema_name = ANY({_sql_text_array(unfollow_schemas)}))"
        )

    exclude_clause = (
        f"NOT (c.object_identity = ANY({_sql_text_array(excluded)}))"
        if excluded else "true"
    )

    capture_fn_sql = f"""
CREATE OR REPLACE FUNCTION _snaplicator_capture_ddl()
RETURNS event_trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    q text := current_query();
    cmd record;
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

END;
$fn$;
"""
    _run_publisher_sql(publisher_connstr, capture_fn_sql)

    # Auto-add: the only part of capture that belongs to one install. It names
    # a publication, and its scope is that publication's coverage — so it gets
    # a name derived from the publication, and a second install on the same
    # primary installs its own beside this one instead of over it.
    #
    # FOR ALL TABLES publications include new tables by themselves (and reject
    # ALTER PUBLICATION ADD TABLE), so the body checks and skips.
    #
    # Excluded schemas stay out — e.g. FDW-managed etl tables, some without
    # PKs: publishing one breaks publisher-side UPDATE/DELETE.
    auto_add = auto_add_name(publication_name)
    auto_add_fn_sql = f"""
CREATE OR REPLACE FUNCTION {auto_add}()
RETURNS event_trigger
LANGUAGE plpgsql
AS $fn$
DECLARE
    tbl record;
    allt boolean;
BEGIN
    SELECT puballtables INTO allt
      FROM pg_publication WHERE pubname = {_quote_literal(publication_name)};
    IF allt IS NOT FALSE THEN
        RETURN;
    END IF;
    FOR tbl IN
        SELECT c.object_identity
          FROM pg_event_trigger_ddl_commands() c
         WHERE c.command_tag IN ('CREATE TABLE', 'CREATE TABLE AS',
                                 'SELECT INTO')
           AND c.object_type = 'table'
           AND c.object_identity !~ '_snaplicator_'
           AND {scope_clause}
           AND {exclude_clause}
    LOOP
        BEGIN
            EXECUTE format(
                'ALTER PUBLICATION {_quote_ident(publication_name)} ADD TABLE %s',
                tbl.object_identity);
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'snaplicator: auto pub add failed for %: %',
                tbl.object_identity, SQLERRM;
        END;
    END LOOP;
END;
$fn$;
"""
    _run_publisher_sql(publisher_connstr, auto_add_fn_sql)

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

    # The functions above are CREATE OR REPLACE, so an existing trigger already
    # points at the new body and recreating it would buy nothing — while the
    # gap between DROP and CREATE is a window where DDL goes uncaptured. With
    # two installs reinstalling on their own schedules that window is somebody
    # else's data loss, so the triggers are only made when they are missing.
    # Disabled counts as missing: a trigger that does not fire is not capture.
    triggers_sql = f"""
DO $do$
BEGIN
    DROP EVENT TRIGGER IF EXISTS _snaplicator_auto_pub_add;

    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger
                    WHERE evtname = '_snaplicator_capture_ddl' AND evtenabled <> 'D') THEN
        DROP EVENT TRIGGER IF EXISTS _snaplicator_capture_ddl;
        CREATE EVENT TRIGGER _snaplicator_capture_ddl
            ON ddl_command_end
            EXECUTE FUNCTION _snaplicator_capture_ddl();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger
                    WHERE evtname = '_snaplicator_capture_drop' AND evtenabled <> 'D') THEN
        DROP EVENT TRIGGER IF EXISTS _snaplicator_capture_drop;
        CREATE EVENT TRIGGER _snaplicator_capture_drop
            ON sql_drop
            EXECUTE FUNCTION _snaplicator_capture_drop();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger
                    WHERE evtname = '{auto_add}' AND evtenabled <> 'D') THEN
        DROP EVENT TRIGGER IF EXISTS {_quote_ident(auto_add)};
        CREATE EVENT TRIGGER {_quote_ident(auto_add)}
            ON ddl_command_end
            EXECUTE FUNCTION {auto_add}();
    END IF;
END;
$do$;
"""
    _run_publisher_sql(publisher_connstr, triggers_sql)

    return {
        "installed": True,
        "publication": publication_name,
        "log_table": CAPTURE_LOG_TABLE,
        "auto_add_trigger": auto_add,
    }


def verify_capture_installed(
    publisher_connstr: str, publication_name: Optional[str] = None
) -> bool:
    """Whether capture is in place — and, if asked, for this publication.

    The two shared triggers say DDL is being logged. They say nothing about
    whether new tables still join *this* publication, which is what the
    per-publication trigger is for; naming the publication asks the whole
    question.
    """
    names = ["_snaplicator_capture_ddl", "_snaplicator_capture_drop"]
    if publication_name:
        names.append(auto_add_name(publication_name))
    listed = ", ".join(_quote_literal(n) for n in names)
    out = _run_publisher_sql(
        publisher_connstr,
        f"SELECT count(*) FROM pg_event_trigger WHERE evtname IN ({listed});",
    )
    return out.strip() == str(len(names))


def uninstall_capture_triggers(
    publisher_connstr: str,
    publication_name: Optional[str] = None,
    drop_log: bool = False,
) -> Dict:
    """Take the capture triggers back off the publisher.

    The counterpart to install, and the thing a primary is owed: these are
    event triggers on somebody's production server, and leaving no way to
    remove them means an install can only ever be added to.

    Naming a publication removes that install's share and leaves the rest: its
    auto-add trigger goes, and the two shared ones go only once no auto-add
    trigger is left, because until then another install is still relying on
    them for its DDL log. Naming nothing means take it all off, which is what
    a single-install publisher wants and what the legacy call did.

    The log table is data — rows that may not have been applied yet — so it
    stays unless asked for, and the functions go with the triggers because
    nothing else calls them.
    """
    if publication_name:
        auto_add = auto_add_name(publication_name)
        _run_publisher_sql(publisher_connstr, f"""
DO $do$
BEGIN
    DROP EVENT TRIGGER IF EXISTS {_quote_ident(auto_add)};

    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger
                    WHERE evtname LIKE '{AUTO_ADD_PREFIX}%') THEN
        DROP EVENT TRIGGER IF EXISTS _snaplicator_auto_pub_add;
        DROP EVENT TRIGGER IF EXISTS _snaplicator_capture_ddl;
        DROP EVENT TRIGGER IF EXISTS _snaplicator_capture_drop;
    END IF;
END;
$do$;
DROP FUNCTION IF EXISTS {auto_add}();
""")
        # Only orphaned now — dropping a function an event trigger still
        # executes would fail, which is exactly the guard we want.
        _run_publisher_sql(publisher_connstr, """
DO $do$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_event_trigger
                    WHERE evtname IN ('_snaplicator_capture_ddl',
                                      '_snaplicator_capture_drop')) THEN
        DROP FUNCTION IF EXISTS _snaplicator_capture_ddl();
        DROP FUNCTION IF EXISTS _snaplicator_capture_drop();
    END IF;
END;
$do$;
""")
    else:
        _run_publisher_sql(publisher_connstr, f"""
DO $do$
DECLARE t record;
BEGIN
    DROP EVENT TRIGGER IF EXISTS _snaplicator_auto_pub_add;
    DROP EVENT TRIGGER IF EXISTS _snaplicator_capture_ddl;
    DROP EVENT TRIGGER IF EXISTS _snaplicator_capture_drop;
    FOR t IN SELECT evtname FROM pg_event_trigger
              WHERE evtname LIKE '{AUTO_ADD_PREFIX}%'
    LOOP
        EXECUTE format('DROP EVENT TRIGGER IF EXISTS %I', t.evtname);
        EXECUTE format('DROP FUNCTION IF EXISTS %I()', t.evtname);
    END LOOP;
END;
$do$;
DROP FUNCTION IF EXISTS _snaplicator_auto_add_to_pub();
DROP FUNCTION IF EXISTS _snaplicator_capture_ddl();
DROP FUNCTION IF EXISTS _snaplicator_capture_drop();
""")
    if drop_log:
        _run_publisher_sql(
            publisher_connstr, f"DROP TABLE IF EXISTS public.{CAPTURE_LOG_TABLE};"
        )
    return {"installed": False, "log_dropped": bool(drop_log)}


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
      * OWNER TO / GRANT / REVOKE  -> record to _snaplicator_ddl_skipped and
                                     do not execute. An owner is a reference
                                     to a role in the publisher's cluster;
                                     this is a different cluster, and the
                                     initial clone already drops ownership
                                     (--no-owner --no-privileges) for that
                                     reason. Executing them can only fail,
                                     and failing is reserved for things that
                                     were supposed to work.
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

    # Kept apart from the failures table on purpose: one is "this did not
    # work", the other is "this was never going to be applied here". Mixing
    # them is what turned a design decision into a page at 2am.
    _sub_sql("""
CREATE OR REPLACE FUNCTION public._snaplicator_ddl_is_local_only(cmd_tag text, ddl text)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $localonly$
    SELECT (cmd_tag IN ('GRANT', 'REVOKE', 'ALTER DEFAULT PRIVILEGES')
            OR ddl ~* '^\\s*ALTER\\s+.*\\sOWNER\\s+TO\\s'
            OR ddl ~* '^\\s*(GRANT|REVOKE)\\s')
       -- ...and only when that is the whole of it. ddl_text is
       -- current_query(), so a batch sent in one round trip is stored as
       -- one row: `ALTER TABLE t ADD COLUMN c; ALTER TABLE t OWNER TO r;`
       -- is a single entry. Skipping that on the strength of the OWNER TO
       -- would drop the ADD COLUMN with it, into a table nobody alerts on.
       -- A batch falls through and fails audibly, as it did before.
       AND btrim(ddl) !~ ';\\s*\\S';
$localonly$;
""")

    _sub_sql("""
CREATE TABLE IF NOT EXISTS public._snaplicator_ddl_skipped (
    id bigserial PRIMARY KEY,
    log_id bigint NOT NULL,
    ddl_text text NOT NULL,
    reason text NOT NULL,
    search_path text,
    skipped_at timestamptz NOT NULL DEFAULT now()
);
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

    -- Ownership and privileges are not replicated, and never were: the
    -- initial schema clone is taken with --no-owner --no-privileges, because
    -- an owner is a reference to a role in *this* cluster and the replica is a
    -- different cluster with none of the primary's roles in it. Replaying
    -- these statements can therefore only fail —
    --
    --   role "brandazine_api_prod" does not exist
    --   ddl: ALTER TABLE url_product OWNER TO brandazine_api_prod
    --
    -- and it failed loudly, into the table that pages someone, for a statement
    -- whose absence is the intended state. Recorded as skipped instead: still
    -- visible, no longer an incident.
    IF public._snaplicator_ddl_is_local_only(NEW.command_tag, NEW.ddl_text) THEN
        INSERT INTO public._snaplicator_ddl_skipped (log_id, ddl_text, reason, search_path)
        VALUES (NEW.id, NEW.ddl_text, 'ownership/privileges are not replicated', NEW.search_path);
    ELSIF NEW.ddl_text ~* 'CONCURRENTLY' THEN
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


def ensure_ddl_publication(publisher_connstr: str, publication_name: str) -> Dict:
    """Put the DDL log table into the publication this replica already reads.

    One publication, not two. The log is Snaplicator's own table travelling to
    Snaplicator's own replica, and a second publication to carry one table
    bought nothing the subscription's existing one does not already give.

    A FOR ALL TABLES publication already contains it, and so does one that
    covers the public schema — pg_publication_tables answers for every form,
    so membership is asked rather than assumed, and ALTER only runs when the
    answer is no.

    What this does cost is that the log table is now inside the object the
    selection screen rewrites. apply_selection has to keep putting it back;
    see the note there. Losing it is silent — replication carries on, DDL
    just stops arriving — which is why that is a rule in code and not a
    convention.
    """
    member = _run_publisher_sql(
        publisher_connstr,
        "SELECT count(*) FROM pg_publication_tables "
        f"WHERE pubname = {_quote_literal(publication_name)} "
        f"AND schemaname = 'public' AND tablename = '{CAPTURE_LOG_TABLE}';",
    ).strip()
    if member != "0":
        return {"publication": publication_name, "added": False}

    _run_publisher_sql(
        publisher_connstr,
        f"ALTER PUBLICATION {_quote_ident(publication_name)} "
        f"ADD TABLE public.{CAPTURE_LOG_TABLE};",
    )
    return {"publication": publication_name, "added": True}


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


def get_recent_ddl_failures(
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
    limit: int = 3,
) -> List[str]:
    """Newest rows from _snaplicator_ddl_failures, compacted to one line
    each — attached to alerts so the page itself carries the failing DDL
    and the error, not just a count."""
    out = _run_subscriber_sql(
        subscriber_container, subscriber_user, subscriber_password,
        subscriber_db,
        "SELECT 'log_id=' || log_id || ' [' || left(error, 160) || '] ddl: ' || left(ddl_text, 140) "
        "FROM public._snaplicator_ddl_failures "
        f"ORDER BY id DESC LIMIT {int(limit)};",
    )
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def get_recent_replication_errors(
    container_name: str, minutes: int = 15, limit: int = 3
) -> List[str]:
    """Recent ERROR lines from the subscriber container's postgres log —
    the only place PG records WHY apply/tablesync failed (the stats views
    carry counts only). Best-effort; empty list on any problem."""
    try:
        proc = subprocess.run(
            ["docker", "logs", "--since", f"{int(minutes)}m", container_name],
            capture_output=True, text=True, timeout=20,
        )
        lines = [
            ln.strip()[:220]
            for ln in (proc.stdout + proc.stderr).splitlines()
            if " ERROR:" in ln
        ]
        return lines[-limit:]
    except Exception:
        return []


# Precedence matters and is not arbitrary. The trigger records a row as
# applied BEFORE it executes it, so a statement that then fails or defers is
# in two tables at once. The later fact is the true one, hence failed and
# deferred outrank applied.
_DDL_STATUS_SQL = """
WITH seed AS (
    SELECT coalesce((SELECT last_applied_id
                       FROM public._snaplicator_ddl_watermark WHERE id = 1), 0) AS id
),
fail AS (
    SELECT DISTINCT ON (log_id) log_id, error, failed_at
      FROM public._snaplicator_ddl_failures ORDER BY log_id, id DESC
),
defr AS (
    SELECT DISTINCT ON (log_id) log_id, executed_at, error, deferred_at
      FROM public._snaplicator_ddl_deferred ORDER BY log_id, id DESC
),
skip AS (%(skip_cte)s)
SELECT l.id::text
    || '|' || CASE
         WHEN f.log_id IS NOT NULL                     THEN 'failed'
         WHEN d.log_id IS NOT NULL AND d.error IS NOT NULL       THEN 'deferred_failed'
         WHEN d.log_id IS NOT NULL AND d.executed_at IS NOT NULL THEN 'deferred_done'
         WHEN d.log_id IS NOT NULL                     THEN 'deferred'
         WHEN s.log_id IS NOT NULL                     THEN 'skipped'
         WHEN a.id IS NOT NULL                         THEN 'applied'
         WHEN l.id <= (SELECT id FROM seed)            THEN 'seed'
         ELSE 'pending'
       END
    || '|' || l.command_tag
    || '|' || coalesce(l.object_identity, '')
    || '|' || coalesce(l.schema_name, '')
    || '|' || to_char(l.captured_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SSZ')
    || '|' || coalesce(to_char(coalesce(f.failed_at, d.executed_at, d.deferred_at)
                                 AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SSZ'), '')
    || '|' || encode(convert_to(coalesce(f.error, d.error, s.reason, ''), 'UTF8'), 'base64')
    || '|' || encode(convert_to(l.ddl_text, 'UTF8'), 'base64')
  FROM public._snaplicator_ddl_log l
  LEFT JOIN public._snaplicator_ddl_applied a ON a.id = l.id
  LEFT JOIN fail f ON f.log_id = l.id
  LEFT JOIN defr d ON d.log_id = l.id
  LEFT JOIN skip s ON s.log_id = l.id
 ORDER BY l.id DESC;
"""

# The skipped table arrived after the others. An install created before it
# does not have one, and asking for a column of a missing table is a syntax
# error, not an empty result — so the CTE is swapped out entirely.
_SKIP_CTE_PRESENT = (
    "SELECT DISTINCT ON (log_id) log_id, reason "
    "FROM public._snaplicator_ddl_skipped ORDER BY log_id, id DESC"
)
_SKIP_CTE_ABSENT = "SELECT NULL::bigint AS log_id, NULL::text AS reason WHERE false"

_DDL_STATUSES = (
    "applied", "deferred", "deferred_done", "deferred_failed",
    "failed", "skipped", "seed", "pending",
)


def get_ddl_apply_rows(
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
    limit: int = 100,
    status: str | None = None,
) -> Dict:
    """Every captured DDL statement with what the subscriber did about it.

    One row per log entry, newest first. `status` filters to a single
    outcome; None returns all. Counts are over the whole log, not the page.
    """
    if status is not None and status not in _DDL_STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {', '.join(_DDL_STATUSES)}")

    def _q(sql: str) -> str:
        return _run_subscriber_sql(
            subscriber_container, subscriber_user, subscriber_password,
            subscriber_db, sql,
        )

    if _q("SELECT to_regclass('public._snaplicator_ddl_log') IS NOT NULL;").strip() != "t":
        # Same keys as the installed answer. A caller that has to branch on
        # which keys exist ends up asserting "installed" twice.
        return {
            "installed": False, "skipped_tracked": False,
            "seed": 0, "counts": {}, "total": 0, "rows": [],
        }

    has_skip = _q(
        "SELECT to_regclass('public._snaplicator_ddl_skipped') IS NOT NULL;"
    ).strip() == "t"

    body = _DDL_STATUS_SQL % {
        "skip_cte": _SKIP_CTE_PRESENT if has_skip else _SKIP_CTE_ABSENT,
    }
    # Counts come from the same query the rows do, wrapped — two hand-written
    # queries would drift the moment the CASE gains a branch.
    counts_sql = (
        "SELECT status || '=' || cnt FROM (SELECT split_part(t.row, '|', 2) AS status, "
        "count(*)::text AS cnt FROM (" + body.rstrip().rstrip(";") +
        ") AS t(row) GROUP BY 1) c;"
    )
    counts = {}
    for line in _q(counts_sql).splitlines():
        if "=" in line:
            k, v = line.strip().split("=", 1)
            counts[k] = int(v)

    # Status lives in a CASE, so it cannot be a WHERE without wrapping the
    # whole query again; the log is small enough that filtering the stream
    # here is cheaper than the second wrap.
    rows: List[Dict] = []
    for line in _q(body).splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 7)
        if len(parts) != 8:
            continue
        rid, st, tag, obj, schema, captured, at, rest = parts
        err_b64, ddl_b64 = rest.split("|", 1) if "|" in rest else (rest, "")
        if status and st != status:
            continue
        rows.append({
            "id": int(rid),
            "status": st,
            "command_tag": tag,
            "object_identity": obj or None,
            "schema_name": schema or None,
            "captured_at": captured,
            "resolved_at": at or None,
            "error": base64.b64decode(err_b64).decode("utf-8") or None,
            "ddl_text": base64.b64decode(ddl_b64).decode("utf-8"),
        })
        if len(rows) >= limit:
            break

    seed = int(_q(
        "SELECT coalesce((SELECT last_applied_id FROM public._snaplicator_ddl_watermark "
        "WHERE id = 1), 0);"
    ).strip() or 0)

    return {
        "installed": True,
        "skipped_tracked": has_skip,
        "seed": seed,
        "counts": counts,
        "total": sum(counts.values()),
        "rows": rows,
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


def catch_up_unapplied_ddl(
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
) -> Dict:
    """Apply log rows that arrived without the trigger seeing them.

    The apply trigger fires on INSERT, and the initial copy of a table is not
    an INSERT: PostgreSQL fills it with COPY, which row triggers do not see.
    The log table is a table like any other, so whatever it already held when
    its copy ran lands on the replica unexecuted — present, above the
    watermark, and silently skipped.

    Most of that is harmless, because the watermark marks everything the
    schema clone already carries. The gap is the DDL captured after the clone
    and before the copy: not in the schema, and never executed. A CREATE
    TABLE lost there is worse than a missing table — the table joins the
    publication, the publisher starts sending its rows, and the apply worker
    jams on a relation that does not exist locally, holding up everything
    behind it.

    Only once the copy is finished. Half a replica is not a thing to run
    migrations against, and the same rows are still here a cycle later.

    Idempotent by the same dedupe set the trigger uses, so a row cannot be
    applied twice however it arrived.
    """
    pending = _run_subscriber_sql(
        subscriber_container, subscriber_user, subscriber_password, subscriber_db,
        f"""
SELECT count(*) FROM public.{CAPTURE_LOG_TABLE} l
 WHERE l.id > coalesce((SELECT last_applied_id FROM public._snaplicator_ddl_watermark
                         WHERE id = 1), 0)
   AND NOT EXISTS (SELECT 1 FROM public._snaplicator_ddl_applied a WHERE a.id = l.id)
   AND NOT EXISTS (SELECT 1 FROM pg_subscription_rel WHERE srsubstate <> 'r');
""",
    ).strip()
    try:
        count = int(pending.splitlines()[0])
    except (IndexError, ValueError):
        count = 0
    if count == 0:
        return {"applied": 0}

    _run_subscriber_sql(
        subscriber_container, subscriber_user, subscriber_password, subscriber_db,
        f"""
DO $catchup$
DECLARE
    r record;
    wm bigint;
    saved_sp text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_subscription_rel WHERE srsubstate <> 'r') THEN
        RETURN;
    END IF;
    SELECT coalesce(last_applied_id, 0) INTO wm
      FROM public._snaplicator_ddl_watermark WHERE id = 1;

    FOR r IN SELECT * FROM public.{CAPTURE_LOG_TABLE} l
              WHERE l.id > coalesce(wm, 0)
                AND NOT EXISTS (SELECT 1 FROM public._snaplicator_ddl_applied a
                                 WHERE a.id = l.id)
              ORDER BY l.id
    LOOP
        -- Claimed before it is run, exactly as the trigger does: a statement
        -- that half-succeeds must not come round again.
        INSERT INTO public._snaplicator_ddl_applied (id) VALUES (r.id)
            ON CONFLICT DO NOTHING;

        -- Same rule as the trigger, through the same function. Both paths
        -- apply the same log to the same database, so a statement the
        -- trigger skips and catch-up executes would put ownership DDL back
        -- into the failures table by the other door.
        IF public._snaplicator_ddl_is_local_only(r.command_tag, r.ddl_text) THEN
            INSERT INTO public._snaplicator_ddl_skipped (log_id, ddl_text, reason, search_path)
            VALUES (r.id, r.ddl_text, 'ownership/privileges are not replicated', r.search_path);
            CONTINUE;
        END IF;

        IF r.ddl_text ~* 'CONCURRENTLY' THEN
            INSERT INTO public._snaplicator_ddl_deferred (log_id, ddl_text, search_path)
            VALUES (r.id, r.ddl_text, r.search_path);
            CONTINUE;
        END IF;

        saved_sp := current_setting('search_path', true);
        BEGIN
            IF r.search_path IS NOT NULL AND r.search_path <> '' THEN
                PERFORM set_config('search_path', r.search_path, true);
            END IF;
            EXECUTE r.ddl_text;
        EXCEPTION WHEN OTHERS THEN
            INSERT INTO public._snaplicator_ddl_failures
                (log_id, ddl_text, error, search_path)
            VALUES (r.id, r.ddl_text, SQLERRM, r.search_path);
            RAISE WARNING 'snaplicator: ddl catch-up failed for log id %: %',
                r.id, SQLERRM;
        END;
        PERFORM set_config('search_path', coalesce(saved_sp, 'public'), true);
    END LOOP;
END;
$catchup$;
""",
    )
    return {"applied": count}


def enable_ddl_apply(
    publisher_connstr: str,
    publication_name: str,
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
    subscription_name: str,
    watermark: Optional[int] = None,
) -> Dict:
    """Connect the DDL stream, idempotently.

    `watermark` is the log id the replica's schema was taken at. The
    bootstrap knows it, because it is the one that took the schema; passing
    it is what makes the DDL between that moment and this one replay instead
    of counting as history. Without it the line falls here, which skips
    exactly the DDL the replica is missing.

    Falling back to the publisher's current max(id) is right only when there
    is no replica schema to be out of step with — a first install, before any
    bootstrap has run. The subscription is only rewritten when it is not yet reading
    the log publication, so steady-state calls are read-only no-ops.

    `publication_name` is the data publication, and the log table joins it —
    the subscription is already reading it, so nothing has to be told about a
    second name.

    Order is load-bearing: the watermark is written from the publisher's
    current max(id) BEFORE the table joins, so the history the refresh copies
    over lands below the line and is skipped instead of replayed.
    """
    wm = watermark if watermark is not None else get_publisher_max_ddl_log_id(publisher_connstr)
    install_ddl_apply(
        subscriber_container, subscriber_user, subscriber_password,
        subscriber_db, initial_watermark=wm,
    )
    added = ensure_ddl_publication(publisher_connstr, publication_name)
    if not added.get("added"):
        return {"watermark": wm, "refreshed": False, **added}

    # REFRESH copies only what just joined; the data tables are already in
    # pg_subscription_rel and are left where they are. copy_data stays on:
    # turning it off here would also silence the initial copy of any data
    # table that happened to join in the same edit.
    _run_subscriber_sql(
        subscriber_container, subscriber_user, subscriber_password,
        subscriber_db,
        f"ALTER SUBSCRIPTION {_quote_ident(subscription_name)} REFRESH PUBLICATION;",
    )
    return {"watermark": wm, "refreshed": True, **added}


def compare_published_schemas(
    publisher_connstr: str,
    publication_name: str,
    subscriber_container: str,
    subscriber_user: str,
    subscriber_password: str | None,
    subscriber_db: str,
) -> Dict:
    """Where the replica's shape no longer matches the primary's.

    DDL replication keeps the two in step from the moment it is connected;
    nothing keeps them in step for the stretch before that. A replica whose
    DDL apply was off, or that was bootstrapped while a migration ran, comes
    out the far side missing a column with no record of it anywhere — and
    the log rows that would have said so are below the watermark, which is
    what makes it silent rather than merely broken.

    A column the primary has and the replica does not is the one that stops
    replication: the publisher sends it and the apply worker has nowhere to
    put it. The others are reported because a drifted replica is worth
    knowing about even when it still runs.

    Read-only. Nothing here repairs anything: the DDL that would fix a
    difference is unknown by the time it is detectable, and guessing it is
    how a diagnostic turns into an outage.
    """
    published = _run_publisher_sql(
        publisher_connstr,
        "SELECT c.table_schema || '.' || c.table_name || '.' || c.column_name "
        "FROM information_schema.columns c "
        "JOIN pg_publication_tables t ON t.schemaname = c.table_schema "
        "                            AND t.tablename = c.table_name "
        f"WHERE t.pubname = '{publication_name}' "
        f"AND c.table_name <> '{CAPTURE_LOG_TABLE}';",
    )
    primary = {l.strip() for l in published.splitlines() if l.strip()}

    local = _run_subscriber_sql(
        subscriber_container, subscriber_user, subscriber_password, subscriber_db,
        "SELECT table_schema || '.' || table_name || '.' || column_name "
        "FROM information_schema.columns "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema');",
    )
    replica = {l.strip() for l in local.splitlines() if l.strip()}

    def table_of(col: str) -> str:
        return col.rsplit(".", 1)[0]

    primary_tables = {table_of(c) for c in primary}
    replica_tables = {table_of(c) for c in replica}

    missing_tables = sorted(primary_tables - replica_tables)
    # Columns are only comparable where both sides have the table; listing
    # every column of a missing table as a missing column would bury the
    # difference that matters under one line per column.
    comparable = primary_tables & replica_tables
    missing_columns = sorted(
        c for c in primary - replica if table_of(c) in comparable
    )
    extra_columns = sorted(
        c for c in replica - primary if table_of(c) in comparable
    )

    return {
        "publication": publication_name,
        "tables_compared": len(comparable),
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "extra_columns": extra_columns,
        # Only the first two stop replication. Extra columns on the replica
        # are ignored by the apply worker.
        "breaks_replication": bool(missing_tables or missing_columns),
    }


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
