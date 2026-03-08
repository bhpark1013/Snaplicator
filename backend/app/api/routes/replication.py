from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from ...core.config import settings
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

        repo_root = Path(__file__).resolve().parents[4]
        sql_path = repo_root / "configs/replication_check.sql"
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
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run replication check: {e}")


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
