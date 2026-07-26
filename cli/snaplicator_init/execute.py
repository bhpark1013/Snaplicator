"""Provisioning executor (stage 2): plan dict in, machine mutations out.

The split mirrors stage 1: `build_steps` is a pure function (plan dict →
ordered step dicts) that owns every safety gate and every command line,
so the whole decision surface unit-tests without root. `Runner` is the
only code that touches the machine, and it speaks a tiny declarative
vocabulary — a step's `check` decides "already satisfied", its `do`
mutates — so every step is idempotent and a re-run after a mid-way
failure resumes where it stopped.

Step shape:
    {"id", "title", "type": "assert" | "step",
     "check": <check> | None,     # assert: must hold; step: skip-if-holds,
                                  #         re-verified after `do`
     "unless": <check> | None,    # skip the whole step when this holds
     "do": <do> | None,
     "best_effort": bool}         # failure logs a warning instead of aborting

check vocabulary (holds when):
    {"cmd": argv}                            exit code 0
    {"cmd_fails": argv}                      exit code != 0
    {"cmd_contains": {"argv", "needle"}}     stdout contains needle
    {"file_min_size": {"path", "min_bytes"}} file exists and is large enough
    {"fs_avail": {"path", "min_bytes"}}      statvfs free space suffices
    {"line_in_file": {"path", "needle"}}     needle appears in the file

do vocabulary:
    {"cmd": argv}
    {"append_line": {"path", "line"}}
    {"loop_direct_io": {"img"}}      losetup --direct-io=on on img's loop device
    {"fstab_uuid": {"dev", "mnt", "path"}}   append a UUID=<blkid dev> mount line
"""

from __future__ import annotations

import os
import posixpath
import shlex
import subprocess
from typing import Any, Callable, Dict, List, Optional

FSTAB_PATH = "/etc/fstab"
FSTAB_TAG = "# added by snaplicator-init"

# fstab options for the loopback pool. `loop` makes mount(8) re-attach the
# image at boot; the direct-io flag is NOT expressible here, so after a
# reboot the pool runs without it until the (best-effort) direct-io step is
# re-applied — a perf nuance, not a correctness one.
LOOP_FSTAB_OPTS = "loop,nofail,x-systemd.device-timeout=10s"
DISK_FSTAB_OPTS = "nofail"


class ExecuteError(RuntimeError):
    """A safety gate refused the plan, or a step failed on the machine."""


def _join(base: str, name: str) -> str:
    return posixpath.join(base, name)


def _step(step_id: str, title: str, do: Dict[str, Any],
          check: Optional[Dict[str, Any]] = None, *,
          unless: Optional[Dict[str, Any]] = None,
          best_effort: bool = False) -> Dict[str, Any]:
    return {"id": step_id, "title": title, "type": "step",
            "check": check, "unless": unless, "do": do,
            "best_effort": best_effort}


def _assert(step_id: str, title: str, check: Dict[str, Any], *,
            unless: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"id": step_id, "title": title, "type": "assert",
            "check": check, "unless": unless, "do": None,
            "best_effort": False}


def _subvolume_steps(target: str, pool_dir: str) -> List[Dict[str, Any]]:
    return [
        _assert(
            "verify-btrfs-mount",
            f"verify {target} is still a mounted btrfs filesystem",
            {"cmd": ["findmnt", "--noheadings", "-t", "btrfs", target]},
        ),
        _step(
            "parent-dir",
            f"ensure parent directory of {pool_dir} exists",
            {"cmd": ["mkdir", "-p", posixpath.dirname(pool_dir)]},
        ),
        _step(
            "create-subvolume",
            f"create btrfs subvolume {pool_dir}",
            {"cmd": ["btrfs", "subvolume", "create", pool_dir]},
            check={"cmd": ["btrfs", "subvolume", "show", pool_dir]},
        ),
    ]


