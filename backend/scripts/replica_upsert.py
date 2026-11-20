#!/usr/bin/env python3
"""Install the configured subscriber upsert policies without restarting the manager."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.services.replica_upsert import ensure_replica_upserts


def main() -> int:
    if not (settings.container_name and settings.postgres_user and settings.postgres_db):
        raise SystemExit("CONTAINER_NAME, POSTGRES_USER and POSTGRES_DB are required")
    result = ensure_replica_upserts(
        settings.container_name, settings.postgres_user, settings.postgres_password,
        settings.postgres_db, settings.replica_upsert_tables,
    )
    print(json.dumps(result))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
