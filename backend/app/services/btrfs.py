from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Dict
from datetime import datetime


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def _is_btrfs_subvolume(path: Path) -> bool:
    try:
        _run(["sudo", "btrfs", "subvolume", "show", str(path)])
        return True
    except subprocess.CalledProcessError:
        return False


def _is_readonly_subvolume(path: Path) -> bool:
    try:
        out = _run(["sudo", "btrfs", "subvolume", "show", str(path)]).stdout
        for line in out.splitlines():
            if line.strip().startswith("Flags:") and "readonly" in line:
                return True
        return False
    except subprocess.CalledProcessError:
        return False


def list_snapshots(root_data_dir: str, main_data_dir: str) -> List[Dict]:
    root = Path(root_data_dir)
    if not root.exists():
        raise FileNotFoundError(f"Root path not found: {root}")

    prefix = f"{main_data_dir}-snapshot-"
    items: List[Dict] = []

    # Scan immediate children for snapshot naming
    for entry in os.scandir(root):
        if not entry.is_dir(follow_symlinks=False):
            continue
        name = entry.name
        if not name.startswith(prefix):
            continue
        p = Path(entry.path)
        if not _is_btrfs_subvolume(p):
            continue
        items.append({
            "name": name,
            "path": str(p),
            "readonly": _is_readonly_subvolume(p)
        })

    # Sort by name (timestamp-friendly)
    items.sort(key=lambda x: x["name"]) 
    return items


def create_snapshot(root_data_dir: str, main_data_dir: str) -> Dict:
    """Create a readonly btrfs snapshot like scripts/create_main_snapshot.sh.

    - Source: {ROOT_DATA_DIR}/{MAIN_DATA_DIR}
    - Target: {ROOT_DATA_DIR}/{MAIN_DATA_DIR}-snapshot-{YYYYMMDD-HHMMSS}
    """
    root = Path(root_data_dir)
    src = root / main_data_dir
    if not src.exists():
        raise FileNotFoundError(f"Source path not found: {src}")
    if not _is_btrfs_subvolume(src):
        raise ValueError(f"Source is not a btrfs subvolume: {src}")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = root / f"{main_data_dir}-snapshot-{ts}"
    if target.exists():
        raise FileExistsError(f"Target snapshot already exists: {target}")

    # Create readonly snapshot
    _run(["sudo", "btrfs", "subvolume", "snapshot", "-r", str(src), str(target)])

    return {
        "name": target.name,
        "path": str(target),
        "readonly": True,
    } 