def _loopback_steps(target: str, mnt: str, img: str, required: int,
                    fstab_path: str) -> List[Dict[str, Any]]:
    fstab_line = f"{img} {mnt} btrfs {LOOP_FSTAB_OPTS} 0 0 {FSTAB_TAG}"
    return [
        _assert(
            "verify-free-space",
            f"verify {target} still has {required} bytes free",
            {"fs_avail": {"path": target, "min_bytes": required}},
            # on a re-run the image itself already consumed that space
            unless={"file_min_size": {"path": img, "min_bytes": required}},
        ),
        _step(
            "parent-dir",
            f"ensure parent directory of {img} exists",
            {"cmd": ["mkdir", "-p", posixpath.dirname(img)]},
        ),
        _step(
            "allocate-image",
            f"allocate {required}-byte image {img} (fallocate, not sparse)",
            {"cmd": ["fallocate", "-l", str(required), img]},
            check={"file_min_size": {"path": img, "min_bytes": required}},
        ),
        _step(
            "mkfs",
            f"create btrfs filesystem inside {img}",
            # no -f: mkfs.btrfs refusing an existing signature is a feature;
            # the check makes re-runs skip this step anyway.
            {"cmd": ["mkfs.btrfs", img]},
            # -p probes the file directly, bypassing the stale blkid cache
            check={"cmd_contains": {
                "argv": ["blkid", "-p", "-o", "value", "-s", "TYPE", img],
                "needle": "btrfs",
            }},
        ),
        _step(
            "mountpoint",
            f"ensure mountpoint {mnt} exists",
            {"cmd": ["mkdir", "-p", mnt]},
        ),
        _step(
            "mount",
            f"mount {img} at {mnt} (loop)",
            {"cmd": ["mount", "-o", "loop", img, mnt]},
            check={"cmd": ["findmnt", "--noheadings", mnt]},
        ),
        _step(
            "loop-direct-io",
            f"enable direct-io on the loop device backing {img}",
            {"loop_direct_io": {"img": img}},
            check={"cmd_contains": {
                "argv": ["losetup", "-j", img, "-O", "DIO", "--noheadings"],
                "needle": "1",
            }},
            best_effort=True,  # kernel/util-linux dependent; perf-only
        ),
        _step(
            "fstab",
            f"persist the mount in {fstab_path}",
            {"append_line": {"path": fstab_path, "line": fstab_line}},
            check={"line_in_file": {"path": fstab_path, "needle": f"{img} "}},
        ),
    ]


def _format_disk_steps(dev: str, mnt: str, fstab_path: str) -> List[Dict[str, Any]]:
    typed_btrfs = {"cmd_contains": {
        "argv": ["blkid", "-p", "-o", "value", "-s", "TYPE", dev],
        "needle": "btrfs",
    }}
    return [
        _assert(
            "verify-bare",
            f"re-verify {dev} carries no filesystem or partition-table signature",
            # blkid -p exits non-zero only when it finds nothing at all
            {"cmd_fails": ["blkid", "-p", dev]},
            unless=typed_btrfs,  # re-run after our own mkfs
        ),
        _step(
            "mkfs",
            f"format {dev} as btrfs",
            {"cmd": ["mkfs.btrfs", dev]},
            check=typed_btrfs,
        ),
        _step(
            "mountpoint",
            f"ensure mountpoint {mnt} exists",
            {"cmd": ["mkdir", "-p", mnt]},
        ),
        _step(
            "mount",
            f"mount {dev} at {mnt}",
            {"cmd": ["mount", dev, mnt]},
            check={"cmd": ["findmnt", "--noheadings", mnt]},
        ),
        _step(
            "fstab",
            f"persist the mount in {fstab_path} (by UUID)",
            {"fstab_uuid": {"dev": dev, "mnt": mnt, "path": fstab_path}},
            check={"line_in_file": {"path": fstab_path, "needle": f" {mnt} "}},
        ),
    ]


