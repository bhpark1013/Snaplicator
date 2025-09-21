from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def _is_btrfs_subvolume(path: Path) -> bool:
    try:
        _run(["sudo", "btrfs", "subvolume", "show", str(path)])
        return True
    except subprocess.CalledProcessError:
        return False


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

    # Run the container
    envs = [
        "-e", f"POSTGRES_USER={opts.postgres_user}",
        "-e", f"POSTGRES_PASSWORD={opts.postgres_password}",
        "-e", f"POSTGRES_DB={opts.postgres_db}",
        "-e", "PGDATA=/var/lib/postgresql/data/pgdata",
    ]

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--network", opts.network_name,
        "-p", f"{opts.host_port}:5432",
        *envs,
        "-v", f"{str(clone_path)}:/var/lib/postgresql/data",
        opts.postgres_image,
        "-c", "max_logical_replication_workers=0",
    ]
    subprocess.run(cmd, check=True)

    return {
        "snapshot": str(snap_path),
        "clone_subvolume": str(clone_path),
        "container_name": container_name,
        "host_port": opts.host_port,
    } 