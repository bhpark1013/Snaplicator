from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import logging
import time

from .core.config import settings
from .api.routes.health import router as health_router
from .api.routes.snapshots import router as snapshots_router
from .api.routes.clones import router as clones_router
from .api.routes.replication import router as replication_router
from .api.routes.notifications import router as notifications_router
from .services import fdw as fdw_svc
from .services import sync_log
from .services.replication import (
    auto_sync_new_tables,
    install_capture_triggers,
    verify_capture_installed,
    enable_ddl_apply,
    get_ddl_apply_status,
    run_deferred_ddl,
    check_subscription_health,
)

logger = logging.getLogger("snaplicator.ddl_sync")
# Root logger defaults to WARNING, which silently swallowed every
# logger.info() in this module (loop start, refresh/apply progress,
# cycle heartbeat). Give app loggers a real INFO handler.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Baseline for change-detection of DDL apply failures across loop iterations.
# None until the first successful status read after process start.
_ddl_apply_seen = {"failures": None}

# Previous-cycle subscription health, for transition/delta alerting. Booleans
# alert on the ok→broken transition (one event per outage, not one per cycle);
# counters alert on any increase. Empty until the first successful check.
_sub_health_seen: dict = {}


def _build_publisher_connstr() -> str | None:
    connstr = settings.publisher_connstr
    if connstr:
        return connstr
    if not (settings.primary_host and settings.primary_port and settings.primary_db and settings.primary_user):
        return None
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


