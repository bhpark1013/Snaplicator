# Snaplicator API (FastAPI)

## Prerequisites
- Python 3.10+
- Linux host (btrfs tools available and permitted via sudo)

## Install & Run (dev)
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Ensure repo root has .env with:
# ROOT_DATA_DIR=/mnt/snaplicator
# MAIN_DATA_DIR=replica

./run.sh
# Open http://localhost:8000/docs
```

## Endpoints
- GET `/health`
- GET `/snapshots` → List btrfs snapshots under `ROOT_DATA_DIR` starting with `MAIN_DATA_DIR-snapshot-`
- POST `/snapshots` → Create readonly snapshot like scripts/create_main_snapshot.sh
- POST `/snapshots/{snapshot_name}/clone` → Create writable clone and start a Postgres container

## Quick test (curl)
```bash
# Health
curl -s http://localhost:8000/health | jq .

# List snapshots
curl -s http://localhost:8000/snapshots | jq .

# Create snapshot
curl -s -X POST http://localhost:8000/snapshots | jq .

# Create clone from a snapshot (example name)
# Required .env: CONTAINER_NAME, NETWORK_NAME, HOST_PORT, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB
curl -s -X POST http://localhost:8000/snapshots/replica-snapshot-20250921-041339/clone | jq .
```

## Notes
- The API uses `sudo btrfs ...` and Docker commands. Configure sudoers or run with proper privileges.
- CORS is enabled for all origins by default. Adjust in `app/core/config.py`. 