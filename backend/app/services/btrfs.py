from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime
import json


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True)


def _is_btrfs_subvolume(path: Path) -> bool:
    try:
        _run(["sudo", "-n", "btrfs", "subvolume", "show", str(path)])
        return True
    except subprocess.CalledProcessError:
        return False


def _is_readonly_subvolume(path: Path) -> bool:
    try:
        out = _run(["sudo", "-n", "btrfs", "subvolume", "show", str(path)]).stdout
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
        out = subprocess.run(["sudo", "-n", "cat", str(meta_path)], text=True, capture_output=True, check=True).stdout
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
        out = subprocess.run(["sudo", "-n", "getfattr", "-n", "user.snaplicator", "--only-values", str(path)], text=True, capture_output=True, check=True).stdout
        if out:
            data = json.loads(out)
            desc = data.get("description")
            if isinstance(desc, str) and desc.strip():
                return desc
    except Exception:
        pass
    return None


def _human_to_bytes(text: str) -> Optional[int]:
    try:
        s = text.strip().lower().replace(',', '')
        # Accept forms like "123456" or "1.23 GiB" or "1.23g"
        parts = s.split()
        if not parts:
            return None
        num_str = parts[0]
        unit = parts[1] if len(parts) > 1 else 'b'
        val = float(num_str)
        mul = 1
        if unit in ('b', 'bytes'):
            mul = 1
        elif unit in ('k', 'kb', 'kib'):
            mul = 1024
        elif unit in ('m', 'mb', 'mib'):
            mul = 1024 ** 2
        elif unit in ('g', 'gb', 'gib'):
            mul = 1024 ** 3
        elif unit in ('t', 'tb', 'tib'):
            mul = 1024 ** 4
        else:
            # try stripping trailing letters like gi, gib
            for u, factor in [('t', 1024**4), ('g', 1024**3), ('m', 1024**2), ('k', 1024)]:
                if unit.startswith(u):
                    mul = factor
                    break
        return int(val * mul)
    except Exception:
        return None


def _get_fs_totals_bytes(path: Path) -> Tuple[Optional[int], Optional[int]]:
    """Return (size_bytes, used_bytes) for the filesystem containing path."""
    try:
        out = _run(["df", "-B1", "--output=size,used", "-P", str(path)]).stdout.splitlines()
        if len(out) >= 2:
            cols = out[1].split()
            if len(cols) >= 2:
                size_b = int(cols[0])
                used_b = int(cols[1])
                return size_b, used_b
    except subprocess.CalledProcessError:
        pass
    # Fallback using stat -f
    try:
        # block size, blocks, blocks used
        bs = int(subprocess.run(["stat", "-f", "-c", "%S", str(path)], check=True, text=True, capture_output=True).stdout.strip())
        total = int(subprocess.run(["stat", "-f", "-c", "%b", str(path)], check=True, text=True, capture_output=True).stdout.strip())
        free = int(subprocess.run(["stat", "-f", "-c", "%a", str(path)], check=True, text=True, capture_output=True).stdout.strip())
        size_b = bs * total
        used_b = bs * (total - free)
        return size_b, used_b
    except Exception:
        return None, None


def _get_subvolume_usage_bytes(path: Path) -> Optional[int]:
    """Best-effort size in bytes for a subvolume.
    Prefer btrfs filesystem du -s (exclusive) if available, fallback to du -sb.
    """
    # Try btrfs filesystem du -s first (requires root)
    try:
        out = _run(["sudo", "-n", "btrfs", "filesystem", "du", "-s", str(path)]).stdout
        # Look for a line like: Total exclusive: 1.12GiB
        for line in out.splitlines():
            l = line.strip().lower()
            if l.startswith("total exclusive:"):
                num = l.split(":", 1)[1].strip()
                val = _human_to_bytes(num)
                if isinstance(val, int):
                    return val
        # As a fallback, try referenced total
        for line in out.splitlines():
            l = line.strip().lower()
            if l.startswith("total referenced:"):
                num = l.split(":", 1)[1].strip()
                val = _human_to_bytes(num)
                if isinstance(val, int):
                    return val
    except subprocess.CalledProcessError:
        pass
    # Fallback to du -sb
    try:
        out = _run(["sudo", "-n", "du", "-sb", str(path)]).stdout
        first = out.splitlines()[0].split()[0]
        return int(first)
    except Exception:
        try:
            out = subprocess.run(["du", "-sb", str(path)], check=True, text=True, capture_output=True).stdout
            first = out.splitlines()[0].split()[0]
            return int(first)
        except Exception:
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
    _run(["sudo", "-n", "btrfs", "subvolume", "snapshot", str(src), str(target)])

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
        _run(["sudo", "-n", "bash", "-lc", f"cat > {meta_path!s} <<'EOF'\n{meta_json}\nEOF\n"])
    except subprocess.CalledProcessError:
        pass
    try:
        # Extended attribute (may fail if user_xattr is not enabled)
        _run(["sudo", "-n", "setfattr", "-n", "user.snaplicator", "-v", meta_json, str(target)])
    except subprocess.CalledProcessError:
        pass

    # 3) Toggle snapshot to readonly
    try:
        _run(["sudo", "-n", "btrfs", "property", "set", "-ts", str(target), "ro", "true"])
    except subprocess.CalledProcessError:
        # Fallback if property subcommand not available with -ts (older btrfs-progs)
        _run(["sudo", "-n", "btrfs", "property", "set", str(target), "ro", "true"])

    return {
        "name": target.name,
        "path": str(target),
        "readonly": True,
        "metadata_path": str(meta_path),
    }


