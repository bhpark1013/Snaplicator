"""Pure planning logic: parsed findmnt/lsblk JSON in, plan dict out.

No subprocess calls, no TTY detection, no filesystem access — everything
here is deterministic on its inputs so the whole decision surface is unit-
testable against captured fixtures (see cli/tests/fixtures/).

Priorities (execution preference among fitting candidates):
  1  existing btrfs mount      -> create a subvolume, nothing to format
  2  real local fs free space  -> loopback file + mkfs.btrfs (standard path)
  3  bare block device         -> mkfs.btrfs the device; NEVER auto-selected
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

GiB = 1024 ** 3
FLOOR_BYTES = 10 * GiB
PAYLOAD_MULTIPLIER = 2

# Filesystems whose free space can host a loopback file (priority 2) or,
# for btrfs, a subvolume (priority 1). Everything else — pseudo filesystems
# (proc, sysfs, tmpfs, ...), network filesystems, overlay, squashfs — is
# not a home for the pool.
REAL_FS = {"ext4", "xfs", "btrfs"}

# Mount targets that are never candidates even when the fstype qualifies.
EXCLUDED_TARGET_PREFIXES = ("/boot", "/snap")


def required_bytes(payload_bytes: int) -> int:
    """Pool size requirement: payload × 2, floor 10 GiB.

    Empirical grounding (prod, 2026-07): a 78 GiB payload grew to ~280 GiB
    of pool usage over months of snapshot/clone retention (~3.6×). ×2 is
    enough to install and run comfortably; long-tail growth is handled by
    online resize plus the runtime usage alert, not by over-allocating on
    day one.
    """
    return max(PAYLOAD_MULTIPLIER * int(payload_bytes), FLOOR_BYTES)


def _walk_findmnt(node: Dict[str, Any], out: List[Dict[str, Any]]) -> None:
    out.append(node)
    for child in node.get("children") or []:
        _walk_findmnt(child, out)


def flatten_findmnt(findmnt_raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """findmnt --json emits a tree (mounts nested under their parents);
    flatten it to one list of mount entries."""
    out: List[Dict[str, Any]] = []
    for fs in findmnt_raw.get("filesystems") or []:
        _walk_findmnt(fs, out)
    return out


def mount_candidates(findmnt_raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Priority 1/2 candidates: mounted real filesystems with usable space.

    Filters, in order:
      * source must be a block device path (/dev/...) — this alone drops
        every pseudo filesystem (proc, sysfs, tmpfs, cgroup2, ...) and
        network filesystems (server:/path sources)
      * fstype allowlist (REAL_FS) — drops squashfs images, overlay, vfat
      * read-only mounts (a pool needs writes)
      * /boot*, /snap* targets (system areas)
      * duplicates of the same source (btrfs subvolume mounts, bind mounts)
        collapse to the entry with the shortest target path
    """
    by_source: Dict[str, Dict[str, Any]] = {}
    for m in flatten_findmnt(findmnt_raw):
        source = m.get("source") or ""
        fstype = m.get("fstype") or ""
        target = m.get("target") or ""
        # btrfs subvolume mounts look like /dev/sdc1[/subvol]; normalize to
        # the device path so they dedupe with the filesystem's root mount.
        device = source.split("[", 1)[0]
        if not device.startswith("/dev/"):
            continue
        if fstype not in REAL_FS:
            continue
        if target.startswith(EXCLUDED_TARGET_PREFIXES):
            continue
        options = (m.get("options") or "").split(",")
        if "ro" in options:
            continue
        prev = by_source.get(device)
        if prev is None or len(target) < len(prev.get("target") or ""):
            m = dict(m)
            m["source"] = device
            by_source[device] = m

    candidates = []
    for m in by_source.values():
        is_btrfs = m["fstype"] == "btrfs"
        candidates.append({
            "priority": 1 if is_btrfs else 2,
            "kind": "btrfs-mount" if is_btrfs else "fs-loopback",
            "target": m["target"],
            "source": m["source"],
            "fstype": m["fstype"],
            "size_bytes": int(m.get("size") or 0),
            "avail_bytes": int(m.get("avail") or 0),
            "action": "subvolume" if is_btrfs else "loopback-file",
        })
    return candidates


def _is_truthy_ro(value: Any) -> bool:
    return value in (True, 1, "1", "true")


