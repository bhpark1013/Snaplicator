from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from ...core.config import settings
from ...services.sql_guard import assert_read_only_sql, ReadOnlyViolation
from ...services.replication import (
    get_replication_lag_seconds,
    get_initial_copy_progress,
    run_replication_check_sql,
    list_replication_tables,
    add_tables_to_publication,
    remove_tables_from_publication,
    refresh_subscription,
    sync_table_schemas_to_subscriber,
    install_auto_add_trigger,
    verify_trigger_installed,
)
from pathlib import Path
import os

router = APIRouter()


def _build_publisher_connstr() -> str:
    """Build publisher connstr from settings, reusing pattern from get_replication_check."""
    connstr = settings.publisher_connstr
    if connstr:
        return connstr
    if not (settings.primary_host and settings.primary_port and settings.primary_db and settings.primary_user):
        raise HTTPException(status_code=400, detail="Missing PUBLISHER_CONNSTR and PRIMARY_* fields are incomplete")
    sslmode = settings.pgsslmode or "prefer"
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
    return " ".join(conn_parts)


def _require_subscriber_settings():
    if not settings.container_name or not settings.postgres_user or not settings.postgres_db:
        raise HTTPException(status_code=400, detail="Missing required settings (CONTAINER_NAME, POSTGRES_USER, POSTGRES_DB)")


@router.get("/lag")
def get_lag():
    try:
        _require_subscriber_settings()
        return get_replication_lag_seconds(settings.container_name, settings.postgres_user, settings.postgres_db)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to compute replication lag: {e}")

@router.get("/copy-progress")
def get_copy_progress():
    try:
        _require_subscriber_settings()
        return get_initial_copy_progress(settings.container_name, settings.postgres_user, settings.postgres_db)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get copy progress: {e}")


@router.get("/check")
def get_replication_check():
    """Run replication check SQL on both publisher and subscriber."""
    try:
        connstr = _build_publisher_connstr()
        _require_subscriber_settings()

        sql_path = _effective_sql_path()
        res = run_replication_check_sql(
            str(sql_path),
            connstr,
            settings.container_name,
            settings.postgres_user,
            settings.postgres_password,
            settings.postgres_db,
        )
        try:
            sql_text = sql_path.read_text(encoding="utf-8")
        except Exception:
            sql_text = None
        return {"sql": sql_text, **res}
    except ReadOnlyViolation as e:
        raise HTTPException(status_code=400, detail=f"Rejected (not read-only): {e}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run replication check: {e}")



class CheckSqlBody(BaseModel):
    sql: str = Field(..., description="Replication-check SQL (read-only only)")


def _seed_sql_path() -> Path:
    """Repo-tracked default, used only to seed first run."""
    return Path(__file__).resolve().parents[4] / "configs/replication_check.sql"


