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
| Native logical replication | `CREATE PUBLICATION` on the primary | All tables in the publication (DML + selected DDL) | in-stream DDL replication (see below); diff reconcilers (added columns, CHECK constraints, schema moves) remain as a backstop |
| `postgres_fdw` foreign tables | `configs/fdw.yaml` | Tables that can't go through the publication (e.g. no PRIMARY KEY, or read-only-by-FDW by design) | remote column drift (added / removed / type-changed) re-imports automatically |

Reflected changes — and any loop errors — are appended to `~/.snaplicator/sync_events.jsonl` (also exposed at `GET /replication/sync-log` and surfaced in the "Auto-Sync Activity" panel of the UI).

### DDL replication

Native logical replication does **not** replicate DDL ([documented restriction](https://www.postgresql.org/docs/current/logical-replication-restrictions.html)): when the publisher's schema changes, incoming rows stop fitting the subscriber's schema and the apply worker crash-loops until someone fixes it by hand. Snaplicator closes this gap with a capture-and-replay pipeline that rides the replication stream itself:

```
publisher                                  subscriber
─────────                                  ──────────
event triggers (ddl_command_end, sql_drop)
  → INSERT into _snaplicator_ddl_log       _snaplicator_ddl_log (replicated copy)
    (same transaction as the DDL,            → ENABLE ALWAYS trigger fires on the
     log table is IN the publication)          arriving row and EXECUTEs ddl_text
                                               at its exact position in the stream
```

Because the log row commits in the same transaction as the DDL, it arrives **in commit order between the surrounding DML** — the subscriber applies `ALTER TABLE` at exactly the point the publisher did. No LSN bookkeeping, no polling, no ordering heuristics.

Safety properties (all covered by tests in `backend/tests/`):

- **Capture guards** — `_snaplicator_*` objects are never captured (recursion), DCL and publication/subscription DDL are filtered (publisher-only concepts), one log row per `(txid, query)` (dedupe).
- **Watermark** — the subscriber skips log rows with `id <=` the install-time watermark, so initial `COPY`, clone artifacts, and re-subscriptions never re-execute history.
- **Failures are loud, never fatal, never retried** — a DDL that cannot apply (e.g. subscriber-local drift) is recorded in `_snaplicator_ddl_failures` (with `search_path` for manual replay) and the stream keeps flowing; the apply trigger never re-raises, because a re-raise would crash-loop the apply worker. Resolution is a human decision.
- **`CONCURRENTLY` is deferred** — `CREATE INDEX CONCURRENTLY` cannot run inside the apply transaction, so it is queued in `_snaplicator_ddl_deferred` for one-shot out-of-band execution.

Known limitations (shared with every replay-based approach, including the [withdrawn core patch](https://commitfest.postgresql.org/patch/3595/)):

- **Volatile statements diverge** — DDL whose effect depends on volatile functions replays with per-node results: `ADD COLUMN ... DEFAULT now()/gen_random_uuid()/nextval()` backfills existing rows with different values on each node, and `CREATE TABLE AS SELECT` materializes different contents. Accepted for a test-data replica; rows later UPDATEd on the publisher self-heal (logical replication re-sends whole-row images). The correction for a table that matters is a **manual table resync**: remove it from the publication, `TRUNCATE` it on the subscriber, re-add it, then `ALTER SUBSCRIPTION ... REFRESH PUBLICATION` — tablesync re-copies the publisher's data wholesale. Automated detection (volatile-pattern scan of the DDL log → checksum confirmation → resync recommendation in the sync log) is tracked in [#17](https://github.com/bhpark1013/Snaplicator/issues/17).
- DDL hidden inside function calls replays the outer statement (`SELECT migrate()` requires the function to exist and behave on the subscriber); plain/Alembic-style migrations and `DO` blocks replay correctly.
- `command_tag` for `CREATE EXTENSION` records the first inner command of the extension script (`ddl_text` is intact and replays correctly).

Context: PostgreSQL core has tried to ship this (commitfest [#3595](https://commitfest.postgresql.org/patch/3595/), 2022–2024, withdrawn; a narrower "take2" is in progress), and [pgl_ddl_deploy](https://github.com/enova/pgl_ddl_deploy) implements the same event-trigger + queue pattern as an extension. Managed services (Aurora/RDS) don't allow that extension, which is why Snaplicator implements the pattern in plain SQL managed by the backend.

**Status**: capture + apply are implemented and tested (`install_capture_triggers`, `install_ddl_apply`, `add_log_table_to_publication` in `backend/app/services/replication.py`); wiring into the backend startup/loop (auto-install, deferred executor, failure surfacing) lands separately. To try it interactively, spin up the smoke environment:

```bash
cd backend
.venv/bin/python scripts/ddl_smoke.py setup      # publisher :55440 / subscriber :55441
.venv/bin/python scripts/ddl_smoke.py status
.venv/bin/python scripts/ddl_smoke.py teardown
```

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
