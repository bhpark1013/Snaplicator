from __future__ import annotations

import os
import subprocess
import logging
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime, timedelta
import json


logger = logging.getLogger(__name__)
if logger.level == logging.NOTSET:
    logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(asctime)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


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


def read_snaplicator_metadata(path: Path) -> Dict[str, Any]:
    meta_path = path / ".snaplicator.json"
    data: Dict[str, Any] = {}

    def _load_json_text(text: str) -> Dict[str, Any]:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return {}

    try:
        out = subprocess.run(["sudo", "-n", "cat", str(meta_path)], text=True, capture_output=True, check=True).stdout
        if out:
            data = _load_json_text(out)
    except Exception:
        try:
            if meta_path.exists():
                with meta_path.open("r", encoding="utf-8") as f:
                    data = _load_json_text(f.read())
        except Exception:
            data = {}

    if not data:
        try:
            out = subprocess.run(["sudo", "-n", "getfattr", "-n", "user.snaplicator", "--only-values", str(path)], text=True, capture_output=True, check=True).stdout
            if out:
                data = _load_json_text(out)
        except Exception:
            pass

    return data


def write_snaplicator_metadata(target: Path, meta: Dict[str, Any]) -> None:
    meta_json = json.dumps(meta, ensure_ascii=False)
    meta_path = target / ".snaplicator.json"
    try:
        _run(["sudo", "-n", "bash", "-lc", f"cat > {meta_path!s} <<'EOF'\n{meta_json}\nEOF\n"])
    except subprocess.CalledProcessError:
        pass
    try:
        _run(["sudo", "-n", "setfattr", "-n", "user.snaplicator", "-v", meta_json, str(target)])
    except subprocess.CalledProcessError:
        pass


def _read_snapshot_description(path: Path) -> Optional[str]:
    data = read_snaplicator_metadata(path)
    desc = data.get("description") if isinstance(data, dict) else None
    if isinstance(desc, str):
        desc = desc.strip()
        if desc:
            return desc
    return None


def _read_clone_display_name(path: Path) -> Optional[str]:
    """User-facing clone Name. Falls back to the legacy description for clones
    created before display_name existed."""
    data = read_snaplicator_metadata(path)
    if not isinstance(data, dict):
        return None
    for key in ("display_name", "description"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _retention_fields(created: datetime, retention_days: Optional[int]) -> Dict[str, Any]:
    """Normalize a retention setting into metadata fields.

    retention_days <= 0 (or None) means "keep forever" (permanent); we store
    retention_days=0 and expires_at=None. Otherwise expires_at is created + N days.
    Retention is advisory metadata for the UI today (no auto-deletion)."""
    days = retention_days if isinstance(retention_days, int) else 14
    if days <= 0:
        return {"retention_days": 0, "expires_at": None}
    return {"retention_days": days, "expires_at": (created + timedelta(days=days)).isoformat()}


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

    items: List[Dict] = []

    # Scan immediate children for snapshot naming
    for entry in os.scandir(root):
        if not entry.is_dir(follow_symlinks=False):
            continue
        name = entry.name
        p = Path(entry.path)
        if not _is_btrfs_subvolume(p):
            continue
        if not _is_readonly_subvolume(p):
            # Skip writable subvolumes; snapshots must be readonly by definition
            continue
        meta = read_snaplicator_metadata(p)
        items.append({
            "name": name,
            "path": str(p),
            "readonly": True,
            "description": _read_snapshot_description(p),
            "metadata": meta if isinstance(meta, dict) and meta else None,
        })

    # Sort by name (timestamp-friendly)
    items.sort(key=lambda x: x["name"])
    return items


def create_snapshot(root_data_dir: str, main_data_dir: str, description: Optional[str] = None, retention_days: int = 14, previous_snapshot: Optional[str] = None, insert_before: Optional[str] = None) -> Dict:
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
        "type": "main_snapshot",
        "previous_snapshot": (previous_snapshot or None),
        "next_snapshot": None,
        **_retention_fields(datetime.now(), retention_days),
    }
    write_snaplicator_metadata(target, meta)

    # 3) Toggle snapshot to readonly
    try:
        _run(["sudo", "-n", "btrfs", "property", "set", "-ts", str(target), "ro", "true"])
    except subprocess.CalledProcessError:
        # Fallback if property subcommand not available with -ts (older btrfs-progs)
        _run(["sudo", "-n", "btrfs", "property", "set", str(target), "ro", "true"])

    # Edge-insert: splice the new snapshot in front of `insert_before`.
    if insert_before:
        ib = insert_before.strip()
        if ib and ib != target.name:
            try:
                update_snapshot_lineage(root_data_dir, ib, previous_snapshot=target.name)
            except Exception as e:
                logger.warning("insert_before relink failed for %s -> %s: %s", ib, target.name, e)

    return {
        "name": target.name,
        "path": str(target),
        "readonly": True,
        "metadata_path": str(target / ".snaplicator.json"),
    }


