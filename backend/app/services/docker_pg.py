from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple
import time
import json


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def _is_btrfs_subvolume(path: Path) -> bool:
    try:
        _run(["sudo", "-n", "btrfs", "subvolume", "show", str(path)])
        return True
    except subprocess.CalledProcessError:
        return False


_TIMING_LOG_PATH = Path(__file__).resolve().parents[3] / "timing.log"


def _timing_log(message: str) -> None:
    try:
        print(message, flush=True)
    except Exception:
        pass
    try:
        with open(_TIMING_LOG_PATH, "a", encoding="utf-8") as f:
            ts = datetime.now().isoformat()
            f.write(f"{ts} {message}\n")
    except Exception:
        pass


def _detect_postgres_uid_gid(image: str) -> tuple[int, int]:
    """Detect numeric uid:gid of the postgres user inside the given image.

    Fallback to 999:999 if detection fails.
    """
    try:
        # Try using sh with id; compatible with most distros (alpine/debian)
        proc = subprocess.run(
            [
                "docker", "run", "--rm", "--entrypoint", "sh", image,
                "-c", "id -u postgres; id -g postgres",
            ],
            check=True, text=True, capture_output=True,
        )
        lines = [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]
        uid = int(lines[0]) if len(lines) >= 1 else 999
        gid = int(lines[1]) if len(lines) >= 2 else uid
        return uid, gid
    except Exception:
        return 999, 999


def _pgdata_env_for_clone_path(clone_path: Path) -> str:
    # Use sudo test to avoid permission issues on files owned by uid 999
    try:
        subprocess.run(["sudo", "test", "-f", str(clone_path / "PG_VERSION")], check=True)
        return "/var/lib/postgresql/data"
    except subprocess.CalledProcessError:
        pass
    try:
        subprocess.run(["sudo", "test", "-f", str(clone_path / "pgdata" / "PG_VERSION")], check=True)
        return "/var/lib/postgresql/data/pgdata"
    except subprocess.CalledProcessError:
        raise RuntimeError(
            f"Could not determine PGDATA inside snapshot. Neither PG_VERSION nor pgdata/PG_VERSION found in {clone_path}"
        )


def _find_free_port(start_port: int, attempts: int = 1000) -> int:
    port = start_port
    for _ in range(attempts):
        # Check with ss -ltn for any listener on the port (any address)
        out = subprocess.run(["ss", "-ltn"], text=True, capture_output=True, check=True).stdout
        if f":{port} " in out:
            port += 1
            continue
        return port
    raise RuntimeError(f"Failed to find a free port starting from {start_port}")


def _find_container_mounting_path(host_path: Path) -> Optional[str]:
    """Return the name of a running container that mounts host_path at /var/lib/postgresql/data*.

    If multiple containers match, return the first.
    """
    try:
        ids_out = subprocess.run(["docker", "ps", "-q"], check=True, text=True, capture_output=True).stdout
        container_ids = [line.strip() for line in ids_out.splitlines() if line.strip()]
    except subprocess.CalledProcessError:
        return None
    for cid in container_ids:
        try:
            ins = subprocess.run(["docker", "inspect", "--format", "{{.Name}}\t{{json .Mounts}}", cid], check=True, text=True, capture_output=True).stdout.strip()
            if not ins:
                continue
            name_raw, mounts_json = (ins.split("\t") + [""])[:2]
            cname = name_raw.lstrip('/')
            mounts = []
            try:
                mounts = json.loads(mounts_json) or []
            except Exception:
                mounts = []
            for m in mounts:
                dest = m.get("Destination", "")
                src = m.get("Source", "")
                if not dest.startswith("/var/lib/postgresql/data") or not src:
                    continue
                try:
                    resolved = str(Path(src).resolve())
                except Exception:
                    resolved = src
                if resolved == str(host_path.resolve()):
                    return cname
        except subprocess.CalledProcessError:
            continue
    return None


