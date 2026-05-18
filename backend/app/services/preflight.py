"""Pre-launch environment doctor.

Checks every prerequisite needed before `make replica` succeeds and returns a
red/green checklist with copy-paste fixes. Designed to run *before* the server
or replica container exist, so it must never import app.core.config at module
load (that raises if configs/.env is incomplete) — it parses configs/.env
directly instead.

Usable three ways:
  - GET /setup/preflight            (web wizard / API)
  - make doctor                     (python -m app.services.preflight)
  - import + call run_preflight()
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import dotenv_values
except Exception:  # pragma: no cover
    dotenv_values = None  # type: ignore

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolve_env_path() -> Path:
    """configs/.env by default; honor ENV_FILE override (abs or repo-relative)
    so `ENV_FILE=... make doctor` and the setup wizard check the right file."""
    override = os.environ.get("ENV_FILE")
    if override:
        op = Path(override)
        return op if op.is_absolute() else _REPO_ROOT / op
    return _REPO_ROOT / "configs" / ".env"


_ENV_PATH = _resolve_env_path()

# Canonical required keys (mirrors scripts/run-replica-postgres.sh `:?` guards)
REQUIRED_ENV = [
    "CONTAINER_NAME", "NETWORK_NAME", "HOST_PORT",
    "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
    "PRIMARY_HOST", "PRIMARY_PORT", "PRIMARY_DB",
    "PRIMARY_USER", "PRIMARY_PASSWORD",
    "SUBSCRIPTION_NAME", "PUBLICATION_NAME",
    "ROOT_DATA_DIR", "MAIN_DATA_DIR",
]

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


def _check(checks: List[Dict[str, Any]], cid: str, title: str,
           status: str, detail: str = "", fix: str = "") -> None:
    checks.append({"id": cid, "title": title, "status": status,
                   "detail": detail, "fix": fix})


def _run(cmd: List[str], timeout: int = 8) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


def _load_env() -> Dict[str, str]:
    if not _ENV_PATH.exists():
        return {}
    if dotenv_values is not None:
        vals = dotenv_values(str(_ENV_PATH))
        return {k: v for k, v in vals.items() if v is not None}
    # Fallback parser: python-dotenv may be absent on a bare
    # machine, but `make doctor` must still diagnose it.
    out: Dict[str, str] = {}
    for raw in _ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def _publisher_connstr(env: Dict[str, str]) -> Optional[str]:
    need = ("PRIMARY_HOST", "PRIMARY_PORT", "PRIMARY_DB", "PRIMARY_USER")
    if not all(env.get(k) for k in need):
        return None
    sslmode = env.get("PGSSLMODE") or "prefer"
    parts = [
        f"host={env['PRIMARY_HOST']}",
        f"port={env['PRIMARY_PORT']}",
        f"dbname={env['PRIMARY_DB']}",
        f"user={env['PRIMARY_USER']}",
    ]
    if env.get("PRIMARY_PASSWORD"):
        parts.append(f"password={env['PRIMARY_PASSWORD']}")
    parts.append(f"sslmode={sslmode}")
    parts.append("connect_timeout=8")
    return " ".join(parts)


def run_preflight(deep: bool = True) -> Dict[str, Any]:
    """Run all checks. `deep=False` skips network calls to the publisher."""
    checks: List[Dict[str, Any]] = []
    env = _load_env()

    # 1. configs/.env presence + required keys
    if not _ENV_PATH.exists():
        _check(checks, "env_file", "configs/.env exists", FAIL,
               f"{_ENV_PATH} not found",
               "cp configs/.env.example configs/.env  # then fill it in")
    else:
        missing = [k for k in REQUIRED_ENV if not env.get(k)]
        if missing:
            _check(checks, "env_file", "configs/.env required keys", FAIL,
                   f"missing/empty: {', '.join(missing)}",
                   "Fill these in configs/.env (see configs/.env.example)")
        else:
            _check(checks, "env_file", "configs/.env required keys", OK,
                   f"all {len(REQUIRED_ENV)} required keys present")

    # 2. Docker daemon
    if shutil.which("docker") is None:
        _check(checks, "docker", "Docker available", FAIL,
               "docker not on PATH",
               "Install Docker and ensure your user can run it")
    else:
        p = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
        if p.returncode == 0:
            _check(checks, "docker", "Docker daemon", OK,
                   f"server {p.stdout.strip()}")
        else:
            _check(checks, "docker", "Docker daemon", FAIL,
                   (p.stderr or "docker info failed").strip()[:200],
                   "Start Docker / add your user to the docker group")

    # 3. psql client on host (publisher ops shell out to host psql)
    if shutil.which("psql") is None:
        _check(checks, "psql", "psql client on host", FAIL,
               "psql not on PATH — backend runs publisher SQL via host psql",
               "Install postgresql-client (apt install postgresql-client)")
    else:
        p = _run(["psql", "--version"])
        _check(checks, "psql", "psql client on host", OK, p.stdout.strip())

    # 4. btrfs tooling
    if shutil.which("btrfs") is None:
        _check(checks, "btrfs_bin", "btrfs-progs installed", FAIL,
               "btrfs not on PATH",
               "Install btrfs-progs (apt install btrfs-progs)")
    else:
        _check(checks, "btrfs_bin", "btrfs-progs installed", OK)

    # 5. ROOT_DATA_DIR on btrfs
    root = env.get("ROOT_DATA_DIR")
    if not root:
        _check(checks, "btrfs_mount", "ROOT_DATA_DIR on btrfs", SKIP,
               "ROOT_DATA_DIR not set")
    else:
        fstype = ""
        if shutil.which("findmnt"):
            fp = _run(["findmnt", "-no", "FSTYPE", "-T", root])
            fstype = (fp.stdout or "").strip()
        if fstype == "btrfs":
            _check(checks, "btrfs_mount", "ROOT_DATA_DIR on btrfs", OK,
                   f"{root} is btrfs")
        elif not Path(root).exists():
            _check(checks, "btrfs_mount", "ROOT_DATA_DIR on btrfs", WARN,
                   f"{root} does not exist yet",
                   "`make replica` can provision an LVM-backed btrfs volume")
        else:
            _check(checks, "btrfs_mount", "ROOT_DATA_DIR on btrfs", WARN,
                   f"{root} fstype='{fstype or 'unknown'}' (not btrfs) — "
                   "snapshots/clones unavailable",
                   "`make replica` can convert it to LVM+btrfs (interactive)")

    # 6. passwordless sudo (backend uses `sudo -n`)
    p = _run(["sudo", "-n", "true"])
    if p.returncode == 0:
        _check(checks, "sudo", "passwordless sudo (sudo -n)", OK)
    else:
        _check(checks, "sudo", "passwordless sudo (sudo -n)", FAIL,
               "sudo -n failed — btrfs/chown/mount/mkdir/mv need it",
               "Add a sudoers drop-in granting NOPASSWD for "
               "btrfs,chown,chmod,mkdir,mv,mount(+LVM tools)")

    # 7. HOST_PORT availability
    hp = env.get("HOST_PORT")
    if not hp:
        _check(checks, "host_port", "HOST_PORT free", SKIP, "HOST_PORT not set")
    else:
        try:
            port = int(hp)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            in_use = s.connect_ex(("127.0.0.1", port)) == 0
            s.close()
            if in_use:
                _check(checks, "host_port", f"HOST_PORT {port} free", WARN,
                       "something is already listening "
                       "(maybe an existing replica — that may be fine)")
            else:
                _check(checks, "host_port", f"HOST_PORT {port} free", OK)
        except ValueError:
            _check(checks, "host_port", "HOST_PORT free", FAIL,
                   f"HOST_PORT='{hp}' is not an integer")

    # 8. Publisher reachable + wal_level + publication (deep, needs network)
    connstr = _publisher_connstr(env)
    if not deep:
        _check(checks, "publisher", "Publisher preflight", SKIP,
               "deep checks disabled")
    elif connstr is None:
        _check(checks, "publisher", "Publisher reachable", SKIP,
               "PRIMARY_* not fully set")
    elif shutil.which("psql") is None:
        _check(checks, "publisher", "Publisher reachable", SKIP,
               "psql not installed (see psql check)")
    else:
        pub = env.get("PUBLICATION_NAME", "")
        sql = (
            "SELECT current_setting('wal_level'), "
            "(SELECT count(*) FROM pg_publication WHERE pubname="
            f"'{pub}'), "
            "current_setting('max_replication_slots'), "
            "(SELECT count(*) FROM pg_replication_slots);"
        )
        p = _run(["psql", connstr, "-tAF,", "-c", sql], timeout=12)
        if p.returncode != 0:
            _check(checks, "publisher", "Publisher reachable", FAIL,
                   (p.stderr or "connection failed").strip()[:240],
                   "Check PRIMARY_* host/port/credentials, network/SSL, "
                   "and that the replication role can log in")
        else:
            row = (p.stdout or "").strip().split(",")
            wal = row[0] if len(row) > 0 else "?"
            pub_cnt = row[1] if len(row) > 1 else "0"
            max_slots = row[2] if len(row) > 2 else "?"
            used_slots = row[3] if len(row) > 3 else "?"
            _check(checks, "publisher", "Publisher reachable", OK,
                   f"connected; wal_level={wal}, slots {used_slots}/{max_slots}")
            if wal != "logical":
                _check(checks, "wal_level", "publisher wal_level=logical",
                       FAIL, f"wal_level='{wal}'",
                       "ALTER SYSTEM SET wal_level='logical';  "
                       "-- then restart the primary")
            else:
                _check(checks, "wal_level", "publisher wal_level=logical", OK)
            if pub_cnt.strip() in ("0", ""):
                _check(checks, "publication",
                       f"publication '{pub}' exists", FAIL,
                       "not found on publisher",
                       f"CREATE PUBLICATION {pub or '<name>'} "
                       "FOR TABLES IN SCHEMA public;")
            else:
                _check(checks, "publication",
                       f"publication '{pub}' exists", OK)

    # 9. FDW (optional) — only if configured
    if env.get("FDW_USER") and env.get("FDW_PASSWORD"):
        fhost = env.get("FDW_HOST") or env.get("PRIMARY_HOST")
        fport = env.get("FDW_PORT") or env.get("PRIMARY_PORT")
        fdb = env.get("FDW_DB") or env.get("PRIMARY_DB")
        if deep and connstr is not None and shutil.which("psql") and fhost:
            fc = (f"host={fhost} port={fport} dbname={fdb} "
                  f"user={env['FDW_USER']} password={env['FDW_PASSWORD']} "
                  f"sslmode={env.get('PGSSLMODE') or 'require'} "
                  "connect_timeout=8")
            p = _run(["psql", fc, "-tAc", "SELECT 1"], timeout=12)
            if p.returncode == 0:
                _check(checks, "fdw", "FDW role can connect", OK,
                       f"{fhost}:{fport}/{fdb}")
            else:
                _check(checks, "fdw", "FDW role can connect", FAIL,
                       (p.stderr or "").strip()[:200],
                       "Check FDW_* credentials / read-only role grants")
        else:
            _check(checks, "fdw", "FDW configured", SKIP,
                   "credentials set; deep check unavailable")
    else:
        _check(checks, "fdw", "FDW (optional)", SKIP,
               "FDW_USER/FDW_PASSWORD blank — FDW disabled")

    counts = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for c in checks:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    overall = FAIL if counts[FAIL] else (WARN if counts[WARN] else OK)
    return {
        "overall": overall,
        "summary": counts,
        "ready": counts[FAIL] == 0,
        "checks": checks,
        "env_file": str(_ENV_PATH),
    }


def _print_report(report: Dict[str, Any]) -> int:
    use_color = os.environ.get("NO_COLOR") is None
    def c(code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if use_color else s
    sym = {OK: c("32", "✔"), WARN: c("33", "▲"),
           FAIL: c("31", "✘"), SKIP: c("90", "–")}
    print(c("1", "\nSnaplicator preflight\n" + "=" * 40))
    for ch in report["checks"]:
        line = f"  {sym.get(ch['status'], '?')} {ch['title']}"
        if ch["detail"]:
            line += c("90", f"  — {ch['detail']}")
        print(line)
        if ch["status"] in (FAIL, WARN) and ch["fix"]:
            print(c("36", f"      fix: {ch['fix']}"))
    s = report["summary"]
    print("\n" + "=" * 40)
    print(f"  ok={s.get(OK,0)}  warn={s.get(WARN,0)}  "
          f"fail={s.get(FAIL,0)}  skip={s.get(SKIP,0)}")
    if report["ready"]:
        print(c("32", "\n  READY — no blocking failures. You can `make replica`.\n"))
        return 0
    print(c("31", "\n  NOT READY — fix the ✘ items above, then re-run `make doctor`.\n"))
    return 1


if __name__ == "__main__":
    import sys
    deep = "--no-deep" not in sys.argv
    sys.exit(_print_report(run_preflight(deep=deep)))