async def ddl_sync_loop():
    """Background task that periodically checks for new tables in publication and syncs them."""
    interval = int(settings.ddl_sync_interval or 30)
    if interval <= 0:
        logger.info("DDL sync disabled (interval <= 0)")
        return

    await asyncio.sleep(5)  # initial delay
    logger.info(f"DDL auto-sync started (interval={interval}s)")

    while True:
        cycle_t0 = time.monotonic()
        try:
            connstr = _build_publisher_connstr()
            pub_name = settings.publication_name
            sub_name = settings.subscription_name
            container = settings.container_name
            user = settings.postgres_user
            password = settings.postgres_password
            db = settings.postgres_db

            if connstr and pub_name and sub_name and container and user and db:
                # Safety net: DDL capture triggers must exist on the publisher —
                # a gap in capture is an unrecoverable hole in the DDL log.
                try:
                    trigger_ok = await asyncio.to_thread(verify_capture_installed, connstr)
                    if not trigger_ok:
                        logger.warning("DDL capture triggers missing on publisher, reinstalling...")
                        await asyncio.to_thread(install_capture_triggers, connstr, pub_name)
                        logger.info("DDL capture triggers reinstalled successfully")
                        sync_log.record("capture_reinstalled", {"publication": pub_name})
                except Exception as e:
                    logger.warning(f"Capture trigger verification failed: {e}")

                # Schema changes (moves, new columns, constraints) replicate
                # in-stream via DDL capture+apply; the legacy diff-reconcilers
                # that re-derived them are gone. The loop keeps exactly the
                # jobs the stream cannot do: connect DML flow for new
                # publication members (below) and watch subscription health.
                result = await asyncio.to_thread(
                    auto_sync_new_tables,
                    connstr, pub_name, container, user, password, db, sub_name,
                )
                sync_log.record_if("table_added", result)
                if result and result.get("synced"):
                    logger.info(f"Subscription refresh: connected {result['synced']}")
                if result and result.get("waiting"):
                    logger.info(
                        "Subscription refresh: waiting for in-stream CREATE of "
                        f"{result['waiting']} (connects next cycle once applied)"
                    )
                if result and result.get("errors"):
                    logger.warning(f"DDL auto-sync errors: {result['errors']}")

                # ── Subscription health watch: disabled / dead worker /
                # apply·sync error counters. Events with error keys reach
                # Slack automatically via sync_log → notify_event.
                try:
                    h = await asyncio.to_thread(
                        check_subscription_health, container, user, password, db, sub_name,
                    )
                    prev = dict(_sub_health_seen)
                    _sub_health_seen.update(h)
                    issues = {}
                    if not h["exists"]:
                        if prev.get("exists", True):
                            issues["error"] = f"subscription '{sub_name}' does not exist"
                    elif not h["enabled"]:
                        if prev.get("enabled", True):
                            issues["error"] = (
                                f"subscription '{sub_name}' is DISABLED "
                                "(disable_on_error or manual) — replication stopped"
                            )
                    elif not h["worker_running"]:
                        if prev.get("worker_running", True):
                            issues["worker_error"] = (
                                f"subscription '{sub_name}' apply worker not running "
                                "(dead or crash-looping)"
                            )
                    for key, label in (("apply_errors", "apply_error"), ("sync_errors", "sync_error")):
                        if prev.get(key) is not None and h[key] > prev[key]:
                            issues[label] = (
                                f"{h[key] - prev[key]} new {label.replace('_', ' ')}(s) "
                                f"on '{sub_name}' (total {h[key]})"
                            )
                    if issues:
                        logger.warning(f"Subscription health: {issues}")
                        # dedupe=False: transition gating above already
                        # ensures once-per-outage; content dedupe would
                        # silently swallow an identical future outage.
                        sync_log.record("subscription_error", issues, dedupe=False)
                except Exception as e:
                    logger.warning(f"Subscription health check failed: {e}")

                # ── In-stream DDL apply (subscriber side) — flag-gated switch ──
                if settings.ddl_apply_enabled:
                    try:
                        res = await asyncio.to_thread(
                            enable_ddl_apply,
                            connstr, pub_name, container, user, password, db, sub_name,
                        )
                        if res.get("added") or res.get("refreshed"):
                            logger.info(f"DDL apply enabled: {res}")
                            sync_log.record("ddl_apply_enabled", res)

                        st = await asyncio.to_thread(
                            get_ddl_apply_status, container, user, password, db,
                        )
                        prev = _ddl_apply_seen["failures"]
                        if prev is not None and st["failures"] > prev:
                            sync_log.record("ddl_apply_failure", {
                                "error": (
                                    f"{st['failures'] - prev} new DDL apply failure(s) "
                                    f"(total {st['failures']}) — see _snaplicator_ddl_failures"
                                ),
                            })
                        _ddl_apply_seen["failures"] = st["failures"]

                        if st["deferred_pending"]:
                            dres = await asyncio.to_thread(
                                run_deferred_ddl, container, user, password, db,
                            )
                            if dres.get("executed") or dres.get("errors"):
                                logger.info(f"Deferred DDL executor: {dres}")
                                sync_log.record("ddl_deferred", dres)
                    except Exception as e:
                        logger.warning(f"DDL apply sync failed: {e}")

            # ── FDW remote column-drift auto-sync ──
            # Independent of the publication gate above: FDW can be in use
            # even when logical replication is not fully configured.
            try:
                if (
                    settings.container_name
                    and settings.postgres_user
                    and settings.postgres_db
                    and settings.fdw_user
                    and settings.fdw_password
                    and settings.effective_fdw_host()
                    and settings.effective_fdw_port()
                    and settings.effective_fdw_db()
                ):
                    yaml_abs = settings.fdw_yaml_abs()
                    if yaml_abs.exists():
                        fdw_cfg = await asyncio.to_thread(
                            fdw_svc.load_yaml, yaml_abs
                        )
                        if fdw_cfg.tables or fdw_cfg.schemas:
                            fdw_apply_args = {
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
                            fdw_res = await asyncio.to_thread(
                                fdw_svc.sync_fdw_drift,
                                fdw_cfg,
                                yaml_abs,
                                settings.fdw_sql_abs(),
                                fdw_apply_args,
                            )
                            sync_log.record_if("fdw_drift", fdw_res)
                            if fdw_res.get("reapplied"):
                                logger.info(
                                    "FDW drift sync: re-imported "
                                    f"{fdw_res.get('drifted')}"
                                )
                            elif fdw_res.get("error"):
                                logger.warning(
                                    f"FDW drift sync error: {fdw_res.get('error')}"
                                )
            except Exception as e:
                logger.warning(f"FDW drift sync failed: {e}")

        except Exception as e:
            logger.error(f"DDL auto-sync error: {e}")
            sync_log.record("loop_error", {"error": str(e)})

        logger.info(f"sync cycle done in {time.monotonic() - cycle_t0:.0f}s")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best-effort: install the DDL capture triggers (outbox) on the publisher
    # at startup. Replaces the legacy auto-add-only trigger — auto pub-add is
    # folded into the capture trigger, scoped to schemas the publication covers.
    try:
        connstr = _build_publisher_connstr()
        pub_name = settings.publication_name
        if connstr and pub_name:
            await asyncio.to_thread(install_capture_triggers, connstr, pub_name)
            logger.info(f"DDL capture triggers installed on publisher for publication '{pub_name}'")
    except Exception as e:
        logger.warning(f"Could not install capture triggers at startup (will retry in polling loop): {e}")

    task = asyncio.create_task(ddl_sync_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Snaplicator API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router, prefix="/health", tags=["health"]) 
app.include_router(snapshots_router, prefix="/snapshots", tags=["snapshots"]) 
app.include_router(clones_router, prefix="/clones", tags=["clones"]) 
app.include_router(replication_router, prefix="/replication", tags=["replication"])
app.include_router(notifications_router, prefix="/notifications", tags=["notifications"])
