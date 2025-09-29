from __future__ import annotations

import subprocess
from typing import Dict, List


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