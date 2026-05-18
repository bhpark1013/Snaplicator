# Snaplicator

**English** | [한국어](README.ko.md)

Snaplicator is a PostgreSQL test-data provisioning toolkit. A long-lived
**replica container** subscribes to a production publication via **native
logical replication**, so its data stays near-real-time without ever touching
the primary. On top of that replica, **btrfs snapshots** and writable
**clones** give you instant, isolated copies of the database — each clone is
its own throwaway Postgres container.

Tables that are not (or cannot be) logically replicated — e.g. an `etl`
schema fed by an external pipeline — are exposed live and read-only through
**`postgres_fdw`**, managed declaratively from `configs/fdw.yaml`.

- **Backend** — FastAPI (`backend/`), orchestrates Docker + btrfs + replication
- **Frontend** — Vite + React management UI (`frontend/`)
- **CLI** — `snaplicator`, a Typer-based psql-style remote client (`backend/cli/`)
- **MCP server** — exposes the REST API as MCP tools (`mcp-server/`)

> Optional anonymization (`configs/anonymize.sql`) is applied automatically
> **only when cloning directly from the live main replica** (not for
> snapshot-derived clones).

---

## How it works

```
            ┌────────────────────┐  logical replication (CREATE SUBSCRIPTION)
 primary ──►│  replica container │◄──────────────── tables in PUBLICATION
 (publisher)│  (Postgres, btrfs) │
            │                    │◄── postgres_fdw ─ tables in configs/fdw.yaml
            └─────────┬──────────┘     (live read-only, e.g. etl schema)
                      │ btrfs snapshot
              ┌───────┴────────┐
              │ snapshots       │  → writable clones, each its own
              │ (read-only subv)│     Postgres container on its own port
              └────────────────┘
```

- The replica runs in Docker with `--network host`, `wal_level=logical`, and
  its `PGDATA` on a btrfs subvolume under `ROOT_DATA_DIR/MAIN_DATA_DIR`.
- After the container is healthy, post-init scripts run **inside** it:
  `05_clone_schema.sh` → `20_create_subscription.sh` → `06_setup_fdw.sh`.
- The backend runs a background **DDL auto-sync loop** that keeps the
  subscriber in step with the publisher: it installs an auto-add event
  trigger on the publisher and periodically syncs new tables, added columns,
  CHECK constraints, `SET SCHEMA` moves, and FDW column drift. Activity is
  recorded in a unified sync log (`/replication/sync-log`).
- A snapshot is a read-only btrfs subvolume; a clone is a writable subvolume
  plus a dedicated Postgres container, created/reset in seconds via
  copy-on-write.

`ROOT_DATA_DIR` is the btrfs filesystem that holds **everything** (the main
replica + every snapshot + every clone, as sibling subvolumes).
`MAIN_DATA_DIR` is the name of just the main replica's subvolume inside it,
so the live replica's data path is `ROOT_DATA_DIR/MAIN_DATA_DIR`.

---

## Prerequisites

**Host (Linux only — for btrfs + Docker):**

- Linux with **btrfs** (`btrfs-progs`). On macOS/Windows run inside a Linux VM
  (UTM, Multipass, Lima, …); Docker Desktop alone cannot host btrfs subvolumes.
- **Docker** (the replica and every clone are containers).
- **`psql` client on the host** — the backend runs publisher SQL via the
  host's `psql` (subscriber SQL goes through `docker exec`).
- **Passwordless sudo** for the operations the toolkit shells out to:
  `btrfs`, `chown`, `chmod`, `mkdir`, `mv`, `mount`, and (when provisioning a
  btrfs volume) the LVM tools. The backend calls `sudo -n …`, so configure
  sudoers accordingly or run as a user that already has these rights.
- `util-linux` / LVM helpers used during btrfs provisioning:
  `findmnt`, `lsblk`, `blkid`, `pvcreate`, `vgcreate`, `lvcreate`, `mkfs.btrfs`.
- `make`.

**Backend:**

- Python **3.10+** with `venv`. Deps (pinned in `backend/requirements.txt`):
  FastAPI, Uvicorn, pydantic-settings, python-dotenv, PyYAML.

**Frontend:**

- Node.js + **pnpm**.

**Database:**

- A primary PostgreSQL with logical replication enabled
  (`wal_level = logical`) and a **publication**.
- A role with replication privileges for `CREATE SUBSCRIPTION`.
- *(Optional)* a separate **read-only role** for `postgres_fdw`, which may
  point at a different host/port (bastion, pgbouncer) than replication.

> Not sure if your host/DB is ready? Run `make doctor` — it checks every
> item above and tells you exactly what is missing and how to fix it.

---

## Configuration

All configuration lives in **`configs/.env`** (loaded by both the shell
scripts and the backend via pydantic-settings). `make setup` writes this for
you interactively; to do it by hand, start from the template:

```bash
cp configs/.env.example configs/.env
$EDITOR configs/.env
```