def delete_snapshot(root_data_dir: str, main_data_dir: str, snapshot_name: str) -> Dict:
    root = Path(root_data_dir).resolve()
    target = (root / snapshot_name).resolve()
    # Safety checks
    if not str(target).startswith(str(root)):
        raise PermissionError(f"Refusing to delete outside ROOT_DATA_DIR. path={target} root={root}")
    if not target.exists() or not target.is_dir():
        raise FileNotFoundError(f"Snapshot path not found: {target}")
    if not _is_btrfs_subvolume(target):
        # Include fstype for diagnostics
        try:
            fstype = subprocess.run(["findmnt", "-no", "FSTYPE", "-T", str(target)], text=True, capture_output=True, check=True).stdout.strip()
        except subprocess.CalledProcessError:
            fstype = "unknown"
        raise RuntimeError(f"Target is not a btrfs subvolume: {target} (fstype={fstype})")
    if not _is_readonly_subvolume(target):
        raise PermissionError(f"Target subvolume must be readonly to delete via API: {target}")
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
    overall_start = time.perf_counter()
    timing_summary: Dict[str, Any] = {}
    clone_timings: List[Dict[str, Any]] = []
    docker_timings: List[Dict[str, Any]] = []
    skipped_snapshot_like = 0
    skipped_readonly = 0

    root = Path(root_data_dir)
    if not root.exists():
        raise FileNotFoundError(f"Root path not found: {root}")

    prefix = f"{main_data_dir}-clone-"
    clones: List[Dict] = []

    scan_start = time.perf_counter()
    entries = [entry for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    timing_summary["scandir_seconds"] = time.perf_counter() - scan_start

    for entry in entries:
        name = entry.name
        if not name.startswith(prefix):
            continue
        if "-snapshot-" in name:
            skipped_snapshot_like += 1
            continue
        p = Path(entry.path)
        subvol_check_start = time.perf_counter()
        is_subvol = _is_btrfs_subvolume(p)
        subvol_check_seconds = time.perf_counter() - subvol_check_start
        if not is_subvol:
            # skip non-btrfs entries
            continue
        readonly_check_start = time.perf_counter()
        is_readonly = _is_readonly_subvolume(p)
        readonly_check_seconds = time.perf_counter() - readonly_check_start
        if is_readonly:
            skipped_readonly += 1
            continue
        desc_start = time.perf_counter()
        description = _read_snapshot_description(p)
        display_name = _read_clone_display_name(p)
        desc_seconds = time.perf_counter() - desc_start
        metadata_seconds = subvol_check_seconds + readonly_check_seconds + desc_seconds

        clone_timings.append({
            "clone": name,
            "subvol_check_seconds": subvol_check_seconds,
            "readonly_check_seconds": readonly_check_seconds,
            "description_read_seconds": desc_seconds,
            "metadata_seconds": metadata_seconds,
        })

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
            "display_name": display_name,
            "description": description,
        })

    # Build map via docker inspect for accurate host Source matching
    docker_ps_start = time.perf_counter()
    try:
        ids_out = subprocess.run(["docker", "ps", "-aq"], check=True, text=True, capture_output=True).stdout
        container_ids = [line.strip() for line in ids_out.splitlines() if line.strip()]
    except subprocess.CalledProcessError:
        container_ids = []
    docker_ps_seconds = time.perf_counter() - docker_ps_start
    timing_summary["docker_ps_seconds"] = docker_ps_seconds

    container_infos = []
    for cid in container_ids:
        try:
            inspect_start = time.perf_counter()
            fmt = "{{.Name}}\t{{json .Mounts}}\t{{.State.Status}}\t{{.State.StartedAt}}\t{{json .NetworkSettings.Ports}}"
            ins = subprocess.run(["docker", "inspect", "--format", fmt, cid], check=True, text=True, capture_output=True).stdout.strip()
            if not ins:
                docker_timings.append({"container_id": cid, "inspect_seconds": time.perf_counter() - inspect_start, "skipped": True})
                continue
            parts = ins.split("\t")
            if len(parts) < 5:
                docker_timings.append({"container_id": cid, "inspect_seconds": time.perf_counter() - inspect_start, "skipped": True})
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
            docker_timings.append({
                "container_id": cid,
                "inspect_seconds": time.perf_counter() - inspect_start,
                "matched_clone": bool(host_src),
            })
        except subprocess.CalledProcessError:
            docker_timings.append({"container_id": cid, "inspect_seconds": time.perf_counter() - inspect_start, "error": True})
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

    total_seconds = time.perf_counter() - overall_start
    timing_summary["total_seconds"] = total_seconds
    timing_summary["clone_count"] = len(clones)
    timing_summary["containers_found"] = len(container_infos)
    timing_summary["docker_inspect_seconds_total"] = sum(item["inspect_seconds"] for item in docker_timings)
    timing_summary["clone_metadata_seconds_total"] = sum(item["metadata_seconds"] for item in clone_timings)
    timing_summary["skipped_snapshot_like"] = skipped_snapshot_like
    timing_summary["skipped_readonly"] = skipped_readonly

    # Sort clone timings by metadata collection duration desc for readability
    clone_timings_sorted = sorted(clone_timings, key=lambda x: x["metadata_seconds"], reverse=True)
    top_clone_timings = clone_timings_sorted[:5]
    docker_timings_sorted = sorted(docker_timings, key=lambda x: x["inspect_seconds"], reverse=True)[:5]
    logger.info(
        "list_clone_subvolumes_with_containers timings: summary=%s top_clone_timings=%s top_docker_timings=%s",
        json.dumps(timing_summary, ensure_ascii=False, default=str),
        json.dumps(top_clone_timings, ensure_ascii=False, default=str),
        json.dumps(docker_timings_sorted, ensure_ascii=False, default=str),
    )

    return clones


