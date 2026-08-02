"""Where a clone creation has got to, while it is still going.

Creating a clone is one POST that does not return for a while — a btrfs
snapshot, a chown over the whole tree, a postgres that has to finish crash
recovery, and an anonymisation script that rewrites every column somebody
listed. Minutes, on a real dataset. The request itself cannot say any of
this: HTTP has one reply and it comes at the end, so a caller watching that
reply learns only "not yet", over and over, and cannot tell a slow chown from
a hung container.

So the work writes down where it is and the answer is read from a second
request. Kept in memory rather than in a file because the record is only
meaningful while the process doing the work is alive: if the manager
restarts, the clone it was building is gone too, and a file would outlive the
truth it describes.

Progress is reported, never enforced. A stage that fails to check in still
runs; the screen is simply less specific about it. Nothing here may raise
into the work it is describing.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple

# In the order they happen. Two are conditional — a clone with no
# anonymize.sql skips it, and a clone with no extra role skips the last —
# so the list is the itinerary, not a promise that every stop is made.
STAGES: List[Tuple[str, str]] = [
    ("checkpoint", "Flushing the replica to disk"),
    ("snapshot", "Taking the btrfs snapshot"),
    ("permissions", "Handing the files to postgres"),
    ("container", "Starting the clone's postgres"),
    ("ready", "Waiting for postgres to finish recovery"),
    ("subscriptions", "Disabling inherited subscriptions"),
    ("sequences", "Re-syncing owned sequences"),
    ("anonymize", "Running anonymize.sql"),
    ("user", "Creating the database user"),
]

_lock = threading.Lock()
_state: Optional[Dict] = None


def begin(what: str = "create", name: Optional[str] = None) -> None:
    """Start a new record, discarding any previous one.

    The calling thread is recorded as its owner. Refreshing and resetting a
    clone run through the same container-launching code as building one, and
    the API serves each request on its own thread, so without an owner a
    refresh started in another tab would file its stages under the build this
    screen is watching. Reports from anywhere else are dropped rather than
    merged: a wrong stage is worse than a missing one.
    """
    global _state
    with _lock:
        _state = {
            "what": what,
            "name": name,
            "_owner": threading.get_ident(),
            "active": True,
            "started_at": time.time(),
            "finished_at": None,
            "stage": None,
            "stage_started_at": None,
            "error": None,
            "stages": [
                {"key": k, "label": l, "status": "pending", "ms": None}
                for k, l in STAGES
            ],
        }


def stage(key: str) -> None:
    """Mark `key` as the stage now running.

    Everything before it is settled on the way past: a stage that was running
    is done, and one still pending was never entered — skipped, not stuck.
    Deriving that here is what lets the work call this once per stage instead
    of bracketing every one of them.
    """
    with _lock:
        if not _state or not _state["active"]:
            return
        if _state.get("_owner") != threading.get_ident():
            return
        now = time.time()
        reached = False
        for s in _state["stages"]:
            if s["key"] == key:
                reached = True
                s["status"] = "running"
                s["_t0"] = now
                continue
            if reached:
                continue
            if s["status"] == "running":
                s["status"] = "done"
                s["ms"] = int((now - s.pop("_t0", now)) * 1000)
            elif s["status"] == "pending":
                s["status"] = "skipped"
        _state["stage"] = key
        _state["stage_started_at"] = now


def finish(error: Optional[str] = None) -> None:
    with _lock:
        if not _state:
            return
        if _state.get("_owner") != threading.get_ident():
            return
        now = time.time()
        for s in _state["stages"]:
            if s["status"] == "running":
                s["status"] = "failed" if error else "done"
                s["ms"] = int((now - s.pop("_t0", now)) * 1000)
            elif s["status"] == "pending" and not error:
                # It finished without complaint, so what was never entered was
                # not needed. After a failure the same stages are unreached
                # rather than unnecessary, and saying "skipped" would claim
                # they were considered.
                s["status"] = "skipped"
        _state["active"] = False
        _state["error"] = error
        _state["finished_at"] = now


def current() -> Optional[Dict]:
    """The record, with the internal timing marks left out."""
    with _lock:
        if not _state:
            return None
        out = {k: v for k, v in _state.items() if k != "stages" and not k.startswith("_")}
        out["stages"] = [
            {k: v for k, v in s.items() if not k.startswith("_")}
            for s in _state["stages"]
        ]
        return out
