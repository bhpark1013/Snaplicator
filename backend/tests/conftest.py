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
