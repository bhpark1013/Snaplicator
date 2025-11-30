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

### 1. Create `configs/.env`
Copy the sample file and edit it with real values:
```bash
cp configs/.env.test configs/.env
```
`configs/.env.test` documents every required section (replica container, primary DB connection, subscription/publication names, etc.), so walk through it line by line and fill in the blanks for your environment. Optional knobs (e.g., `ALLOW_ORIGINS`, `POSTGRES_IMAGE`, extra slots) can stay commented until needed.

Create the publication on the primary instance once:
```sql
CREATE PUBLICATION snaplicator_pub FOR TABLES IN SCHEMA public;
```

### 2. Install dependencies
```bash
# Backend virtualenv + Python deps
cd /home/bhpark/Snaplicator
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt

# Frontend deps
cd /home/bhpark/Snaplicator/frontend
pnpm install
```

### 3. Prepare Docker and btrfs
1. Create the Docker network once: `docker network create snaplicator-net`
2. Ensure `ROOT_DATA_DIR` resides on btrfs. If not, run `scripts/run-replica-postgres.sh`; it can provision an LVM-backed btrfs volume interactively.

### 4. Start the replica container
All workflows require the replica container to be alive first.
```bash
# Preferred (Makefile)
make replica

# Direct script invocation
./scripts/run-replica-postgres.sh
```
If the script fails, inspect `replica-init.log` and fix issues before moving on.

### 5. Run backend and frontend
```bash
# FastAPI server (defaults to 0.0.0.0:8888)
make server-prepare   # first run only
make server

# Frontend UI (defaults to http://localhost:5173)
make fe    # wraps pnpm dev

# Bring up both concurrently
make dev
```

### 6. Quick API smoke test
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
To clone directly from the main replica, open the frontend and use “Clone from Main”.

### 7. Anonymization behavior
- `configs/anonymize.sql` runs automatically **only** when cloning from the live main replica.
- Snapshot-derived clones skip the script. If you need sanitized data, either run the script manually or sanitize before capturing the snapshot.

### 8. Handy scripts
- `scripts/create_main_snapshot.sh`: take a snapshot from the main replica
- `scripts/create-clone-from-snapshot-postgres.sh`: CLI helper for launching a clone container
- `scripts/maintenance/cleanup_all.sh`: prune stale clones and containers
- `replication/replica-init/*.sh`, `slot_*.sql`: subscription and logical-replication utilities

### Troubleshooting
- Replica initialization log: `replica-init.log`
- Clone container failures: `docker logs <container>`
- Running out of btrfs space: delete old subvolumes under `MAIN_DATA_DIR` with `sudo btrfs subvolume delete ...`
- macOS reminder: keep actual data on the Linux VM’s btrfs mount; Docker Desktop alone cannot host btrfs snapshots.

Keep `.env` and `configs/anonymize.sql` in sync with your environment, and feel free to extend the Makefile/scripts to automate your own workflows.