def build_steps(
    plan: Dict[str, Any],
    data_dir: Optional[str] = None,
    format_disk: Optional[str] = None,
    assume_yes: bool = False,
    fstab_path: str = FSTAB_PATH,
) -> Dict[str, Any]:
    """Pure: plan dict → {"action", "pool_dir", "steps"}.

    Raises ExecuteError when a safety gate refuses:
      * unknown plan version
      * no-fit plan without an explicit --format-disk
      * --format-disk naming anything but a fitting bare-disk candidate
      * several fitting candidates without --data-dir or --yes
    """
    if plan.get("version") != 1:
        raise ExecuteError(f"unsupported plan version: {plan.get('version')!r}")
    required = int(plan["required_bytes"])
    data_dir = data_dir or plan.get("data_dir")
    if data_dir and not data_dir.startswith("/"):
        raise ExecuteError(f"--data-dir must be an absolute path: {data_dir}")

    if format_disk:
        bare = [c for c in plan.get("candidates") or []
                if c["priority"] == 3 and c["target"] == format_disk]
        if not bare:
            raise ExecuteError(
                f"{format_disk} is not a bare-disk candidate in this plan — "
                "only a whole disk with no partitions, no filesystem signature "
                "and no mount may be formatted"
            )
        if not bare[0]["fits"]:
            raise ExecuteError(
                f"{format_disk} is too small: {bare[0]['size_bytes']} bytes "
                f"< {required} required"
            )
        mnt = data_dir or "/data/snaplicator"
        return {
            "action": "format-disk",
            "pool_dir": mnt,
            "steps": _format_disk_steps(format_disk, mnt, fstab_path),
        }

    chosen = plan.get("chosen")
    if chosen is None:
        hint = ""
        fitting_bare = [c for c in plan.get("candidates") or []
                        if c["priority"] == 3 and c["fits"]]
        if fitting_bare:
            hint = (
                "; a bare disk fits — formatting is destructive and never "
                f"automatic, pass --format-disk {fitting_bare[0]['target']}"
            )
        raise ExecuteError(f"plan is no-fit: nothing to provision{hint}")
    if plan.get("needs_confirmation") and not assume_yes:
        raise ExecuteError(
            "several candidates fit; pin one with --data-dir PATH or accept "
            f"the recommendation ({chosen['target']}) with --yes"
        )

    if chosen["action"] == "subvolume":
        pool_dir = data_dir or _join(chosen["target"], "snaplicator")
        return {
            "action": "subvolume",
            "pool_dir": pool_dir,
            "steps": _subvolume_steps(chosen["target"], pool_dir),
        }

    if chosen["action"] == "loopback-file":
        mnt = data_dir or _join(chosen["target"], "snaplicator")
        img = mnt.rstrip("/") + ".img"
        return {
            "action": "loopback-file",
            "pool_dir": mnt,
            "steps": _loopback_steps(chosen["target"], mnt, img, required,
                                     fstab_path),
        }

    raise ExecuteError(f"unknown action in plan: {chosen['action']!r}")


# ── rendering (dry run) ───────────────────────────────────────────────

def _describe_do(do: Dict[str, Any]) -> str:
    if "cmd" in do:
        return shlex.join(do["cmd"])
    if "append_line" in do:
        a = do["append_line"]
        return f"append to {a['path']}: {a['line']}"
    if "loop_direct_io" in do:
        img = do["loop_direct_io"]["img"]
        return f"losetup --direct-io=on $(losetup -j {img} -O NAME --noheadings)"
    if "fstab_uuid" in do:
        f = do["fstab_uuid"]
        return (f"append to {f['path']}: UUID=<uuid of {f['dev']}> "
                f"{f['mnt']} btrfs {DISK_FSTAB_OPTS} 0 0 {FSTAB_TAG}")
    return repr(do)


def render_steps(built: Dict[str, Any]) -> str:
    lines = [f"execution steps ({built['action']} → {built['pool_dir']}):"]
    for i, s in enumerate(built["steps"], 1):
        if s["type"] == "assert":
            lines.append(f"  {i}. [assert] {s['title']}")
        else:
            suffix = "  (best-effort)" if s["best_effort"] else ""
            lines.append(f"  {i}. {s['title']}{suffix}")
            lines.append(f"       $ {_describe_do(s['do'])}")
    return "\n".join(lines)


# ── the runner: the only code that mutates the machine ────────────────

