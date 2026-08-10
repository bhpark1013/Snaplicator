"""Executor tests.

`build_steps` is pure (plan dict → step dicts), so every safety gate and
every command line is asserted without root. `Runner` is exercised with an
injected fake `run_cmd` plus tmp_path files — no machine mutation here;
the real-machine e2e (loopback + subvolume, with teardown) runs on the dev
host outside pytest.
"""

import json
import subprocess
from pathlib import Path

import pytest

from snaplicator_init.execute import (
    FSTAB_TAG,
    ExecuteError,
    Runner,
    build_steps,
    render_steps,
)
from snaplicator_init.plan import GiB, make_plan

from test_plan import EMPTY_LSBLK, bare_disk, findmnt_of, fs_entry, lsblk_of

FIXTURES = Path(__file__).parent / "fixtures"


def golden_plan():
    return json.loads((FIXTURES / "golden-prod-plan.json").read_text(encoding="utf-8"))


def by_id(built, step_id):
    matches = [s for s in built["steps"] if s["id"] == step_id]
    assert len(matches) == 1, f"expected exactly one step {step_id!r}"
    return matches[0]


def loopback_plan(avail=100 * GiB, payload=20 * GiB):
    fm = findmnt_of(fs_entry("/", "/dev/sda1", "ext4", avail=avail))
    return make_plan(fm, EMPTY_LSBLK, payload_bytes=payload)


# ── subvolume action ─────────────────────────────────────────────────

def test_golden_plan_builds_subvolume_steps():
    built = build_steps(golden_plan())
    assert built["action"] == "subvolume"
    assert built["pool_dir"] == "/data/snaplicator/snaplicator"
    create = by_id(built, "create-subvolume")
    assert create["do"]["cmd"] == [
        "btrfs", "subvolume", "create", "/data/snaplicator/snaplicator"]
    assert create["check"]["cmd"] == [
        "btrfs", "subvolume", "show", "/data/snaplicator/snaplicator"]
    verify = by_id(built, "verify-btrfs-mount")
    assert verify["type"] == "assert"
    assert verify["check"]["cmd"] == [
        "findmnt", "--noheadings", "-t", "btrfs", "/data/snaplicator"]


def test_data_dir_param_overrides_pool_dir():
    built = build_steps(golden_plan(), data_dir="/data/snaplicator/e2e")
    assert built["pool_dir"] == "/data/snaplicator/e2e"


def test_data_dir_recorded_in_plan_is_used():
    plan = golden_plan()
    plan["data_dir"] = "/data/snaplicator/from-plan"
    assert build_steps(plan)["pool_dir"] == "/data/snaplicator/from-plan"


# ── loopback action ──────────────────────────────────────────────────

def test_loopback_paths_and_allocation():
    plan = loopback_plan()
    built = build_steps(plan)
    assert built["action"] == "loopback-file"
    assert built["pool_dir"] == "/snaplicator"
    alloc = by_id(built, "allocate-image")
    assert alloc["do"]["cmd"] == [
        "fallocate", "-l", str(plan["required_bytes"]), "/snaplicator.img"]
    assert alloc["check"]["file_min_size"]["min_bytes"] == plan["required_bytes"]


def test_loopback_mkfs_never_forces():
    built = build_steps(loopback_plan())
    assert "-f" not in by_id(built, "mkfs")["do"]["cmd"]


def test_loopback_free_space_assert_superseded_by_existing_image():
    space = by_id(build_steps(loopback_plan()), "verify-free-space")
    assert space["type"] == "assert"
    assert space["unless"]["file_min_size"]["path"] == "/snaplicator.img"


def test_loopback_direct_io_is_best_effort():
    dio = by_id(build_steps(loopback_plan()), "loop-direct-io")
    assert dio["best_effort"] is True
    assert dio["do"] == {"loop_direct_io": {"img": "/snaplicator.img"}}


