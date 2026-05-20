from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# Persistent, append-only ring buffer of auto-sync events. Lives OUTSIDE the
# repo and the replica reset scope (same rationale as the check-sql store) so
# the activity history survives full re-initialization. Override the location
# with SYNC_LOG_PATH.

_LOCK = threading.Lock()
_MAX_EVENTS = 500
_LAST_BY_KIND: dict = {}
_LAST_INIT = {"done": False}

_NOTABLE = (
    "synced", "refreshed", "errors", "moved", "orphans", "skipped",
    "columns_added", "constraints_synced", "drifted", "reapplied",
    "changes", "error",
)


def _path() -> Path:
    env = os.environ.get("SYNC_LOG_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".snaplicator" / "sync_events.jsonl"


def record(kind: str, detail: dict) -> None:
    """Append one event. Best-effort: observability must never break the loop."""
    try:
        ev = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "detail": detail,
        }
        sig = json.dumps(detail, ensure_ascii=False, default=str,
                         sort_keys=True)
        line = json.dumps(ev, ensure_ascii=False, default=str)
        p = _path()
        with _LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            existing = (
                p.read_text(encoding="utf-8").splitlines() if p.exists() else []
            )
            if not _LAST_INIT["done"]:
                for _ln in existing:
                    try:
                        _e = json.loads(_ln)
                        _LAST_BY_KIND[_e.get("kind")] = json.dumps(
                            _e.get("detail"), ensure_ascii=False,
                            default=str, sort_keys=True,
                        )
                    except Exception:
                        continue
                _LAST_INIT["done"] = True
            if _LAST_BY_KIND.get(kind) == sig:
                return
            _LAST_BY_KIND[kind] = sig
            existing.append(line)
            if len(existing) > _MAX_EVENTS:
                existing = existing[-_MAX_EVENTS:]
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
            os.replace(tmp, p)
    except Exception:
        pass


def record_if(kind: str, res) -> None:
    """Record only when ``res`` carries a reflected change or an error, so
    idle no-op sync cycles don't spam the log."""
    if not res or not isinstance(res, dict):
        return
    detail = {k: res[k] for k in _NOTABLE if res.get(k)}
    if detail:
        record(kind, detail)


def read_events(limit: int = 100) -> list:
    """Newest-first list of recorded events (parsed)."""
    try:
        p = _path()
        if not p.exists():
            return []
        out = []
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        out.reverse()
        return out[: max(1, min(limit, _MAX_EVENTS))]
    except Exception:
        return []
