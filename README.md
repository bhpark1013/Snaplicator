## Snaplicator

Snaplicator is a Postgres test-data provisioning toolkit that combines logical replication with btrfs snapshots. A continuously running replica container subscribes to the production publication, so every snapshot or clone reflects near-real-time data without touching the primary. The backend is written in FastAPI, the frontend in Vite + React, and `configs/anonymize.sql` can mask sensitive fields whenever a clone is created.

### Project map
- `backend/`: FastAPI services plus Docker/btrfs orchestration
- `frontend/`: management UI (Vite + React, powered by `pnpm`)
- `scripts/`: helper utilities for running the replica container and managing snapshots/clones
- `configs/`: `.env`, anonymization script, and misc SQL helpers

### Prerequisites
- Linux host (native, WSL2, or a Linux VM) with `btrfs-progs`, Docker, and `make`
  - macOS users: run Docker Desktop plus a lightweight Linux VM (UTM, Multipass, Lima, etc.) so `ROOT_DATA_DIR` can live on a btrfs volume.
- Python 3.10+ with `python3 -m venv`
- `pnpm` for frontend dependencies
- A primary Postgres database that exposes logical replication (`CREATE PUBLICATION` privilege required)

---

## Replication Modes

Snaplicator supports two replication modes:

| Mode | DDL Support | Setup Complexity | Use Case |
|------|-------------|------------------|----------|
| **Native PostgreSQL** | No | Simple | Standard DML-only replication |
| **pgstream** | Yes | Medium | Full DDL + DML replication |

### Native PostgreSQL Replication (Default)
Uses PostgreSQL's built-in logical replication with `CREATE SUBSCRIPTION`. Simple and reliable, but does not replicate DDL changes (ALTER TABLE, CREATE INDEX, etc.).

### pgstream Replication
Uses [pgstream](https://github.com/xataio/pgstream) for CDC with DDL support. Requires:
- `wal2json` plugin on the publisher (natively available in AWS Aurora PostgreSQL)
- All tables must have a PRIMARY KEY

---

## Quick Start

### 1. Create `configs/.env`
Copy the sample file and edit it with real values:
```bash
cp configs/.env.test configs/.env
```
`configs/.env.test` documents every required section (replica container, primary DB connection, subscription/publication names, etc.), so walk through it line by line and fill in the blanks for your environment.

**For pgstream mode**, add these settings:
```bash
# Enable pgstream mode (set to 1 to use pgstream instead of native replication)
USE_PGSTREAM=1

# pgstream settings (optional, defaults shown)
PGSTREAM_SLOT_NAME=pgstream_slot
PGSTREAM_LOG_LEVEL=info
```

### 2. Publisher Setup

#### For Native Replication
Create the publication on the primary instance:
```sql
CREATE PUBLICATION snaplicator_pub FOR TABLES IN SCHEMA public;
```

#### For pgstream
Ensure the publisher has:
1. `wal_level = logical` (required)
2. `wal2json` plugin available (Aurora PostgreSQL has this by default)
3. All tables must have PRIMARY KEY constraints

Check wal2json availability:
```sql
-- Should return 'wal2json' in the list
SHOW shared_preload_libraries;
-- Or try creating a test slot
SELECT pg_create_logical_replication_slot('test_wal2json', 'wal2json');
SELECT pg_drop_replication_slot('test_wal2json');
```

### 3. Install dependencies
```bash
# Backend virtualenv + Python deps
cd /path/to/Snaplicator
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt

# Frontend deps
cd /path/to/Snaplicator/frontend
pnpm install
```

### 4. Prepare Docker and btrfs
1. Create the Docker network once: `docker network create snaplicator-net`
2. Ensure `ROOT_DATA_DIR` resides on btrfs. If not, run `scripts/run-replica-postgres.sh`; it can provision an LVM-backed btrfs volume interactively.

### 5. Start the replica container
```bash
# Native replication (default)
make replica

# pgstream replication
USE_PGSTREAM=1 make replica
# Or set USE_PGSTREAM=1 in configs/.env
```
If the script fails, inspect `replica-init.log` and fix issues before moving on.

### 6. Run backend and frontend
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

## pgstream Operations

### Check pgstream status
```bash
# Inside the replica container
docker exec -it snaplicator_replica bash
ps aux | grep pgstream
tail -f /var/lib/postgresql/pgstream.log
```

### Restart pgstream manually
```bash
# Inside the replica container
pkill -f "pgstream run"
pgstream run --config /var/lib/postgresql/pgstream.env --log-level info
```

### Check replication slot on publisher
```sql
SELECT slot_name, active, restart_lsn, confirmed_flush_lsn 
FROM pg_replication_slots 
WHERE slot_name = 'pgstream_slot';
```

### Drop and recreate pgstream slot
```sql
-- On publisher
SELECT pg_drop_replication_slot('pgstream_slot');
-- Then restart the replica container to reinitialize
```

---

## API smoke test
```bash
# Health
curl -s http://localhost:8888/health | jq .

# List snapshots
curl -s http://localhost:8888/snapshots | jq .

# Create a new snapshot
curl -s -X POST http://localhost:8888/snapshots | jq .

# Launch a clone from a snapshot (replace name)
curl -s -X POST http://localhost:8888/snapshots/<snapshot_name>/clone | jq .
```
To clone directly from the main replica, open the frontend and use "Clone from Main".

---

## Anonymization behavior
- `configs/anonymize.sql` runs automatically **only** when cloning from the live main replica.
- Snapshot-derived clones skip the script. If you need sanitized data, either run the script manually or sanitize before capturing the snapshot.

---

## Handy scripts
- `scripts/create_main_snapshot.sh`: take a snapshot from the main replica
- `scripts/create-clone-from-snapshot-postgres.sh`: CLI helper for launching a clone container
- `scripts/maintenance/cleanup_all.sh`: prune stale clones and containers
- `replication/replica-init/*.sh`: initialization scripts for native and pgstream replication

---

## Troubleshooting

### General
- Replica initialization log: `replica-init.log`
- Clone container failures: `docker logs <container>`
- Running out of btrfs space: delete old subvolumes under `MAIN_DATA_DIR` with `sudo btrfs subvolume delete ...`
- macOS reminder: keep actual data on the Linux VM's btrfs mount; Docker Desktop alone cannot host btrfs snapshots.

### pgstream-specific
- pgstream log: `/var/lib/postgresql/pgstream.log` inside the container
- "No primary key" error: All tables need a PRIMARY KEY for pgstream to work
- Empty queries in logs: Check `PGSTREAM_INJECTOR_STORE_POSTGRES_URL` is set correctly
- Slot doesn't exist: pgstream will auto-create the slot on init

Keep `.env` and `configs/anonymize.sql` in sync with your environment, and feel free to extend the Makefile/scripts to automate your own workflows.
