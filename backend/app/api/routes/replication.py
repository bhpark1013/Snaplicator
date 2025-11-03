from fastapi import APIRouter, HTTPException
from ...core.config import settings
from ...services.replication import get_replication_lag_seconds, get_initial_copy_progress, run_replication_check_sql
from pathlib import Path

router = APIRouter()

@router.get("/lag")
def get_lag():
	try:
		if not settings.container_name or not settings.postgres_user or not settings.postgres_db:
			raise HTTPException(status_code=400, detail="Missing required settings (CONTAINER_NAME, POSTGRES_USER, POSTGRES_DB)")
		return get_replication_lag_seconds(settings.container_name, settings.postgres_user, settings.postgres_db)
	except HTTPException:
		raise
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to compute replication lag: {e}")

@router.get("/copy-progress")
def get_copy_progress():
	try:
		if not settings.container_name or not settings.postgres_user or not settings.postgres_db:
			raise HTTPException(status_code=400, detail="Missing required settings (CONTAINER_NAME, POSTGRES_USER, POSTGRES_DB)")
		return get_initial_copy_progress(settings.container_name, settings.postgres_user, settings.postgres_db)
	except HTTPException:
		raise
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to get copy progress: {e}") 


@router.get("/check")
def get_replication_check():
	"""Run replication check SQL on both publisher and subscriber.

	Always returns 200 with structured ok/error fields so FE can display both sides.
	"""
	try:
		# Build publisher connstr from env like existing scripts if not explicitly provided
		connstr = settings.publisher_connstr
		if not connstr:
			# Require base fields
			if not (settings.primary_host and settings.primary_port and settings.primary_db and settings.primary_user):
				raise HTTPException(status_code=400, detail="Missing PUBLISHER_CONNSTR in environment and PRIMARY_* fields are incomplete")
			sslmode = settings.pgsslmode or "prefer"
			# libpq key=value (password may be None)
			conn_parts = [
				f"host={settings.primary_host}",
				f"port={settings.primary_port}",
				f"dbname={settings.primary_db}",
				f"user={settings.primary_user}",
				f"sslmode={sslmode}",
				"target_session_attrs=read-write",
				"options='-c lock_timeout=0 -c statement_timeout=0'",
			]
			if settings.primary_password:
				conn_parts.insert(4, f"password={settings.primary_password}")
			connstr = " ".join(conn_parts)

		if not settings.container_name or not settings.postgres_user or not settings.postgres_db:
			raise HTTPException(status_code=400, detail="Missing required settings (CONTAINER_NAME, POSTGRES_USER, POSTGRES_DB)")

		# Use default SQL path under repo root
		repo_root = Path(__file__).resolve().parents[4]
		sql_path = repo_root / "configs/replication_check.sql"
		res = run_replication_check_sql(
			str(sql_path),
			connstr,  # type: ignore[arg-type]
			settings.container_name,     # type: ignore[arg-type]
			settings.postgres_user,      # type: ignore[arg-type]
			settings.postgres_password,  # may be None
			settings.postgres_db,        # type: ignore[arg-type]
		)
		# include SQL text in response so FE can display what was run
		try:
			sql_text = sql_path.read_text(encoding="utf-8")
		except Exception:
			sql_text = None  # tolerate read error but still return results
		return {"sql": sql_text, **res}
	except HTTPException:
		raise
	except Exception as e:
		raise HTTPException(status_code=500, detail=f"Failed to run replication check: {e}")