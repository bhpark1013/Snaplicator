"""Two clones built at the same moment must not collide on name or host port.

2026-11-02: a prod-exec rehearsal and a QA host both POSTed /clones in the
same second; both got `replica-clone-20261102-061437` and the second btrfs
snapshot failed with "File exists" -> 500.
"""
import re
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from app.services import docker_pg


def test_unique_stamp_is_time_sortable_and_unique_within_a_second():
    stamps = {docker_pg._unique_stamp() for _ in range(200)}
    assert len(stamps) == 200
    for s in stamps:
        assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", s)


def test_concurrent_launches_get_different_ports(tmp_path, monkeypatch):
    published: set[int] = set()
    picked: list[int] = []
    lock = threading.Lock()

    def fake_run(cmd, *args, **kwargs):
        if cmd[:2] == ["ss", "-ltn"]:
            with lock:
                listening = "\n".join(f"LISTEN 0 0 0.0.0.0:{p} 0.0.0.0:*" for p in published)
            return SimpleNamespace(returncode=0, stdout=listening + "\n", stderr="")
        if cmd[:3] == ["docker", "run", "-d"]:
            port = int(cmd[cmd.index("-p") + 1].split(":")[0])
            time.sleep(0.2)  # docker takes a moment before the port shows up as listening
            with lock:
                picked.append(port)
                published.add(port)
            # Fail on purpose: the test only cares which port was chosen.
            return SimpleNamespace(returncode=125, stdout="", stderr="stop here")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(docker_pg.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_pg, "_ensure_docker_network", lambda name: None)

    opts = docker_pg.CloneOptions(
        root_data_dir=str(tmp_path),
        main_data_dir="replica",
        snapshot_name="",
        container_name="snaplicator_replica",
        network_name="net",
        host_port=5434,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
    )

    def launch(i):
        with pytest.raises(RuntimeError, match="docker run exit 125"):
            docker_pg._launch_clone_container(
                clone_path=tmp_path / f"c{i}",
                opts=opts,
                container_name=f"clone-{i}",
                host_port_hint=None,
                description=None,
                remove_existing=False,
                run_anonymize=False,
            )

    threads = [threading.Thread(target=launch, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(picked) == [5434, 5435, 5436]
    # The lock is released even when docker run fails.
    assert docker_pg._PORT_LOCK.acquire(timeout=1)
    docker_pg._PORT_LOCK.release()
