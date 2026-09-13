"""Durable config reconciliation and fail-closed schema changes on PostgreSQL."""
import subprocess

import pytest

from app.services import replica_upsert
from app.services.replica_upsert import ensure_replica_upserts
from conftest import PG_DB, PG_PASSWORD, PG_USER, psql


def reconcile(container, tables):
    return ensure_replica_upserts(container, PG_USER, PG_PASSWORD, PG_DB, tables)


def test_connection_failure_does_not_print_password(monkeypatch):
    secret = "must-not-appear-in-error"

    def fail(*args):
        raise subprocess.CalledProcessError(
            1, ["docker", "exec", "-e", f"PGPASSWORD={secret}"],
            stderr="psql: connection to server failed",
        )

    monkeypatch.setattr(replica_upsert, "_run_subscriber_sql", fail)
    with pytest.raises(RuntimeError) as error:
        ensure_replica_upserts("replica", "user", secret, "db", {})
    assert secret not in str(error.value)


def test_policy_reinstalls_lost_trigger_and_removes_opt_out(pg_container):
    table = "upsert_policy_lifecycle"
    ddl = f"CREATE TABLE {table} (id int PRIMARY KEY, key int NOT NULL UNIQUE, value text);"
    policy = {f"public.{table}": f"{table}_key_key"}
    psql(ddl)
    assert reconcile(pg_container, policy) == {
        "installed": [f"public.{table}"], "removed": [], "errors": [],
    }
    assert reconcile(pg_container, policy) == {"installed": [], "removed": [], "errors": []}

    psql(f"ALTER TABLE {table} DISABLE TRIGGER _snaplicator_replica_upsert;")
    assert reconcile(pg_container, policy)["installed"] == [f"public.{table}"]
    psql(f"DROP TABLE {table}; {ddl}")
    assert reconcile(pg_container, policy)["installed"] == [f"public.{table}"]
    assert psql(f"SELECT tgenabled FROM pg_trigger WHERE tgrelid='{table}'::regclass;") == "R"

    assert reconcile(pg_container, {}) == {
        "installed": [], "removed": [f"public.{table}"], "errors": [],
    }
    assert psql(f"SELECT count(*) FROM pg_trigger WHERE tgrelid='{table}'::regclass;") == "0"
    psql(f"DROP TABLE {table};")


def test_existing_handler_reports_foreign_key_and_unique_key_drift(pg_container):
    table = "upsert_policy_drift"
    policy = {f"public.{table}": f"{table}_key_key"}
    psql(f"CREATE TABLE {table} (id int PRIMARY KEY, key int NOT NULL UNIQUE);")
    assert reconcile(pg_container, policy)["installed"] == [f"public.{table}"]

    # A metadata-current trigger must not conceal newly unsafe schema.
    psql(f"CREATE TABLE upsert_policy_child (parent int REFERENCES {table}(id));")
    res = reconcile(pg_container, policy)
    assert not res["installed"]
    assert len(res["errors"]) == 1
    assert "foreign keys" in res["errors"][0]
    psql("DROP TABLE upsert_policy_child;")

    psql(f"ALTER TABLE {table} ALTER COLUMN key DROP NOT NULL;")
    assert "non-null" in reconcile(pg_container, policy)["errors"][0]
    psql(f"ALTER TABLE {table} ALTER COLUMN key SET NOT NULL;")
    psql(f"ALTER TABLE {table} DROP CONSTRAINT {table}_key_key;")
    assert "unique key" in reconcile(pg_container, policy)["errors"][0]
    psql(f"DROP TABLE {table};")
