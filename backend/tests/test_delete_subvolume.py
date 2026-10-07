import subprocess

import pytest

from app.services import btrfs


def _fail(cmd, **_):
    raise subprocess.CalledProcessError(1, cmd, stderr="ERROR: Could not statfs: No such file or directory")


def test_already_deleted_subvolume_raises_not_found(tmp_path, monkeypatch):
    # Concurrent DELETE: another request removed the subvolume first.
    monkeypatch.setattr(btrfs, "_run", _fail)
    with pytest.raises(FileNotFoundError):
        btrfs.delete_subvolume(tmp_path / "gone")


def test_real_failure_still_raises_runtime_error(tmp_path, monkeypatch):
    monkeypatch.setattr(btrfs, "_run", _fail)
    target = tmp_path / "still-here"
    target.mkdir()
    with pytest.raises(RuntimeError, match="btrfs subvolume delete failed"):
        btrfs.delete_subvolume(target)


def test_success(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(btrfs, "_run", lambda cmd: calls.append(cmd))
    btrfs.delete_subvolume(tmp_path / "x")
    assert calls and calls[0][-1] == str(tmp_path / "x")
