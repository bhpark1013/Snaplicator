"""Two Snaplicator instances against one primary, for real.

    python3 scripts/two_instances_smoke.py     # needs docker + psql

Not a unit test: three postgres containers, real logical replication, and the
same service calls the manager makes. Phase 1 puts both instances on ONE
publication; phase 2 gives them different ones and checks that what belongs
to a single instance reaches only that instance.

Phase 2 is the one that used to fail. Capture was a single trigger under a
fixed name whose body named a publication, so installing the second instance
rewrote the first one's function and the first one's new tables stopped
joining its publication — silently, because from PostgreSQL's side nothing
was wrong. Run this against the code before that split and the line reading
"A still auto-adds after B was installed" comes back false.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.replication import (  # noqa: E402
    auto_add_name,
    auto_sync_new_tables,
    enable_ddl_apply,
    ensure_ddl_publication,
    get_publisher_max_ddl_log_id,
    install_capture_triggers,
    install_ddl_apply,
)

NET = "twoinst_net"
PUB_C, A_C, B_C = "twoinst_pub", "twoinst_a", "twoinst_b"
PUB_P, A_P, B_P = 25440, 25441, 25442
USER, PW, DB = "testuser", "testpass", "testdb"

results: list[tuple[bool, str]] = []


def check(ok: bool, desc: str) -> None:
    results.append((bool(ok), desc))
    print(f"  {'PASS' if ok else 'FAIL'}  {desc}", flush=True)


def sh(*args: str) -> str:
    p = subprocess.run(args, text=True, capture_output=True)
    return (p.stdout or "").strip()


def connstr(port: int) -> str:
    return f"host=127.0.0.1 port={port} dbname={DB} user={USER} password={PW}"


def q(port: int, sql: str) -> str:
    p = subprocess.run(
        ["psql", connstr(port), "-X", "-v", "ON_ERROR_STOP=1", "-At", "-c", sql],
        text=True, capture_output=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"port {port}: {(p.stderr or p.stdout).strip()}")
    return (p.stdout or "").strip()


def up() -> None:
    for c in (PUB_C, A_C, B_C):
        sh("docker", "rm", "-f", c)
    sh("docker", "network", "rm", NET)
    subprocess.run(["docker", "network", "create", NET], check=True, capture_output=True)
    for name, port in ((PUB_C, PUB_P), (A_C, A_P), (B_C, B_P)):
        subprocess.run([
            "docker", "run", "-d", "--rm", "--name", name, "--network", NET,
            "-e", f"POSTGRES_USER={USER}", "-e", f"POSTGRES_PASSWORD={PW}",
            "-e", f"POSTGRES_DB={DB}", "-p", f"{port}:5432",
            "--tmpfs", "/var/lib/postgresql/data",
            "postgres:15-alpine", "-c", "wal_level=logical",
        ], check=True, capture_output=True)
    for port in (PUB_P, A_P, B_P):
        for _ in range(120):
            try:
                if q(port, "SELECT 1;") == "1":
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError(f"postgres on {port} never came up")


def down() -> None:
    for c in (PUB_C, A_C, B_C):
        sh("docker", "rm", "-f", c)
    sh("docker", "network", "rm", NET)


def attach(container: str, port: int, publication: str, subscription: str) -> None:
    """One instance's wiring, in the order the manager does it."""
    pub = connstr(PUB_P)
    wm = get_publisher_max_ddl_log_id(pub)
    install_ddl_apply(container, USER, PW, DB, initial_watermark=wm)
    ensure_ddl_publication(pub, publication)
    q(port, f"CREATE SUBSCRIPTION {subscription} "
            f"CONNECTION 'host={PUB_C} port=5432 dbname={DB} user={USER} password={PW}' "
            f"PUBLICATION {publication};")
    enable_ddl_apply(pub, publication, container, USER, PW, DB, subscription)


