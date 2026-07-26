"""Planning-logic tests.

Two kinds of input:
  * captured fixtures — real `findmnt --json` / `lsblk --json` output from
    the two live hosts. We know their disk topology, so the correct answer
    is known in advance (answer-key testing).
  * synthetic inputs — hand-built dicts for shapes the real hosts don't
    exhibit (fresh VM, bare disk, nothing-fits, ...).
"""

import json
from pathlib import Path

import pytest

from snaplicator_init.plan import (
    GiB,
    bare_disk_candidates,
    make_plan,
    mount_candidates,
    required_bytes,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def prod():
    return load("fixture-prod-findmnt.json"), load("fixture-prod-lsblk.json")


def dev():
    return load("fixture-dev-findmnt.json"), load("fixture-dev-lsblk.json")


# ── synthetic input builders ──────────────────────────────────────────

def fs_entry(target, source, fstype, avail, size=None, options="rw,relatime", children=None):
    e = {
        "target": target,
        "source": source,
        "fstype": fstype,
        "size": size if size is not None else avail * 2,
        "avail": avail,
        "used": 0,
        "options": options,
    }
    if children:
        e["children"] = children
    return e


def findmnt_of(*entries):
    root = dict(entries[0])
    root["children"] = list(root.get("children") or []) + [dict(e) for e in entries[1:]]
    return {"filesystems": [root]}


def lsblk_of(*devices):
    return {"blockdevices": list(devices)}


def bare_disk(name, size):
    return {"name": name, "type": "disk", "fstype": None, "size": size,
            "mountpoint": None, "ro": False}


EMPTY_LSBLK = {"blockdevices": []}


# ── requirement formula ───────────────────────────────────────────────

def test_required_is_twice_payload():
    assert required_bytes(100 * GiB) == 200 * GiB


def test_required_floor_10gib():
    assert required_bytes(1 * GiB) == 10 * GiB
    assert required_bytes(0) == 10 * GiB


def test_exactly_required_avail_fits():
    fm = findmnt_of(fs_entry("/", "/dev/sda1", "ext4", avail=20 * GiB))
    plan = make_plan(fm, EMPTY_LSBLK, payload_bytes=10 * GiB)
    assert plan["status"] == "ok"
    assert plan["chosen"]["target"] == "/"


# ── captured fixture: prod host ───────────────────────────────────────

def test_prod_btrfs_pool_is_priority1_candidate():
    fm, lb = prod()
    cands = mount_candidates(fm)
    pool = [c for c in cands if c["target"] == "/data/snaplicator"]
    assert len(pool) == 1
    assert pool[0]["priority"] == 1
    assert pool[0]["fstype"] == "btrfs"
    assert pool[0]["action"] == "subvolume"


def test_prod_pseudo_filesystems_all_excluded():
    fm, _ = prod()
    for c in mount_candidates(fm):
        assert c["source"].startswith("/dev/")
        assert not c["target"].startswith(("/proc", "/sys", "/run", "/dev"))


def test_prod_has_no_bare_disk():
    # prod's sdb carries a leftover zfs_member partition — NOT bare; sda is
    # fully partitioned. Nothing on prod may be offered for formatting.
    _, lb = prod()
    assert bare_disk_candidates(lb) == []


def test_prod_plan_chooses_existing_pool():
    fm, lb = prod()
    plan = make_plan(fm, lb, payload_bytes=78 * GiB)
    assert plan["status"] == "ok"
    assert plan["chosen"]["target"] == "/data/snaplicator"
    assert plan["chosen"]["action"] == "subvolume"
    # root ext4 has ~5 GiB free — the btrfs pool is the only eligible fit
    assert plan["auto_selected"] is True
    assert plan["needs_confirmation"] is False


def test_prod_golden_plan():
    """Plan JSON is the stage-2 executor's input contract — freeze it."""
    fm, lb = prod()
    plan = make_plan(fm, lb, payload_bytes=78 * GiB)
    golden_path = FIXTURES / "golden-prod-plan.json"
    assert plan == json.loads(golden_path.read_text(encoding="utf-8"))


# ── captured fixture: dev host (btrfs on an LVM logical volume) ──────

def test_dev_lvm_btrfs_is_priority1():
    fm, _ = dev()
    pool = [c for c in mount_candidates(fm) if c["fstype"] == "btrfs"]
    assert len(pool) == 1
    assert pool[0]["target"] == "/data/snaplicator"
    assert pool[0]["source"].startswith("/dev/mapper/")
    assert pool[0]["priority"] == 1


def test_dev_has_no_bare_disk():
    _, lb = dev()
    assert bare_disk_candidates(lb) == []


# ── synthetic: fresh machine, no btrfs anywhere ──────────────────────

def test_fresh_vm_falls_to_loopback():
    fm = findmnt_of(fs_entry("/", "/dev/sda1", "ext4", avail=100 * GiB))
    plan = make_plan(fm, EMPTY_LSBLK, payload_bytes=20 * GiB)
    assert plan["status"] == "ok"
    assert plan["chosen"]["kind"] == "fs-loopback"
    assert plan["chosen"]["action"] == "loopback-file"
    assert plan["auto_selected"] is True


def test_btrfs_too_small_falls_to_ext4():
    fm = findmnt_of(
        fs_entry("/", "/dev/sda1", "ext4", avail=500 * GiB),
        fs_entry("/small-pool", "/dev/sdb1", "btrfs", avail=15 * GiB),
    )
    plan = make_plan(fm, EMPTY_LSBLK, payload_bytes=50 * GiB)  # requires 100 GiB
    assert plan["chosen"]["fstype"] == "ext4"
    small = [c for c in plan["candidates"] if c["target"] == "/small-pool"]
    assert small[0]["fits"] is False


# ── synthetic: bare disks ─────────────────────────────────────────────

def test_bare_disk_listed_but_never_auto_selected():
    fm = findmnt_of(fs_entry("/", "/dev/sda1", "ext4", avail=5 * GiB))
    lb = lsblk_of(bare_disk("sdc", 500 * GiB))
    plan = make_plan(fm, lb, payload_bytes=50 * GiB)
    assert plan["status"] == "no-fit"          # the fitting disk is priority 3
    assert plan["chosen"] is None
    disks = [c for c in plan["candidates"] if c["kind"] == "bare-disk"]
    assert disks[0]["target"] == "/dev/sdc" and disks[0]["fits"] is True
    assert any("--format-disk /dev/sdc" in r for r in plan["remediation"])


def test_disk_with_children_is_not_bare():
    lb = lsblk_of({
        "name": "sdb", "type": "disk", "fstype": None, "size": 500 * GiB,
        "mountpoint": None, "ro": False,
        "children": [{"name": "sdb1", "type": "part", "fstype": "zfs_member",
                      "size": 500 * GiB, "mountpoint": None, "ro": False}],
    })
    assert bare_disk_candidates(lb) == []


def test_disk_with_foreign_signature_is_not_bare():
    lb = lsblk_of({"name": "sdd", "type": "disk", "fstype": "zfs_member",
                   "size": 500 * GiB, "mountpoint": None, "ro": False})
    assert bare_disk_candidates(lb) == []


# ── synthetic: ranking ────────────────────────────────────────────────

def test_multiple_fits_prefers_btrfs_and_asks_confirmation():
    fm = findmnt_of(
        fs_entry("/", "/dev/sda1", "ext4", avail=500 * GiB),
        fs_entry("/pool", "/dev/sdb1", "btrfs", avail=300 * GiB),
    )
    plan = make_plan(fm, EMPTY_LSBLK, payload_bytes=50 * GiB)
    assert plan["chosen"]["fstype"] == "btrfs"     # priority beats size
    assert plan["needs_confirmation"] is True
    assert plan["auto_selected"] is False


def test_root_filesystem_ranks_last_within_priority():
    fm = findmnt_of(
        fs_entry("/", "/dev/sda1", "ext4", avail=900 * GiB),
        fs_entry("/data", "/dev/sdb1", "ext4", avail=200 * GiB),
    )
    plan = make_plan(fm, EMPTY_LSBLK, payload_bytes=50 * GiB)
    assert plan["chosen"]["target"] == "/data"     # despite less free space


# ── synthetic: exclusion rules ────────────────────────────────────────

def test_readonly_mount_excluded():
    fm = findmnt_of(
        fs_entry("/", "/dev/sda1", "ext4", avail=5 * GiB),
        fs_entry("/ro-data", "/dev/sdb1", "ext4", avail=900 * GiB,
                 options="ro,relatime"),
    )
    assert all(c["target"] != "/ro-data" for c in mount_candidates(fm))


def test_boot_and_snap_targets_excluded():
    fm = findmnt_of(
        fs_entry("/", "/dev/sda1", "ext4", avail=100 * GiB),
        fs_entry("/boot", "/dev/sda2", "ext4", avail=900 * GiB),
        fs_entry("/snap/foo/1", "/dev/sdb1", "ext4", avail=900 * GiB),
    )
    targets = {c["target"] for c in mount_candidates(fm)}
    assert targets == {"/"}


def test_network_and_pseudo_sources_excluded():
    fm = findmnt_of(
        fs_entry("/", "/dev/sda1", "ext4", avail=100 * GiB),
        fs_entry("/nfs", "server:/export", "ext4", avail=900 * GiB),
        fs_entry("/tmp", "tmpfs", "tmpfs", avail=900 * GiB),
    )
    targets = {c["target"] for c in mount_candidates(fm)}
    assert targets == {"/"}


def test_same_source_mounted_twice_dedupes():
    # btrfs subvolume mounts: same filesystem visible at several targets
    fm = findmnt_of(
        fs_entry("/", "/dev/sda1", "ext4", avail=100 * GiB),
        fs_entry("/pool", "/dev/sdb1", "btrfs", avail=300 * GiB),
        fs_entry("/pool/clones/c1", "/dev/sdb1[/clones/c1]", "btrfs", avail=300 * GiB),
    )
    btrfs = [c for c in mount_candidates(fm) if c["fstype"] == "btrfs"]
    assert len(btrfs) == 1
    assert btrfs[0]["target"] == "/pool"


# ── synthetic: no-fit remediation ────────────────────────────────────

def test_nothing_fits_remediation():
    fm = findmnt_of(fs_entry("/", "/dev/sda1", "ext4", avail=30 * GiB))
    top = [{"name": "public.events_log", "bytes": 400 * GiB}]
    plan = make_plan(fm, EMPTY_LSBLK, payload_bytes=300 * GiB, top_tables=top)
    assert plan["status"] == "no-fit"
    joined = "\n".join(plan["remediation"])
    assert "short by" in joined
    assert "public.events_log" in joined          # narrow-the-scope hint
    assert "attach" in joined


# ── --data-dir pinning ───────────────────────────────────────────────

def test_data_dir_pins_choice():
    fm = findmnt_of(
        fs_entry("/", "/dev/sda1", "ext4", avail=500 * GiB),
        fs_entry("/data", "/dev/sdb1", "ext4", avail=200 * GiB),
    )
    plan = make_plan(fm, EMPTY_LSBLK, payload_bytes=50 * GiB, data_dir="/data/snaplicator")
    assert plan["chosen"]["target"] == "/data"
    assert plan["auto_selected"] is True


def test_data_dir_too_small_needs_force():
    fm = findmnt_of(
        fs_entry("/", "/dev/sda1", "ext4", avail=500 * GiB),
        fs_entry("/data", "/dev/sdb1", "ext4", avail=20 * GiB),
    )
    no_force = make_plan(fm, EMPTY_LSBLK, payload_bytes=50 * GiB, data_dir="/data/x")
    assert no_force["status"] == "no-fit"
    assert any("--force" in r for r in no_force["remediation"])

    forced = make_plan(fm, EMPTY_LSBLK, payload_bytes=50 * GiB, data_dir="/data/x", force=True)
    assert forced["chosen"]["target"] == "/data"