def _force_checkpoint_on_container(container_name: str, user: str, db: str) -> None:
    """Force CHECKPOINT (and switch WAL) inside the given Postgres container.

    Best-effort: ignore failures but surface via timing logs.
    """
    t0 = time.monotonic()
    try:
        # Switch WAL to ensure current WAL segment is closed, then CHECKPOINT
        subprocess.run([
            "docker", "exec", container_name,
            "psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db,
            "-c", "SELECT pg_switch_wal();",
        ], check=True, text=True, capture_output=True)
        subprocess.run([
            "docker", "exec", container_name,
            "psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db,
            "-c", "CHECKPOINT;",
        ], check=True, text=True, capture_output=True)
        t1 = time.monotonic()
        _timing_log(f"[CLONE_TIMING] pre_checkpoint_ms={int((t1-t0)*1000)} container={container_name}")
    except subprocess.CalledProcessError as e:
        t1 = time.monotonic()
        _timing_log(f"[CLONE_TIMING] pre_checkpoint_failed_ms={int((t1-t0)*1000)} container={container_name} err={str(e).strip()}")


def _ensure_docker_network(network_name: str) -> None:
    try:
        tn0 = time.monotonic()
        out = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"], check=True, text=True, capture_output=True).stdout
        nets = set(line.strip() for line in out.splitlines())
        if network_name not in nets:
            subprocess.run(["docker", "network", "create", network_name], check=True)
        tn1 = time.monotonic()
        _timing_log(f"[CLONE_TIMING] docker_network_prepare_ms={int((tn1-tn0)*1000)} network={network_name}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to ensure docker network: {e}")


_SEQUENCE_SYNC_SQL = """DO $$
DECLARE
  r RECORD;
  v_max       bigint;
  v_start     bigint;
  seq_oqname  text;
BEGIN
  FOR r IN
    SELECT
      ns.nspname        AS table_schema,
      tbl.relname       AS table_name,
      col.attname       AS column_name,
      seq_ns.nspname    AS seq_schema,
      seq.relname       AS seq_name,
      seq.oid           AS seq_oid
    FROM pg_class seq
    JOIN pg_namespace seq_ns ON seq_ns.oid = seq.relnamespace
    JOIN pg_depend dep       ON dep.objid = seq.oid AND dep.deptype = 'a'
    JOIN pg_class tbl        ON tbl.oid = dep.refobjid AND tbl.relkind IN ('r','p')
    JOIN pg_namespace ns     ON ns.oid = tbl.relnamespace
    JOIN pg_attribute col    ON col.attrelid = tbl.oid AND col.attnum = dep.refobjsubid AND NOT col.attisdropped
    WHERE seq.relkind = 'S'
  LOOP
    seq_oqname := format('%I.%I', r.seq_schema, r.seq_name);
    EXECUTE format('SELECT max(%I) FROM %I.%I', r.column_name, r.table_schema, r.table_name)
      INTO v_max;
    SELECT s.start_value
      INTO v_start
      FROM pg_sequences s
     WHERE s.schemaname  = r.seq_schema
       AND s.sequencename = r.seq_name;
    IF v_max IS NULL THEN
      PERFORM setval(seq_oqname, v_start, true);
    ELSE
      PERFORM setval(seq_oqname, v_max, true);
    END IF;
  END LOOP;
END$$;"""


