from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List
import time


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