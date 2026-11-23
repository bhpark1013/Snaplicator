"""Delete clones nobody uses, after saying so.

Clones pin the blocks the live replica has since rewritten, so a forgotten
clone costs disk for as long as it exists. Here "used" means someone is
connected: every TICK the manager asks each running clone how many client
backends it has, and any at all moves its last access to now.

Once a day, a clone idle for CLONE_IDLE_DAYS is marked with a deletion date
CLONE_DELETE_GRACE_DAYS out and announced on Slack; a marked clone still idle
on that date is deleted. Any connection, "extend" or "keep" clears the mark.

State lives in one JSON file in the pool's .snaplicator dir (same place as
bootstrap state), not inside the subvolumes: a refresh replaces the clone's
data and its metadata with it, and the clock must not reset with it.

A clone the manager has never seen gets last access = the moment it is first
seen, so the first deploy starts every clock at zero instead of deleting
everything that predates the tracking.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..core.config import settings

logger = logging.getLogger(__name__)

TICK_SECONDS = 300
# The daily pass waits for this UTC hour (10:00 KST) so the notice lands
# when people are around to read it.
DAILY_HOUR_UTC = 1

_LOCK = threading.Lock()

_ACCESS_SQL = (
    "select count(*) from pg_stat_activity "
    "where backend_type = 'client backend' and pid <> pg_backend_pid()"
)


# ── state file ───────────────────────────────────────────────────────

def _path() -> Path:
    env = os.environ.get("CLONE_RETENTION_STATE_PATH")
    if env:
        return Path(env).expanduser()
    return Path(settings.root_data_dir) / ".snaplicator" / "clone_retention.json"


def load_state() -> Dict:
    try:
        p = _path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("clones", {})
                return data
    except Exception as e:
        # An unreadable file restarts every clock at now — the safe direction.
        logger.warning("clone retention state unreadable, starting fresh: %s", e)
    return {"clones": {}}


def save_state(state: Dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── pure decisions (tested) ──────────────────────────────────────────

def record_access(entries: Dict[str, Dict], seen: Iterable[str], accessed: Iterable[str], now: datetime) -> None:
    """Fold one probe round into entries, in place.

    seen: every current clone. accessed: those with a client connected.
    A new name starts its clock now; an accessed one restarts it and loses
    any deletion mark.
    """
    accessed = set(accessed)
    for name in seen:
        e = entries.setdefault(name, {"first_seen_at": _iso(now), "last_accessed_at": _iso(now)})
        if name in accessed:
            e["last_accessed_at"] = _iso(now)
            e.pop("delete_after", None)


def plan(
    entries: Dict[str, Dict],
    names: Iterable[str],
    now: datetime,
    idle_days: int,
    grace_days: int,
    auto_delete: bool,
) -> Dict[str, List]:
    """Which clones to mark for deletion, which to delete, which marks to drop.

    names: the clones eligible this pass (current, not busy). A name without
    an entry has not been seen by the probe yet and is left alone.
    Returns {"mark": [(name, delete_after)], "delete": [name], "unmark": [name]}.
    """
    out: Dict[str, List] = {"mark": [], "delete": [], "unmark": []}
    idle_for = timedelta(days=max(1, int(idle_days)))
    for name in names:
        e = entries.get(name)
        if not e:
            continue
        keep_until = _parse(e.get("keep_until"))
        last = _parse(e.get("last_accessed_at")) or _parse(e.get("first_seen_at"))
        protected = bool(e.get("permanent")) or (keep_until is not None and now < keep_until)
        idle = last is not None and now - last >= idle_for
        if protected or not idle:
            if e.get("delete_after"):
                out["unmark"].append(name)
            continue
        delete_after = _parse(e.get("delete_after"))
        if delete_after is None:
            out["mark"].append((name, now + timedelta(days=max(0, int(grace_days)))))
        elif delete_after <= now and auto_delete:
            out["delete"].append(name)
    return out


def due_daily(state: Dict, now: datetime) -> bool:
    return state.get("last_daily_run") != now.date().isoformat() and now.hour >= DAILY_HOUR_UTC


def keep(entry: Dict, now: datetime, days: Optional[int] = None, permanent: Optional[bool] = None) -> Dict:
    """Apply an "extend" / "keep forever" request to one entry, in place."""
    if permanent is not None:
        entry["permanent"] = bool(permanent)
    if days is not None:
        entry["keep_until"] = _iso(now + timedelta(days=max(1, int(days))))
    if permanent is None and days is None:
        entry["last_accessed_at"] = _iso(now)
    entry.pop("delete_after", None)
    return entry


# ── side effects ─────────────────────────────────────────────────────

def _client_count(container: str) -> Optional[int]:
    proc = subprocess.run(
        ["docker", "exec", container, "psql", "-U", str(settings.postgres_user),
         "-d", str(settings.postgres_db), "-tAc", _ACCESS_SQL],
        text=True, capture_output=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:300])
    return int((proc.stdout or "0").strip() or 0)


def _busy_names() -> set:
    from . import clone_progress
    busy = set(clone_progress.deleting_names())
    cur = clone_progress.current() or {}
    if cur.get("active") and cur.get("name"):
        busy.add(cur["name"])
    return busy


def _fmt_bytes(n: Optional[int]) -> str:
    if not isinstance(n, (int, float)):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit in ("B", "KB") else f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def _kst(dt: Optional[datetime]) -> str:
    if dt is None:
        return "?"
    return dt.astimezone(timezone(timedelta(hours=9))).strftime("%m/%d")


def _send(text: str, kind: str) -> None:
    from . import notify
    host = socket.gethostname() or "unknown-host"
    res = notify.send(f"{text}\n_`{host}`_", kind=kind)
    if not res.get("ok"):
        logger.warning("clone retention notice not sent (%s): %s", kind, res)


def tick() -> Dict:
    """One probe round, plus the daily pass when it is due. Blocking."""
    from .btrfs import list_clone_subvolumes_with_containers
    from .docker_pg import delete_clone
    from . import clone_progress, divergence

    clones = list_clone_subvolumes_with_containers(settings.root_data_dir, settings.main_data_dir)
    by_name = {c["name"]: c for c in clones}
    accessed: List[str] = []
    for c in clones:
        if not (c.get("is_running") and c.get("container_name")):
            continue
        try:
            if (_client_count(c["container_name"]) or 0) > 0:
                accessed.append(c["name"])
        except Exception as e:
            # A clone we cannot ask is not shown to be idle: count it as used.
            logger.warning("access probe failed for %s, counting as used: %s", c["container_name"], e)
            accessed.append(c["name"])

    now = _now()
    with _LOCK:
        state = load_state()
        entries = state["clones"]
        record_access(entries, by_name.keys(), accessed, now)
        for gone in [n for n in entries if n not in by_name]:
            entries.pop(gone, None)
        daily = due_daily(state, now)
        decision: Dict[str, List] = {"mark": [], "delete": [], "unmark": []}
        if daily:
            busy = _busy_names()
            eligible = [
                n for n, c in by_name.items()
                if n not in busy and (c.get("container_name") or "") not in busy
            ]
            decision = plan(
                entries, eligible, now,
                settings.clone_idle_days, settings.clone_delete_grace_days, settings.clone_auto_delete,
            )
            for n in decision["unmark"]:
                entries[n].pop("delete_after", None)
            for n, when in decision["mark"]:
                entries[n]["delete_after"] = _iso(when)
            state["last_daily_run"] = now.date().isoformat()
        save_state(state)
        snapshot = {n: dict(e) for n, e in entries.items()}

    if not daily:
        return {"accessed": len(accessed), "clones": len(clones)}

    sizes = ((divergence.load_cached() or {}).get("subvolumes") or {})

    def line(n: str) -> str:
        c, e = by_name[n], snapshot.get(n, {})
        label = (c.get("display_name") or c.get("description") or "").strip()
        last = _parse(e.get("last_accessed_at"))
        excl = (sizes.get(n) or {}).get("exclusive_bytes")
        return (
            f"• `{n}`" + (f" ({label})" if label else "")
            + f" · 마지막 사용 {_kst(last)} · 혼자 쓰는 용량 {_fmt_bytes(excl)}"
            + f" · 삭제 예정 {_kst(_parse(e.get('delete_after')))}"
        )

    if decision["mark"]:
        what = "삭제됩니다" if settings.clone_auto_delete else "삭제 대상입니다 (자동 삭제 꺼짐)"
        _send(
            f":wastebasket: {settings.clone_idle_days}일 넘게 아무도 접속하지 않은 클론 "
            f"{len(decision['mark'])}개가 {settings.clone_delete_grace_days}일 뒤 {what}.\n"
            + "\n".join(line(n) for n, _ in decision["mark"])
            + "\n계속 쓰려면 웹 화면 클론 상세에서 「연장」이나 「계속 보관」을 누르세요.",
            "clone_retention_notice",
        )

    deleted, failed = [], []
    for n in decision["delete"]:
        # Someone may have pressed "extend" since the plan was made.
        with _LOCK:
            fresh = load_state()["clones"]
        if n not in plan(
            fresh, [n], _now(), settings.clone_idle_days, settings.clone_delete_grace_days, True,
        )["delete"]:
            continue
        c = by_name[n]
        target = c.get("container_name") or n
        aliases = [x for x in (n, c.get("container_name")) if x]
        clone_progress.delete_begin(*aliases)
        try:
            delete_clone(settings.root_data_dir, settings.main_data_dir, target)
            deleted.append(n)
        except Exception as e:
            logger.error("idle clone delete failed for %s: %s", n, e)
            failed.append(f"`{n}`: {str(e)[:200]}")
        finally:
            clone_progress.delete_finish(*aliases)
    if deleted or failed:
        with _LOCK:
            state = load_state()
            for n in deleted:
                state["clones"].pop(n, None)
            save_state(state)
        msg = ""
        if deleted:
            msg += f":wastebasket: 오래 안 쓴 클론 {len(deleted)}개를 삭제했습니다.\n" + "\n".join(
                f"• `{n}`" + (f" ({(by_name[n].get('display_name') or '').strip()})" if by_name[n].get("display_name") else "")
                + f" · {_fmt_bytes((sizes.get(n) or {}).get('exclusive_bytes'))}"
                for n in deleted
            )
        if failed:
            msg += ("\n" if msg else "") + ":warning: 삭제 실패\n" + "\n".join(f"• {f}" for f in failed)
        _send(msg, "clone_retention_deleted")

    return {
        "accessed": len(accessed),
        "clones": len(clones),
        "marked": [n for n, _ in decision["mark"]],
        "deleted": deleted,
        "failed": failed,
    }


def annotate(clones: List[Dict]) -> List[Dict]:
    """Add the retention fields to clone dicts from the listing, in place."""
    try:
        entries = load_state().get("clones", {})
    except Exception:
        entries = {}
    for c in clones:
        if isinstance(c, dict):
            e = entries.get(c.get("name"), {})
            c["last_accessed_at"] = e.get("last_accessed_at")
            c["delete_after"] = e.get("delete_after")
            c["keep_until"] = e.get("keep_until")
            c["permanent"] = bool(e.get("permanent"))
    return clones


def set_keep(name: str, days: Optional[int] = None, permanent: Optional[bool] = None) -> Dict:
    now = _now()
    with _LOCK:
        state = load_state()
        e = state["clones"].setdefault(name, {"first_seen_at": _iso(now), "last_accessed_at": _iso(now)})
        keep(e, now, days=days, permanent=permanent)
        save_state(state)
        return {"name": name, **e, "permanent": bool(e.get("permanent"))}


# ── replica VACUUM schedule ──────────────────────────────────────────

VACUUM_HOUR_UTC = 19  # 04:00 KST


def vacuum_due(state: Dict, now: datetime, interval_hours: int) -> bool:
    if interval_hours <= 0:
        return False
    last = _parse(state.get("replica_vacuum_last"))
    if last is not None and now - last < timedelta(hours=interval_hours) - timedelta(minutes=30):
        return False
    # Daily-or-longer intervals wait for the quiet hour.
    return interval_hours < 24 or now.hour == VACUUM_HOUR_UTC


def mark_vacuum_run(now: Optional[datetime] = None) -> None:
    with _LOCK:
        state = load_state()
        state["replica_vacuum_last"] = _iso(now or _now())
        save_state(state)
