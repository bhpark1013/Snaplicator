#!/usr/bin/env python3
"""Harden a host-network PostgreSQL replica container for read-only access."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import secrets
import stat
import subprocess
import sys
import tempfile
import time


ROLE = "snaplicator_readonly"
HBA_TARGET = "/etc/postgresql/snaplicator-main-hba.conf"
HBA_CONTENT = """# Managed by harden-main-replica.py
local all all trust
host all snaplicator_readonly all scram-sha-256
host all all all reject
"""


class HardenError(RuntimeError):
    pass


def run(argv: list[str], *, stdin: str | None = None, timeout: int = 30,
        check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv, input=stdin, text=True, capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HardenError(f"Command failed: {argv[0]}") from None
    if check and result.returncode:
        raise HardenError(f"Command failed: {argv[0]}")
    return result


def inspect_container(name: str) -> dict:
    result = run(["docker", "inspect", name])
    try:
        values = json.loads(result.stdout)
        if len(values) != 1:
            raise ValueError
        return values[0]
    except (ValueError, json.JSONDecodeError, KeyError):
        raise HardenError("Docker returned invalid container metadata") from None


def parse_env(config: dict) -> tuple[list[str], dict[str, str]]:
    lines = config.get("Env") or []
    parsed: dict[str, str] = {}
    for line in lines:
        if not isinstance(line, str) or "=" not in line or "\n" in line or "\x00" in line:
            raise HardenError("Container environment cannot be preserved safely")
        key, value = line.split("=", 1)
        parsed[key] = value
    return lines, parsed


def require_supported(metadata: dict, hba_path: pathlib.Path) -> None:
    state = metadata.get("State") or {}
    host = metadata.get("HostConfig") or {}
    if not state.get("Running"):
        raise HardenError("Container is not running")
    if host.get("NetworkMode") != "host":
        raise HardenError("Container must use host networking")

    unsupported = {
        "Privileged": False,
        "ReadonlyRootfs": False,
        "AutoRemove": False,
        "CapAdd": None,
        "CapDrop": None,
        "Devices": None,
        "DeviceRequests": None,
        "Dns": None,
        "DnsOptions": None,
        "DnsSearch": None,
        "ExtraHosts": None,
        "GroupAdd": None,
        "Links": None,
        "PortBindings": None,
        "PublishAllPorts": False,
        "SecurityOpt": None,
        "Tmpfs": None,
        "Ulimits": None,
        "VolumesFrom": None,
    }
    for key, default in unsupported.items():
        value = host.get(key)
        if value not in (default, [], {}, ""):
            raise HardenError(f"Unsupported Docker option: {key}")

    resource_defaults = {
        "BlkioWeight": 0,
        "CpuPeriod": 0,
        "CpuQuota": 0,
        "CpuShares": 0,
        "CpusetCpus": "",
        "CpusetMems": "",
        "KernelMemory": 0,
        "Memory": 0,
        "MemoryReservation": 0,
        "MemorySwap": 0,
        "NanoCpus": 0,
        "PidsLimit": None,
    }
    for key, default in resource_defaults.items():
        if host.get(key, default) != default:
            raise HardenError(f"Unsupported Docker resource option: {key}")

    mounts = metadata.get("Mounts") or []
    for mount in mounts:
        if mount.get("Type") not in ("bind", "volume"):
            raise HardenError("Unsupported Docker mount type")
        if mount.get("Destination") == HBA_TARGET:
            source = pathlib.Path(mount.get("Source", "")).resolve()
            if source != hba_path or mount.get("RW", True):
                raise HardenError("Existing HBA mount does not match requested path")


def validate_paths(state_dir: pathlib.Path, hba_path: pathlib.Path) -> None:
    if not hba_path.is_absolute():
        raise HardenError("--hba-path must be absolute")
    if hba_path.is_symlink():
        raise HardenError("--hba-path must not be a symlink")
    if hba_path.exists() and not hba_path.is_file():
        raise HardenError("--hba-path must be a regular file")
    if not hba_path.parent.is_dir() or not os.access(hba_path.parent, os.W_OK):
        raise HardenError("HBA parent directory is not writable")
    if not state_dir.is_absolute():
        raise HardenError("--state-dir must be absolute")
    if state_dir.exists() and (not state_dir.is_dir() or state_dir.is_symlink()):
        raise HardenError("--state-dir must be a real directory")
    parent = state_dir if state_dir.exists() else state_dir.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        raise HardenError("State directory parent is not writable")


def psql(container: str, user: str, database: str, sql: str,
         *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run([
        "docker", "exec", "-i", container, "psql", "-X", "-qAt",
        "-v", "ON_ERROR_STOP=1", "-U", user, "-d", database,
    ], stdin=sql, timeout=30, check=check)


def preflight_database(container: str, admin: str, database: str) -> dict:
    sql = r"""