def _check_sql_path() -> Path:
    """Persistent store, OUTSIDE the repo and the reset scope so a custom
    check query survives full re-initialization. Override with CHECK_SQL_PATH.
    """
    env = os.environ.get("CHECK_SQL_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".snaplicator" / "replication_check.sql"


def _effective_sql_path() -> Path:
    p = _check_sql_path()
    return p if p.exists() else _seed_sql_path()


@router.get("/check-sql")
def get_check_sql():
    """Return the current replication-check SQL text (persistent if saved,
    otherwise the repo default seed)."""
    persist = _check_sql_path()
    eff = _effective_sql_path()
    try:
        text = eff.read_text(encoding="utf-8") if eff.exists() else ""
        return {"sql": text, "persisted": persist.exists(), "path": str(persist)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read check SQL: {e}")


@router.put("/check-sql")
def put_check_sql(body: CheckSqlBody):
    """Validate (read-only) and save the replication-check SQL.

    Rejects anything that is not provably read-only. This is the mandatory
    write-prevention gate on save; execution is additionally wrapped in a
    READ ONLY transaction.
    """
    try:
        assert_read_only_sql(body.sql)
    except ReadOnlyViolation as e:
        raise HTTPException(status_code=400, detail=f"Rejected (not read-only): {e}")
    p = _check_sql_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body.sql, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save check SQL: {e}")
    return {"ok": True, "sql": body.sql}


# ── Replication Table Management Endpoints ──────────────────────


@router.get("/tables")
def get_tables():
    """List all public tables with publication/subscriber status."""
    try:
        connstr = _build_publisher_connstr()
        _require_subscriber_settings()
        pub_name = settings.publication_name
        if not pub_name:
            raise HTTPException(status_code=400, detail="Missing PUBLICATION_NAME setting")
        return list_replication_tables(
            connstr,
            pub_name,
            settings.container_name,
            settings.postgres_user,
            settings.postgres_password,
            settings.postgres_db,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list replication tables: {e}")



@router.get("/info")
def get_info():
    """Return publisher/subscriber connection info for display."""
    return {
        "publisher": {
            "host": settings.primary_host,
            "port": settings.primary_port,
            "user": settings.primary_user,
            "db": settings.primary_db,
            "password": settings.primary_password,
        },
        "subscriber": {
            "container": settings.container_name,
            "host": "localhost",
            "port": settings.host_port,
            "user": settings.postgres_user,
            "db": settings.postgres_db,
            "password": settings.postgres_password,
        },
        "publication_name": settings.publication_name,
        "subscription_name": settings.subscription_name,
    }


class TablesRequest(BaseModel):
    tables: List[str]
    refresh: bool = False


@router.post("/tables")
def post_tables(body: TablesRequest):
    """Add tables to the publication."""
    try:
        connstr = _build_publisher_connstr()
        pub_name = settings.publication_name
        if not pub_name:
            raise HTTPException(status_code=400, detail="Missing PUBLICATION_NAME setting")
        if not body.tables:
            raise HTTPException(status_code=400, detail="No tables specified")

        result = add_tables_to_publication(connstr, pub_name, body.tables)

        # Auto-sync schemas to subscriber for newly added tables
        added = result.get("added", [])
        if added:
            _require_subscriber_settings()
            sync_result = sync_table_schemas_to_subscriber(
                connstr,
                added,
                settings.container_name,
                settings.postgres_user,
                settings.postgres_password,
                settings.postgres_db,
            )
            result["schema_sync"] = sync_result

        if body.refresh:
            _require_subscriber_settings()
            sub_name = settings.subscription_name
            if not sub_name:
                raise HTTPException(status_code=400, detail="Missing SUBSCRIPTION_NAME setting")
            refresh_result = refresh_subscription(
                settings.container_name,
                settings.postgres_user,
                settings.postgres_password,
                settings.postgres_db,
                sub_name,
            )
            result["refresh"] = refresh_result

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add tables: {e}")


@router.delete("/tables")
def delete_tables(body: TablesRequest):
    """Remove tables from the publication."""
    try:
        connstr = _build_publisher_connstr()
        pub_name = settings.publication_name
        if not pub_name:
            raise HTTPException(status_code=400, detail="Missing PUBLICATION_NAME setting")
        if not body.tables:
            raise HTTPException(status_code=400, detail="No tables specified")

        result = remove_tables_from_publication(connstr, pub_name, body.tables)

        if body.refresh:
            _require_subscriber_settings()
            sub_name = settings.subscription_name
            if not sub_name:
                raise HTTPException(status_code=400, detail="Missing SUBSCRIPTION_NAME setting")
            refresh_result = refresh_subscription(
                settings.container_name,
                settings.postgres_user,
                settings.postgres_password,
                settings.postgres_db,
                sub_name,
            )
            result["refresh"] = refresh_result

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to remove tables: {e}")


@router.post("/refresh")
def post_refresh():
    """Refresh the subscription to pick up publication changes."""
    try:
        _require_subscriber_settings()
        sub_name = settings.subscription_name
        if not sub_name:
            raise HTTPException(status_code=400, detail="Missing SUBSCRIPTION_NAME setting")
        return refresh_subscription(
            settings.container_name,
            settings.postgres_user,
            settings.postgres_password,
            settings.postgres_db,
            sub_name,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to refresh subscription: {e}")


@router.get("/trigger-status")
def get_trigger_status():
    """Check if the auto-add event trigger is installed on the publisher."""
    try:
        connstr = _build_publisher_connstr()
        installed = verify_trigger_installed(connstr)
        return {"installed": installed, "publication": settings.publication_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check trigger status: {e}")


@router.post("/trigger-install")
def post_trigger_install():
    """Install or update the auto-add event trigger on the publisher."""
    try:
        connstr = _build_publisher_connstr()
        pub_name = settings.publication_name
        if not pub_name:
            raise HTTPException(status_code=400, detail="Missing PUBLICATION_NAME setting")
        result = install_auto_add_trigger(connstr, pub_name)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to install trigger: {e}")


import re
import subprocess
from fastapi import Query


SUBSCRIPTION_LOG_FILTERS = [
    "logical replication",
    "subscription",
    "ERROR",
    "FATAL",
]

SUBSCRIPTION_LOG_EXCLUDES = [
    'background worker "logical replication worker"',
]


@router.get("/subscription-status")
def get_subscription_status():
    """Check real-time subscription status via pg_stat_subscription."""
    try:
        if not settings.container_name or not settings.postgres_user or not settings.postgres_db:
            raise HTTPException(status_code=400, detail="Missing required settings")

        cmd = [
            "docker", "exec", settings.container_name,
            "psql", "-U", settings.postgres_user, "-d", settings.postgres_db,
            "-t", "-A", "-F", "|",
            "-c", "SELECT subname, pid, received_lsn, latest_end_lsn, latest_end_time FROM pg_stat_subscription;",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        raw = (proc.stdout or "").strip()

        if not raw:
            return {"status": "unknown", "subscriptions": []}

        subs = []
        for line in raw.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 5:
                pid = parts[1] if parts[1] else None
                subs.append({
                    "name": parts[0],
                    "pid": int(pid) if pid else None,
                    "worker_running": pid is not None and pid != "",
                    "received_lsn": parts[2] or None,
                    "latest_end_lsn": parts[3] or None,
                    "latest_end_time": parts[4] or None,
                })

        all_ok = all(s["worker_running"] for s in subs) if subs else False
        return {
            "status": "ok" if all_ok else "error",
            "subscriptions": subs,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="psql command timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check subscription status: {e}")


@router.get("/logs")
def get_subscription_logs(tail: int = Query(default=500, le=5000)):
    """Return replication-related log lines from the main replica container.

    Filters: includes lines matching any of SUBSCRIPTION_LOG_FILTERS,
    excludes lines matching any of SUBSCRIPTION_LOG_EXCLUDES.
    """
    try:
        if not settings.container_name:
            raise HTTPException(status_code=400, detail="Missing CONTAINER_NAME setting")

        proc = subprocess.run(
            ["docker", "logs", "--tail", str(tail), settings.container_name],
            capture_output=True, text=True, timeout=10,
        )
        raw = (proc.stdout or "") + (proc.stderr or "")

        include_pattern = re.compile(
            "|".join(f"({re.escape(f)})" for f in SUBSCRIPTION_LOG_FILTERS),
            re.IGNORECASE,
        )
        exclude_pattern = re.compile(
            "|".join(f"({re.escape(f)})" for f in SUBSCRIPTION_LOG_EXCLUDES),
            re.IGNORECASE,
        )

        lines = [
            line for line in raw.splitlines()
            if include_pattern.search(line) and not exclude_pattern.search(line)
        ]

        error_pattern = re.compile(r"\b(ERROR|FATAL)\b")
        error_count = sum(1 for line in lines if error_pattern.search(line))

        # Deduplicate consecutive identical messages (strip timestamp for comparison)
        deduped: list[str] = []
        seen_msgs: set[str] = set()
        for line in lines:
            msg_part = re.sub(
                r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+UTC\s+\[\d+\]\s*",
                "", line,
            )
            if msg_part not in seen_msgs:
                seen_msgs.add(msg_part)
                deduped.append(line)

        return {
            "container_name": settings.container_name,
            "lines": deduped,
            "total_matched": len(lines),
            "error_count": error_count,
            "has_errors": error_count > 0,
            "filters": {
                "include": SUBSCRIPTION_LOG_FILTERS,
                "exclude": SUBSCRIPTION_LOG_EXCLUDES,
                "tail": tail,
            },
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="docker logs command timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get subscription logs: {e}")


# ── FDW (postgres_fdw) Management Endpoints ────────────────────────────


from ...services import fdw as fdw_svc


class FdwTableRef(BaseModel):
    schema_: str = Field(alias="schema")
    name: str
    model_config = {"populate_by_name": True}


class FdwTablesRequest(BaseModel):
    tables: List[FdwTableRef]


class FdwSchemasRequest(BaseModel):
    schemas: List[str]


def _require_fdw_credentials():
    if not settings.fdw_user or not settings.fdw_password:
        raise HTTPException(
            status_code=400,
            detail="FDW_USER / FDW_PASSWORD not configured in .env",
        )


def _require_primary():
    if not (settings.primary_host and settings.primary_port and settings.primary_db):
        raise HTTPException(
            status_code=400,
            detail="PRIMARY_HOST / PRIMARY_PORT / PRIMARY_DB not configured in .env",
        )


def _build_fdw_apply_args() -> dict:
    _require_subscriber_settings()
    _require_primary()
    _require_fdw_credentials()
    return {
        "container": settings.container_name,
        "pg_user": settings.postgres_user,
        "pg_db": settings.postgres_db,
        "pg_password": settings.postgres_password,
        "primary_host": settings.effective_fdw_host(),
        "primary_port": settings.effective_fdw_port(),
        "primary_db": settings.effective_fdw_db(),
        "fdw_user": settings.fdw_user,
        "fdw_password": settings.fdw_password,
    }


@router.get("/fdw")
def get_fdw_state():
    """Return yaml config + live foreign-table state on the replica."""
    try:
        cfg = fdw_svc.load_yaml(settings.fdw_yaml_abs())
        live = []
        try:
            _require_subscriber_settings()
            live = fdw_svc.list_foreign_tables_on_replica(
                settings.container_name,
                settings.postgres_user,
                settings.postgres_db,
                settings.postgres_password,
            )
        except HTTPException:
            pass
        return {
            "server": cfg.server.model_dump(),
            "schemas": [s.model_dump() for s in cfg.schemas],
            "tables": [{"schema": t.schema_name, "name": t.name} for t in cfg.tables],
            "live_foreign_tables": live,
            "yaml_path": str(settings.fdw_yaml_abs()),
            "sql_path": str(settings.fdw_sql_abs()),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read fdw state: {e}")


@router.post("/fdw/tables")
def post_fdw_tables(body: FdwTablesRequest):
    """Add tables to FDW. Validates against publication overlap; rejects if any
    requested table is currently a published replicated table."""
    try:
        if not body.tables:
            raise HTTPException(status_code=400, detail="No tables specified")

        cfg = fdw_svc.load_yaml(settings.fdw_yaml_abs())
        new = [(t.schema_, t.name) for t in body.tables]

        result = fdw_svc.add_tables(
            cfg,
            settings.fdw_yaml_abs(),
            settings.fdw_sql_abs(),
            new,
            apply_args=_build_fdw_apply_args(),
            publisher_connstr=_build_publisher_connstr(),
            publication_name=settings.publication_name,
        )
        if result.get("errors"):
            raise HTTPException(status_code=400, detail={"errors": result["errors"]})
        if result.get("applied") is False:
            raise HTTPException(
                status_code=500,
                detail={"message": "Apply failed", "result": result.get("result", {})},
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add fdw tables: {e}")


@router.delete("/fdw/tables")
def delete_fdw_tables(body: FdwTablesRequest):
    """Remove tables from FDW. Foreign tables and yaml entries are both removed."""
    try:
        if not body.tables:
            raise HTTPException(status_code=400, detail="No tables specified")
        cfg = fdw_svc.load_yaml(settings.fdw_yaml_abs())
        targets = [(t.schema_, t.name) for t in body.tables]
        result = fdw_svc.remove_tables(
            cfg,
            settings.fdw_yaml_abs(),
            settings.fdw_sql_abs(),
            targets,
            apply_args=_build_fdw_apply_args(),
        )
        if result.get("applied") is False:
            raise HTTPException(
                status_code=500,
                detail={"message": "Apply failed", "result": result.get("result", {})},
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to remove fdw tables: {e}")


@router.post("/fdw/schemas")
def post_fdw_schemas(body: FdwSchemasRequest):
    """Add schemas to FDW (IMPORT FOREIGN SCHEMA on the whole schema)."""
    try:
        if not body.schemas:
            raise HTTPException(status_code=400, detail="No schemas specified")
        cfg = fdw_svc.load_yaml(settings.fdw_yaml_abs())
        result = fdw_svc.add_schemas(
            cfg,
            settings.fdw_yaml_abs(),
            settings.fdw_sql_abs(),
            body.schemas,
            apply_args=_build_fdw_apply_args(),
            publisher_connstr=_build_publisher_connstr(),
            publication_name=settings.publication_name,
        )
        if result.get("errors"):
            raise HTTPException(status_code=400, detail={"errors": result["errors"]})
        if result.get("applied") is False:
            raise HTTPException(
                status_code=500,
                detail={"message": "Apply failed", "result": result.get("result", {})},
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add fdw schemas: {e}")


@router.delete("/fdw/schemas")
def delete_fdw_schemas(body: FdwSchemasRequest):
    """Remove schemas from FDW."""
    try:
        if not body.schemas:
            raise HTTPException(status_code=400, detail="No schemas specified")
        cfg = fdw_svc.load_yaml(settings.fdw_yaml_abs())
        result = fdw_svc.remove_schemas(
            cfg,
            settings.fdw_yaml_abs(),
            settings.fdw_sql_abs(),
            body.schemas,
            apply_args=_build_fdw_apply_args(),
        )
        if result.get("applied") is False:
            raise HTTPException(
                status_code=500,
                detail={"message": "Apply failed", "result": result.get("result", {})},
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to remove fdw schemas: {e}")


@router.post("/fdw/regenerate")
def post_fdw_regenerate():
    """Re-render fdw_setup.generated.sql from current yaml and re-apply to replica.
    Useful after manual yaml edits or to recover from drift."""
    try:
        cfg = fdw_svc.load_yaml(settings.fdw_yaml_abs())
        val_errs = fdw_svc.validate_config(cfg)
        if val_errs:
            raise HTTPException(status_code=400, detail={"errors": val_errs})
        pub_errs = fdw_svc.validate_against_publication(
            cfg, _build_publisher_connstr(), settings.publication_name or "",
        ) if settings.publication_name else []
        if pub_errs:
            raise HTTPException(status_code=400, detail={"errors": pub_errs})

        result = fdw_svc._regenerate_and_apply(
            cfg,
            settings.fdw_yaml_abs(),
            settings.fdw_sql_abs(),
            _build_fdw_apply_args(),
        )
        if not result.get("applied"):
            raise HTTPException(
                status_code=500,
                detail={"message": "Apply failed", "result": result.get("result", {})},
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to regenerate fdw: {e}")