def get_clone_detail(root_data_dir: str, main_data_dir: str, identifier: str) -> Dict[str, Any]:
    clones = list_clone_subvolumes_with_containers(root_data_dir, main_data_dir)
    for clone in clones:
        name = clone.get("name")
        container_name = clone.get("container_name")
        if identifier == name or (container_name and identifier == container_name):
            detail = dict(clone)
            path = Path(detail["path"])
            meta = read_snaplicator_metadata(path)
            detail["metadata"] = meta
            if isinstance(meta.get("description"), str) and meta["description"].strip():
                detail["description"] = meta["description"].strip()
            detail["readonly"] = _is_readonly_subvolume(path)
            detail["created_at"] = meta.get("created_at")
            detail["refreshed_at"] = meta.get("refreshed_at")
            detail["reset_at"] = meta.get("reset_at")
            detail["reset_from_snapshot"] = meta.get("reset_from_snapshot")
            detail["exists"] = path.exists()
            return detail
    raise FileNotFoundError(f"Clone not found for identifier: {identifier}")


def get_clone_usage_summary(root_data_dir: str, main_data_dir: str, identifier: str) -> Dict[str, Any]:
    detail = get_clone_detail(root_data_dir, main_data_dir, identifier)
    clone_path = Path(detail["path"])
    if not clone_path.exists() or not _is_btrfs_subvolume(clone_path):
        raise FileNotFoundError(f"Clone path not found or not a btrfs subvolume: {clone_path}")

    usage_b = _get_subvolume_usage_bytes(clone_path)
    fs_size_b, fs_used_b = _get_fs_totals_bytes(Path(root_data_dir))
    return {
        "clone": detail.get("name"),
        "path": detail.get("path"),
        "container_name": detail.get("container_name"),
        "usage_bytes": usage_b,
        "fs_size_bytes": fs_size_b,
        "fs_used_bytes": fs_used_b,
        "calculated_at": datetime.now().isoformat(),
    }


