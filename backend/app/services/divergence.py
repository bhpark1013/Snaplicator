"""Which clones and snapshots hold disk, and how much deleting them frees.

`btrfs filesystem du` answers "exclusive" per subvolume, but not the question
that matters when cleaning up: clones and snapshots taken from the same
moment share the blocks they all diverged on, so each one's exclusive is
small while deleting the whole group frees a lot. Answering that needs, for
every physical block, the set of subvolumes that reference it.

That is what this module computes, without qgroups (a filesystem setting
with a write cost on a snapshot-heavy pool): FIEMAP gives every file's
physical extents, one sweep over all subvolumes' extents splits the disk
into runs referenced by the same set of subvolumes, and the result is kept
as {set of names: bytes}. Every number the screen shows comes from that map:

  exclusive    bytes whose set is exactly {name} — freed by deleting it alone
  not_in_live  bytes whose set contains name but not the live replica — what
               it has written or kept since it was taken
  freed(sel)   bytes whose set is a subset of sel — computed client-side for
               any selection

Block-level, so it is an estimate: btrfs frees a whole extent only once its
last reference is gone, and an extent still partly referenced elsewhere
(a bookend) keeps its unreferenced blocks allocated.

One pass reads every file's extent map (~minutes on prod), so it runs in a
background thread and the API serves the cached result with its timestamp.
"""
from __future__ import annotations

import array
import fcntl
import json
import logging
import os
import re
import struct
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

BLOCK = 4096

_FS_IOC_FIEMAP = 0xC020660B
_HDR = struct.Struct("=QQLLLL")  # fm_start, fm_length, fm_flags, fm_mapped_extents, fm_extent_count, fm_reserved
_EXT = struct.Struct("=QQQQQLLLL")  # fe_logical, fe_physical, fe_length, reserved64[2], fe_flags, reserved[3]
_BATCH = 512
_LAST = 0x1
# No stable physical location: not yet allocated, or stored inside metadata.
_SKIP = 0x2 | 0x4 | 0x200  # UNKNOWN | DELALLOC | DATA_INLINE


def file_extents(path: str) -> List[Tuple[int, int]]:
    """Physical (offset, length) of every data extent of one file."""
    out: List[Tuple[int, int]] = []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return out
    try:
        start = 0
        while True:
            buf = bytearray(_HDR.size + _EXT.size * _BATCH)
            _HDR.pack_into(buf, 0, start, 0xFFFFFFFFFFFFFFFF, 0, 0, _BATCH, 0)
            try:
                fcntl.ioctl(fd, _FS_IOC_FIEMAP, buf)
            except OSError:
                return out
            mapped = _HDR.unpack_from(buf, 0)[3]
            if mapped == 0:
                break
            last = False
            for i in range(mapped):
                logical, physical, length, _, _, flags, _, _, _ = _EXT.unpack_from(
                    buf, _HDR.size + i * _EXT.size,
                )
                if not flags & _SKIP:
                    out.append((physical, length))
                if flags & _LAST:
                    last = True
                start = logical + length
            if last:
                break
    finally:
        os.close(fd)
    return out


def _run_array(max_block: int) -> array.array:
    # Block numbers fit in 32 bits up to a 16 TiB pool; halves memory on prod
    # (~2M runs per 80G subvolume).
    return array.array("I" if max_block < 2 ** 32 else "q")