| Variable | Required | Purpose |
|---|---|---|
| `CONTAINER_NAME` | ✓ | Replica container name |
| `NETWORK_NAME` | ✓ | Docker network name (replica forces host networking) |
| `HOST_PORT` | ✓ | Replica Postgres port |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | ✓ | Replica superuser/db |
| `POSTGRES_IMAGE` | – | Postgres image (default `postgres:17`; use a custom image for extra extensions) |
| `ROOT_DATA_DIR` | ✓ | btrfs root holding the replica + all snapshots/clones |
| `MAIN_DATA_DIR` | ✓ | Subvolume name of the main replica under `ROOT_DATA_DIR` |
| `PRIMARY_HOST` / `PRIMARY_PORT` / `PRIMARY_DB` | ✓ | Publisher connection |
| `PRIMARY_USER` / `PRIMARY_PASSWORD` | ✓ | Replication role credentials |
| `PGSSLMODE` | – | e.g. `require` (defaults to `prefer`) |
| `PUBLICATION_NAME` | ✓ | Publication to subscribe to |
| `SUBSCRIPTION_NAME` | ✓ | Subscription name created on the replica |
| `DUMP_SCHEMAS` | – | Comma-separated schemas to schema-clone (DDL) from primary (default `public`) |
| `PRECREATED_SLOT_NAME` | – | Reuse a pre-created replication slot instead of letting `CREATE SUBSCRIPTION` make one |
| `FDW_USER` / `FDW_PASSWORD` | – | postgres_fdw role; **blank disables FDW entirely** |
| `FDW_HOST` / `FDW_PORT` / `FDW_DB` | – | FDW target; fall back to `PRIMARY_*` if blank |
| `DDL_SYNC_INTERVAL` | – | Auto-sync loop interval in seconds (default 30; `0` disables) |

Other config files in `configs/`:

- **`fdw.yaml`** — single source of truth for `postgres_fdw`. Lists the
  foreign `schema.table`s (e.g. the `etl` schema) and server options. Edit via
  the Replication UI (preferred) or by hand; if hand-edited,
  `POST /replication/fdw/regenerate` re-renders `fdw_setup.generated.sql` and
  re-applies it. The `.generated.sql` file is derived — never edit it directly.
- **`anonymize.sql`** *(gitignored; copy from `anonymize-example.sql`)* —
  masking SQL run automatically on clone-from-main only.
- **`replication_check.example.sql`** — default seed for the web-editable
  replication check query. The live, environment-specific version is stored
  outside the repo at **`~/.snaplicator/replication_check.sql`** (survives
  re-clone/reset; editable from the Replication UI, write-guarded read-only).

---

## Getting started

### Easiest: one-command setup

```bash
git clone <repo-url> Snaplicator && cd Snaplicator
make setup
```

`make setup` installs dependencies, walks you through `configs/.env` with
sensible defaults (press Enter to accept), runs the preflight **doctor**, and
— once everything is green — offers to bring up the replica and start the
API + UI. Re-running it is safe (it backs up an existing `configs/.env`).

You still need the host prerequisites above and the publisher prepared
(`wal_level=logical` + a publication). `make doctor` tells you exactly
what is missing and how to fix it, any time:

```bash
make doctor          # red/green checklist + copy-paste fixes
```

### Manual steps (what `make setup` automates)

```bash
git clone <repo-url> Snaplicator && cd Snaplicator

# 1. Configure
cp configs/.env.example configs/.env && $EDITOR configs/.env
# (optional) cp configs/anonymize-example.sql configs/anonymize.sql
# (optional) edit configs/fdw.yaml for postgres_fdw tables

# 2. On the PRIMARY (publisher), once:
#    ALTER SYSTEM SET wal_level = logical;   -- then restart
#    CREATE PUBLICATION <PUBLICATION_NAME> FOR TABLES IN SCHEMA public;
#    -- grant the replication role REPLICATION + SELECT as needed

# 3. Install dependencies
make server-prepare              # backend venv + pip install
( cd frontend && pnpm install )  # frontend deps

# 4. Verify everything is ready
make doctor                      # fix any ✘ before continuing

# 5. Bring up the replica (provisions btrfs interactively if needed,
#    starts the container, runs schema-clone + subscription + FDW)
make replica

# 6. Run the API + UI
make dev                         # backend :8888 and frontend :3000 together
#   or separately:  make server   /   make fe

# 7. Open the UI
#    http://localhost:3000   (the UI proxies /api → http://localhost:8888)
#    API docs: http://localhost:8888/docs
```

`make replica` is interactive the first time if `ROOT_DATA_DIR` is **not**
already on btrfs: it can initialize an LVM-backed btrfs volume (it will prompt
before any destructive step). On failure it copies the in-container
`replica-init.log` to the repo root — check it before retrying.

---

## Make targets