WITH unsafe AS (
  SELECT format('table %I.%I: %s', n.nspname, c.relname, x.privilege_type) item
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN LATERAL aclexplode(COALESCE(
    c.relacl,
    acldefault(CASE WHEN c.relkind = 'S' THEN 'S'::"char" ELSE 'r'::"char" END,
               c.relowner))) x
  WHERE x.grantee = 0
    AND x.privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER')
    AND n.nspname NOT IN ('pg_catalog','information_schema')
    AND n.nspname !~ '^pg_toast'
  UNION ALL
  SELECT format('schema %I: CREATE', n.nspname)
  FROM pg_namespace n
  CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) x
  WHERE x.grantee = 0 AND x.privilege_type = 'CREATE'
    AND n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_'
  UNION ALL
  SELECT format('database %I: CREATE', d.datname)
  FROM pg_database d
  CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) x
  WHERE d.datname = current_database() AND x.grantee = 0 AND x.privilege_type = 'CREATE'
), funcs AS (
  SELECT format('%I.%I(%s)', n.nspname, p.proname,
                pg_get_function_identity_arguments(p.oid)) signature
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
  WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog','information_schema')
    AND n.nspname !~ '^pg_'
), managed_role AS (
  SELECT json_build_object(
    'superuser', r.rolsuper,
    'create_db', r.rolcreatedb,
    'create_role', r.rolcreaterole,
    'replication', r.rolreplication,
    'bypass_rls', r.rolbypassrls,
    'memberships', COALESCE((
      SELECT json_agg(json_build_object('role', parent.rolname, 'admin', m.admin_option)
                      ORDER BY parent.rolname)
      FROM pg_auth_members m JOIN pg_roles parent ON parent.oid = m.roleid
      WHERE m.member = r.oid), '[]'::json)) value
  FROM pg_roles r WHERE r.rolname = 'snaplicator_readonly'
)
SELECT json_build_object(
  'unsafe_public', COALESCE((SELECT json_agg(item ORDER BY item) FROM unsafe), '[]'::json),
  'security_definer_functions', COALESCE((SELECT json_agg(signature ORDER BY signature) FROM funcs), '[]'::json),
  'managed_role', (SELECT value FROM managed_role),
  'settings', json_build_object(
    'log_connections', current_setting('log_connections'),
    'log_disconnections', current_setting('log_disconnections'),
    'log_statement', current_setting('log_statement'),
    'log_line_prefix', current_setting('log_line_prefix'))
);
"""
    result = psql(container, admin, database, sql)
    try:
        data = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        raise HardenError("Database preflight returned invalid data") from None
    unsafe = data.get("unsafe_public") or []
    if unsafe:
        raise HardenError("PUBLIC has write privileges; no changes were made")
    return data


def atomic_write(path: pathlib.Path, content: str, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def load_or_create_state(path: pathlib.Path, base: dict) -> dict:
    if path.exists():
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise HardenError("State file permissions must be 0600")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise HardenError("State file is invalid") from None
        if state.get("role") != ROLE or not state.get("password"):
            raise HardenError("State file does not match this hardening script")
        if (state.get("container") != base["container"] or
                pathlib.Path(state.get("hba_path", "")).resolve() !=
                pathlib.Path(base["hba_path"]).resolve()):
            raise HardenError("State file belongs to a different target")
        return state
    state = dict(base)
    state.update({
        "role": ROLE,
        "password": secrets.token_urlsafe(36),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    atomic_write(path, json.dumps(state, indent=2, sort_keys=True) + "\n", 0o600)
    return state


def write_hba(path: pathlib.Path) -> None:
    atomic_write(path, HBA_CONTENT, 0o644)


def apply_database_hardening(container: str, admin: str, database: str,
                             password: str) -> None:
    quoted = password.replace("'", "''")
    sql = f"""
SET log_statement = 'none';
SET password_encryption = 'scram-sha-256';
DO $body$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{ROLE}') THEN
    CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION;
  END IF;