class Runner:
    """Executes a step list. `run_cmd` is injectable for tests."""

    def __init__(self,
                 run_cmd: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
                 log: Callable[[str], None] = print):
        self._run = run_cmd or self._default_run
        self._log = log

    @staticmethod
    def _default_run(argv: List[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(argv, text=True, capture_output=True)
        except FileNotFoundError:
            return subprocess.CompletedProcess(argv, 127, stdout="",
                                               stderr=f"{argv[0]}: not found")

    # ── checks ──
    def _check_ok(self, check: Dict[str, Any]) -> bool:
        if "cmd" in check:
            return self._run(check["cmd"]).returncode == 0
        if "cmd_fails" in check:
            return self._run(check["cmd_fails"]).returncode != 0
        if "cmd_contains" in check:
            c = check["cmd_contains"]
            proc = self._run(c["argv"])
            return proc.returncode == 0 and c["needle"] in (proc.stdout or "")
        if "file_min_size" in check:
            c = check["file_min_size"]
            try:
                return os.path.getsize(c["path"]) >= c["min_bytes"]
            except OSError:
                return False
        if "fs_avail" in check:
            c = check["fs_avail"]
            try:
                st = os.statvfs(c["path"])
            except OSError:
                return False
            return st.f_bavail * st.f_frsize >= c["min_bytes"]
        if "line_in_file" in check:
            c = check["line_in_file"]
            try:
                with open(c["path"], "r", encoding="utf-8") as f:
                    return any(c["needle"] in line for line in f)
            except OSError:
                return False
        raise ExecuteError(f"unknown check: {check!r}")

    # ── dos ──
    def _cmd_or_raise(self, argv: List[str]) -> str:
        proc = self._run(argv)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise ExecuteError(f"`{shlex.join(argv)}` failed: {detail}")
        return proc.stdout or ""

    def _apply(self, do: Dict[str, Any]) -> None:
        if "cmd" in do:
            self._cmd_or_raise(do["cmd"])
            return
        if "append_line" in do:
            a = do["append_line"]
            with open(a["path"], "a", encoding="utf-8") as f:
                f.write(a["line"] + "\n")
            return
        if "loop_direct_io" in do:
            img = do["loop_direct_io"]["img"]
            out = self._cmd_or_raise(
                ["losetup", "-j", img, "-O", "NAME", "--noheadings"])
            devices = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if not devices:
                raise ExecuteError(f"no loop device is attached to {img}")
            self._cmd_or_raise(["losetup", "--direct-io=on", devices[0]])
            return
        if "fstab_uuid" in do:
            f = do["fstab_uuid"]
            uuid = self._cmd_or_raise(
                ["blkid", "-o", "value", "-s", "UUID", f["dev"]]).strip()
            if not uuid:
                raise ExecuteError(f"blkid reported no UUID for {f['dev']}")
            line = (f"UUID={uuid} {f['mnt']} btrfs {DISK_FSTAB_OPTS} 0 0 "
                    f"{FSTAB_TAG}")
            with open(f["path"], "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            return
        raise ExecuteError(f"unknown do: {do!r}")

    # ── driver ──
    def run(self, built: Dict[str, Any]) -> None:
        steps = built["steps"]
        n = len(steps)
        for i, s in enumerate(steps, 1):
            label = f"[{i}/{n}] {s['title']}"
            if s.get("unless") and self._check_ok(s["unless"]):
                self._log(f"{label} — skipped (superseded)")
                continue
            if s["type"] == "assert":
                if self._check_ok(s["check"]):
                    self._log(f"{label} — ok")
                    continue
                raise ExecuteError(f"{s['title']}: assertion failed")
            if s.get("check") and self._check_ok(s["check"]):
                self._log(f"{label} — already done")
                continue
            try:
                self._apply(s["do"])
                if s.get("check") and not self._check_ok(s["check"]):
                    raise ExecuteError(
                        f"{s['title']}: command ran but verification still fails")
            except ExecuteError as e:
                if s["best_effort"]:
                    self._log(f"{label} — WARNING (best-effort): {e}")
                    continue
                raise
            self._log(f"{label} — done")