def _sync_owned_sequences(container_name: str, user: str, db: str) -> None:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [
                "docker", "exec", container_name,
                "psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db,
                "-c", _SEQUENCE_SYNC_SQL,
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        t1 = time.monotonic()
        _timing_log(f"[CLONE_TIMING] sequence_sync_ms={int((t1-t0)*1000)} container={container_name}")
        stdout = (proc.stdout or "").strip()
        if stdout:
            _timing_log(f"[CLONE_TIMING] sequence_sync_stdout container={container_name} out={stdout}")
    except subprocess.CalledProcessError as e:
        t1 = time.monotonic()
        stderr = (e.stderr or e.stdout or "").strip()
        _timing_log(f"[CLONE_TIMING] sequence_sync_failed_ms={int((t1-t0)*1000)} container={container_name} err={stderr}")
        raise RuntimeError(f"Sequence synchronization failed: {stderr}") from e


@dataclass
class CloneOptions:
    root_data_dir: str
    main_data_dir: str
    snapshot_name: str
    container_name: str
    network_name: str
    host_port: int
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_image: str = "postgres:17"
    description: Optional[str] = None


def _launch_clone_container(
    clone_path: Path,
    opts: CloneOptions,
    container_name: str,
    host_port_hint: Optional[int],
    description: Optional[str],
    remove_existing: bool = True,
) -> Tuple[int, str, bool, Optional[str]]:
    container_pgdata = _pgdata_env_for_clone_path(clone_path)

    if remove_existing:
        subprocess.run(["docker", "rm", "-f", container_name], check=False, capture_output=True)

    _ensure_docker_network(opts.network_name)

    host_port = int(host_port_hint) if host_port_hint is not None else _find_free_port(int(opts.host_port))

    labels = [
        "--label", "snaplicator=1",
        "--label", f"snaplicator.role=clone",
        "--label", f"snaplicator.main={opts.main_data_dir}",
    ]
    if description is not None:
        labels.extend(["--label", f"snaplicator.description={description}"])

    envs = [
        "-e", f"POSTGRES_USER={opts.postgres_user}",
        "-e", f"POSTGRES_PASSWORD={opts.postgres_password}",
        "-e", f"POSTGRES_DB={opts.postgres_db}",
        "-e", f"PGDATA={container_pgdata}",
    ]

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--network", opts.network_name,
        "-p", f"{host_port}:5432",
        *labels,
        *envs,
        "-v", f"{str(clone_path)}:/var/lib/postgresql/data",
        opts.postgres_image,
        "-c", "max_logical_replication_workers=0",
    ]

    anonymize_ran = False
    anonymize_output: Optional[str] = None

    tr0 = time.monotonic()
    subprocess.run(cmd, check=True)
    tr1 = time.monotonic()
    _timing_log(f"[CLONE_TIMING] docker_run_ms={int((tr1-tr0)*1000)} container={container_name} port={host_port}")

    tw0 = time.monotonic()
    for _ in range(60):
        ready = subprocess.run(
            [
                "docker", "exec", container_name,
                "pg_isready", "-U", opts.postgres_user, "-d", opts.postgres_db,
            ],
            capture_output=True, text=True,
        )
        if ready.returncode == 0:
            break
        time.sleep(1)
    for _ in range(10):
        st = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container_name], capture_output=True, text=True)
        ok = st.returncode == 0 and st.stdout.strip() == "true"
        ex = subprocess.run(["docker", "exec", container_name, "sh", "-c", "true"], capture_output=True, text=True)
        if ok and ex.returncode == 0:
            break
        time.sleep(1)
    tw1 = time.monotonic()
    _timing_log(f"[CLONE_TIMING] container_ready_wait_ms={int((tw1-tw0)*1000)} container={container_name}")

    subs_proc = subprocess.run(
        [
            "docker", "exec", container_name,
            "psql", "-U", opts.postgres_user, "-d", opts.postgres_db, "-tAc",
            "SELECT subname FROM pg_subscription",
        ],
        capture_output=True, text=True,
    )
    subs_out = subs_proc.stdout.strip()
    if subs_out:
        for sub in subs_out.splitlines():
            sub = sub.strip()
            if not sub:
                continue
            subprocess.run(
                [
                    "docker", "exec", container_name,
                    "psql", "-v", "ON_ERROR_STOP=1", "-U", opts.postgres_user, "-d", opts.postgres_db,
                    "-c", f"ALTER SUBSCRIPTION \"{sub}\" DISABLE;",
                ],
                check=False,
            )

    try:
        _sync_owned_sequences(container_name, opts.postgres_user, opts.postgres_db)
    except Exception:
        subprocess.run(["docker", "rm", "-f", container_name], check=False, capture_output=True)
        raise

    repo_root = str(Path(__file__).resolve().parents[3])
    anon_file = Path(repo_root) / "configs/anonymize.sql"
    if anon_file.exists():
        _timing_log(f"[CLONE_TIMING] anonymize_start file={anon_file}")
        ta0 = time.monotonic()
        copy_ok = False
        for _ in range(5):
            cp = subprocess.run(["docker", "cp", str(anon_file), f"{container_name}:/tmp/anonymize.sql"], capture_output=True, text=True)
            if cp.returncode == 0:
                copy_ok = True
                break
            time.sleep(1)
        if not copy_ok:
            subprocess.run(["docker", "rm", "-f", container_name], check=False, capture_output=True)
            raise RuntimeError("Anonymization setup failed: unable to copy anonymize.sql into container")

        exec_ok = False
        last_err = ""
        last_out = ""
        for _ in range(5):
            run_anon = subprocess.run(
                [
                    "docker", "exec", container_name,
                    "psql", "-v", "ON_ERROR_STOP=1", "-U", opts.postgres_user, "-d", opts.postgres_db,
                    "-f", "/tmp/anonymize.sql",
                ],
                capture_output=True, text=True,
            )
            if run_anon.returncode == 0:
                exec_ok = True
                anonymize_ran = True
                last_out = (run_anon.stdout or "").strip()
                break
            last_err = (run_anon.stderr or run_anon.stdout or "").strip()
            time.sleep(1)
        if not exec_ok:
            subprocess.run(["docker", "rm", "-f", container_name], check=False, capture_output=True)
            raise RuntimeError(f"Anonymization failed: {last_err}")
        anonymize_output = last_out
        ta1 = time.monotonic()
        _timing_log(f"[CLONE_TIMING] anonymization_ms={int((ta1-ta0)*1000)} container={container_name}")

    return host_port, container_pgdata, anonymize_ran, anonymize_output


