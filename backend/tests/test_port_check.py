"""Port picking must catch ports that are bound but not listening."""
from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from app.services import docker_pg


def _no_listeners(monkeypatch):
    monkeypatch.setattr(docker_pg.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))


def test_bound_but_not_listening_port_is_in_use(monkeypatch):
    _no_listeners(monkeypatch)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))  # bound like an outgoing connection's source port, no listen()
    port = s.getsockname()[1]
    try:
        assert not docker_pg._port_listening(port)
        assert docker_pg.is_port_in_use(port)
        assert docker_pg._find_free_port(port) != port
    finally:
        s.close()
    assert not docker_pg.is_port_in_use(port)


def test_listener_still_counts(monkeypatch):
    monkeypatch.setattr(docker_pg.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="LISTEN 0 0 0.0.0.0:41999 0.0.0.0:*\n", stderr=""))
    assert docker_pg.is_port_in_use(41999)


def _opts(tmp_path):
    return docker_pg.CloneOptions(
        root_data_dir=str(tmp_path), main_data_dir="replica", snapshot_name="",
        container_name="snaplicator_replica", network_name="net", host_port=41000,
        postgres_user="u", postgres_password="p", postgres_db="d",
    )


def _fake_docker(monkeypatch, fail_ports, message="Error: address already in use"):
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        if cmd[:3] == ["docker", "run", "-d"]:
            port = int(cmd[cmd.index("-p") + 1].split(":")[0])
            if port in fail_ports:
                return SimpleNamespace(returncode=125, stdout="", stderr=message)
            return SimpleNamespace(returncode=125, stdout="", stderr="stop here")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(docker_pg.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_pg, "_ensure_docker_network", lambda name: None)
    monkeypatch.setattr(docker_pg, "_port_bindable", lambda port: True)
    return calls


def test_autopicked_port_conflict_retries_once_and_removes_dead_container(tmp_path, monkeypatch):
    calls = _fake_docker(monkeypatch, {41000})
    with pytest.raises(RuntimeError, match="stop here"):
        docker_pg._launch_clone_container(tmp_path, _opts(tmp_path), "c1", None, None,
                                          remove_existing=False, run_anonymize=False)
    runs = [c for c in calls if c[:3] == ["docker", "run", "-d"]]
    assert [c[c.index("-p") + 1] for c in runs] == ["41000:5432", "41001:5432"]
    assert calls.count(["docker", "rm", "-f", "c1"]) == 2
    assert "wal_compression=on" in runs[0]


def test_requested_port_conflict_is_not_retried(tmp_path, monkeypatch):
    calls = _fake_docker(monkeypatch, {41000})
    with pytest.raises(RuntimeError, match="address already in use"):
        docker_pg._launch_clone_container(tmp_path, _opts(tmp_path), "c1", 41000, None,
                                          remove_existing=False, run_anonymize=False)
    assert len([c for c in calls if c[:3] == ["docker", "run", "-d"]]) == 1


def test_name_conflict_does_not_remove_the_other_container(tmp_path, monkeypatch):
    calls = _fake_docker(monkeypatch, {41000},
                         message='Conflict. The container name "/c1" is already in use by container "abc"')
    with pytest.raises(RuntimeError):
        docker_pg._launch_clone_container(tmp_path, _opts(tmp_path), "c1", 41000, None,
                                          remove_existing=False, run_anonymize=False)
    assert ["docker", "rm", "-f", "c1"] not in calls
