import json
import subprocess
from types import SimpleNamespace

import snaplicator_install.__main__ as install


def args(overrides=None):
    return SimpleNamespace(
        connstr="postgresql://publisher:secret@db.example/prod",
        pool="/data/snaplicator",
        publication="snaplicator_publication",
        subscription="snaplicator_subscription",
        replica_port=5433,
        web_port=8080,
        api_port=8888,
        project="snaplicator",
        postgres_image=None,
        replica_password="replica-secret",
        set=overrides,
        force=True,
    )


def configure(monkeypatch, tmp_path, overrides=None):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(install, "ENV_FILE", env_file)
    monkeypatch.setattr(install, "is_btrfs", lambda _path: True)
    install.cmd_configure(args(overrides))
    return env_file


def source_value(env_file):
    result = subprocess.run(
        ["bash", "-c", '. "$1"; printf %s "$REPLICA_UPSERT_TABLES"',
         "bash", str(env_file)],
        check=True, capture_output=True, text=True)
    return result.stdout


def test_configure_writes_safe_empty_default(monkeypatch, tmp_path):
    env_file = configure(monkeypatch, tmp_path)

    assert "REPLICA_UPSERT_TABLES='{}'\n" in env_file.read_text()
    assert json.loads(source_value(env_file)) == {}


def test_configure_preserves_existing_table_map(monkeypatch, tmp_path):
    env_file = configure(monkeypatch, tmp_path, [
        'REPLICA_UPSERT_TABLES={"public.look_insights":"look_id_unique"}'])
    install.cmd_configure(args())

    assert json.loads(source_value(env_file)) == {
        "public.look_insights": "look_id_unique"}


def test_configure_explicit_empty_map_disables_existing_policy(monkeypatch, tmp_path):
    env_file = configure(monkeypatch, tmp_path, [
        'REPLICA_UPSERT_TABLES={"public.look_insights":"look_id_unique"}'])
    install.cmd_configure(args(["REPLICA_UPSERT_TABLES={}"]))

    assert "REPLICA_UPSERT_TABLES='{}'\n" in env_file.read_text()
    assert json.loads(source_value(env_file)) == {}


def test_configure_quotes_json_without_changing_names(monkeypatch, tmp_path):
    mapping = {"odd$table": "constraint'with`characters"}
    env_file = configure(monkeypatch, tmp_path, [
        f"REPLICA_UPSERT_TABLES={json.dumps(mapping)}"])

    line = next(line for line in env_file.read_text().splitlines()
                if line.startswith("REPLICA_UPSERT_TABLES="))
    assert line.startswith("REPLICA_UPSERT_TABLES='") and line.endswith("'")
    assert json.loads(source_value(env_file)) == mapping
