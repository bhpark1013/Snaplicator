from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import logging

from .core.config import settings
from .api.routes.health import router as health_router
from .api.routes.snapshots import router as snapshots_router
from .api.routes.clones import router as clones_router
from .api.routes.replication import router as replication_router
from .services.replication import auto_sync_new_tables, sync_column_changes, install_auto_add_trigger, verify_trigger_installed

logger = logging.getLogger("snaplicator.ddl_sync")


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
        try:
            connstr = _build_publisher_connstr()
            pub_name = settings.publication_name
            sub_name = settings.subscription_name
            container = settings.container_name
            user = settings.postgres_user
            password = settings.postgres_password
            db = settings.postgres_db

            if connstr and pub_name and sub_name and container and user and db:
                # Safety net: verify event trigger exists on publisher
                try:
                    trigger_ok = await asyncio.to_thread(verify_trigger_installed, connstr)
                    if not trigger_ok:
                        logger.warning("Auto-add trigger missing on publisher, reinstalling...")
                        await asyncio.to_thread(install_auto_add_trigger, connstr, pub_name)
                        logger.info("Auto-add trigger reinstalled successfully")
                except Exception as e:
                    logger.warning(f"Trigger verification failed: {e}")

                result = await asyncio.to_thread(
                    auto_sync_new_tables,
                    connstr, pub_name, container, user, password, db, sub_name,
                )
                if result and result.get("synced"):
                    logger.info(f"DDL auto-sync: synced {result['synced']}, refreshed={result.get('refreshed')}")
                if result and result.get("errors"):
                    logger.warning(f"DDL auto-sync errors: {result['errors']}")

                # Sync column changes (ADD COLUMN) for existing tables
                try:
                    col_result = await asyncio.to_thread(
                        sync_column_changes,
                        connstr, pub_name, container, user, password, db,
                    )
                    if col_result and col_result.get("columns_added"):
                        logger.info(f"Column sync: added {col_result['columns_added']}")
                    if col_result and col_result.get("errors"):
                        logger.warning(f"Column sync errors: {col_result['errors']}")
                except Exception as e:
                    logger.warning(f"Column sync failed: {e}")
        except Exception as e:
            logger.error(f"DDL auto-sync error: {e}")

        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best-effort: install event trigger on publisher at startup
    try:
        connstr = _build_publisher_connstr()
        pub_name = settings.publication_name
        if connstr and pub_name:
            await asyncio.to_thread(install_auto_add_trigger, connstr, pub_name)
            logger.info(f"Auto-add event trigger installed on publisher for publication '{pub_name}'")
    except Exception as e:
        logger.warning(f"Could not install auto-add trigger at startup (will retry in polling loop): {e}")

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