def sync(container: str, publication: str, subscription: str, tries: int = 12) -> None:
    """What the manager's loop does every cycle.

    ALTER PUBLICATION ADD TABLE does not enlist the table in an existing
    subscription — pg_subscription_rel only grows on REFRESH PUBLICATION. And
    REFRESH hard-fails on a published table the subscriber does not have yet,
    so this retries while the in-stream CREATE catches up.
    """
    for _ in range(tries):
        auto_sync_new_tables(connstr(PUB_P), publication, container, USER, PW, DB,
                             subscription)
        time.sleep(0.5)


def settle(port: int, sql: str, want: str, secs: float = 20) -> bool:
    deadline = time.time() + secs
    while time.time() < deadline:
        try:
            if q(port, sql) == want:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def members(publication: str) -> set[str]:
    out = q(PUB_P, "SELECT schemaname || '.' || tablename FROM pg_publication_tables "
                   f"WHERE pubname = '{publication}';")
    return {l.strip() for l in out.splitlines() if l.strip()}


# ── phase 1: two instances, one publication ─────────────────────────
def phase_one() -> None:
    print("\n=== phase 1: two instances on ONE publication ===", flush=True)
    up()
    pub = connstr(PUB_P)
    q(PUB_P, "CREATE TABLE seed (id int PRIMARY KEY, val text);")
    q(PUB_P, "CREATE PUBLICATION shared_pub FOR TABLE seed;")
    for port in (A_P, B_P):
        q(port, "CREATE TABLE seed (id int PRIMARY KEY, val text);")

    install_capture_triggers(pub, "shared_pub", follow_schemas=["public"])
    attach(A_C, A_P, "shared_pub", "sub_a")
    attach(B_C, B_P, "shared_pub", "sub_b")

    q(PUB_P, "INSERT INTO seed VALUES (1, 'first');")
    check(settle(A_P, "SELECT count(*) FROM seed;", "1"), "A receives baseline rows")
    check(settle(B_P, "SELECT count(*) FROM seed;", "1"), "B receives baseline rows")

    # a new table on the primary
    q(PUB_P, "CREATE TABLE orders (id int PRIMARY KEY, amount int);")
    check("public.orders" in members("shared_pub"), "new table auto-joins the publication")
    check(settle(A_P, "SELECT count(*) FROM information_schema.tables "
                      "WHERE table_name = 'orders';", "1"), "A gets the new table (DDL)")
    check(settle(B_P, "SELECT count(*) FROM information_schema.tables "
                      "WHERE table_name = 'orders';", "1"), "B gets the new table (DDL)")

    sync(A_C, "shared_pub", "sub_a")
    sync(B_C, "shared_pub", "sub_b")
    q(PUB_P, "INSERT INTO orders VALUES (1, 100);")
    check(settle(A_P, "SELECT amount::text FROM orders WHERE id = 1;", "100"),
          "A receives rows of the new table")
    check(settle(B_P, "SELECT amount::text FROM orders WHERE id = 1;", "100"),
          "B receives rows of the new table")

    # a schema change on an existing table
    q(PUB_P, "ALTER TABLE seed ADD COLUMN note text;")
    check(settle(A_P, "SELECT count(*) FROM information_schema.columns "
                      "WHERE table_name = 'seed' AND column_name = 'note';", "1"),
          "A applies ALTER TABLE ADD COLUMN")
    check(settle(B_P, "SELECT count(*) FROM information_schema.columns "
                      "WHERE table_name = 'seed' AND column_name = 'note';", "1"),
          "B applies ALTER TABLE ADD COLUMN")

    q(PUB_P, "INSERT INTO seed VALUES (2, 'second', 'hello');")
    check(settle(A_P, "SELECT note FROM seed WHERE id = 2;", "hello"),
          "A receives data written through the new column")
    check(settle(B_P, "SELECT note FROM seed WHERE id = 2;", "hello"),
          "B receives data written through the new column")
    down()


