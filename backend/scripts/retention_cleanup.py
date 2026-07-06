#!/usr/bin/env python3
"""Retention cleanup for Snaplicator snapshots (cron entrypoint).

Dry-run by default; pass --apply to actually delete expired snapshots.
Clones are separate btrfs subvolumes and are NEVER affected (btrfs ref-counts
the shared CoW extents), so this only removes expired snapshot subvolumes.
"""
import sys
import os
from datetime import datetime

# make `app` importable (this file lives in backend/scripts/)
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.core.config import settings  # noqa: E402  (import loads configs/.env)
from app.services.btrfs import cleanup_expired_snapshots  # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    stamp = datetime.now().isoformat(timespec="seconds")
    summary = cleanup_expired_snapshots(settings.root_data_dir, settings.main_data_dir, apply=apply)
    mode = "APPLY" if apply else "DRY-RUN"
    names = ", ".join(r["name"] for r in summary["results"]) or "(none)"
    print(f"[{stamp}] retention {mode}: total={summary['total_snapshots']} "
          f"expired={summary['expired']} deleted={summary['deleted']} | {names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
