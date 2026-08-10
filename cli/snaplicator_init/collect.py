"""Collectors: the only module that runs external commands for discovery.

Kept deliberately thin — everything downstream operates on the parsed JSON
these return, so captured outputs (fixtures) can stand in for a live
machine. `--collect-fixture` dumps exactly what the planner would consume;
a fixture directory doubles as the bug-report format for misclassified
topologies.

Runs unprivileged. No command here mutates anything.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Tuple

FINDMNT_CMD = [
    "findmnt", "--json", "--bytes",
    "-o", "TARGET,SOURCE,FSTYPE,SIZE,AVAIL,USED,OPTIONS",
]
LSBLK_CMD = [
    "lsblk", "--json", "--bytes",
    "-o", "NAME,TYPE,FSTYPE,SIZE,MOUNTPOINT,RO",
]

FINDMNT_FIXTURE = "findmnt.json"
LSBLK_FIXTURE = "lsblk.json"


class CollectError(RuntimeError):
    """A discovery command is missing or produced unusable output."""


def _run_json(cmd: list) -> Dict[str, Any]:
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, check=True)
    except FileNotFoundError:
        raise CollectError(
            f"'{cmd[0]}' not found — install util-linux (this tool supports "
            "Linux hosts; on Ubuntu/Debian: apt install util-linux)"
        )
    except subprocess.CalledProcessError as e:
        raise CollectError(
            f"'{' '.join(cmd)}' failed: {(e.stderr or e.stdout or '').strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise CollectError(f"'{cmd[0]}' emitted invalid JSON: {e}")


def collect() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Live discovery: (findmnt_raw, lsblk_raw)."""
    return _run_json(FINDMNT_CMD), _run_json(LSBLK_CMD)


def write_fixture(directory: str) -> Tuple[Path, Path]:
    """Dump live discovery output for offline replay / bug reports."""
    findmnt_raw, lsblk_raw = collect()
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    findmnt_path = d / FINDMNT_FIXTURE
    lsblk_path = d / LSBLK_FIXTURE
    findmnt_path.write_text(json.dumps(findmnt_raw, indent=2), encoding="utf-8")
    lsblk_path.write_text(json.dumps(lsblk_raw, indent=2), encoding="utf-8")
    return findmnt_path, lsblk_path


def read_fixture(directory: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Replay a fixture directory instead of touching the live machine."""
    d = Path(directory)
    try:
        findmnt_raw = json.loads((d / FINDMNT_FIXTURE).read_text(encoding="utf-8"))
        lsblk_raw = json.loads((d / LSBLK_FIXTURE).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise CollectError(
            f"fixture directory {d} must contain {FINDMNT_FIXTURE} and "
            f"{LSBLK_FIXTURE}: {e}"
        )
    except json.JSONDecodeError as e:
        raise CollectError(f"fixture in {d} is invalid JSON: {e}")
    return findmnt_raw, lsblk_raw
