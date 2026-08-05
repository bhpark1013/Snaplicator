from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import List, Optional
from ...core.config import settings
from ...services.sql_guard import assert_read_only_sql, ReadOnlyViolation
from ...services import sync_log
from ...services import bootstrap as bootstrap_svc
from ...services import capacity as capacity_svc
from ...services import selection as selection_svc
from ...services import fdw_creds
from ...services import publication as publication_svc
from ...services.replication import (
    _run_publisher_sql,
    _run_subscriber_sql,
    get_replication_lag_seconds,
    get_initial_copy_progress,
    run_replication_check_sql,
    list_replication_tables,
    add_tables_to_publication,
    remove_tables_from_publication,
    refresh_subscription,
    sync_table_schemas_to_subscriber,
    install_capture_triggers,
    verify_capture_installed,
    compare_published_schemas,
)
from pathlib import Path
import os
import re

router = APIRouter()


def _active_publication() -> Optional[str]:
    """The publication in force — what was chosen here, else what the install proposed.

    PUBLICATION_NAME is written by a script that never looked at the primary,
    so on a server that already has publications it is a guess. The recorded
    choice is the answer; this falls back to the guess only until one is made.
    """
    return publication_svc.active(settings.publication_name)


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
    """Run replication check SQL on both publisher and subscriber.

    Returns ``configured: False`` and runs nothing when no check query has
    been written yet. A query that was never supplied has not failed, and
    reporting it as a failure puts a red badge on a healthy replica.
    """
    try:
        sql_path = _effective_sql_path()
        try:
            sql_text = sql_path.read_text(encoding="utf-8")
        except Exception:
            sql_text = None

        if not _is_configured_check(sql_path, sql_text):
            return {
                "sql": sql_text,
                "configured": False,
                "publisher": {"ok": False, "output": None, "error": None},
                "subscriber": {"ok": False, "output": None, "error": None},
            }

        connstr = _build_publisher_connstr()
        _require_subscriber_settings()

        res = run_replication_check_sql(
            str(sql_path),
            connstr,
            settings.container_name,
            settings.postgres_user,
            settings.postgres_password,
            settings.postgres_db,
        )
        return {"sql": sql_text, "configured": True, **res}
    except ReadOnlyViolation as e:
        raise HTTPException(status_code=400, detail=f"Rejected (not read-only): {e}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run replication check: {e}")



class CheckSqlBody(BaseModel):
    sql: str = Field(..., description="Replication-check SQL (read-only only)")


def _seed_sql_path() -> Path:
    """Seed used only when no persistent custom query exists. Prefer a
    local (gitignored, environment-specific) replication_check.sql if
    present; otherwise fall back to the tracked example template."""
    cfg = Path(__file__).resolve().parents[4] / "configs"
    local = cfg / "replication_check.sql"
    return local if local.exists() else cfg / "replication_check.example.sql"


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


def _example_sql_path() -> Path:
    """The tracked template. It is an example of a check, not a check."""
    return Path(__file__).resolve().parents[4] / "configs" / "replication_check.example.sql"


_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _has_executable_sql(text: str) -> bool:
    """True when anything survives stripping comments, semicolons and space."""
    body = _SQL_BLOCK_COMMENT.sub(" ", text or "")
    lines = []
    for line in body.splitlines():
        i = line.find("--")
        lines.append(line if i < 0 else line[:i])
    return bool(" ".join(lines).replace(";", " ").strip())


def _is_configured_check(sql_path: Path, sql_text: Optional[str]) -> bool:
    """Whether there is a check query at all.

    Two ways there is not, and they look identical from outside: nothing but
    comments, or the shipped template — which names tables that exist only in
    the example, so running it always errors and always looks like broken
    replication rather than an unanswered question.
    """
    if sql_path.resolve() == _example_sql_path().resolve():
        return False
    return _has_executable_sql(sql_text or "")


@router.get("/check-sql")
def get_check_sql():
    """Return the current replication-check SQL text (persistent if saved,
    otherwise the repo default seed)."""
    persist = _check_sql_path()
    eff = _effective_sql_path()
    try:
        text = eff.read_text(encoding="utf-8") if eff.exists() else ""
        return {
            "sql": text,
            "persisted": persist.exists(),
            "configured": _is_configured_check(eff, text),
            "path": str(persist),
        }
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
        pub_name = _active_publication()
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
        "publication_name": _active_publication(),
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
        pub_name = _active_publication()
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
        pub_name = _active_publication()
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


