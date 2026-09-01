"""Stepwise install CLI: `python3 -m snaplicator_install <step>`.

The one-shot installer (deploy/install.sh) and this CLI share the same stage
order; this exists so each stage can be run, inspected and re-run on its own.
Every command re-checks what the previous stage should have left behind and
refuses with "run X first" instead of doing things out of order — so a run
that stopped anywhere can be resumed by running `status` and following it.

Written for a human at a terminal and equally for an agent driving an
install on someone's behalf. The rule for agents: any flag whose help text
begins with "DECISION" encodes a choice a person must make — ask them, then
carry the answer; everything else has a safe default. Steps marked
[read-only] change nothing and may be run freely; the rest say what they
touch.

Stages, in order (same as deploy/install.sh):

  1. prereqs      host tooling check                      [read-only]
  2. plan         survey pool locations                   [read-only]
  3. provision    create the btrfs pool                   [root; changes this host]
  4. publication  reuse or create the publication         [may change the publisher]
  5. configure    write deploy/.env                       [writes one file]
  6. up           build + start manager & web             [starts containers]
  7. bootstrap    start the initial copy                  [copies data; point of no return]

  status          where the install stands, what to run next   [read-only]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CLI_DIR = Path(__file__).resolve().parents[1]      # <repo>/cli
REPO_ROOT = CLI_DIR.parent                          # <repo>
DEPLOY_DIR = REPO_ROOT / "deploy"
ENV_FILE = DEPLOY_DIR / ".env"

# Same keys, same order, same defaults as deploy/install.sh writes — the two
# entry points must produce interchangeable installs.
ENV_DEFAULTS = {
    "WEB_PORT": "8080",
    "BACKEND_PORT": "8888",
    "COMPOSE_PROJECT": "snaplicator",
    "MAIN_DATA_DIR": "main",
    "CONTAINER_NAME": "snaplicator_replica",
    "NETWORK_NAME": "snaplicator",
    "HOST_PORT": "5433",
    "POSTGRES_IMAGE": "postgres:17",
    "POSTGRES_USER": "postgres",
    "POSTGRES_DB": "postgres",
    "PGSSLMODE": "prefer",
    "PUBLICATION_NAME": "snaplicator_publication",
    "SUBSCRIPTION_NAME": "snaplicator_subscription",
    "DDL_SYNC_INTERVAL": "30",
    "DDL_APPLY_ENABLED": "1",
}


def info(msg: str) -> None:
    print(f"[snaplicator-install] {msg}")


def die(msg: str, code: int = 1) -> "None":
    print(f"[snaplicator-install] ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def have(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def sh(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def read_env() -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v
    return out


def parse_connstr(connstr: str) -> Dict[str, str]:
    u = urllib.parse.urlsplit(connstr)
    if u.scheme not in ("postgres", "postgresql"):
        die(f"not a postgres URI: {connstr[:40]}...")
    return {
        "PRIMARY_HOST": u.hostname or "",
        "PRIMARY_PORT": str(u.port or 5432),
        "PRIMARY_DB": (u.path or "/").lstrip("/") or "postgres",
        "PRIMARY_USER": urllib.parse.unquote(u.username or ""),
        "PRIMARY_PASSWORD": urllib.parse.unquote(u.password or ""),
    }


def api_url(env: Dict[str, str], override: Optional[str]) -> str:
    if override:
        return override.rstrip("/")
    return f"http://127.0.0.1:{env.get('BACKEND_PORT', '8888')}"


def http(method: str, url: str, timeout: int = 15):
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def psql(connstr: str, sql: str) -> Tuple[bool, str]:
    r = sh(["psql", connstr, "-Atc", sql])
    return r.returncode == 0, (r.stdout if r.returncode == 0 else r.stderr).strip()


def is_btrfs(path: str) -> bool:
    r = sh(["findmnt", "-no", "FSTYPE", "-T", path])
    return r.returncode == 0 and r.stdout.strip() == "btrfs"


# ── 1. prereqs ────────────────────────────────────────────────────────

def cmd_prereqs(args) -> None:
    checks = [
        ("docker", have("docker"), "install docker (get.docker.com)"),
        ("docker daemon reachable",
         have("docker") and sh(["docker", "info"]).returncode == 0,
         "start dockerd, or add this user to the docker group"),
        ("docker compose v2",
         have("docker") and sh(["docker", "compose", "version"]).returncode == 0,
         "install the docker-compose-plugin package"),
        ("mkfs.btrfs", have("mkfs.btrfs"), "install btrfs-progs"),
        ("psql", have("psql"), "install postgresql-client"),
    ]
    failed = False
    for name, ok, hint in checks:
        print(f"  {'✓' if ok else '✗'} {name}" + ("" if ok else f"   → {hint}"))
        failed = failed or not ok
    if failed:
        die("missing prerequisites — fix the ✗ lines above, then re-run", 1)
    info("all prerequisites present — next: plan")


# ── 2. plan / 3. provision — delegate to snaplicator_init ─────────────

def _init(argv: List[str]) -> int:
    return subprocess.call([sys.executable, "-m", "snaplicator_init", *argv],
                           cwd=str(CLI_DIR))


def cmd_plan(args) -> None:
    argv = [args.connstr] if args.connstr else []
    if args.payload_bytes:
        argv += ["--payload-bytes", str(args.payload_bytes)]
    if args.pool_bytes:
        argv += ["--pool-bytes", str(args.pool_bytes)]
    rc = _init(argv)
    print()
    info("DECISION — pick where the pool lives, from the candidates above:")
    info("  a ✓ candidate has room to grow (recommended); a △ one holds the")
    info("  data but little more — a person may accept that trade.")
    info("carry the choice to the next step:")
    info("  provision --data-dir <mount>/snaplicator   (subvolume/loopback)")
    info("  provision --format-disk /dev/X             (DESTRUCTIVE — erases the disk)")
    raise SystemExit(rc)


def cmd_provision(args) -> None:
    if bool(args.data_dir) == bool(args.format_disk):
        die("pass exactly one of --data-dir / --format-disk — this is the "
            "human's pool-location choice from `plan`, never defaulted here")
    if os.geteuid() != 0:
        die("provision changes the host (subvolume/loopback/mkfs) — run as root")
    argv = ["--apply", "--yes"]
    if args.connstr:
        argv.append(args.connstr)
    elif args.pool_bytes:
        argv += ["--pool-bytes", str(args.pool_bytes)]
    else:
        die("pass the publisher CONNSTR (to size the pool) or --pool-bytes")
    if args.data_dir:
        argv += ["--data-dir", args.data_dir]
    if args.format_disk:
        argv += ["--format-disk", args.format_disk]
    if args.force:
        argv += ["--force"]
    rc = _init(argv)
    if rc == 0:
        info("next: publication")
    raise SystemExit(rc)


# ── 4. publication ────────────────────────────────────────────────────

def cmd_publication(args) -> None:
    ident_ok = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.name)
    if not ident_ok:
        die(f"publication name is not a plain identifier: {args.name}")
    ok, out = psql(args.connstr,
                   f"SELECT count(*) FROM pg_publication WHERE pubname='{args.name}'")
    if not ok:
        die(f"cannot reach the publisher: {out}")
    if out != "0":
        ok, tables = psql(
            args.connstr,
            f"SELECT count(*) FROM pg_publication_tables WHERE pubname='{args.name}'")
        info(f"publication {args.name} already exists"
             f" ({tables if ok else '?'} tables) — reusing it as-is")
        info("next: configure")
        return
    if args.create_for_all_tables:
        ok, out = psql(args.connstr,
                       f"CREATE PUBLICATION {args.name} FOR ALL TABLES")
        if not ok:
            die(f"could not create the publication: {out}")
        info(f"created publication {args.name} FOR ALL TABLES")
        info("next: configure")
        return
    info(f"publication {args.name} does not exist yet.")
    info("DECISION — what should replicate? Three ways to answer:")
    info("  • reuse an existing publication: re-run with its --name")
    info("  • replicate everything: re-run with --create-for-all-tables")
    info("  • pick tables in the UI after `up` — leave the publication to it")
    raise SystemExit(1)


# ── 5. configure ──────────────────────────────────────────────────────

def cmd_configure(args) -> None:
    prev = read_env()
    env = dict(ENV_DEFAULTS)
    env.update(parse_connstr(args.connstr))
    env["ROOT_DATA_DIR"] = args.pool
    env["PUBLICATION_NAME"] = args.publication
    env["SUBSCRIPTION_NAME"] = args.subscription
    env["HOST_PORT"] = str(args.replica_port)
    env["WEB_PORT"] = str(args.web_port)
    env["BACKEND_PORT"] = str(args.api_port)
    env["COMPOSE_PROJECT"] = args.project
    env["POSTGRES_DB"] = env["PRIMARY_DB"]
    if args.postgres_image:
        env["POSTGRES_IMAGE"] = args.postgres_image
    # A re-run must keep the password it gave out: the replica container was
    # created with it and is not recreated here.
    env["POSTGRES_PASSWORD"] = (args.replica_password
                                or prev.get("POSTGRES_PASSWORD")
                                or secrets.token_hex(16))

    if not is_btrfs(args.pool) and not args.force:
        die(f"{args.pool} is not on a mounted btrfs filesystem — run "
            "`provision` first (or --force if you know better)")

    keys = ["WEB_PORT", "BACKEND_PORT", "COMPOSE_PROJECT", "ROOT_DATA_DIR",
            "MAIN_DATA_DIR", "CONTAINER_NAME", "NETWORK_NAME", "HOST_PORT",
            "POSTGRES_IMAGE", "POSTGRES_USER", "POSTGRES_PASSWORD",
            "POSTGRES_DB", "PRIMARY_HOST", "PRIMARY_PORT", "PRIMARY_DB",
            "PRIMARY_USER", "PRIMARY_PASSWORD", "PGSSLMODE",
            "PUBLICATION_NAME", "SUBSCRIPTION_NAME", "DDL_SYNC_INTERVAL",
            "DDL_APPLY_ENABLED"]
    body = "".join(f"{k}={env[k]}\n" for k in keys)
    if ENV_FILE.exists() and ENV_FILE.read_text() != body:
        ENV_FILE.replace(ENV_FILE.with_suffix(".env.bak"))
    ENV_FILE.write_text(body)
    os.chmod(ENV_FILE, 0o600)
    info(f"wrote {ENV_FILE}")
    info(f"  replica  : port {env['HOST_PORT']}, image {env['POSTGRES_IMAGE']}")
    info(f"  pub/sub  : {env['PUBLICATION_NAME']} / {env['SUBSCRIPTION_NAME']}")
    info(f"  ui/api   : {env['WEB_PORT']} / {env['BACKEND_PORT']}"
         f"   compose project: {env['COMPOSE_PROJECT']}")
    info("next: up")


# ── 6. up ─────────────────────────────────────────────────────────────

def cmd_up(args) -> None:
    env = read_env()
    if not env:
        die(f"{ENV_FILE} not found — run `configure` first")
    project = env.get("COMPOSE_PROJECT", "snaplicator")
    info(f"building and starting the management plane (project {project})...")
    rc = subprocess.call(
        ["docker", "compose", "-p", project, "up", "-d", "--build"],
        cwd=str(DEPLOY_DIR))
    if rc != 0:
        die("docker compose failed — its output above says why")
    base = api_url(env, args.api)
    for _ in range(30):
        try:
            http("GET", f"{base}/health", timeout=3)
            break
        except Exception:
            time.sleep(2)
    else:
        die(f"backend never became healthy — "
            f"check: docker compose -p {project} logs manager")
    info(f"management plane healthy — UI :{env.get('WEB_PORT')}, API {base}")
    info("next: bootstrap  (or pick tables in the UI first — the table set "
         "is frozen the moment the copy starts)")


# ── 7. bootstrap ──────────────────────────────────────────────────────

def cmd_bootstrap(args) -> None:
    env = read_env()
    base = api_url(env, args.api)
    try:
        http("GET", f"{base}/health", timeout=5)
    except Exception as e:
        die(f"manager not reachable at {base} ({e}) — run `up` first")
    url = f"{base}/replication/bootstrap" + ("?force=true" if args.force else "")
    try:
        started = http("POST", url, timeout=60)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        if e.code == 409:
            die(f"refused (409): {detail}\n"
                "  a capacity refusal can be overridden with --force — "
                "that is a DECISION, not a retry")
        die(f"bootstrap failed to start ({e.code}): {detail}")
    info(f"bootstrap started: {started}")
    if not args.watch:
        info(f"follow it with: GET {base}/replication/bootstrap?tail=40")
        return
    last = ""
    while True:
        time.sleep(args.poll_seconds)
        try:
            st = http("GET", f"{base}/replication/bootstrap?tail=6", timeout=10)
        except Exception:
            continue
        tail = (st.get("log_tail") or "").strip().splitlines()
        line = tail[-1] if tail else ""
        if line and line != last:
            print(f"    {line}")
            last = line
        if st.get("state") in ("succeeded", "failed", "not_started"):
            info(f"bootstrap {st.get('state')}"
                 + (f" (exit {st.get('exit_code')})" if st.get("exit_code") is not None else ""))
            raise SystemExit(0 if st.get("state") == "succeeded" else 1)


# ── status ────────────────────────────────────────────────────────────

def cmd_status(args) -> None:
    env = read_env()
    base = api_url(env, args.api)
    pool = env.get("ROOT_DATA_DIR", "")

    def row(name, ok, detail=""):
        print(f"  {'✓' if ok else '·'} {name}" + (f"   {detail}" if detail else ""))
        return ok

    print("install progress (stages in order):")
    ok_tools = row("1 prereqs", have("docker") and have("psql") and have("mkfs.btrfs"))
    ok_pool = row("2-3 pool provisioned", bool(pool) and is_btrfs(pool),
                  pool or "(no ROOT_DATA_DIR yet)")
    ok_env = row("4-5 configured", bool(env), str(ENV_FILE) if env else "")
    healthy = False
    if env:
        try:
            healthy = http("GET", f"{base}/health", timeout=3).get("status") == "ok"
        except Exception:
            pass
    ok_up = row("6 management plane", healthy, base if healthy else "")
    boot = {}
    if healthy:
        try:
            boot = http("GET", f"{base}/replication/bootstrap?tail=1", timeout=5)
        except Exception:
            pass
    row("7 bootstrap", boot.get("state") == "succeeded",
        f"state: {boot.get('state', 'unknown')}" if env else "")

    for stage, done, nxt in [
        (1, ok_tools, "prereqs"), (3, ok_pool, "plan, then provision"),
        (5, ok_env, "publication, then configure"), (6, ok_up, "up"),
        (7, boot.get("state") == "succeeded", "bootstrap"),
    ]:
        if not done:
            info(f"next: {nxt}")
            return
    info("install complete — the UI has the replica and clones")


# ── argparse wiring ──────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="snaplicator-install",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    CONNSTR_HELP = (
        "publisher connection URI, postgres://user:pw@host:port/db — the "
        "database being replicated FROM. The user must be a superuser "
        "(event triggers) with replication rights (rds_replication on "
        "RDS/Aurora).")

    def connstr_arg(sp, required=True, positional=True):
        if positional:
            sp.add_argument("connstr", nargs=None if required else "?",
                            help=CONNSTR_HELP)
        else:
            sp.add_argument("--connstr", required=required, help=CONNSTR_HELP)

    sp = sub.add_parser(
        "prereqs", help="1. check host tooling (read-only)",
        description="Checks docker, docker compose v2, btrfs-progs and psql. "
                    "Changes nothing; prints an install hint per missing tool.")
    sp.set_defaults(fn=cmd_prereqs)

    sp = sub.add_parser(
        "plan", help="2. survey pool locations (read-only)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Measures the payload on the publisher and ranks every "
            "place the btrfs pool could live.\n\n"
            "Reading the result:\n"
            "  ✓  holds payload × 2 — room for months of snapshots/clones (recommended)\n"
            "  △  holds the data, little more — selectable, a human may accept the trade\n"
            "  ✗  cannot hold the data — not offered\n\n"
            "DECISION — the pool location. Present the ✓/△ candidates to the "
            "human; carry their answer to `provision` as --data-dir or "
            "--format-disk.")
    connstr_arg(sp, required=False)
    sp.add_argument("--payload-bytes", type=int,
                    help="skip measuring and assume this payload size (testing)")
    sp.add_argument("--pool-bytes", type=int,
                    help="pin the pool size outright — replaces the ×1/×2 marks")
    sp.set_defaults(fn=cmd_plan)

    sp = sub.add_parser(
        "provision", help="3. create the btrfs pool (root)",
        description="Creates the pool at the location chosen in `plan`: a "
            "subvolume on an existing btrfs mount, a loopback file on "
            "ext4/xfs, or — destructively — a formatted disk. Re-running "
            "after success is a no-op; after a mid-way failure it resumes.")
    connstr_arg(sp, required=False)
    sp.add_argument("--data-dir",
                    help="DECISION — pool path from `plan`, e.g. "
                         "/data/snaplicator (subvolume or loopback; nothing "
                         "is erased)")
    sp.add_argument("--format-disk", metavar="DEV",
                    help="DECISION — format this whole disk as btrfs. "
                         "DESTRUCTIVE: everything on DEV is erased. Only for "
                         "a bare disk `plan` listed.")
    sp.add_argument("--pool-bytes", type=int,
                    help="pool size when no connstr is given to measure from")
    sp.add_argument("--force", action="store_true",
                    help="override the free-space check on --data-dir")
    sp.set_defaults(fn=cmd_provision)

    sp = sub.add_parser(
        "publication", help="4. reuse or create the publication",
        description="A publication is the set of tables that will replicate — "
            "it is what the initial copy copies. An existing one is reused "
            "untouched (its table set is already someone's decision).")
    connstr_arg(sp)
    sp.add_argument("--name", default="snaplicator_publication",
                    help="DECISION — which publication to replicate from. An "
                         "existing name reuses that table set; a new name "
                         "needs --create-for-all-tables or the UI to define "
                         "it. (default: %(default)s)")
    sp.add_argument("--create-for-all-tables", action="store_true",
                    help="DECISION — create the publication FOR ALL TABLES: "
                         "every table, including ones added later. The "
                         "alternative is picking tables in the UI after `up`.")
    sp.set_defaults(fn=cmd_publication)

    sp = sub.add_parser(
        "configure", help="5. write deploy/.env",
        description="Writes the one file every later stage reads. Safe to "
            "re-run; the previous file is kept as .env.bak and the replica "
            "password, once minted, is preserved.")
    sp.add_argument("--connstr", required=True,
                    help="publisher URI (as in `plan`)")
    sp.add_argument("--pool", required=True,
                    help="pool path `provision` printed, e.g. /data/snaplicator")
    sp.add_argument("--publication", default="snaplicator_publication",
                    help="DECISION — publication name from step 4 "
                         "(default: %(default)s)")
    sp.add_argument("--subscription", default="snaplicator_subscription",
                    help="DECISION — subscription name. Must be UNIQUE among "
                         "all subscribers of this publisher: it names the "
                         "replication slot, and a second install reusing a "
                         "name collides with the first. (default: %(default)s)")
    sp.add_argument("--replica-port", type=int, default=5433,
                    help="DECISION — host port for the replica postgres; pick "
                         "one nothing on this host listens on "
                         "(default: %(default)s)")
    sp.add_argument("--web-port", type=int, default=8080,
                    help="UI port (default: %(default)s)")
    sp.add_argument("--api-port", type=int, default=8888,
                    help="manager API port (default: %(default)s)")
    sp.add_argument("--project", default="snaplicator",
                    help="docker compose project name — how THIS install's "
                         "containers are told apart from another install's "
                         "on the same host (default: %(default)s)")
    sp.add_argument("--postgres-image",
                    help="replica postgres image. Default postgres:17 is a "
                         "fallback; a primary with extensions needs an image "
                         "that carries them — deploy/build-replica-image.sh "
                         "CONNSTR builds and prints one.")
    sp.add_argument("--replica-password",
                    help="replica superuser password (default: kept from the "
                         "existing .env, else generated)")
    sp.add_argument("--force", action="store_true",
                    help="write the .env even if --pool is not a btrfs mount")
    sp.set_defaults(fn=cmd_configure)

    sp = sub.add_parser(
        "up", help="6. build + start manager & web",
        description="docker compose up for the management plane, then waits "
            "for /health. Re-running rebuilds and restarts in place.")
    sp.add_argument("--api", help="manager API URL if not 127.0.0.1:<BACKEND_PORT>")
    sp.set_defaults(fn=cmd_up)

    sp = sub.add_parser(
        "bootstrap", help="7. start the initial copy (point of no return)",
        description="Clones the schema, creates the subscription and starts "
            "the initial data copy. THE TABLE SET FREEZES HERE: what the "
            "publication covers at this moment is what replicates — "
            "changing your mind later means tearing the replica down. If "
            "tables were to be hand-picked, that happens in the UI before "
            "this step. The copy runs for minutes to hours, server-side; "
            "this command returns immediately unless --watch (default) "
            "follows it to the end.")
    sp.add_argument("--api", help="manager API URL if not 127.0.0.1:<BACKEND_PORT>")
    sp.add_argument("--force", action="store_true",
                    help="DECISION — proceed past a pool-capacity refusal. "
                         "The refusal means the copy may not fit; overriding "
                         "it is a judgement about disk usage, not a retry.")
    sp.add_argument("--no-watch", dest="watch", action="store_false",
                    help="return after starting instead of following progress")
    sp.add_argument("--poll-seconds", type=int, default=10,
                    help="watch poll interval (default: %(default)s)")
    sp.set_defaults(fn=cmd_bootstrap, watch=True)

    sp = sub.add_parser(
        "status", help="where the install stands (read-only)",
        description="Checks each stage's observable result and names the "
            "next command. The right first command on any machine.")
    sp.add_argument("--api", help="manager API URL if not 127.0.0.1:<BACKEND_PORT>")
    sp.set_defaults(fn=cmd_status)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