def get_fs_usage_summary(root_data_dir: str) -> Dict[str, Any]:
    root = Path(root_data_dir)
    if not root.exists():
        raise FileNotFoundError(f"Root path not found: {root}")
    size_b, used_b = _get_fs_totals_bytes(root)
    return {
        "root_data_dir": str(root),
        "fs_size_bytes": size_b,
        "fs_used_bytes": used_b,
        "calculated_at": datetime.now().isoformat(),
    }


def create_clone_snapshot(
    root_data_dir: str,
    main_data_dir: str,
    identifier: str,
    description: Optional[str] = None,
    previous_snapshot: Optional[str] = None,
    retention_days: int = 14,
    insert_before: Optional[str] = None,
) -> Dict[str, Any]:
    detail = get_clone_detail(root_data_dir, main_data_dir, identifier)
    clone_path = Path(detail["path"])
    if not clone_path.exists() or not _is_btrfs_subvolume(clone_path):
        raise FileNotFoundError(f"Clone path not found or not a subvolume: {clone_path}")

    root = Path(root_data_dir)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot_name = f"{detail['name']}-snapshot-{ts}"
    target = root / snapshot_name
    if target.exists():
        raise FileExistsError(f"Target snapshot already exists: {target}")

    _run(["sudo", "-n", "btrfs", "subvolume", "snapshot", str(clone_path), str(target)])

    meta = {
        "name": target.name,
        "path": str(target),
        "root_data_dir": str(root),
        "main_data_dir": main_data_dir,
        "source_clone_name": detail.get("name"),
        "source_clone_display_name": detail.get("display_name"),
        "source_clone_path": detail.get("path"),
        "source_container_name": detail.get("container_name"),
        "created_at": datetime.now().isoformat(),
        "created_by": "snaplicator-api",
        "description": description,
        "type": "clone_snapshot",
        "previous_snapshot": previous_snapshot or None,
        "next_snapshot": None,
        **_retention_fields(datetime.now(), retention_days),
    }
    write_snaplicator_metadata(target, meta)

    try:
        _run(["sudo", "-n", "btrfs", "property", "set", "-ts", str(target), "ro", "true"])
    except subprocess.CalledProcessError:
        _run(["sudo", "-n", "btrfs", "property", "set", str(target), "ro", "true"])

    # Edge-insert: splice the new snapshot in front of `insert_before` so the
    # graph becomes previous_snapshot -> new -> insert_before.
    if insert_before:
        ib = insert_before.strip()
        if ib and ib != target.name:
            try:
                update_snapshot_lineage(root_data_dir, ib, previous_snapshot=target.name)
            except Exception as e:
                logger.warning("insert_before relink failed for %s -> %s: %s", ib, target.name, e)

    return {
        "name": target.name,
        "path": str(target),
        "readonly": True,
        "metadata": meta,
    }


def _set_subvolume_ro(path: Path, ro: bool) -> None:
    val = "true" if ro else "false"
    try:
        _run(["sudo", "-n", "btrfs", "property", "set", "-ts", str(path), "ro", val])
    except subprocess.CalledProcessError:
        _run(["sudo", "-n", "btrfs", "property", "set", str(path), "ro", val])


