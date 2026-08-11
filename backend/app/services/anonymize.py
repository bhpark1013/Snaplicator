from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..core.config import settings

logger = logging.getLogger(__name__)

# Management of configs/anonymize.sql — the script docker_pg.py runs inside
# every clone created from the live main replica.
#
# The path is NOT configurable: docker_pg.py resolves the same repo-relative
# location, and a second source of truth is exactly how a clone would end up
# masked by a file nobody edited. Kept out of git (see .gitignore) because it
# names real columns and carries the replacement values.

_MAX_BYTES = 1_000_000
_KEEP_BACKUPS = 10

# Written into the script itself to declare that this deployment's data needs
# no masking. It has to be stated in the file because the alternative — an
# absent or empty script — is indistinguishable from a misconfiguration at
# the one moment it matters, when a clone full of production data is handed
# to someone.
NO_OP_MARKER = "snaplicator: no-anonymization-needed"


def sql_path() -> Path:
    # services/ -> app/ -> backend/ -> repo root (same walk as docker_pg.py)
    return Path(__file__).resolve().parents[3] / "configs" / "anonymize.sql"


def _backup_dir() -> Path:
    return sql_path().parent


def _strip_comments_and_split(sql: str) -> List[str]:
    """Split into statements, ignoring `--` comments. Quote-aware so a `--`
    or `;` inside a literal (the iamport_data JSON, bcrypt hashes) is not
    mistaken for syntax. Dollar-quoting is handled only for bare `$$`."""
    # psql meta-commands (\set, \echo, \i) are terminated by the newline, not
    # by a semicolon. Left in, the first one swallows every SQL statement up
    # to the next `;` — which is how the leading `\set ON_ERROR_STOP on`
    # would hide the statement right after it.
    sql = "\n".join(
        "" if ln.lstrip().startswith("\\") else ln for ln in sql.splitlines()
    )
    out: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(sql)
    in_squote = False
    in_dollar = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_squote:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":  # escaped quote
                    buf.append(nxt)
                    i += 2
                    continue
                in_squote = False
            i += 1
            continue
        if in_dollar:
            if ch == "$" and nxt == "$":
                in_dollar = False
                buf.append("$$")
                i += 2
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and nxt == "-":  # line comment
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$" and nxt == "$":
            in_dollar = True
            buf.append("$$")
            i += 2
            continue
        if ch == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return [s.strip() for s in out if s.strip()]


_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_TARGET_RE = re.compile(
    rf"^\s*(?:update\s+(?:only\s+)?|alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?|"
    rf"insert\s+into\s+|delete\s+from\s+(?:only\s+)?|truncate\s+(?:table\s+)?(?:only\s+)?)"
    rf"({_IDENT}(?:\.{_IDENT})?)",
    re.IGNORECASE,
)


def referenced_tables(sql: str) -> List[str]:
    """Best-effort list of tables the script writes to, in first-seen order.
    Advisory only — used to warn before a missing relation aborts a clone."""
    seen: List[str] = []
    for stmt in _strip_comments_and_split(sql):
        m = _TARGET_RE.match(stmt)
        if not m:
            continue
        ref = m.group(1)
        if ref not in seen:
            seen.append(ref)
    return seen


def _split_qualified(ref: str) -> Tuple[Optional[str], str]:
    parts = re.findall(_IDENT, ref)
    parts = [p[1:-1] if p.startswith('"') else p for p in parts]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, parts[0]


def check_tables_exist(refs: List[str]) -> Dict[str, bool]:
    """Resolve each reference against the live replica, honouring its
    search_path exactly as the clone's psql session will."""
    if not refs:
        return {}
    container = settings.container_name
    user = settings.postgres_user
    db = settings.postgres_db
    if not (container and user and db):
        return {}
    selects = []
    for ref in refs:
        schema, table = _split_qualified(ref)
        qualified = f'"{schema}"."{table}"' if schema else f'"{table}"'
        literal = qualified.replace("'", "''")
        selects.append(f"SELECT '{ref.replace(chr(39), chr(39) * 2)}', to_regclass('{literal}') IS NOT NULL")
    sql = " UNION ALL ".join(selects) + ";"
    try:
        out = subprocess.run(
            ["docker", "exec", container, "psql", "-U", user, "-d", db, "-At", "-F", "|", "-c", sql],
            check=True, text=True, capture_output=True, timeout=30,
        ).stdout
    except Exception as e:
        logger.warning("anonymize table check failed: %s", e)
        return {}
    result: Dict[str, bool] = {}
    for line in out.splitlines():
        if "|" in line:
            ref, exists = line.rsplit("|", 1)
            result[ref] = exists.strip() == "t"
    return result