def merge_blocks(extents: Iterable[Tuple[int, int]]) -> array.array:
    """Byte extents -> sorted, disjoint [start, end) block runs, flattened."""
    runs = sorted(
        (off // BLOCK, (off + length + BLOCK - 1) // BLOCK)
        for off, length in extents if length > 0
    )
    flat = _run_array(runs[-1][1] if runs else 0)
    if not runs:
        return flat
    cur_s, cur_e = runs[0]
    for s, e in runs[1:]:
        if s <= cur_e:
            if e > cur_e:
                cur_e = e
            continue
        flat.append(cur_s)
        flat.append(cur_e)
        cur_s, cur_e = s, e
    flat.append(cur_s)
    flat.append(cur_e)
    return flat


def subvolume_blocks(path: str) -> array.array:
    """Every physical block a subvolume's files reference, as merged runs."""
    extents: List[Tuple[int, int]] = []
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            extents.extend(file_extents(os.path.join(dirpath, f)))
    return merge_blocks(extents)


# Disk is swept in windows of this many blocks (4 GiB) so the sorted event
# list stays small; runs crossing a window edge are clipped to it.
WINDOW_BLOCKS = 1 << 20


def sweep(blocks: Sequence[Sequence[int]], window: int = WINDOW_BLOCKS) -> Dict[int, int]:
    """Bytes per set of referencing subvolumes, the set as a bitmask of
    indexes into blocks (bit i = blocks[i]).

    Each input is one subvolume's merged runs (disjoint within itself), so a
    run's start and end each just toggle that subvolume's bit: within a
    window, sort every boundary and XOR as you go.
    """
    n = len(blocks)
    shift = max(1, n.bit_length())
    low = (1 << shift) - 1
    cursor = [0] * n  # next run (pair index) per subvolume
    top = max((flat[-1] for flat in blocks if len(flat)), default=0)
    out: Dict[int, int] = {}
    for w0 in range(0, top, window):
        w1 = w0 + window
        evs: List[int] = []
        for i, flat in enumerate(blocks):
            j = cursor[i]
            ln = len(flat)
            while j < ln:
                s, e = flat[j], flat[j + 1]
                if s >= w1:
                    break
                s = s if s > w0 else w0
                if e > w1:
                    evs.append((s << shift) | i)
                    evs.append((w1 << shift) | i)
                    break  # continues into the next window; don't advance
                if e > s:
                    evs.append((s << shift) | i)
                    evs.append((e << shift) | i)
                j += 2
            cursor[i] = j
        evs.sort()
        mask = 0
        prev = w0
        for ev in evs:
            pos = ev >> shift
            if pos != prev:
                if mask:
                    out[mask] = out.get(mask, 0) + (pos - prev)
                prev = pos
            mask ^= 1 << (ev & low)
    return {m: nblocks * BLOCK for m, nblocks in out.items()}


def _members(mask: int) -> List[int]:
    out = []
    i = 0
    while mask:
        if mask & 1:
            out.append(i)
        mask >>= 1
        i += 1
    return out


def summarize(names: Sequence[str], refsets: Dict[int, int], live: str) -> Dict:
    """Per-subvolume numbers and pool totals from sweep()'s {bitmask: bytes}.

    Sets that include the live replica are never freed by deleting clones, so
    only their total is kept; the rest are returned (by name) for selection
    sums on the client.
    """
    live_idx = names.index(live) if live in names else -1
    per = {n: {"exclusive_bytes": 0, "not_in_live_bytes": 0, "referenced_bytes": 0} for n in names}
    live_bytes = 0
    union = 0
    groups: List[Dict] = []
    for mask, nbytes in refsets.items():
        key = _members(mask)
        union += nbytes
        has_live = live_idx in key
        if has_live:
            live_bytes += nbytes
        for i in key:
            p = per[names[i]]
            p["referenced_bytes"] += nbytes
            if not has_live:
                p["not_in_live_bytes"] += nbytes
        if len(key) == 1:
            per[names[key[0]]]["exclusive_bytes"] += nbytes
        if not has_live:
            groups.append({"names": [names[i] for i in key], "bytes": nbytes})
    groups.sort(key=lambda g: -g["bytes"])
    return {
        "live": live,
        "live_bytes": live_bytes,
        "referenced_bytes": union,
        "not_in_live_bytes": union - live_bytes,
        "subvolumes": per,
        "groups": groups,
    }


def freed_by(groups: Iterable[Dict], selection: Iterable[str]) -> int:
    """Bytes freed by deleting every subvolume in selection (block-level)."""
    sel = set(selection)
    return sum(g["bytes"] for g in groups if set(g["names"]) <= sel)


# ---------------------------------------------------------------------------
# Background measurement + cache

_LOCK = threading.Lock()
_running = False
_last_error: Optional[str] = None
# A full pass reads every extent map on the pool; once every few hours is
# plenty for a cleanup screen, and the UI can ask for a fresh one.
INTERVAL_SECONDS = 6 * 3600


def _cache_path() -> Path:
    env = os.environ.get("DIVERGENCE_CACHE_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".snaplicator" / "divergence_cache.json"


def load_cached() -> Optional[Dict]:
    try:
        p = _cache_path()
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _store(data: Dict) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def measure(root_data_dir: str, live_name: str) -> Dict:
    """One full pass (blocking). Subvolumes are the btrfs subvolumes directly
    under root_data_dir, the same set the usage cache tracks."""
    from .usage import all_subvolume_paths

    t0 = time.monotonic()
    paths = all_subvolume_paths(root_data_dir)
    names = [os.path.basename(p) for p in paths]
    blocks = []
    for p in paths:
        blocks.append(subvolume_blocks(p))
    refsets = sweep(blocks)
    out = summarize(names, refsets, live_name)
    out["measured_at"] = datetime.now().isoformat(timespec="seconds")
    out["duration_seconds"] = round(time.monotonic() - t0, 1)
    return out


def _worker(root_data_dir: str, live_name: str) -> None:
    global _running, _last_error
    try:
        data = measure(root_data_dir, live_name)
        with _LOCK:
            _store(data)
            _last_error = None
        logger.info("divergence measured in %ss", data["duration_seconds"])
    except Exception as e:
        logger.warning("divergence measure failed: %s", e)
        with _LOCK:
            _last_error = str(e)
    finally:
        with _LOCK:
            _running = False


def start_refresh(root_data_dir: str, live_name: str) -> bool:
    """Start a background pass unless one is running. True if started."""
    global _running
    with _LOCK:
        if _running:
            return False
        _running = True
    threading.Thread(target=_worker, args=(root_data_dir, live_name), daemon=True).start()
    return True


def status() -> Dict:
    with _LOCK:
        return {"running": _running, "error": _last_error}


def is_stale(data: Optional[Dict], max_age_seconds: int = INTERVAL_SECONDS) -> bool:
    if not data or not data.get("measured_at"):
        return True
    try:
        age = datetime.now() - datetime.fromisoformat(data["measured_at"])
        return age.total_seconds() > max_age_seconds
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Response shaping (pure; the route only adds settings and fs totals)

_TS = re.compile(r"(\d{8})-(\d{6})")


def live_name(root_data_dir: str, main_data_dir: str) -> str:
    """Subvolume name of the live replica (MAIN_DATA_DIR may be relative)."""
    p = Path(main_data_dir)
    if p.is_absolute():
        try:
            p = p.relative_to(root_data_dir)
        except ValueError:
            return p.name
    return p.parts[0] if p.parts else ""


def describe(name: str, live: str) -> Dict:
    """Kind and creation time, read from the subvolume name: clones and
    snapshots are named with the timestamp they were taken at, and a
    snapshot's own timestamp is the last one in its name."""
    if name == live:
        kind = "live"
    elif "-snapshot-" in name or name.startswith(f"{live}-snapshot"):
        kind = "snapshot"
    else:
        kind = "clone"
    stamps = _TS.findall(name)
    created = None
    if stamps:
        d, t = stamps[-1]
        created = f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}"
    return {"kind": kind, "created_at": created}


# Sets smaller than this are left out of the response: on prod ~10k sets
# exist, 630 are >= 1 MiB and they cover all but ~0.3 GiB of 184 GiB.
MIN_GROUP_BYTES = 1 << 20


def build_response(data: Optional[Dict], live: str, fs_size: Optional[int], fs_used: Optional[int], status: Dict) -> Dict:
    items = []
    if data:
        for name, nums in data.get("subvolumes", {}).items():
            items.append({"name": name, **describe(name, live), **nums})
        items.sort(key=lambda i: (-i.get("not_in_live_bytes", 0), i["name"]))
    return {
        "measured_at": (data or {}).get("measured_at"),
        "duration_seconds": (data or {}).get("duration_seconds"),
        "live": live,
        "live_bytes": (data or {}).get("live_bytes"),
        "referenced_bytes": (data or {}).get("referenced_bytes"),
        "not_in_live_bytes": (data or {}).get("not_in_live_bytes"),
        "fs_size_bytes": fs_size,
        "fs_used_bytes": fs_used,
        "items": items,
        "groups": [g for g in (data or {}).get("groups", []) if g["bytes"] >= MIN_GROUP_BYTES],
        "refreshing": status.get("running", False),
        "error": status.get("error"),
    }
