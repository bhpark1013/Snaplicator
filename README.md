## Snaplicator

Snaplicator is a Postgres test-data provisioning toolkit that combines logical replication with btrfs snapshots. A continuously running replica container subscribes to the production publication, so every snapshot or clone reflects near-real-time data without touching the primary. The backend is written in FastAPI, the frontend in Vite + React, and `configs/anonymize.sql` can mask sensitive fields whenever a clone is created.

### Project map
- `backend/`: FastAPI services plus Docker/btrfs orchestration
- `frontend/`: management UI (Vite + React, powered by `pnpm`)
- `cli/`: `snaplicator` CLI — psql-style remote client for the REST API
- `mcp-server/`: MCP server that wraps the REST API for agentic clients
- `replication/replica-init/`: container init scripts (schema clone, extensions, FDW, subscription)
- `scripts/`: helper utilities for running the replica container and managing snapshots/clones
- `configs/`: `.env`, `anonymize.sql`, `fdw.yaml`, and misc SQL helpers

### Prerequisites
- Linux host (native, WSL2, or a Linux VM) with `btrfs-progs`, Docker, and `make`
  - macOS users: run Docker Desktop plus a lightweight Linux VM (UTM, Multipass, Lima, etc.) so `ROOT_DATA_DIR` can live on a btrfs volume.
- Python 3.10+ with `python3 -m venv`
- `pnpm` for frontend dependencies
- A primary Postgres database that exposes logical replication (`CREATE PUBLICATION` privilege required)

---

## How replication works

Snaplicator uses two complementary paths to keep the replica current, with the FastAPI backend running a 30s loop that reconciles drift automatically:

| Path | Source of truth | What it covers | Auto-sync |
|------|-----------------|----------------|-----------|
| Native logical replication | `CREATE PUBLICATION` on the primary | All tables in the publication (DML + selected DDL) | new tables (event trigger on publisher), added columns, CHECK constraints, schema moves |
| `postgres_fdw` foreign tables | `configs/fdw.yaml` | Tables that can't go through the publication (e.g. no PRIMARY KEY, or read-only-by-FDW by design) | remote column drift (added / removed / type-changed) re-imports automatically |

Reflected changes — and any loop errors — are appended to `~/.snaplicator/sync_events.jsonl` (also exposed at `GET /replication/sync-log` and surfaced in the "Auto-Sync Activity" panel of the UI).

---

## Quick Start

### 1. Create `configs/.env`
Copy the sample file and edit it with real values:
```bash
cp configs/.env.test configs/.env
```
`configs/.env.test` documents every required section (replica container, primary DB connection, subscription/publication names, FDW credentials, etc.), so walk through it line by line and fill in the blanks for your environment.

### 2. Publisher setup
Create the publication on the primary instance:
```sql
CREATE PUBLICATION snaplicator_pub FOR TABLES IN SCHEMA public;
```

The backend installs an event trigger on the publisher at startup that auto-adds new `public.*` tables to this publication (see `GET /replication/trigger-status`). New tables therefore start replicating without manual `ALTER PUBLICATION`.

### 3. (Optional) Configure `postgres_fdw` targets
For tables that should be exposed as foreign tables instead of logically replicated, edit `configs/fdw.yaml`:
```yaml
server:
  name: prod_fdw
  options: { sslmode: require, fetch_size: '10000', use_remote_estimate: 'true' }
schemas: []
tables:
  - { schema: etl, name: some_view_v1 }
```
The yaml is the single source of truth; saving via the UI or `POST /replication/fdw/regenerate` re-renders `configs/fdw_setup.generated.sql` and applies it to the live replica idempotently. The same SQL is what `replication/replica-init/06_setup_fdw.sh` runs on container init. Connection host/port/db and credentials are passed at apply-time from `.env` (`PRIMARY_*`, `FDW_USER`, `FDW_PASSWORD`) and never baked into the file.

### 4. Install dependencies
```bash
# Backend virtualenv + Python deps
cd /path/to/Snaplicator
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt

# Frontend deps
cd /path/to/Snaplicator/frontend
pnpm install
```

### 5. Prepare Docker and btrfs
1. Create the Docker network once: `docker network create snaplicator-net`
2. Ensure `ROOT_DATA_DIR` resides on btrfs. If not, run `scripts/run-replica-postgres.sh`; it can provision an LVM-backed btrfs volume interactively.

### 6. Start the replica container
```bash
make replica
```
If the script fails, inspect `replica-init.log` and fix issues before moving on.

### 7. Run backend and frontend
```bash
# FastAPI server (defaults to 0.0.0.0:8888)
make server-prepare   # first run only
make server

# Frontend UI (defaults to http://localhost:5173)
make fe    # wraps pnpm dev

# Bring up both concurrently
make dev
```

---

## API smoke test
```bash
# Health
curl -s http://localhost:8888/health | jq .

# Replication state
curl -s http://localhost:8888/replication/check         | jq .
curl -s http://localhost:8888/replication/lag           | jq .
curl -s http://localhost:8888/replication/sync-log      | jq .

# FDW yaml inspection
curl -s http://localhost:8888/replication/fdw           | jq .

# Snapshots / clones
curl -s http://localhost:8888/snapshots | jq .
curl -s -X POST http://localhost:8888/snapshots | jq .
curl -s -X POST http://localhost:8888/snapshots/<snapshot_name>/clone | jq .
```
To clone directly from the main replica, open the frontend and use "Clone from Main" (or `POST /clones`).

---

## Anonymization behavior
- `configs/anonymize.sql` runs automatically **only** when cloning from the live main replica.
- Snapshot-derived clones skip the script. If you need sanitized data, either run the script manually or sanitize before capturing the snapshot.

---

## Handy scripts
- `scripts/run-replica-postgres.sh`: provision the replica container (and optionally an LVM-backed btrfs volume)
- `scripts/create_main_snapshot.sh`: take a snapshot from the main replica
- `scripts/create-clone-from-snapshot-postgres.sh`: CLI helper for launching a clone container
- `scripts/maintenance/cleanup_all.sh`: prune stale clones and containers
- `replication/replica-init/*.sh`: container init steps run inside the replica image (schema clone, extensions, FDW setup, subscription create)

---

## Troubleshooting

- Replica initialization log: `replica-init.log`
- Clone container failures: `docker logs <container>`
- Auto-sync history / errors: `cat ~/.snaplicator/sync_events.jsonl` or `GET /replication/sync-log`
- Auto-add event trigger missing on publisher: hit `POST /replication/trigger-install` (also reinstalled automatically by the 30s loop if it goes missing)
- FDW table looks stale: confirm the table is listed in `configs/fdw.yaml`; the drift detector only reconciles configured targets. Schema-level entries pick up new tables on the next re-import.
- Running out of btrfs space: delete old subvolumes under `MAIN_DATA_DIR` with `sudo btrfs subvolume delete ...`
- macOS reminder: keep actual data on the Linux VM's btrfs mount; Docker Desktop alone cannot host btrfs snapshots.

Keep `configs/.env`, `configs/anonymize.sql`, and `configs/fdw.yaml` in sync with your environment, and feel free to extend the Makefile/scripts to automate your own workflows.