def test_loopback_fstab_line_persists_and_is_tagged():
    fstab = by_id(build_steps(loopback_plan()), "fstab")
    line = fstab["do"]["append_line"]["line"]
    assert line.startswith("/snaplicator.img /snaplicator btrfs loop,nofail")
    assert line.endswith(FSTAB_TAG)
    assert fstab["do"]["append_line"]["path"] == "/etc/fstab"


# ── format-disk action ───────────────────────────────────────────────

def test_format_disk_requires_matching_bare_candidate():
    with pytest.raises(ExecuteError, match="not a bare-disk candidate"):
        build_steps(loopback_plan(), format_disk="/dev/sdz")


def test_format_disk_too_small_refused():
    fm = findmnt_of(fs_entry("/", "/dev/sda1", "ext4", avail=5 * GiB))
    plan = make_plan(fm, lsblk_of(bare_disk("sdc", 20 * GiB)),
                     payload_bytes=50 * GiB)
    with pytest.raises(ExecuteError, match="too small"):
        build_steps(plan, format_disk="/dev/sdc")


def test_format_disk_steps():
    fm = findmnt_of(fs_entry("/", "/dev/sda1", "ext4", avail=5 * GiB))
    plan = make_plan(fm, lsblk_of(bare_disk("sdc", 500 * GiB)),
                     payload_bytes=50 * GiB)
    built = build_steps(plan, format_disk="/dev/sdc")
    assert built["action"] == "format-disk"
    assert built["pool_dir"] == "/data/snaplicator"
    bare = by_id(built, "verify-bare")
    assert bare["check"]["cmd_fails"] == ["blkid", "-p", "/dev/sdc"]
    assert "btrfs" in bare["unless"]["cmd_contains"]["needle"]
    assert by_id(built, "mkfs")["do"]["cmd"] == ["mkfs.btrfs", "/dev/sdc"]
    assert by_id(built, "fstab")["do"]["fstab_uuid"]["mnt"] == "/data/snaplicator"


# ── safety gates ─────────────────────────────────────────────────────

def test_no_fit_plan_refused_with_bare_disk_hint():
    fm = findmnt_of(fs_entry("/", "/dev/sda1", "ext4", avail=5 * GiB))
    plan = make_plan(fm, lsblk_of(bare_disk("sdc", 500 * GiB)),
                     payload_bytes=50 * GiB)
    with pytest.raises(ExecuteError, match=r"--format-disk /dev/sdc"):
        build_steps(plan)


def test_needs_confirmation_requires_yes():
    fm = findmnt_of(
        fs_entry("/", "/dev/sda1", "ext4", avail=500 * GiB),
        fs_entry("/pool", "/dev/sdb1", "btrfs", avail=300 * GiB),
    )
    plan = make_plan(fm, EMPTY_LSBLK, payload_bytes=50 * GiB)
    with pytest.raises(ExecuteError, match="--yes"):
        build_steps(plan)
    assert build_steps(plan, assume_yes=True)["action"] == "subvolume"


def test_unknown_plan_version_refused():
    plan = golden_plan()
    plan["version"] = 2
    with pytest.raises(ExecuteError, match="version"):
        build_steps(plan)


def test_relative_data_dir_refused():
    with pytest.raises(ExecuteError, match="absolute"):
        build_steps(golden_plan(), data_dir="relative/path")


# ── rendering ────────────────────────────────────────────────────────

def test_render_steps_shows_commands_and_asserts():
    text = render_steps(build_steps(loopback_plan()))
    assert "[assert]" in text
    assert "$ fallocate -l" in text
    assert "(best-effort)" in text


# ── runner ───────────────────────────────────────────────────────────

class FakeRun:
    """argv-tuple → (returncode, stdout); default (0, ""). Values may be
    callables for stateful scripts (e.g. check fails until `do` ran)."""

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        entry = self.script.get(tuple(argv), (0, ""))
        if callable(entry):
            entry = entry()
        rc, out = entry
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="fake")


def one_step(**kw):
    step = {"id": "s", "title": "step", "type": "step", "check": None,
            "unless": None, "do": {"cmd": ["do-it"]}, "best_effort": False}
    step.update(kw)
    return {"action": "test", "pool_dir": "/x", "steps": [step]}


