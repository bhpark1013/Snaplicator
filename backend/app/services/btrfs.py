from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime
import json


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


def _read_snapshot_description(path: Path) -> Optional[str]:
    # 1) Try metadata file
    meta_path = path / ".snaplicator.json"
    try:
        out = subprocess.run(["sudo", "cat", str(meta_path)], text=True, capture_output=True, check=True).stdout
        if out:
            data = json.loads(out)
            desc = data.get("description")
            if isinstance(desc, str) and desc.strip():
                return desc
    except Exception:
        # fallback: try without sudo
        try:
            if meta_path.exists():
                with meta_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    desc = data.get("description")
                    if isinstance(desc, str) and desc.strip():
                        return desc
        except Exception:
            pass
    # 2) Try xattr user.snaplicator
    try:
        out = subprocess.run(["sudo", "getfattr", "-n", "user.snaplicator", "--only-values", str(path)], text=True, capture_output=True, check=True).stdout
        if out:
            data = json.loads(out)
            desc = data.get("description")
            if isinstance(desc, str) and desc.strip():
                return desc
    except Exception:
        pass
    return None


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
            "readonly": _is_readonly_subvolume(p),
            "description": _read_snapshot_description(p),
        })

    # Sort by name (timestamp-friendly)
    items.sort(key=lambda x: x["name"])
    return items


def create_snapshot(root_data_dir: str, main_data_dir: str, description: Optional[str] = None) -> Dict:
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

    # 1) Create writable snapshot
    _run(["sudo", "btrfs", "subvolume", "snapshot", str(src), str(target)])

    # 2) Write metadata file and xattr on the snapshot root
    meta = {
        "name": target.name,
        "path": str(target),
        "source_path": str(src),
        "root_data_dir": str(root),
        "main_data_dir": main_data_dir,
        "created_at": datetime.now().isoformat(),
        "created_by": "snaplicator-api",
        "description": description,
    }
    meta_json = json.dumps(meta, ensure_ascii=False)
    meta_path = target / ".snaplicator.json"
    try:
        # Use sudo to ensure we can write even if owner is uid 999
        _run(["sudo", "bash", "-lc", f"cat > {meta_path!s} <<'EOF'\n{meta_json}\nEOF\n"])
    except subprocess.CalledProcessError:
        pass
    try:
        # Extended attribute (may fail if user_xattr is not enabled)
        _run(["sudo", "setfattr", "-n", "user.snaplicator", "-v", meta_json, str(target)])
    except subprocess.CalledProcessError:
        pass

    # 3) Toggle snapshot to readonly
    try:
        _run(["sudo", "btrfs", "property", "set", "-ts", str(target), "ro", "true"])
    except subprocess.CalledProcessError:
        # Fallback if property subcommand not available with -ts (older btrfs-progs)
        _run(["sudo", "btrfs", "property", "set", str(target), "ro", "true"])

    return {
        "name": target.name,
        "path": str(target),
        "readonly": True,
        "metadata_path": str(meta_path),
    }


def list_clone_subvolumes_with_containers(root_data_dir: str, main_data_dir: str) -> List[Dict]:
    """List clones based on btrfs subvolumes only (name starts with {MAIN_DATA_DIR}-clone-),
    and annotate if a docker container is mounting each clone path.
    """
    root = Path(root_data_dir)
    if not root.exists():
        raise FileNotFoundError(f"Root path not found: {root}")

    prefix = f"{main_data_dir}-clone-"
    clones: List[Dict] = []

    for entry in os.scandir(root):
        if not entry.is_dir(follow_symlinks=False):
            continue
        name = entry.name
        if not name.startswith(prefix):
            continue
        p = Path(entry.path)
        if not _is_btrfs_subvolume(p):
            # skip non-btrfs entries
            continue
        clones.append({
            "name": name,
            "path": str(p),
            "is_btrfs": True,
            "has_container": False,
            "is_running": False,
            "container_name": None,
            "container_status": None,
            "container_ports": None,
            "container_started_at": None,
            "description": _read_snapshot_description(p),
        })

    # Build map via docker inspect for accurate host Source matching
    try:
        ids_out = subprocess.run(["docker", "ps", "-aq"], check=True, text=True, capture_output=True).stdout
        container_ids = [line.strip() for line in ids_out.splitlines() if line.strip()]
    except subprocess.CalledProcessError:
        container_ids = []

    container_infos = []
    for cid in container_ids:
        try:
            fmt = "{{.Name}}\t{{json .Mounts}}\t{{.State.Status}}\t{{.State.StartedAt}}\t{{json .NetworkSettings.Ports}}"
            ins = subprocess.run(["docker", "inspect", "--format", fmt, cid], check=True, text=True, capture_output=True).stdout.strip()
            if not ins:
                continue
            parts = ins.split("\t")
            if len(parts) < 5:
                continue
            name_raw, mounts_json, state_status, started_at, ports_json = parts
            cname = name_raw.lstrip('/')
            mounts = []
            try:
                mounts = __import__('json').loads(mounts_json) or []
            except Exception:
                mounts = []
            # Find host source for PGDATA mount
            host_src = None
            for m in mounts:
                dest = m.get("Destination", "")
                src = m.get("Source", "")
                if dest.startswith("/var/lib/postgresql/data") and src:
                    try:
                        host_src = str(Path(src).resolve())
                    except Exception:
                        host_src = src
                    break
            # Build ports summary (optional)
            ports_text = None
            try:
                ports = __import__('json').loads(ports_json)
                pairs = []
                if isinstance(ports, dict):
                    for key, arr in ports.items():
                        if not arr:
                            continue
                        for entry in arr:
                            pairs.append(f"{entry.get('HostIp','')}:{entry.get('HostPort','')}->{key}")
                    ports_text = ", ".join(pairs) if pairs else None
            except Exception:
                ports_text = None
            container_infos.append({
                "name": cname,
                "host_src": host_src,
                "status": state_status,  # 'running' | 'exited' | ...
                "ports": ports_text,
                "started_at": started_at,
            })
        except subprocess.CalledProcessError:
            continue

    # Associate containers to clones by exact host path match
    src_to_info = {}
    for info in container_infos:
        if info.get("host_src"):
            src_to_info[info["host_src"]] = info

    for c in clones:
        cpath = str(Path(c["path"]).resolve())
        info = src_to_info.get(cpath)
        if info:
            c["has_container"] = True
            c["container_name"] = info.get("name")
            c["container_status"] = info.get("status")
            c["is_running"] = (info.get("status") == "running")
            c["container_ports"] = info.get("ports")
            c["container_started_at"] = info.get("started_at")

    clones.sort(key=lambda x: x["name"])
    return clones 