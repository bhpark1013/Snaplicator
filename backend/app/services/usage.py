from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Per-subvolume disk usage (btrfs filesystem du -s), cached on disk.
#
# Measuring walks every extent of a subvolume (~10s each on prod, ~30 subvols),
# so API reads NEVER measure inline: they serve whatever is cached and queue
# stale entries for a background worker thread. The cache file lives OUTSIDE
# the repo and the replica reset scope (same rationale as sync_log/notify) so
# a redeploy or --reload restart doesn't trigger a multi-minute remeasure
# storm. Override the location with USAGE_CACHE_PATH.
#
# exclusive_bytes is what `btrfs fi du` reports as Exclusive: bytes referenced
# by this subvolume ONLY (not shared with any other snapshot/clone) — i.e. the
# space freed if it were deleted, and a good proxy for how much it has
# diverged since it was taken.

_LOCK = threading.Lock()  # guards cache-file read/modify/write and _inflight
_inflight: set = set()  # subvolume paths queued or being measured right now
_TTL = timedelta(hours=24)


def _path() -> Path:
    env = os.environ.get("USAGE_CACHE_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".snaplicator" / "usage_cache.json"


def _load() -> Dict[str, dict]:
    try:
        p = _path()
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _store(cache: Dict[str, dict]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def measure_subvolume(path: str) -> Optional[dict]:
    """Measure one subvolume with `btrfs fi du -s --raw` (blocking, ~10s)."""
    try:
        out = subprocess.run(
            ["sudo", "-n", "btrfs", "filesystem", "du", "-s", "--raw", path],
            check=True, text=True, capture_output=True,
        ).stdout
    except Exception as e:
        logger.warning("usage measure failed for %s: %s", path, e)
        return None
    # Data row: "<total> <exclusive> <set_shared> <filename>" (bytes with --raw)
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[0].isdigit() and cols[1].isdigit():
            return {
                "total_bytes": int(cols[0]),
                "exclusive_bytes": int(cols[1]),
                "measured_at": datetime.now().isoformat(timespec="seconds"),
            }
    logger.warning("usage measure: could not parse output for %s", path)
    return None


def _is_stale(entry: Optional[dict]) -> bool:
    if not entry or not entry.get("measured_at"):
        return True
    try:
        return datetime.now() - datetime.fromisoformat(entry["measured_at"]) > _TTL
    except Exception:
        return True


def _refresh_worker(paths: List[str]) -> None:
    for path in paths:
        entry = measure_subvolume(path) if Path(path).exists() else None
        with _LOCK:
            _inflight.discard(path)
            if entry:
                cache = _load()
                cache[path] = entry
                _store(cache)


def get_usage(paths: List[str], refresh: bool = True) -> Dict[str, dict]:
    """Cached usage keyed by subvolume path. Never blocks on a measurement:
    stale/missing entries return with refreshing=True and are queued for the
    background worker (dedup'd via _inflight, so concurrent calls and the
    hourly sweep can't double-measure)."""
    to_measure: List[str] = []
    out: Dict[str, dict] = {}
    with _LOCK:
        cache = _load()
        for path in paths:
            entry = cache.get(path)
            stale = _is_stale(entry)
            if stale and refresh and path not in _inflight:
                _inflight.add(path)
                to_measure.append(path)
            out[path] = {
                **(entry or {}),
                "stale": stale,
                "refreshing": path in _inflight,
            }
    if to_measure:
        threading.Thread(target=_refresh_worker, args=(to_measure,), daemon=True).start()
    return out


def prune_except(paths: List[str]) -> None:
    """Drop cache entries for subvolumes that no longer exist."""
    keep = set(paths)
    with _LOCK:
        cache = _load()
        pruned = {k: v for k, v in cache.items() if k in keep}
        if len(pruned) != len(cache):
            _store(pruned)


def all_subvolume_paths(root_data_dir: str) -> List[str]:
    """Every btrfs subvolume directly under the root data dir (main, clones,
    snapshots) — the daily warm sweep measures all of them."""
    from .btrfs import _is_btrfs_subvolume
    root = Path(root_data_dir)
    if not root.exists():
        return []
    out: List[str] = []
    for entry in os.scandir(root):
        if entry.is_dir(follow_symlinks=False) and _is_btrfs_subvolume(Path(entry.path)):
            out.append(entry.path)
    return sorted(out)
