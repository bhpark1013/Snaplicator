from __future__ import annotations

import subprocess
from typing import Dict


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
	# Expect: "apply\tnetwork" as text values separated by | or spaces depending on -tAc
	# With -tAc and comma separation, default separator is | only when using \x or \pset; safer to split on whitespace
	parts = [p for p in line.replace("|", " ").split() if p]
	if len(parts) >= 2:
		apply_lag = float(parts[0])
		network_lag = float(parts[1])
	else:
		# Fallback: attempt CSV parsing by comma
		parts = [p for p in line.split(",") if p]
		apply_lag = float(parts[0]) if parts else 0.0
		network_lag = float(parts[1]) if len(parts) > 1 else 0.0
	return {
		"network_lag_seconds": network_lag,
		"apply_lag_seconds": apply_lag,
	} 