# ── phase 2: two instances, different publications ──────────────────
def phase_two() -> None:
    print("\n=== phase 2: two instances, DIFFERENT publications ===", flush=True)
    up()
    pub = connstr(PUB_P)
    for port in (PUB_P, A_P, B_P):
        q(port, "CREATE SCHEMA sa; CREATE SCHEMA sb;")
    q(PUB_P, "CREATE TABLE sa.anchor (id int PRIMARY KEY);"
             "CREATE TABLE sb.anchor (id int PRIMARY KEY);")
    for port in (A_P, B_P):
        q(port, "CREATE TABLE sa.anchor (id int PRIMARY KEY);"
                "CREATE TABLE sb.anchor (id int PRIMARY KEY);")
    q(PUB_P, "CREATE PUBLICATION pub_a FOR TABLE sa.anchor;")
    q(PUB_P, "CREATE PUBLICATION pub_b FOR TABLE sb.anchor;")

    # A first, then B. Before the split, installing B rewrote the one shared
    # function and A stopped auto-adding from here on.
    install_capture_triggers(pub, "pub_a", follow_schemas=["sa"])
    install_capture_triggers(pub, "pub_b", follow_schemas=["sb"])
    check(auto_add_name("pub_a") != auto_add_name("pub_b"), "each has its own trigger name")
    trig = q(PUB_P, "SELECT count(*) FROM pg_event_trigger "
                    f"WHERE evtname IN ('{auto_add_name('pub_a')}', '{auto_add_name('pub_b')}');")
    check(trig == "2", "both auto-add triggers are installed side by side")

    attach(A_C, A_P, "pub_a", "sub_a")
    attach(B_C, B_P, "pub_b", "sub_b")

    # a table in A's schema, created AFTER B installed
    q(PUB_P, "CREATE TABLE sa.only_a (id int PRIMARY KEY, v text);")
    check("sa.only_a" in members("pub_a"), "A still auto-adds after B was installed")
    check("sa.only_a" not in members("pub_b"), "and B's publication does not take it")

    q(PUB_P, "CREATE TABLE sb.only_b (id int PRIMARY KEY, v text);")
    check("sb.only_b" in members("pub_b"), "B auto-adds its own schema")
    check("sb.only_b" not in members("pub_a"), "and A's publication does not take it")

    sync(A_C, "pub_a", "sub_a")
    sync(B_C, "pub_b", "sub_b")
    q(PUB_P, "INSERT INTO sa.only_a VALUES (1, 'a-side');")
    q(PUB_P, "INSERT INTO sb.only_b VALUES (1, 'b-side');")
    check(settle(A_P, "SELECT v FROM sa.only_a WHERE id = 1;", "a-side"),
          "A receives its own table's rows")
    check(settle(B_P, "SELECT v FROM sb.only_b WHERE id = 1;", "b-side"),
          "B receives its own table's rows")
    check(settle(B_P, "SELECT count(*) FROM sa.only_a;", "0", secs=8),
          "B receives NO rows of A's table")
    check(settle(A_P, "SELECT count(*) FROM sb.only_b;", "0", secs=8),
          "A receives NO rows of B's table")

    # scope: a schema neither follows
    q(PUB_P, "CREATE SCHEMA sc; CREATE TABLE sc.nobody (id int PRIMARY KEY);")
    check("sc.nobody" not in members("pub_a") and "sc.nobody" not in members("pub_b"),
          "a schema nobody follows joins neither publication")

    # the known, accepted gap: the log table is shared, so DDL rows reach both
    both = settle(B_P, "SELECT count(*) FROM information_schema.tables "
                       "WHERE table_schema = 'sa' AND table_name = 'only_a';", "1", secs=8)
    print(f"  NOTE  B also executed A's CREATE TABLE (empty shell): {both}", flush=True)
    down()


if __name__ == "__main__":
    try:
        phase_one()
        phase_two()
    finally:
        down()
    bad = [d for ok, d in results if not ok]
    print(f"\n{len(results) - len(bad)} passed, {len(bad)} failed", flush=True)
    for d in bad:
        print(f"  FAILED: {d}", flush=True)
    sys.exit(1 if bad else 0)