END
$body$;
ALTER ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION
  PASSWORD '{quoted}';
GRANT pg_read_all_data TO {ROLE};
ALTER ROLE {ROLE} SET default_transaction_read_only = on;
DO $body$
DECLARE f record;
BEGIN
  FOR f IN
    SELECT p.oid::regprocedure AS function_name
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE p.prosecdef AND n.nspname NOT IN ('pg_catalog','information_schema')
      AND n.nspname !~ '^pg_'
  LOOP
    EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', f.function_name);
  END LOOP;
END
$body$;
ALTER SYSTEM SET log_connections = 'on';
ALTER SYSTEM SET log_disconnections = 'on';
ALTER SYSTEM SET log_statement = 'mod';
ALTER SYSTEM SET log_line_prefix = '%m [%p] user=%u db=%d app=%a client=%r session=%c xid=%x ';
SELECT pg_reload_conf();
"""
    psql(container, admin, database, sql)


def replacement_cmd(metadata: dict, hba_path: pathlib.Path, env_file: pathlib.Path) -> list[str]:
    config = metadata.get("Config") or {}
    host = metadata.get("HostConfig") or {}
    name = metadata.get("Name", "").lstrip("/")
    argv = ["docker", "create", "--name", name, "--network", "host",
            "--env-file", str(env_file)]

    restart = host.get("RestartPolicy") or {}
    policy = restart.get("Name") or "no"
    if policy == "on-failure" and restart.get("MaximumRetryCount"):
        policy += f":{restart['MaximumRetryCount']}"
    argv += ["--restart", policy]
    if host.get("ShmSize"):
        argv += ["--shm-size", str(host["ShmSize"])]
    log_config = host.get("LogConfig") or {}
    if log_config.get("Type"):
        argv += ["--log-driver", log_config["Type"]]
        for key, value in sorted((log_config.get("Config") or {}).items()):
            argv += ["--log-opt", f"{key}={value}"]
    for bind in host.get("Binds") or []:
        destination = bind.split(":", 2)[1] if ":" in bind else ""
        if destination != HBA_TARGET:
            argv += ["--volume", bind]
    argv += ["--volume", f"{hba_path}:{HBA_TARGET}:ro"]
    for key, value in sorted((config.get("Labels") or {}).items()):
        argv += ["--label", f"{key}={value}"]
    if config.get("User"):
        argv += ["--user", config["User"]]
    if config.get("WorkingDir"):
        argv += ["--workdir", config["WorkingDir"]]
    if config.get("Hostname"):
        argv += ["--hostname", config["Hostname"]]
    if config.get("Domainname"):
        argv += ["--domainname", config["Domainname"]]
    if config.get("StopSignal"):
        argv += ["--stop-signal", config["StopSignal"]]

    entrypoint = config.get("Entrypoint") or []
    if len(entrypoint) > 1:
        raise HardenError("Multi-part Docker entrypoint cannot be preserved safely")
    if entrypoint:
        argv += ["--entrypoint", entrypoint[0]]
    cmd = list(config.get("Cmd") or [])
    replaced = False
    for index in range(len(cmd) - 1):
        if cmd[index] == "-c" and cmd[index + 1].startswith("hba_file="):
            cmd[index + 1] = f"hba_file={HBA_TARGET}"
            replaced = True
    if not replaced:
        cmd += ["-c", f"hba_file={HBA_TARGET}"]
    argv += [metadata["Image"]] + cmd
    return argv


def wait_ready(container: str, admin: str, database: str) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = run(["docker", "exec", container, "pg_isready", "-U", admin,
                      "-d", database], timeout=5, check=False)
        if result.returncode == 0:
            return
        time.sleep(1)
    raise HardenError("Replacement container did not become ready")


def tcp_psql(container: str, user: str, database: str, password: str,
             sql: str, *, check: bool) -> subprocess.CompletedProcess[str]:
    shell = "IFS= read -r PGPASSWORD; export PGPASSWORD; exec psql -X -qAt -v ON_ERROR_STOP=1 -h 127.0.0.1 -U \"$1\" -d \"$2\" -c \"$3\""
    return run(["docker", "exec", "-i", container, "sh", "-c", shell, "sh",
                user, database, sql], stdin=password + "\n", timeout=15, check=check)


def verify_access(container: str, admin: str, database: str, env: dict[str, str],
                  password: str) -> None:
    result = tcp_psql(
        container, ROLE, database, password,
        "SELECT current_user, current_setting('transaction_read_only')",
        check=True,
    )
    if result.stdout.strip() != f"{ROLE}|on":
        raise HardenError("Read-only login verification failed")
    denied = tcp_psql(
        container, ROLE, database, password,
        "CREATE TEMP TABLE snaplicator_write_test(id integer)", check=False,
    )
    if denied.returncode == 0:
        raise HardenError("Read-only role unexpectedly accepted a write")
    admin_password = env.get("POSTGRES_PASSWORD")
    if admin_password:
        denied_admin = tcp_psql(
            container, admin, database, admin_password, "SELECT 1", check=False
        )
        if denied_admin.returncode == 0:
            raise HardenError("Administrative TCP login was not rejected")


def recreate(metadata: dict, hba_path: pathlib.Path, env_lines: list[str],
             admin: str, database: str, env: dict[str, str], password: str) -> str:
    name = metadata["Name"].lstrip("/")
    suffix = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    # The manager treats '<main-name>-*' as writable clones. Keep rollback
    # containers outside that namespace: they mount the live main data.
    old_name = f"access-rollback-{name}-{suffix}"
    fd, env_name = tempfile.mkstemp(prefix="snaplicator-env-")
    env_path = pathlib.Path(env_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(env_lines) + "\n")
        create_argv = replacement_cmd(metadata, hba_path, env_path)
        run(["docker", "stop", "--time", "30", name], timeout=45)
        run(["docker", "rename", name, old_name])
        try:
            run(create_argv, timeout=30)
            run(["docker", "start", name], timeout=30)
            wait_ready(name, admin, database)
            verify_access(name, admin, database, env, password)
        except HardenError:
            run(["docker", "rm", "-f", name], timeout=30, check=False)
            run(["docker", "rename", old_name, name], timeout=30, check=False)
            run(["docker", "start", name], timeout=30, check=False)
            raise HardenError("Replacement failed; original container was restored") from None
        return old_name
    finally:
        try:
            env_path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--state-dir", required=True, type=pathlib.Path)
    parser.add_argument("--hba-path", required=True, type=pathlib.Path)
    parser.add_argument("--check", action="store_true", help="run preflight checks only")
    args = parser.parse_args()

    state_dir = args.state_dir.expanduser()
    hba_path = args.hba_path.expanduser()
    validate_paths(state_dir, hba_path)
    metadata = inspect_container(args.container)
    require_supported(metadata, hba_path.resolve())
    env_lines, env = parse_env(metadata.get("Config") or {})
    admin = env.get("POSTGRES_USER", "postgres")
    database = env.get("POSTGRES_DB", admin)
    preflight = preflight_database(args.container, admin, database)
    state_path = state_dir / "main-readonly.json"
    managed_role = preflight.get("managed_role")
    if managed_role:
        unsafe_role = any(managed_role.get(key) for key in
                          ("superuser", "create_db", "create_role", "replication", "bypass_rls"))
        unsafe_membership = any(
            membership.get("role") != "pg_read_all_data" or membership.get("admin")
            for membership in managed_role.get("memberships", [])
        )
        if unsafe_role or unsafe_membership:
            raise HardenError("Existing read-only role has unsafe attributes or memberships")
        if not state_path.exists():
            raise HardenError("Existing read-only role is not managed by this state directory")
    if args.check:
        print("Preflight checks passed. No changes were made.")
        return 0

    state_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
    os.chmod(state_dir, 0o700)
    state = load_or_create_state(state_path, {
        "container": args.container,
        "hba_path": str(hba_path.resolve()),
        "previous_settings": preflight["settings"],
        "revoked_public_execute": preflight["security_definer_functions"],
        "rollback": [
            "Stop and remove the replacement container.",
            "Rename the container recorded in rollback_container to the original name.",
            "Start the restored container.",
            "Restore previous_settings with ALTER SYSTEM if log settings must be reverted.",
            "Grant EXECUTE to PUBLIC only for reviewed functions in revoked_public_execute.",
        ],
    })
    write_hba(hba_path)
    apply_database_hardening(args.container, admin, database, state["password"])
    old_name = recreate(metadata, hba_path.resolve(), env_lines, admin, database,
                        env, state["password"])
    state["rollback_container"] = old_name
    state["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic_write(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n", 0o600)
    print("Replica access hardening completed.")
    print(f"Rollback container: {old_name}")
    print(f"State file: {state_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HardenError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