def update_snapshot_lineage(
    root_data_dir: str,
    snapshot_name: str,
    previous_snapshot: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-link a snapshot's `previous_snapshot` pointer. Snapshots are readonly
    subvolumes, so we briefly toggle ro=false to rewrite the metadata, then back.
    Lineage is display-only (test-scenario ordering); the PG data is untouched."""
    root = Path(root_data_dir).resolve()
    target = (root / snapshot_name).resolve()
    if not str(target).startswith(str(root)):
        raise PermissionError(f"Refusing to edit outside ROOT_DATA_DIR: {target}")
    if not target.exists() or not _is_btrfs_subvolume(target):
        raise FileNotFoundError(f"Snapshot not found or not a subvolume: {target}")

    prev = (previous_snapshot or "").strip() or None
    if prev == snapshot_name:
        raise ValueError("A snapshot cannot be its own previous snapshot")
    if prev is not None:
        prev_path = (root / prev).resolve()
        if not prev_path.exists() or not _is_btrfs_subvolume(prev_path):
            raise FileNotFoundError(f"Previous snapshot not found: {prev}")

    meta = dict(read_snaplicator_metadata(target) or {})
    meta["previous_snapshot"] = prev

    was_ro = _is_readonly_subvolume(target)
    if was_ro:
        _set_subvolume_ro(target, False)
    try:
        write_snaplicator_metadata(target, meta)
    finally:
        if was_ro:
            _set_subvolume_ro(target, True)
    return {"name": snapshot_name, "previous_snapshot": prev}


def reorder_snapshots(root_data_dir: str, updates: List[Dict[str, Optional[str]]]) -> Dict[str, Any]:
    """Apply several `previous_snapshot` re-assignments at once (drag-to-move).

    `updates` is a list of {"snapshot": name, "previous_snapshot": prev|None}.
    We validate the FINAL graph (all existing previous links + these overrides)
    is acyclic and self-reference-free BEFORE touching any subvolume, so an
    intermediate state can never reject a valid reorder. Then each change is
    written via the RO-toggle path. Lineage is display-only; PG data untouched."""
    root = Path(root_data_dir).resolve()
    if not isinstance(updates, list) or not updates:
        return {"applied": []}

    # Current previous-map for every snapshot under root.
    current: Dict[str, Optional[str]] = {}
    for s in list_snapshots(root_data_dir, ""):
        meta = s.get("metadata") or {}
        current[s["name"]] = (meta.get("previous_snapshot") or None) if isinstance(meta, dict) else None

    # Normalize + validate each requested change.
    normalized: List[Tuple[str, Optional[str]]] = []
    for u in updates:
        name = (u.get("snapshot") or "").strip()
        prev = (u.get("previous_snapshot") or None)
        prev = prev.strip() if isinstance(prev, str) else None
        prev = prev or None
        if not name:
            raise ValueError("Each update needs a snapshot name")
        if name not in current:
            raise FileNotFoundError(f"Snapshot not found: {name}")
        if prev is not None and prev not in current:
            raise FileNotFoundError(f"Previous snapshot not found: {prev}")
        if prev == name:
            raise ValueError(f"A snapshot cannot be its own previous: {name}")
        normalized.append((name, prev))

    # Compute final graph and reject cycles.
    final = dict(current)
    for name, prev in normalized:
        final[name] = prev
    for start in final:
        seen = set()
        cur: Optional[str] = start
        while cur is not None:
            if cur in seen:
                raise ValueError(f"Re-link would create a cycle through {start}")
            seen.add(cur)
            cur = final.get(cur)

    # Apply (skip no-ops).
    applied: List[Dict[str, Optional[str]]] = []
    for name, prev in normalized:
        if current.get(name) == prev:
            continue
        update_snapshot_lineage(root_data_dir, name, previous_snapshot=prev)
        applied.append({"snapshot": name, "previous_snapshot": prev})
    return {"applied": applied}


def cleanup_expired_snapshots(
    root_data_dir: str,
    main_data_dir: str,
    apply: bool = False,
    now_iso: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete snapshots whose retention has elapsed (expires_at <= now).

    SAFE FOR CLONES: a clone is its own btrfs subvolume; btrfs ref-counts the
    CoW extents, so deleting a snapshot never removes data a clone still
    references. Only the snapshot subvolume goes away.

    - Skips permanent snapshots (retention_days==0) and any without expires_at.
    - Before deleting, re-points surviving children to their nearest non-deleted
      ancestor so the lineage chain stays connected (display-only).
    - apply=False (default) is a dry run: reports candidates, deletes nothing.
    Returns a summary dict; also logged."""
    now = datetime.fromisoformat(now_iso) if now_iso else datetime.now()

    snaps = list_snapshots(root_data_dir, main_data_dir)
    prev_of: Dict[str, Optional[str]] = {}
    for s in snaps:
        m = s.get("metadata") or {}
        prev_of[s["name"]] = (m.get("previous_snapshot") or None) if isinstance(m, dict) else None

    candidates: List[Dict[str, Any]] = []
    for s in snaps:
        m = s.get("metadata") or {}
        if not isinstance(m, dict):
            continue
        rd = m.get("retention_days")
        exp = m.get("expires_at")
        if not isinstance(rd, int) or rd <= 0:  # permanent or unset
            continue
        if not exp:
            continue
        try:
            exp_dt = datetime.fromisoformat(exp)
        except Exception:
            continue
        if exp_dt <= now:
            candidates.append({"name": s["name"], "expires_at": exp})

    to_delete = {c["name"] for c in candidates}

    def first_alive_ancestor(name: str) -> Optional[str]:
        p = prev_of.get(name)
        seen: set = set()
        while p is not None and p in to_delete and p not in seen:
            seen.add(p)
            p = prev_of.get(p)
        return p

    results: List[Dict[str, Any]] = []
    if apply and to_delete:
        # 1) heal surviving children that point at a soon-deleted node
        for s in snaps:
            child = s["name"]
            if child in to_delete:
                continue
            if prev_of.get(child) in to_delete:
                new_prev = first_alive_ancestor(child)
                try:
                    update_snapshot_lineage(root_data_dir, child, previous_snapshot=new_prev)
                except Exception as e:
                    logger.warning("cleanup heal %s failed: %s", child, e)
        # 2) delete expired snapshots
        for c in candidates:
            entry = {"name": c["name"], "expires_at": c["expires_at"], "deleted": False}
            try:
                delete_snapshot(root_data_dir, main_data_dir, c["name"])
                entry["deleted"] = True
            except Exception as e:
                entry["error"] = str(e)
            results.append(entry)
    else:
        results = [{"name": c["name"], "expires_at": c["expires_at"], "deleted": False} for c in candidates]

    summary = {
        "now": now.isoformat(),
        "apply": apply,
        "total_snapshots": len(snaps),
        "expired": len(candidates),
        "deleted": sum(1 for r in results if r.get("deleted")),
        "results": results,
    }
    logger.info("cleanup_expired_snapshots: %s", json.dumps(summary, ensure_ascii=False, default=str))
    return summary


def list_snapshots_for_clone(root_data_dir: str, main_data_dir: str, identifier: str) -> List[Dict[str, Any]]:
    detail = get_clone_detail(root_data_dir, main_data_dir, identifier)
    root = Path(root_data_dir)
    snapshots: List[Dict[str, Any]] = []

    overall_start = time.perf_counter()
    scan_start = time.perf_counter()
    entries = [entry for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    scan_seconds = time.perf_counter() - scan_start

    timings: List[Dict[str, Any]] = []
    skipped_non_btrfs = 0
    skipped_missing_meta = 0
    skipped_unmatched = 0

    for entry in entries:
        per_start = time.perf_counter()
        path = Path(entry.path)
        if not _is_btrfs_subvolume(path):
            skipped_non_btrfs += 1
            continue
        readonly_start = time.perf_counter()
        readonly = _is_readonly_subvolume(path)
        readonly_seconds = time.perf_counter() - readonly_start
        meta_start = time.perf_counter()
        meta = read_snaplicator_metadata(path)
        meta_seconds = time.perf_counter() - meta_start
        if not isinstance(meta, dict) or not meta:
            skipped_missing_meta += 1
            continue
        source_path = meta.get("source_clone_path")
        source_name = meta.get("source_clone_name")
        if source_path == detail.get("path") or source_name == detail.get("name"):
            snapshots.append({
                "name": entry.name,
                "path": str(path),
                "readonly": readonly,
                "description": meta.get("description"),
                "metadata": meta,
            })
            timings.append({
                "name": entry.name,
                "total_seconds": time.perf_counter() - per_start,
                "readonly_seconds": readonly_seconds,
                "meta_seconds": meta_seconds,
            })
        else:
            skipped_unmatched += 1

    total_seconds = time.perf_counter() - overall_start
    if logger.isEnabledFor(logging.INFO):
        top_snapshots = sorted(timings, key=lambda x: x["total_seconds"], reverse=True)[:5]
        logger.info(
            "list_snapshots_for_clone timings: summary=%s top_entries=%s",
            json.dumps({
                "clone": detail.get("name"),
                "total_seconds": total_seconds,
                "scan_seconds": scan_seconds,
                "entries": len(entries),
                "matched": len(snapshots),
                "skipped_non_btrfs": skipped_non_btrfs,
                "skipped_missing_meta": skipped_missing_meta,
                "skipped_unmatched": skipped_unmatched,
            }, ensure_ascii=False, default=str),
            json.dumps(top_snapshots, ensure_ascii=False, default=str),
        )

    snapshots.sort(key=lambda x: x["name"])
    return snapshots 