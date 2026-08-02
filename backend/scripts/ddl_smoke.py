#!/usr/bin/env python
"""Interactive smoke environment for DDL replication (phase 1+2).

Spins up a persistent publisher/subscriber pair with the real capture +
apply infrastructure installed, wired by a real logical replication
subscription. Stays up until torn down.

Usage (on the dev box, from backend/):
    .venv/bin/python scripts/ddl_smoke.py setup
    .venv/bin/python scripts/ddl_smoke.py status
    .venv/bin/python scripts/ddl_smoke.py teardown
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.replication import (  # noqa: E402
	CAPTURE_LOG_PUBLICATION,
	ensure_ddl_publication,
	get_ddl_apply_status,
	install_capture_triggers,
	install_ddl_apply,
)

NET = "snap_smoke_net"
PUB_CONTAINER = "snap_smoke_pub"
SUB_CONTAINER = "snap_smoke_sub"
PUB_PORT, SUB_PORT = 55440, 55441
USER, PASSWORD, DB = "testuser", "testpass", "testdb"
PUBLICATION = "snaplicator_publication"
SUBSCRIPTION = "smoke_subscription"
IMAGE = "postgres:15-alpine"


def connstr(port: int) -> str:
	return f"host=127.0.0.1 port={port} dbname={DB} user={USER} password={PASSWORD}"


def psql(port: int, sql: str) -> str:
	p = subprocess.run(
		["psql", connstr(port), "-X", "-v", "ON_ERROR_STOP=1", "-At", "-c", sql],
		capture_output=True, text=True,
	)
	if p.returncode != 0:
		raise RuntimeError(p.stderr.strip())
	return p.stdout.strip()


def teardown() -> None:
	for c in (PUB_CONTAINER, SUB_CONTAINER):
		subprocess.run(["docker", "rm", "-f", c], capture_output=True)
	subprocess.run(["docker", "network", "rm", NET], capture_output=True)
	print("smoke environment removed.")


def setup() -> None:
	teardown()
	subprocess.run(["docker", "network", "create", NET], check=True, capture_output=True)
	for name, port in ((PUB_CONTAINER, PUB_PORT), (SUB_CONTAINER, SUB_PORT)):
		subprocess.run(
			[
				"docker", "run", "-d", "--rm", "--name", name, "--network", NET,
				"-e", f"POSTGRES_USER={USER}",
				"-e", f"POSTGRES_PASSWORD={PASSWORD}",
				"-e", f"POSTGRES_DB={DB}",
				"-p", f"{port}:5432",
				IMAGE, "-c", "wal_level=logical",
			],
			check=True, capture_output=True,
		)
	for port in (PUB_PORT, SUB_PORT):
		for _ in range(180):
			try:
				if psql(port, "SELECT 1;") == "1":
					break
			except RuntimeError:
				time.sleep(0.5)
		else:
			raise RuntimeError(f"postgres on :{port} not ready")

	# publisher: seed table + table-list publication (prod shape) + capture
	psql(PUB_PORT, "CREATE TABLE orders (id int PRIMARY KEY, item text, qty int NOT NULL);")
	psql(PUB_PORT, f"CREATE PUBLICATION {PUBLICATION} FOR TABLE orders;")
	install_capture_triggers(connstr(PUB_PORT), PUBLICATION)

	# subscriber: same starting schema, apply infra with watermark
	psql(SUB_PORT, "CREATE TABLE orders (id int PRIMARY KEY, item text, qty int NOT NULL);")
	max_id = int(psql(PUB_PORT, "SELECT coalesce(max(id), 0) FROM _snaplicator_ddl_log;"))
	install_ddl_apply(SUB_CONTAINER, USER, PASSWORD, DB, initial_watermark=max_id)
	ensure_ddl_publication(connstr(PUB_PORT))

	psql(
		SUB_PORT,
		f"CREATE SUBSCRIPTION {SUBSCRIPTION} "
		f"CONNECTION 'host={PUB_CONTAINER} port=5432 dbname={DB} "
		f"user={USER} password={PASSWORD}' "
		f'PUBLICATION {PUBLICATION}, "{CAPTURE_LOG_PUBLICATION}";',
	)
	for _ in range(120):
		if psql(SUB_PORT, "SELECT count(*) FROM pg_subscription_rel WHERE srsubstate IN ('r','s');") == "2":
			break
		time.sleep(0.5)
	else:
		raise RuntimeError("initial table sync did not finish")

	print("smoke environment ready.")
	print(f"  publisher : psql '{connstr(PUB_PORT)}'")
	print(f"  subscriber: psql '{connstr(SUB_PORT)}'")


def status() -> None:
	print("apply status  :", get_ddl_apply_status(SUB_CONTAINER, USER, PASSWORD, DB))
	print("pub log rows  :", psql(PUB_PORT, "SELECT count(*) FROM _snaplicator_ddl_log;"))
	print("sub log rows  :", psql(SUB_PORT, "SELECT count(*) FROM _snaplicator_ddl_log;"))
	print("subscription  :", psql(SUB_PORT, "SELECT subname, subenabled FROM pg_subscription;"))
	print(
		"worker        :",
		psql(SUB_PORT, "SELECT coalesce(string_agg(pid::text, ','), 'NOT RUNNING') FROM pg_stat_subscription WHERE pid IS NOT NULL;"),
	)


if __name__ == "__main__":
	cmd = sys.argv[1] if len(sys.argv) > 1 else "setup"
	{"setup": setup, "status": status, "teardown": teardown}[cmd]()