def validate(sql: str) -> Dict:
    """Advisory pre-flight. Never blocks a save: the whole point of editing
    this file is to prepare for a schema that may not exist yet. But a
    missing relation aborts the script under ON_ERROR_STOP and takes the
    whole clone with it, so surface it loudly before that happens."""
    refs = referenced_tables(sql)
    existence = check_tables_exist(refs)
    missing = [r for r in refs if existence.get(r) is False]
    return {
        "referenced_tables": refs,
        "missing_tables": missing,
        "checked": bool(existence),
        "warnings": (
            [
                f"{len(missing)} table(s) referenced here do not exist on the replica: "
                f"{', '.join(missing)}. The script aborts at the first one "
                "(ON_ERROR_STOP) and clone creation from main will fail."
            ]
            if missing else []
        ),
    }


def readiness() -> Dict:
    """Whether the script can actually mask a clone. Callers that are about to
    publish a clone must refuse when this is not ok — a silent skip produces
    an unmasked clone that looks exactly like a masked one."""
    p = sql_path()
    if not p.exists():
        return {"ok": False, "opted_out": False, "reason": (
            f"{p} does not exist, so nothing would be masked. Upload one from the "
            "Config page, or run: cp configs/anonymize-example.sql configs/anonymize.sql"
        )}
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "opted_out": False, "reason": f"Cannot read {p}: {e}"}
    if NO_OP_MARKER in text:
        return {"ok": True, "opted_out": True,
                "reason": f"Masking explicitly opted out via the {NO_OP_MARKER} marker."}
    if not _strip_comments_and_split(text):
        return {"ok": False, "opted_out": False, "reason": (
            f"{p} has no statement that changes data, so a clone would be published with "
            f"production data intact. Add the masking statements, or put "
            f"'-- {NO_OP_MARKER}' in the file if this data genuinely needs none."
        )}
    return {"ok": True, "opted_out": False, "reason": ""}


def get_content() -> Dict:
    p = sql_path()
    if not p.exists():
        return {"exists": False, "content": "", "size_bytes": 0,
                "modified_at": None, "path": str(p)}
    text = p.read_text(encoding="utf-8")
    st = p.stat()
    return {
        "exists": True,
        "content": text,
        "size_bytes": st.st_size,
        "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "path": str(p),
    }


def list_backups() -> List[Dict]:
    d = _backup_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("anonymize.sql.bak.*"), reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            "name": p.name,
            "size_bytes": st.st_size,
            "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    return out


def save_content(content: str) -> Dict:
    """Replace the script, keeping a timestamped copy of what it replaced.
    Backups exist because a bad upload is only discovered on the next clone
    creation, which may be days later and by someone else."""
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    data = content.encode("utf-8")
    if len(data) > _MAX_BYTES:
        raise ValueError(f"File too large: {len(data)} bytes (max {_MAX_BYTES})")
    if "\x00" in content:
        raise ValueError("File is not text (contains NUL bytes)")

    p = sql_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    backup_name: Optional[str] = None
    if p.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = p.with_name(f"{p.name}.bak.{ts}")
        backup.write_bytes(p.read_bytes())
        backup_name = backup.name

    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(p)

    # Prune oldest backups; the recent few are what anyone actually restores.
    backups = sorted(_backup_dir().glob("anonymize.sql.bak.*"), reverse=True)
    for old in backups[_KEEP_BACKUPS:]:
        try:
            old.unlink()
        except OSError:
            pass

    return {
        "ok": True,
        "size_bytes": len(data),
        "backup": backup_name,
        "path": str(p),
        "readiness": readiness(),
        **validate(content),
    }