def clone_from_snapshot_and_run(opts: CloneOptions) -> Dict:
    root = Path(opts.root_data_dir)
    snap_path = root / opts.snapshot_name
    if not snap_path.exists() or not _is_btrfs_subvolume(snap_path):
        raise FileNotFoundError(f"Snapshot not found or not a subvolume: {snap_path}")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    clone_name = f"{opts.main_data_dir}-clone-{ts}"
    clone_path = root / clone_name

    t0 = time.monotonic()
    _run(["sudo", "-n", "btrfs", "subvolume", "snapshot", str(snap_path), str(clone_path)])
    t1 = time.monotonic()
    _timing_log(f"[CLONE_TIMING] btrfs_snapshot_ms={int((t1-t0)*1000)} source={snap_path} target={clone_path}")

    uid, gid = _detect_postgres_uid_gid(opts.postgres_image)
    _run(["sudo", "-n", "chown", "-R", f"{uid}:{gid}", str(clone_path)])
    _run(["sudo", "-n", "chmod", "-R", "u+rwX,go-rwx", str(clone_path)])

    meta = {
        "name": clone_name,
        "path": str(clone_path),
        "source_snapshot": str(snap_path),
        "root_data_dir": str(root),
        "main_data_dir": opts.main_data_dir,
        "created_at": datetime.now().isoformat(),
        "created_by": "snaplicator-api",
        "description": opts.description,
    }
    meta_json = json.dumps(meta, ensure_ascii=False)
    meta_path = clone_path / ".snaplicator.json"
    try:
        _run(["sudo", "-n", "bash", "-lc", f"cat > {meta_path!s} <<'EOF'\n{meta_json}\nEOF\n"])
    except subprocess.CalledProcessError:
        pass
    try:
        _run(["sudo", "-n", "setfattr", "-n", "user.snaplicator", "-v", meta_json, str(clone_path)])
    except subprocess.CalledProcessError:
        pass

    container_name = f"{opts.container_name}-{ts}"
    host_port, container_pgdata, anonymize_ran, anonymize_output = _launch_clone_container(
        clone_path=clone_path,
        opts=opts,
        container_name=container_name,
        host_port_hint=None,
        description=opts.description,
    )

    return {
        "snapshot": str(snap_path),
        "clone_subvolume": str(clone_path),
        "container_name": container_name,
        "host_port": host_port,
        "pgdata": container_pgdata,
        "metadata_path": str(meta_path),
        "anonymize_ran": anonymize_ran,
        "anonymize_output": anonymize_output,
    }