def delete_snapshot(root_data_dir: str, main_data_dir: str, snapshot_name: str) -> Dict:
    root = Path(root_data_dir).resolve()
    target = (root / snapshot_name).resolve()
    # Safety checks
    if not str(target).startswith(str(root)):
        raise PermissionError(f"Refusing to delete outside ROOT_DATA_DIR. path={target} root={root}")
    if not target.exists() or not target.is_dir():
        raise FileNotFoundError(f"Snapshot path not found: {target}")
    prefix = f"{main_data_dir}-snapshot-"
    if not snapshot_name.startswith(prefix):
        raise PermissionError(f"Name does not match snapshot prefix: expected '{prefix}*', got '{snapshot_name}'")
    if not _is_btrfs_subvolume(target):
        # Include fstype for diagnostics
        try:
            fstype = subprocess.run(["findmnt", "-no", "FSTYPE", "-T", str(target)], text=True, capture_output=True, check=True).stdout.strip()
        except subprocess.CalledProcessError:
            fstype = "unknown"
        raise RuntimeError(f"Target is not a btrfs subvolume: {target} (fstype={fstype})")
    # If mounted separately, refuse
    try:
        mnt = subprocess.run(["findmnt", "-T", str(target)], text=True, capture_output=True, check=True).stdout
        if mnt and str(target) in mnt and "subvol=" in mnt:
            # Appears mounted; ask user to unmount
            raise RuntimeError(f"Snapshot appears mounted; unmount before delete. details=\n{mnt}")
    except subprocess.CalledProcessError:
        pass
    # Delete subvolume
    try:
        _run(["sudo", "-n", "btrfs", "subvolume", "delete", str(target)])
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or e.stdout or "").strip()
        raise RuntimeError(f"btrfs subvolume delete failed for {target}: {stderr}")
    return {"subvolume_deleted": str(target)}


def list_clone_subvolumes_with_containers(root_data_dir: str, main_data_dir: str) -> List[Dict]:
    """List clones based on btrfs subvolumes only (name starts with {MAIN_DATA_DIR}-clone-),
    and annotate if a docker container is mounting each clone path.
    """
    root = Path(root_data_dir)
    if not root.exists():
        raise FileNotFoundError(f"Root path not found: {root}")

    prefix = f"{main_data_dir}-clone-"
    clones: List[Dict] = []

    # Filesystem totals (same for all clones under root)
    fs_size_b, fs_used_b = _get_fs_totals_bytes(root)

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
        usage_b = _get_subvolume_usage_bytes(p)
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
            "usage_bytes": usage_b,
            "fs_size_bytes": fs_size_b,
            "fs_used_bytes": fs_used_b,
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
            # Build ports summary (optional) and extract host_port for 5432/tcp if present
            ports_text = None
            host_port_int: Optional[int] = None
            try:
                ports = __import__('json').loads(ports_json)
                pairs = []
                if isinstance(ports, dict):
                    for key, arr in ports.items():
                        if not arr:
                            continue
                        for entry in arr:
                            pairs.append(f"{entry.get('HostIp','')}:{entry.get('HostPort','')}->{key}")
                            # Prefer exact mapping for postgres container port 5432/tcp
                            if key.startswith("5432/") and not host_port_int:
                                try:
                                    hp = entry.get('HostPort')
                                    if hp:
                                        host_port_int = int(hp)
                                except Exception:
                                    pass
                    ports_text = ", ".join(pairs) if pairs else None
                # Fallback: if 5432 not found, pick the first tcp mapping
                if host_port_int is None and isinstance(ports, dict):
                    for key, arr in ports.items():
                        if key.endswith('/tcp') and arr:
                            try:
                                hp = arr[0].get('HostPort') if isinstance(arr, list) else None
                                if hp:
                                    host_port_int = int(hp)
                                    break
                            except Exception:
                                continue
            except Exception:
                ports_text = None
            container_infos.append({
                "name": cname,
                "host_src": host_src,
                "status": state_status,  # 'running' | 'exited' | ...
                "ports": ports_text,
                "host_port": host_port_int,
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
            c["host_port"] = info.get("host_port")

    clones.sort(key=lambda x: x["name"])
    return clones 