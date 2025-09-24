from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List
import time
import json


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def _is_btrfs_subvolume(path: Path) -> bool:
    try:
        _run(["sudo", "btrfs", "subvolume", "show", str(path)])
        return True
    except subprocess.CalledProcessError:
        return False


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


def clone_from_snapshot_and_run(opts: CloneOptions) -> Dict:
    root = Path(opts.root_data_dir)
    snap_path = root / opts.snapshot_name
    if not snap_path.exists() or not _is_btrfs_subvolume(snap_path):
        raise FileNotFoundError(f"Snapshot not found or not a subvolume: {snap_path}")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    clone_name = f"{opts.main_data_dir}-clone-{ts}"
    clone_path = root / clone_name

    # Create writable snapshot
    _run(["sudo", "btrfs", "subvolume", "snapshot", str(snap_path), str(clone_path)])

    # Permissions for postgres uid/gid 999
    _run(["sudo", "chown", "-R", "999:999", str(clone_path)])
    _run(["sudo", "chmod", "-R", "u+rwX,go-rwx", str(clone_path)])

    # Write clone metadata (.snaplicator.json and xattr)
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
        _run(["sudo", "bash", "-lc", f"cat > {meta_path!s} <<'EOF'\n{meta_json}\nEOF\n"])
    except subprocess.CalledProcessError:
        pass
    try:
        _run(["sudo", "setfattr", "-n", "user.snaplicator", "-v", meta_json, str(clone_path)])
    except subprocess.CalledProcessError:
        pass

    # Determine PGDATA inside the clone
    container_pgdata = _pgdata_env_for_clone_path(clone_path)

    # Prepare docker network
    try:
        out = subprocess.run(["docker", "network", "ls", "--format", "{{.Name}}"], check=True, text=True, capture_output=True).stdout
        nets = set(line.strip() for line in out.splitlines())
        if opts.network_name not in nets:
            subprocess.run(["docker", "network", "create", opts.network_name], check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to ensure docker network: {e}")

    # Container name with timestamp suffix
    container_name = f"{opts.container_name}-{ts}"

    # Remove existing container with same name if any
    subprocess.run(["docker", "rm", "-f", container_name], check=False)

    # Find available host port starting from opts.host_port
    selected_port = _find_free_port(int(opts.host_port))

    # Run the container (add labels for identification)
    labels = [
        "--label", "snaplicator=1",
        "--label", f"snaplicator.role=clone",
        "--label", f"snaplicator.main={opts.main_data_dir}",
    ]

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
        "-p", f"{selected_port}:5432",
        *labels,
        *envs,
        "-v", f"{str(clone_path)}:/var/lib/postgresql/data",
        opts.postgres_image,
        "-c", "max_logical_replication_workers=0",
    ]
    subprocess.run(cmd, check=True)

    # Wait for readiness up to 60s
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

    # Disable all subscriptions to avoid slot conflicts
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

    return {
        "snapshot": str(snap_path),
        "clone_subvolume": str(clone_path),
        "container_name": container_name,
        "host_port": selected_port,
        "pgdata": container_pgdata,
        "metadata_path": str(meta_path),
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
    # Inspect target container mounts to resolve the host clone subvolume path
    mounts_json = ""
    try:
        mounts_json = subprocess.run(
            ["docker", "inspect", container_name, "--format", "{{json .Mounts}}"],
            check=True, text=True, capture_output=True,
        ).stdout
    except subprocess.CalledProcessError:
        raise FileNotFoundError(f"Container not found: {container_name}")

    try:
        mounts = json.loads(mounts_json) or []
    except json.JSONDecodeError:
        mounts = []

    host_src: Optional[str] = None
    for m in mounts:
        dest = m.get("Destination", "")
        src = m.get("Source", "")
        if dest.startswith("/var/lib/postgresql/data") and src:
            host_src = src
            break

    if not host_src:
        raise RuntimeError(f"Could not determine clone subvolume path from container mounts. mounts={mounts_json[:400]}")

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
        _run(["sudo", "btrfs", "subvolume", "delete", str(host_path)])
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or e.stdout or "").strip()
        raise RuntimeError(f"btrfs subvolume delete failed for {host_path}: {stderr}")

    return {
        "containers_removed": removed_containers,
        "subvolume_deleted": str(host_path),
    } 