| Target | What it does |
|---|---|
| `make setup` | **One-command first-run**: deps + interactive `configs/.env` + `doctor` + optional launch |
| `make doctor` | Pre-launch environment check — red/green checklist with copy-paste fixes (no server needed) |
| `make replica` | Provision btrfs (if needed) + run the replica container + post-init (schema clone, subscription, FDW) |
| `make server-prepare` | Create `backend/.venv` and install Python deps (first run only) |
| `make server` | Run the FastAPI server on `0.0.0.0:8888` (`--reload`) |
| `make fe` | `pnpm install` + `pnpm dev` for the frontend on `:3000` |
| `make dev` | Run `server` and `fe` concurrently |

---

## CLI

A Typer-based remote client talks to a running Snaplicator API:

```bash
pip install -e backend            # installs the `snaplicator` command
export SNAPLICATOR_URL=http://localhost:8888

snaplicator health
snaplicator clones ...            # manage clones
snaplicator snap ...              # manage snapshots
snaplicator repl ...              # monitor/manage replication
```

`--host/-H` overrides `SNAPLICATOR_URL`.

## MCP server

`mcp-server/server.py` exposes the REST API as MCP tools over stdio (clones,
snapshots, replication). It needs the `mcp` and `httpx` packages in its venv
and reads `SNAPLICATOR_URL` (default `http://localhost:8888`):

```bash
mcp-server/.venv/bin/python mcp-server/server.py
```

---

## API smoke test

```bash
curl -s localhost:8888/health | jq .
curl -s 'localhost:8888/setup/preflight' | jq .          # same checks as `make doctor`
curl -s 'localhost:8888/setup/preflight?deep=false' | jq .  # skip network calls (fast)
curl -s localhost:8888/snapshots | jq .
curl -s -X POST localhost:8888/snapshots -H 'content-type: application/json' \
     -d '{"description":"before migration"}' | jq .
curl -s -X POST localhost:8888/snapshots/<snapshot_name>/clone | jq .
curl -s localhost:8888/replication/lag | jq .
curl -s localhost:8888/replication/sync-log | jq .
```

Route groups: `/health`, `/setup` (`/setup/preflight`), `/snapshots`,
`/clones`, `/replication` (incl. `/replication/fdw*`,
`/replication/sync-log`, `/replication/check-sql`). Full schema at `/docs`.

---

## Scripts

- `scripts/setup.sh` — backs `make setup`: deps, interactive `configs/.env`,
  preflight, optional launch.
- `scripts/run-replica-postgres.sh` — the heart of `make replica`: btrfs/LVM
  provisioning, container run, in-container post-init.
- `scripts/create_main_snapshot.sh` — snapshot the main replica.
- `scripts/create-clone-from-snapshot-postgres.sh` — launch a clone container.
- `scripts/maintenance/cleanup_all.sh` — prune stale clones/containers.
- `replication/replica-init/*.sh` — in-container init steps
  (`01_wait_for_db`, `03_install_extensions`, `05_clone_schema`,
  `06_setup_fdw`, `20_create_subscription`).

`backend/app/services/preflight.py` holds the doctor logic and is runnable
standalone (`python -m app.services.preflight`); it is also served at
`GET /setup/preflight`.

---

## Troubleshooting

- **Anything before launch** — run `make doctor`; it pinpoints missing
  prerequisites (env keys, Docker, psql, btrfs, sudo, publisher
  `wal_level`/publication) with copy-paste fixes.
- **Replica init failure** — read `replica-init.log` at the repo root (copied
  out of the container on failure) and `docker logs <CONTAINER_NAME>`.
- **Clone container won't start** — `docker logs <clone-container>`.
- **Out of btrfs space** — delete old subvolumes:
  `sudo btrfs subvolume delete <ROOT_DATA_DIR>/<subvol>`.
- **Subscription not created** — verify `wal_level=logical` and the
  publication on the primary, that the replication role can connect, and (if
  set) that `PRECREATED_SLOT_NAME` exists.
- **FDW not set up** — `06_setup_fdw.sh` no-ops unless `FDW_USER`/
  `FDW_PASSWORD` are set and a host/port/db resolves (FDW_* or PRIMARY_*).
  Re-render/re-apply with `POST /replication/fdw/regenerate`.
- **DDL changes not propagating** — the auto-sync loop runs every
  `DDL_SYNC_INTERVAL`s; check `/replication/sync-log` and the
  `snaplicator.ddl_sync` logs. Ensure the auto-add trigger is installed
  (`/replication/trigger-status`).
- **`sudo` prompts / permission denied** — the backend uses `sudo -n`
  (non-interactive); configure passwordless sudo for btrfs/chown/mount/LVM.
- **macOS** — keep `ROOT_DATA_DIR` on the Linux VM's btrfs mount.

Keep `configs/.env`, `configs/fdw.yaml`, and `configs/anonymize.sql` in sync
with your environment, and extend the Makefile/scripts for your own workflows.