def test_runner_skips_when_check_already_holds():
    fake = FakeRun()  # every command exits 0 → check passes
    Runner(run_cmd=fake, log=lambda _: None).run(
        one_step(check={"cmd": ["is-done"]}))
    assert fake.calls == [["is-done"]]  # `do` never ran


def test_runner_runs_do_then_reverifies():
    state = {"done": False}

    def probe():
        return (0, "") if state["done"] else (1, "")

    def do():
        state["done"] = True
        return (0, "")

    fake = FakeRun({("is-done",): probe, ("do-it",): do})
    Runner(run_cmd=fake, log=lambda _: None).run(
        one_step(check={"cmd": ["is-done"]}))
    assert ["do-it"] in fake.calls
    assert fake.calls.count(["is-done"]) == 2  # pre-check + post-verify


def test_runner_fails_when_postverify_still_fails():
    fake = FakeRun({("is-done",): (1, "")})
    with pytest.raises(ExecuteError, match="verification still fails"):
        Runner(run_cmd=fake, log=lambda _: None).run(
            one_step(check={"cmd": ["is-done"]}))


def test_runner_assert_failure_aborts():
    fake = FakeRun({("must-hold",): (1, "")})
    with pytest.raises(ExecuteError, match="assertion failed"):
        Runner(run_cmd=fake, log=lambda _: None).run(
            one_step(type="assert", check={"cmd": ["must-hold"]}, do=None))


def test_runner_best_effort_failure_continues():
    fake = FakeRun({("do-it",): (1, "")})
    Runner(run_cmd=fake, log=lambda _: None).run(one_step(best_effort=True))


def test_runner_command_failure_raises_with_argv():
    fake = FakeRun({("do-it",): (1, "")})
    with pytest.raises(ExecuteError, match="do-it"):
        Runner(run_cmd=fake, log=lambda _: None).run(one_step())


def test_runner_unless_skips_entire_step():
    fake = FakeRun()  # unless-check exits 0 → step skipped
    Runner(run_cmd=fake, log=lambda _: None).run(
        one_step(unless={"cmd": ["superseded"]}))
    assert fake.calls == [["superseded"]]


def test_runner_append_line_is_idempotent(tmp_path):
    fstab = tmp_path / "fstab"
    fstab.write_text("existing\n", encoding="utf-8")
    built = one_step(
        check={"line_in_file": {"path": str(fstab), "needle": "/img "}},
        do={"append_line": {"path": str(fstab), "line": "/img /mnt btrfs loop 0 0"}},
    )
    runner = Runner(run_cmd=FakeRun(), log=lambda _: None)
    runner.run(built)
    runner.run(built)  # second run: check holds, no duplicate
    content = fstab.read_text(encoding="utf-8")
    assert content.count("/img /mnt") == 1


def test_runner_file_min_size_check(tmp_path):
    f = tmp_path / "img"
    f.write_bytes(b"x" * 100)
    r = Runner(run_cmd=FakeRun(), log=lambda _: None)
    assert r._check_ok({"file_min_size": {"path": str(f), "min_bytes": 100}})
    assert not r._check_ok({"file_min_size": {"path": str(f), "min_bytes": 101}})
    assert not r._check_ok({"file_min_size": {"path": str(f) + ".nope", "min_bytes": 1}})


def test_runner_fstab_uuid_appends_blkid_uuid(tmp_path):
    fstab = tmp_path / "fstab"
    fstab.write_text("", encoding="utf-8")
    fake = FakeRun({
        ("blkid", "-o", "value", "-s", "UUID", "/dev/sdc"): (0, "abc-123\n"),
    })
    Runner(run_cmd=fake, log=lambda _: None)._apply(
        {"fstab_uuid": {"dev": "/dev/sdc", "mnt": "/data", "path": str(fstab)}})
    line = fstab.read_text(encoding="utf-8").strip()
    assert line.startswith("UUID=abc-123 /data btrfs nofail 0 0")
    assert line.endswith(FSTAB_TAG)