def clone_from_main_and_run(opts: CloneOptions) -> Dict:
    root = Path(opts.root_data_dir)
    src_main = root / opts.main_data_dir
    if not src_main.exists() or not _is_btrfs_subvolume(src_main):
        raise FileNotFoundError(f"Main replica not found or not a subvolume: {src_main}")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    clone_name = f"{opts.main_data_dir}-clone-{ts}"
    clone_path = root / clone_name

    try:
        src_container = _find_container_mounting_path(src_main)
        if src_container:
            _force_checkpoint_on_container(src_container, opts.postgres_user, opts.postgres_db)
        else:
            _timing_log(f"[CLONE_TIMING] pre_checkpoint_skipped reason=no_container_for_src path={src_main}")
    except Exception as e:
        _timing_log(f"[CLONE_TIMING] pre_checkpoint_error path={src_main} err={str(e).strip()}")

    t0 = time.monotonic()
    _run(["sudo", "-n", "btrfs", "subvolume", "snapshot", str(src_main), str(clone_path)])
    t1 = time.monotonic()
    _timing_log(f"[CLONE_TIMING] btrfs_snapshot_ms={int((t1-t0)*1000)} source={src_main} target={clone_path}")

    uid, gid = _detect_postgres_uid_gid(opts.postgres_image)
    _run(["sudo", "-n", "chown", "-R", f"{uid}:{gid}", str(clone_path)])
    _run(["sudo", "-n", "chmod", "-R", "u+rwX,go-rwx", str(clone_path)])

    meta = {
        "name": clone_name,
        "path": str(clone_path),
        "source_main_path": str(src_main),
        "root_data_dir": str(root),
        "main_data_dir": opts.main_data_dir,
        "created_at": datetime.now().isoformat(),
        "created_by": "snaplicator-api",
        "description": opts.description,
    }
    meta_json = json.dumps(meta, ensure_ascii=False)
    meta_path = clone_path / ".snaplicator.json"
    try:
        _run(["sudo", "-n", "bash", "-lc", f"cat > {meta_path!s} <<'EOF'\n{meta_json}\nEOF\n"])
    except subprocess.CalledProcessError:
        pass
    try:
        _run(["sudo", "-n", "setfattr", "-n", "user.snaplicator", "-v", meta_json, str(clone_path)])
    except subprocess.CalledProcessError:
        pass

    container_name = f"{opts.container_name}-{ts}"
    host_port, container_pgdata, anonymize_ran, anonymize_output = _launch_clone_container(
        clone_path=clone_path,
        opts=opts,
        container_name=container_name,
        host_port_hint=None,
        description=opts.description,
    )

    return {
        "source_main": str(src_main),
        "clone_subvolume": str(clone_path),
        "container_name": container_name,
        "host_port": host_port,
        "pgdata": container_pgdata,
        "metadata_path": str(meta_path),
        "anonymize_ran": anonymize_ran,
        "anonymize_output": anonymize_output,
    }