class SelectionBody(BaseModel):
    tables: List[str] = Field(default_factory=list, description="schema.table to replicate")
    auto_schemas: List[str] = Field(
        default_factory=list,
        description="schemas whose future tables should join on their own",
    )


class PublicationChoice(BaseModel):
    name: str
    # create — a new one, empty, ours to narrow
    # reuse  — an existing one, read as it stands, never rewritten
    # adopt  — an existing one, taken over: this install may now rewrite it
    mode: str = Field(pattern="^(create|reuse|adopt)$")


@router.get("/publications")
def list_publications():
    """What the primary already carries, and which one this replica speaks for."""
    try:
        chosen = publication_svc.load()
        return {
            "proposed": settings.publication_name,
            "active": _active_publication(),
            "chosen": chosen["chosen"],
            "ours": chosen["ours"],
            "publications": publication_svc.list_existing(_build_publisher_connstr()),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list publications: {e}")


@router.put("/publication")
def choose_publication(body: PublicationChoice):
    """Say which publication this replica speaks for, and whether it may rewrite it.

    Reuse and adopt differ only in that permission, and the difference is the
    whole reason this endpoint exists: narrowing a publication drops it, and a
    publication that predates this install may be feeding a replica that has
    nothing to do with us.
    """
    connstr = _build_publisher_connstr()
    try:
        if body.mode == "create":
            return publication_svc.create(connstr, body.name)
        existing = {p["name"] for p in publication_svc.list_existing(connstr)}
        if body.name not in existing:
            raise HTTPException(
                status_code=404,
                detail=f"the primary has no publication named {body.name}",
            )
        return publication_svc.save(body.name, ours=(body.mode == "adopt"))
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to choose publication: {e}")


@router.get("/selection")
def get_selection():
    """The publication as a set of tables, plus which schemas follow future ones."""
    try:
        return selection_svc.current_selection(_build_publisher_connstr(), _active_publication() or "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read selection: {e}")


@router.put("/selection")
def put_selection(body: SelectionBody):
    """Make the publication say exactly this.

    Recreating is not an implementation detail that could be avoided: a
    FOR ALL TABLES publication cannot have a table removed from it, so the
    first exclusion always replaces the publication.
    """
    try:
        pub = _active_publication()
        if not pub:
            raise HTTPException(status_code=400, detail="Missing PUBLICATION_NAME setting")
        sub = None
        if settings.container_name and settings.postgres_user and settings.postgres_db:
            sub = {
                "container": settings.container_name,
                "user": settings.postgres_user,
                "password": settings.postgres_password,
                "db": settings.postgres_db,
                "subscription": settings.subscription_name,
            }
        return selection_svc.apply_selection(
            _build_publisher_connstr(), pub, body.tables, body.auto_schemas, sub
        )
    except HTTPException:
        raise
    except PermissionError as e:
        # Not a failure to apply — a refusal to rewrite someone else's
        # publication. 409 so the UI can offer the choice instead of an error.
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to apply selection: {e}")


@router.get("/bootstrap")
def get_bootstrap(tail: int = Query(default=40, le=2000)):
    """Whether the replica has been brought up, is being brought up, or neither.

    Polled by the UI, so it stays cheap and never raises for the ordinary
    "nothing has happened yet" case — that is a state, not an error.
    """
    return bootstrap_svc.status(tail=tail)


@router.post("/bootstrap")
def post_bootstrap(force: bool = False):
    """Clone the schema and create the subscription — the first byte moves here.

    Returns immediately: an initial copy is measured in minutes to hours, and
    the run continues independently of this request. Progress is read back
    from GET /replication/bootstrap.
    """
    try:
        # The subscription needs something to subscribe to, and the primary
        # may have nothing yet: the install no longer decides the shape of the
        # publication. Whatever the user settled on has already been applied
        # by then; this only covers the case where they changed nothing.
        selection_svc.ensure_publication(
            _build_publisher_connstr(), _active_publication() or ""
        )
        # Asked here rather than at install time, because here the selection
        # exists: the check is about the tables actually chosen, not about the
        # largest set anyone might have chosen. Only a copy that cannot finish
        # is refused; being tight is reported and left to the caller.
        if not force:
            why = capacity_svc.refusal(
                capacity_svc.check(
                    _build_publisher_connstr(), _active_publication() or ""
                )
            )
            if why:
                raise HTTPException(status_code=409, detail=why)
        return bootstrap_svc.start(
            force=force, publisher_connstr=_build_publisher_connstr()
        )
    except RuntimeError as e:
        # Already running, or already subscribed: the caller asked for
        # something that has happened, which is a conflict rather than a fault.
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start bootstrap: {e}")


@router.delete("/bootstrap")
def delete_bootstrap():
    """Stop a running bootstrap. Leaves whatever it managed to create."""
    try:
        return bootstrap_svc.cancel()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cancel bootstrap: {e}")


@router.get("/capacity")
def get_capacity():
    """Will the current selection fit in the pool?

    Two answers, deliberately: `fits` is a fact about this disk today and is
    what the copy refuses over; `comfortable` is a forecast about the
    snapshots and clones that come later, and is only ever said.
    """
    try:
        return capacity_svc.check(
            _build_publisher_connstr(), _active_publication() or ""
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to measure capacity: {e}")


@router.get("/schema-drift")
def get_schema_drift():
    """Where the replica's shape no longer matches what the primary publishes.

    Read-only, and deliberately so: by the time a difference is detectable
    the DDL that would close it is gone, and inventing one is how a
    diagnostic becomes an outage.
    """
    try:
        return compare_published_schemas(
            _build_publisher_connstr(),
            _active_publication() or "",
            settings.container_name,
            settings.postgres_user,
            settings.postgres_password,
            settings.postgres_db,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to compare schemas: {e}")


@router.get("/trigger-status")
def get_trigger_status():
    """Check if the DDL capture event triggers are installed on the publisher."""
    try:
        connstr = _build_publisher_connstr()
        pub_name = _active_publication()
        # Asked of this publication: the shared triggers being there says DDL
        # is logged, not that new tables still join ours.
        installed = verify_capture_installed(connstr, pub_name)
        return {"installed": installed, "publication": pub_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check trigger status: {e}")


@router.post("/trigger-install")
def post_trigger_install():
    """Install or update the DDL capture event triggers on the publisher."""
    try:
        connstr = _build_publisher_connstr()
        pub_name = _active_publication()
        if not pub_name:
            raise HTTPException(status_code=400, detail="Missing PUBLICATION_NAME setting")
        result = install_capture_triggers(connstr, pub_name)
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
    if not fdw_creds.configured():
        raise HTTPException(
            status_code=400,
            detail="No FDW login configured — set one in the UI, or FDW_USER / FDW_PASSWORD in .env",
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
        "primary_host": fdw_creds.host(),
        "primary_port": fdw_creds.port(),
        "primary_db": fdw_creds.dbname(),
        "fdw_user": fdw_creds.user(),
        "fdw_password": fdw_creds.password(),
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
            # Whether a login exists and where it came from — never the login
            # itself. A password that goes in does not come back out.
            "credentials": {
                "configured": fdw_creds.configured(),
                "source": fdw_creds.source(),
                "user": fdw_creds.user(),
                "host": fdw_creds.host(),
                "port": fdw_creds.port(),
                "dbname": fdw_creds.dbname(),
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read fdw state: {e}")


class FdwCredentialsBody(BaseModel):
    user: str = Field(min_length=1)
    password: str = Field(min_length=1)
    # Blank means "wherever replication connects" — the common case, and one
    # less thing to get wrong. A bastion or a pooler is the reason to differ.
    host: Optional[str] = None
    port: Optional[int] = None
    dbname: Optional[str] = None


@router.put("/fdw/credentials")
def put_fdw_credentials(body: FdwCredentialsBody):
    """Set the login foreign tables are read as, and build the server with it."""
    try:
        host = body.host or settings.primary_host
        port = body.port or settings.primary_port
        db = body.dbname or settings.primary_db
        if not (host and port and db):
            raise HTTPException(status_code=400, detail="No primary connection known to test against")

        err = fdw_creds.check(body.user, body.password, host, int(port), db)
        if err:
            raise HTTPException(status_code=400, detail=f"Could not connect as '{body.user}': {err}")

        fdw_creds.save(body.user, body.password, body.host, body.port, body.dbname)

        # Build the foreign server now if there is a replica to build it in.
        # Nothing about this needs the replica to be recreated: the server and
        # its user mapping are ordinary SQL, applied the same way the table
        # mappings are.
        applied = None
        try:
            cfg = fdw_svc.load_yaml(settings.fdw_yaml_abs())
            applied = fdw_svc._regenerate_and_apply(
                cfg, settings.fdw_yaml_abs(), settings.fdw_sql_abs(), _build_fdw_apply_args()
            )
        except Exception as e:
            applied = {"applied": False, "result": {"stderr": str(e)}}

        return {
            "configured": True,
            "user": body.user,
            "server_applied": bool(applied and applied.get("applied")),
            "detail": (applied or {}).get("result", {}).get("stderr") or None,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to set FDW credentials: {e}")


@router.delete("/fdw/credentials")
def delete_fdw_credentials():
    """Forget the stored login. Leaves the foreign server and its mapping alone."""
    try:
        fdw_creds.clear()
        return {"configured": fdw_creds.configured(), "source": fdw_creds.source()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear FDW credentials: {e}")


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
            publication_name=_active_publication(),
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
            publication_name=_active_publication(),
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
            cfg, _build_publisher_connstr(), _active_publication() or "",
        ) if _active_publication() else []
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


@router.get("/sync-log")
def get_sync_log(limit: int = 100):
    """Recent auto-sync activity (new tables, column/constraint adds, schema
    moves, FDW drift re-imports, errors) recorded by the background loop."""
    return {"events": sync_log.read_events(limit)}


# ── Fidelity: what the replica could not reproduce ───────────────────
#
# Both endpoints answer the same question from different ends: is this replica
# actually a copy of the primary, or only of the parts that happened to work?
# The initial schema apply runs with ON_ERROR_STOP=0 — it has to, or one
# unusable object would abort the whole clone — so failures have to be
# recorded somewhere that can be asked, rather than left in a log nobody reads.


@router.get("/extensions")
def get_extension_parity():
    """The primary's extensions against what this replica can offer.

    An extension is missing for one of two reasons, and they need different
    fixes: not installed (the file is there, CREATE EXTENSION was never run)
    or not available (the binary is not in the image, so no SQL can fix it —
    only a different POSTGRES_IMAGE can).
    """
    try:
        connstr = _build_publisher_connstr()
        _require_subscriber_settings()

        src = _run_publisher_sql(
            connstr,
            "SELECT extname || ',' || extversion FROM pg_extension ORDER BY 1;",
        )
        source = []
        for line in src.splitlines():
            line = line.strip()
            if not line:
                continue
            name, _, version = line.partition(",")
            source.append({"name": name, "version": version})

        sub = _run_subscriber_sql(
            settings.container_name,
            settings.postgres_user,
            settings.postgres_password,
            settings.postgres_db,
            "SELECT name || ',' || coalesce(installed_version, '') || ',' || default_version "
            "FROM pg_available_extensions ORDER BY 1;",
        )
        available: dict[str, dict] = {}
        for line in sub.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            available[parts[0]] = {"installed": parts[1] or None, "default": parts[2]}

        missing_not_installed = []
        missing_not_available = []
        for e in source:
            got = available.get(e["name"])
            if got is None:
                missing_not_available.append(e)
            elif not got["installed"]:
                missing_not_installed.append({**e, "available_version": got["default"]})

        return {
            "source": source,
            "replica_available_count": len(available),
            "missing_not_installed": missing_not_installed,
            "missing_not_available": missing_not_available,
            "ok": not missing_not_installed and not missing_not_available,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to compare extensions: {e}")


@router.get("/schema-errors")
def get_schema_errors(limit: int = Query(200, ge=1, le=2000)):
    """Objects the initial schema clone could not create.

    Empty — including when the table does not exist — means the clone either
    reported nothing or predates this record. Absence of evidence, so it is
    reported as such rather than as a clean bill of health.
    """
    try:
        _require_subscriber_settings()
        exists = _run_subscriber_sql(
            settings.container_name,
            settings.postgres_user,
            settings.postgres_password,
            settings.postgres_db,
            "SELECT to_regclass('public._snaplicator_schema_errors') IS NOT NULL;",
        ).strip()
        if exists != "t":
            return {"recorded": False, "count": 0, "errors": []}

        out = _run_subscriber_sql(
            settings.container_name,
            settings.postgres_user,
            settings.postgres_password,
            settings.postgres_db,
            "SELECT replace(coalesce(message, ''), chr(10), ' ') FROM public._snaplicator_schema_errors "
            f"ORDER BY id LIMIT {int(limit)};",
        )
        errors = [l.strip() for l in out.splitlines() if l.strip()]
        return {"recorded": True, "count": len(errors), "errors": errors}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read schema errors: {e}")
