"""Fixtures for Snaplicator backend integration tests.

DDL-capture tests (Phase 1) run against a disposable postgres:15 container
(prod publisher/subscriber are PG 15.x) started with wal_level=logical, so
tests never touch the live dev replication stack.

Host requirements: docker, psql.

Run from backend/:
    .venv/bin/python -m pytest
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest

TEST_PG_IMAGE = os.environ.get("TEST_PG_IMAGE", "postgres:15-alpine")
TEST_PG_PORT = int(os.environ.get("TEST_PG_PORT", "55432"))
TEST_PG_CONTAINER = os.environ.get("TEST_PG_CONTAINER", "snaplicator_capture_test_pg")

PG_USER = "testuser"
PG_PASSWORD = "testpass"  # throwaway, container-local
PG_DB = "testdb"
PUBLICATION = "snaplicator_publication"  # table-list publication (prod shape)

ALLTABLES_DB = "testdb_alltables"
ALLTABLES_PUBLICATION = "all_tables_publication"  # FOR ALL TABLES (dev shape)

LOG_TABLE = "_snaplicator_ddl_log"


def connstr_for(db: str) -> str:
	return (
		f"host=127.0.0.1 port={TEST_PG_PORT} dbname={db} "
		f"user={PG_USER} password={PG_PASSWORD}"
	)


def psql_multi(commands: list[str], db: str = PG_DB, sep: str = "|") -> str:
	"""Run several -c commands over ONE psql session.

	Session state (BEGIN, SET search_path, ...) persists across the commands;
	each -c is its own PQexec.
	"""
	cmd = ["psql", connstr_for(db), "-X", "-v", "ON_ERROR_STOP=1", "-At", "-F", sep]
	for c in commands:
		cmd += ["-c", c]
	proc = subprocess.run(cmd, text=True, capture_output=True)
	if proc.returncode != 0:
		raise RuntimeError((proc.stderr or proc.stdout).strip())
	return (proc.stdout or "").strip()


def psql(sql: str, db: str = PG_DB) -> str:
	"""Run one query string via psql.

	A multi-statement string goes out as a single PQexec, i.e. one implicit
	transaction and one current_query() — exactly how app migrations look.
	"""
	return psql_multi([sql], db=db)


def log_count(where: str = "TRUE", db: str = PG_DB) -> int:
	return int(psql(f"SELECT count(*) FROM {LOG_TABLE} WHERE {where};", db=db))


def log_rows(where: str = "TRUE", db: str = PG_DB) -> list[dict]:
	"""Fetch log rows. ddl_text newlines are flattened; assert with LIKE for
	anything fancy — this helper is for simple single-line DDL."""
	sep = "\x01"  # ddl_text may contain '|'
	out = psql_multi(
		[
			f"SELECT id, command_tag, coalesce(object_identity,''), "
			f"coalesce(schema_name,''), replace(ddl_text, E'\\n', ' '), "
			f"coalesce(search_path,''), lsn::text, txid::text "
			f"FROM {LOG_TABLE} WHERE {where} ORDER BY id;"
		],
		db=db,
		sep=sep,
	)
	rows = []
	for line in out.splitlines():
		if not line:
			continue
		f = line.split(sep)
		rows.append(
			{
				"id": int(f[0]),
				"command_tag": f[1],
				"object_identity": f[2],
				"schema_name": f[3],
				"ddl_text": f[4],
				"search_path": f[5],
				"lsn": f[6],
				"txid": f[7],
			}
		)
	return rows


@pytest.fixture(scope="session")
def pg_container():
	"""Disposable publisher container. Fresh per test session, removed after."""
	subprocess.run(["docker", "rm", "-f", TEST_PG_CONTAINER], capture_output=True)
	subprocess.run(
		[
			"docker", "run", "-d", "--rm",
			"--name", TEST_PG_CONTAINER,
			"-e", f"POSTGRES_USER={PG_USER}",
			"-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
			"-e", f"POSTGRES_DB={PG_DB}",
			"-p", f"{TEST_PG_PORT}:5432",
			"--tmpfs", "/var/lib/postgresql/data",
			TEST_PG_IMAGE,
			"-c", "wal_level=logical",
		],
		check=True,
		capture_output=True,
	)
	deadline = time.time() + 90
	while time.time() < deadline:
		try:
			if psql("SELECT 1;") == "1":
				break
		except RuntimeError:
			time.sleep(0.5)
	else:
		subprocess.run(["docker", "rm", "-f", TEST_PG_CONTAINER], capture_output=True)
		raise RuntimeError("test postgres did not become ready in 90s")
	yield TEST_PG_CONTAINER
	subprocess.run(["docker", "rm", "-f", TEST_PG_CONTAINER], capture_output=True)


@pytest.fixture(scope="session")
def capture_installed(pg_container):
	"""Empty table-list publication (prod shape) + capture triggers installed."""
	from app.services.replication import install_capture_triggers

	psql(f"CREATE PUBLICATION {PUBLICATION};")
	install_capture_triggers(connstr_for(PG_DB), PUBLICATION)
	return connstr_for(PG_DB)


@pytest.fixture(scope="session")
def capture_installed_alltables(pg_container):
	"""Second DB with a FOR ALL TABLES publication (dev shape)."""
	from app.services.replication import install_capture_triggers

	psql(f"CREATE DATABASE {ALLTABLES_DB};")
	psql(f"CREATE PUBLICATION {ALLTABLES_PUBLICATION} FOR ALL TABLES;", db=ALLTABLES_DB)
	install_capture_triggers(connstr_for(ALLTABLES_DB), ALLTABLES_PUBLICATION)
	return connstr_for(ALLTABLES_DB)


@pytest.fixture()
def clean_log(capture_installed):
	"""Empty the DDL log so each test only sees its own rows."""
	psql(f"TRUNCATE {LOG_TABLE};")
	yield


# ── e2e pair: publisher + subscriber over real logical replication ──

E2E_NET = "snap_e2e_net"
E2E_PUB = "snap_e2e_pub"
E2E_SUB = "snap_e2e_sub"
E2E_PUB_PORT = int(os.environ.get("TEST_PG_PUB_PORT", "55434"))
E2E_SUB_PORT = int(os.environ.get("TEST_PG_SUB_PORT", "55435"))
E2E_SUBSCRIPTION = "e2e_subscription"


def psql_conn(connstr: str, sql: str, sep: str = "|") -> str:
	"""Run one query string against an arbitrary connstr."""
	proc = subprocess.run(
		["psql", connstr, "-X", "-v", "ON_ERROR_STOP=1", "-At", "-F", sep, "-c", sql],
		text=True, capture_output=True,
	)
	if proc.returncode != 0:
		raise RuntimeError((proc.stderr or proc.stdout).strip())
	return (proc.stdout or "").strip()


def e2e_connstr(port: int) -> str:
	return (
		f"host=127.0.0.1 port={port} dbname={PG_DB} "
		f"user={PG_USER} password={PG_PASSWORD}"
	)


def wait_until(fn, timeout: float = 30.0, interval: float = 0.5, desc: str = "condition"):
	"""Poll fn() until truthy; replication apply is asynchronous."""
	deadline = time.time() + timeout
	last_exc = None
	while time.time() < deadline:
		try:
			if fn():
				return
			last_exc = None
		except Exception as e:  # transient: object not there yet, etc.
			last_exc = e
		time.sleep(interval)
	raise AssertionError(f"timed out waiting for {desc} (last error: {last_exc})")


def _run_e2e_container(name: str, port: int) -> None:
	subprocess.run(
		[
			"docker", "run", "-d", "--rm",
			"--name", name,
			"--network", E2E_NET,
			"-e", f"POSTGRES_USER={PG_USER}",
			"-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
			"-e", f"POSTGRES_DB={PG_DB}",
			"-p", f"{port}:5432",
			"--tmpfs", "/var/lib/postgresql/data",
			TEST_PG_IMAGE,
			"-c", "wal_level=logical",
		],
		check=True, capture_output=True,
	)


@pytest.fixture(scope="session")
def pg_pair():
	"""Publisher + subscriber containers wired with a real subscription.

	Mirrors the production shape: table-list publication, capture triggers on
	the publisher (host-psql path), apply infra on the subscriber (docker-exec
	path — same as _run_subscriber_sql in production), log table in the
	publication, watermark initialised to the publisher's max(id) BEFORE the
	subscription starts so pre-existing log rows are never replayed.
	"""
	from app.services.replication import (
		add_log_table_to_publication,
		install_capture_triggers,
		install_ddl_apply,
	)

	for c in (E2E_PUB, E2E_SUB):
		subprocess.run(["docker", "rm", "-f", c], capture_output=True)
	subprocess.run(["docker", "network", "rm", E2E_NET], capture_output=True)
	subprocess.run(["docker", "network", "create", E2E_NET], check=True, capture_output=True)

	_run_e2e_container(E2E_PUB, E2E_PUB_PORT)
	_run_e2e_container(E2E_SUB, E2E_SUB_PORT)

	pub = e2e_connstr(E2E_PUB_PORT)
	sub = e2e_connstr(E2E_SUB_PORT)
	for connstr in (pub, sub):
		wait_until(
			lambda c=connstr: psql_conn(c, "SELECT 1;") == "1",
			timeout=90, desc="postgres ready",
		)

	# publisher: seed table (in publication from the start) + capture triggers
	psql_conn(pub, "CREATE TABLE seed (id int PRIMARY KEY, val text, req int NOT NULL);")
	psql_conn(pub, f"CREATE PUBLICATION {PUBLICATION} FOR TABLE seed;")
	install_capture_triggers(pub, PUBLICATION)

	# subscriber starts with the same seed schema (as a real replica would)
	psql_conn(sub, "CREATE TABLE seed (id int PRIMARY KEY, val text, req int NOT NULL);")

	# DDL captured BEFORE the watermark must never be replayed on the
	# subscriber (a sequence: not publishable, so it cannot break tablesync)
	psql_conn(pub, "CREATE SEQUENCE pre_watermark_seq;")

	max_id = int(psql_conn(pub, f"SELECT coalesce(max(id), 0) FROM {LOG_TABLE};"))
	install_ddl_apply(E2E_SUB, PG_USER, PG_PASSWORD, PG_DB, initial_watermark=max_id)
	add_log_table_to_publication(pub, PUBLICATION)

	psql_conn(
		sub,
		f"CREATE SUBSCRIPTION {E2E_SUBSCRIPTION} "
		f"CONNECTION 'host={E2E_PUB} port=5432 dbname={PG_DB} "
		f"user={PG_USER} password={PG_PASSWORD}' "
		f"PUBLICATION {PUBLICATION};",
	)
	# wait until initial sync of both tables (seed + ddl log) is done
	wait_until(
		lambda: psql_conn(
			sub,
			"SELECT count(*) FROM pg_subscription_rel WHERE srsubstate IN ('r','s');",
		) == "2",
		timeout=60, desc="initial table sync",
	)

	yield {"pub": pub, "sub": sub}

	for c in (E2E_PUB, E2E_SUB):
		subprocess.run(["docker", "rm", "-f", c], capture_output=True)
	subprocess.run(["docker", "network", "rm", E2E_NET], capture_output=True)