def bare_disk_candidates(lsblk_raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Priority 3 candidates: whole disks with no partitions, no filesystem
    signature, not mounted, not read-only.

    Deliberately strict — a disk carrying ANY child node or foreign
    filesystem signature (a leftover zfs_member partition, say) is not
    "bare" and must never be offered for formatting. Loop devices and
    partitions are excluded by type; unformatted partitions are a possible
    future extension, not part of this rule.

    lsblk's fstype comes from the blkid cache and can be stale for mounted
    volumes, which is why mounted filesystems are discovered via findmnt
    instead; stage 2 re-verifies bare disks with blkid as root before any
    format.
    """
    candidates = []
    for dev in lsblk_raw.get("blockdevices") or []:
        if dev.get("type") != "disk":
            continue
        if dev.get("children"):
            continue
        if dev.get("fstype"):
            continue
        if dev.get("mountpoint"):
            continue
        if _is_truthy_ro(dev.get("ro")):
            continue
        name = dev.get("name") or ""
        candidates.append({
            "priority": 3,
            "kind": "bare-disk",
            "target": f"/dev/{name}",
            "source": f"/dev/{name}",
            "fstype": None,
            "size_bytes": int(dev.get("size") or 0),
            "avail_bytes": int(dev.get("size") or 0),
            "action": "format-disk",
        })
    return candidates


def _rank_key(candidate: Dict[str, Any]):
    # priority ascending; the root filesystem ranks last within its priority
    # (filling the OS filesystem endangers the whole machine); then larger
    # free space first.
    return (
        candidate["priority"],
        1 if candidate["target"] == "/" else 0,
        -candidate["avail_bytes"],
    )


def _find_mount_for_path(candidates: List[Dict[str, Any]], path: str) -> Optional[Dict[str, Any]]:
    """Longest-prefix match: which candidate mount contains `path`?"""
    best = None
    for c in candidates:
        if c["priority"] == 3:
            continue
        target = c["target"]
        prefix = target if target.endswith("/") else target + "/"
        if path == target or path.startswith(prefix):
            if best is None or len(target) > len(best["target"]):
                best = c
    return best


def make_plan(
    findmnt_raw: Dict[str, Any],
    lsblk_raw: Dict[str, Any],
    payload_bytes: int,
    data_dir: Optional[str] = None,
    force: bool = False,
    top_tables: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Assemble the full plan.

      status              "ok" (a home exists) | "no-fit"
      payload_bytes / required_bytes
      candidates          every discovered candidate, ranked, each with "fits"
      chosen              the selected candidate or None
      auto_selected       True when exactly one eligible candidate existed
                          (or --data-dir pinned the choice)
      needs_confirmation  True when several fit and a human should pick
      remediation         actionable strings, non-empty when status == "no-fit"
    """
    required = required_bytes(payload_bytes)
    candidates = mount_candidates(findmnt_raw) + bare_disk_candidates(lsblk_raw)
    for c in candidates:
        c["fits"] = c["avail_bytes"] >= required
    candidates.sort(key=_rank_key)

    chosen: Optional[Dict[str, Any]] = None
    auto_selected = False
    needs_confirmation = False
    remediation: List[str] = []

    if data_dir:
        pinned = _find_mount_for_path(candidates, data_dir)
        if pinned is None:
            remediation.append(
                f"--data-dir {data_dir} is not on any eligible mounted filesystem"
            )
        elif not pinned["fits"] and not force:
            remediation.append(
                f"--data-dir {data_dir} is on {pinned['target']} with only "
                f"{pinned['avail_bytes']} bytes free (< {required} required); "
                "pass --force to override the space check"
            )
        else:
            chosen = pinned
            auto_selected = True
    else:
        eligible = [c for c in candidates if c["fits"] and c["priority"] < 3]
        if eligible:
            chosen = eligible[0]
            auto_selected = len(eligible) == 1
            needs_confirmation = len(eligible) > 1

    if chosen is None and not remediation:
        best = max(candidates, key=lambda c: c["avail_bytes"], default=None)
        if best is not None:
            shortfall = required - best["avail_bytes"]
            remediation.append(
                f"need {required} bytes, largest candidate {best['target']} "
                f"has {best['avail_bytes']} free (short by {shortfall})"
            )
        for d in (c for c in candidates if c["priority"] == 3 and c["fits"]):
            remediation.append(
                f"bare disk {d['target']} ({d['size_bytes']} bytes) fits — "
                "formatting is destructive and never automatic; "
                f"pass --format-disk {d['target']} explicitly (stage 2)"
            )
        if top_tables:
            remediation.append(
                "narrow the replication scope (--tables/--schemas); largest tables: "
                + ", ".join(f"{t['name']} ({t['bytes']})" for t in top_tables[:5])
            )
        remediation.append("attach or free up a disk, then re-run")

    return {
        "version": 1,
        "payload_bytes": int(payload_bytes),
        "required_bytes": required,
        "candidates": candidates,
        "chosen": chosen,
        "auto_selected": auto_selected,
        "needs_confirmation": needs_confirmation,
        "remediation": remediation,
        "status": "ok" if chosen is not None else "no-fit",
    }