def refresh_clone_in_place(
    target_container: str,
    opts: CloneOptions,
    description_override: Optional[str] = None,
) -> Dict:
    root = Path(opts.root_data_dir)
    src_main = root / opts.main_data_dir
    if not src_main.exists() or not _is_btrfs_subvolume(src_main):
        raise FileNotFoundError(f"Main replica not found or not a subvolume: {src_main}")

    try:
        inspect_out = subprocess.run(
            ["docker", "inspect", target_container],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        raise FileNotFoundError(f"Container not found: {target_container}") from e

    try:
        inspect_data = json.loads(inspect_out)[0]
    except (IndexError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Failed to inspect container: {target_container}") from e

    ports = inspect_data.get("NetworkSettings", {}).get("Ports", {})
    port_binding = ports.get("5432/tcp")
    if not port_binding:
        raise RuntimeError(f"Container {target_container} does not expose 5432/tcp")
    try:
        host_port = int(port_binding[0]["HostPort"])
    except (IndexError, KeyError, ValueError, TypeError) as e:
        raise RuntimeError(f"Failed to determine host port for container {target_container}") from e

    host_path: Optional[Path] = None
    for m in inspect_data.get("Mounts", []):
        dest = m.get("Destination", "")
        src = m.get("Source", "")
        if dest.startswith("/var/lib/postgresql/data") and src:
            host_path = Path(src)
            break
    if not host_path:
        raise RuntimeError(f"Could not determine clone subvolume path for container {target_container}")

    description = description_override
    if description is None:
        meta_path_old = host_path / ".snaplicator.json"
        try:
            meta_raw = subprocess.run(
                ["sudo", "-n", "cat", str(meta_path_old)],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            meta_old = json.loads(meta_raw)
            description = meta_old.get("description")
        except Exception:
            description = None

    # Best-effort: force checkpoint on source main container before snapshot
    try:
        src_container = _find_container_mounting_path(src_main)
        if src_container:
            _force_checkpoint_on_container(src_container, opts.postgres_user, opts.postgres_db)
        else:
            _timing_log(f"[CLONE_TIMING] pre_checkpoint_skipped reason=no_container_for_src path={src_main}")
    except Exception as e:
        _timing_log(f"[CLONE_TIMING] pre_checkpoint_error path={src_main} err={str(e).strip()}")

    subprocess.run(["docker", "rm", "-f", target_container], check=False, capture_output=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    temp_path = host_path.parent / f"{host_path.name}-refresh-{ts}"
    backup_path: Optional[Path] = None

    try:
        t0 = time.monotonic()
        _run(["sudo", "-n", "btrfs", "subvolume", "snapshot", str(src_main), str(temp_path)])
        t1 = time.monotonic()
        _timing_log(f"[CLONE_TIMING] btrfs_snapshot_ms={int((t1-t0)*1000)} source={src_main} target={temp_path}")

        uid, gid = _detect_postgres_uid_gid(opts.postgres_image)
        _run(["sudo", "-n", "chown", "-R", f"{uid}:{gid}", str(temp_path)])
        _run(["sudo", "-n", "chmod", "-R", "u+rwX,go-rwx", str(temp_path)])

        meta = {
            "name": host_path.name,
            "path": str(host_path),
            "source_main_path": str(src_main),
            "root_data_dir": str(root),
            "main_data_dir": opts.main_data_dir,
            "refreshed_at": datetime.now().isoformat(),
            "created_by": "snaplicator-api",
            "description": description,
        }
        meta_json = json.dumps(meta, ensure_ascii=False)
        meta_path_new = temp_path / ".snaplicator.json"
        try:
            _run(["sudo", "-n", "bash", "-lc", f"cat > {meta_path_new!s} <<'EOF'\n{meta_json}\nEOF\n"])
        except subprocess.CalledProcessError:
            pass
        try:
            _run(["sudo", "-n", "setfattr", "-n", "user.snaplicator", "-v", meta_json, str(temp_path)])
        except subprocess.CalledProcessError:
            pass

        if host_path.exists():
            backup_path = host_path.parent / f"{host_path.name}-prev-{ts}"
            _run(["sudo", "-n", "mv", str(host_path), str(backup_path)])

        _run(["sudo", "-n", "mv", str(temp_path), str(host_path)])

    except Exception:
        try:
            if temp_path.exists():
                _run(["sudo", "-n", "btrfs", "subvolume", "delete", str(temp_path)])
        except subprocess.CalledProcessError:
            pass
        if backup_path and backup_path.exists() and not host_path.exists():
            try:
                _run(["sudo", "-n", "mv", str(backup_path), str(host_path)])
            except subprocess.CalledProcessError:
                pass
        raise

    anonymize_ran = False
    anonymize_output = None
    refresh_success = False
    try:
        host_port, container_pgdata, anonymize_ran, anonymize_output = _launch_clone_container(
            clone_path=host_path,
            opts=opts,
            container_name=target_container,
            host_port_hint=host_port,
            description=description,
            remove_existing=False,
        )
        refresh_success = True
    finally:
        if not refresh_success:
            # Keep backup for manual recovery
            pass

    if backup_path and backup_path.exists() and refresh_success:
        try:
            _run(["sudo", "-n", "btrfs", "subvolume", "delete", str(backup_path)])
        except subprocess.CalledProcessError:
            pass

    return {
        "refreshed_container": target_container,
        "host_port": host_port,
        "clone_subvolume": str(host_path),
        "pgdata": container_pgdata,
        "metadata_path": str(host_path / ".snaplicator.json"),
        "description": description,
        "anonymize_ran": anonymize_ran,
        "anonymize_output": anonymize_output,
    }


def list_clones(root_data_dir: str, base_container_name: Optional[str] = None) -> List[Dict]:
    """List docker containers relevant to Snaplicator clones/replica.

    Heuristics:
    - Include containers with label snaplicator=1 OR
      name startswith base_container_name (if provided) OR
      Mounts contain root_data_dir.
    - is_replica = label snaplicator.role=replica OR name == base_container_name.
    - is_clone = label snaplicator.role=clone OR name startswith f"{base_container_name}-".
    """
    try:
        out = subprocess.run(
            [
                "docker", "ps", "-a",
                "--format", "{{.ID}}\t{{.Names}}\t{{.Ports}}\t{{.Status}}\t{{.Labels}}\t{{.Mounts}}",
            ],
            check=True, text=True, capture_output=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"docker ps failed: {e}")

    clones: List[Dict] = []
    for line in out.splitlines():
        cid, name, ports, status, labels, mounts = (line.split("\t") + ["", "", "", "", "", ""])[:6]
        labels = labels or ""
        mounts = mounts or ""
        has_label = "snaplicator=1" in labels
        name_match = bool(base_container_name) and name.startswith(str(base_container_name))
        mounts_match = root_data_dir.rstrip("/") in mounts
        if not (has_label or name_match or mounts_match):
            continue
        is_replica = ("snaplicator.role=replica" in labels) or (bool(base_container_name) and name == str(base_container_name))
        is_clone = ("snaplicator.role=clone" in labels) or (bool(base_container_name) and name.startswith(f"{base_container_name}-"))
        clones.append({
            "id": cid,
            "name": name,
            "ports": ports,
            "status": status,
            "labels": labels,
            "is_replica": bool(is_replica),
            "is_clone": bool(is_clone),
        })
    return clones


def delete_clone(root_data_dir: str, main_data_dir: Optional[str], container_name: str) -> Dict:
    """Delete a clone by removing its docker container(s) mounting the clone subvolume, then delete the btrfs subvolume.

    Safety checks:
    - Inspect container mounts to find host source for /var/lib/postgresql/data*
    - Ensure source path is under ROOT_DATA_DIR
    - If MAIN_DATA_DIR provided, ensure basename startswith f"{MAIN_DATA_DIR}-clone-"
    - Ensure source is a btrfs subvolume
    """
    # First attempt: inspect container mounts to resolve the host clone subvolume path
    host_src: Optional[str] = None
    try:
        mounts_json = subprocess.run(
            ["docker", "inspect", container_name, "--format", "{{json .Mounts}}"],
            check=True, text=True, capture_output=True,
        ).stdout
        try:
            mounts = json.loads(mounts_json) or []
        except json.JSONDecodeError:
            mounts = []
        for m in mounts:
            dest = m.get("Destination", "")
            src = m.get("Source", "")
            if dest.startswith("/var/lib/postgresql/data") and src:
                host_src = src
                break
    except subprocess.CalledProcessError:
        # Container not found. Treat the provided name as a clone subvolume name under ROOT_DATA_DIR.
        candidate = Path(root_data_dir) / container_name
        if candidate.exists():
            host_src = str(candidate)
        else:
            # Fall back to original error for clarity
            raise FileNotFoundError(f"Container not found and no matching subvolume: {container_name}")

    if not host_src:
        raise RuntimeError("Could not determine clone subvolume path from container or subvolume name")

    host_path = Path(host_src).resolve()
    root_path = Path(root_data_dir).resolve()
    if not str(host_path).startswith(str(root_path)):
        raise PermissionError(f"Refusing to delete a path outside ROOT_DATA_DIR. path={host_path} root={root_path}")

    if main_data_dir:
        expected_prefix = f"{main_data_dir}-clone-"
        if not host_path.name.startswith(expected_prefix):
            raise PermissionError(f"Target subvolume name does not match MAIN_DATA_DIR clone naming. name={host_path.name} expected_prefix={expected_prefix}")

    if not _is_btrfs_subvolume(host_path):
        # Include filesystem type for diagnostics
        fstype = ""
        try:
            fstype = subprocess.run(["findmnt", "-no", "FSTYPE", "-T", str(host_path)], text=True, capture_output=True, check=True).stdout.strip()
        except subprocess.CalledProcessError:
            pass
        if not fstype:
            try:
                fstype = subprocess.run(["stat", "-f", "-c", "%T", str(host_path)], text=True, capture_output=True, check=True).stdout.strip()
            except subprocess.CalledProcessError:
                fstype = "unknown"
        raise RuntimeError(f"Target path is not a btrfs subvolume: {host_path} (fstype={fstype})")

    # Find and remove ALL containers that mount this host_path
    removed_containers: List[str] = []
    try:
        ids_out = subprocess.run(["docker", "ps", "-aq"], check=True, text=True, capture_output=True).stdout
        container_ids = [line.strip() for line in ids_out.splitlines() if line.strip()]
    except subprocess.CalledProcessError:
        container_ids = []

    for cid in container_ids:
        try:
            fmt = "{{.Name}}\t{{json .Mounts}}"
            ins = subprocess.run(["docker", "inspect", "--format", fmt, cid], check=True, text=True, capture_output=True).stdout.strip()
            if not ins:
                continue
            name_raw, mounts_json2 = (ins.split("\t") + [""])[:2]
            cname = name_raw.lstrip('/')
            mounts2 = []
            try:
                mounts2 = json.loads(mounts_json2) or []
            except Exception:
                mounts2 = []
            match = False
            for m in mounts2:
                dest = m.get("Destination", "")
                src = m.get("Source", "")
                if dest.startswith("/var/lib/postgresql/data") and src:
                    try:
                        resolved = str(Path(src).resolve())
                    except Exception:
                        resolved = src
                    if resolved == str(host_path):
                        match = True
                        break
            if match:
                # stop and remove
                subprocess.run(["docker", "rm", "-f", cname], check=False)
                removed_containers.append(cname)
        except subprocess.CalledProcessError:
            continue

    # Delete subvolume with error forwarding
    try:
        _run(["sudo", "-n", "btrfs", "subvolume", "delete", str(host_path)])
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or e.stdout or "").strip()
        raise RuntimeError(f"btrfs subvolume delete failed for {host_path}: {stderr}")

    return {
        "containers_removed": removed_containers,
        "subvolume_deleted": str(host_path),
    } 