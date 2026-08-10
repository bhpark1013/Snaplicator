"""The FDW login, settable at runtime instead of only in .env.

.env is mounted into this container read-only — deliberately, since it is the
one file the installer, compose and the bootstrap script all read, and a
process that can rewrite it can rewrite where the replica points. So a login
given through the UI is kept beside the other state this manager owns, in the
volume that already survives rebuilds, and .env keeps its say: an FDW_USER
set there wins, because someone who edited the file meant it.

The file holds a password, so it is written 0600 and never returned by any
endpoint — the API answers whether a login is configured, not what it is.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

from ..core.config import settings


def _path() -> Path:
    d = Path.home() / ".snaplicator"
    d.mkdir(parents=True, exist_ok=True)
    return d / "fdw_credentials.json"


def stored() -> Dict:
    try:
        return json.loads(_path().read_text())
    except Exception:
        return {}


def save(user: str, password: str, host: Optional[str] = None,
         port: Optional[int] = None, dbname: Optional[str] = None) -> None:
    data = {"user": user, "password": password}
    if host:
        data["host"] = host
    if port:
        data["port"] = int(port)
    if dbname:
        data["dbname"] = dbname
    p = _path()
    # Create with the mode already restricted rather than widening then
    # narrowing: between those two steps the password would be readable.
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)


def clear() -> None:
    try:
        _path().unlink()
    except FileNotFoundError:
        pass


def user() -> Optional[str]:
    return settings.fdw_user or stored().get("user") or None


def password() -> Optional[str]:
    return settings.fdw_password or stored().get("password") or None


def host() -> Optional[str]:
    return settings.effective_fdw_host() or stored().get("host") or settings.primary_host


def port() -> Optional[int]:
    return settings.effective_fdw_port() or stored().get("port") or settings.primary_port


def dbname() -> Optional[str]:
    return settings.effective_fdw_db() or stored().get("dbname") or settings.primary_db


def configured() -> bool:
    return bool(user() and password())


def source() -> str:
    if settings.fdw_user and settings.fdw_password:
        return "env"
    if stored().get("user"):
        return "ui"
    return "none"


def check(user_: str, password_: str, host_: str, port_: int, db_: str) -> Optional[str]:
    """Connect to the primary as this login. Returns an error message, or None.

    Checked here rather than trusted, because the alternative place to find out
    is inside the replica after CREATE USER MAPPING, where the failure arrives
    as a foreign table that errors on every query — a login that was never
    going to work should not become part of the replica's catalog.
    """
    import subprocess

    uri = f"postgresql://{user_}:{password_}@{host_}:{port_}/{db_}"
    try:
        proc = subprocess.run(
            ["psql", uri, "-At", "-c", "SELECT 1"],
            capture_output=True, text=True, timeout=20,
            env={**os.environ, "PGCONNECT_TIMEOUT": "10"},
        )
    except Exception as e:
        return str(e)
    if proc.returncode != 0:
        return (proc.stderr or "connection failed").strip().splitlines()[-1]
    return None
