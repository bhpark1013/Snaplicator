## Snaplicator

### Prerequisites
- Linux host with Docker and btrfs (for snapshots) recommended
- configs/.env populated (ROOT_DATA_DIR, MAIN_DATA_DIR, CONTAINER_NAME, NETWORK_NAME, HOST_PORT, POSTGRES_*)
- A publication must already exist on the publisher database (used by the replica's subscription)

Minimal example (run on publisher as a superuser/owner):
```sql
-- create a publication that includes all tables in public schema
CREATE PUBLICATION snaplicator_pub FOR TABLES IN SCHEMA public;
```

### IMPORTANT: Start the replica DB first
All workflows in this project assume the replica Postgres container is running. Always bring it up first.

- Script (core):
```bash
./scripts/run-replica-postgres.sh
```
- Makefile:
```bash
make replica
```

If this step fails, fix it before running the API or frontend.

### Run the backend (FastAPI)
- Virtualenv-less invocation via Makefile:
```bash
make server
```
- Or directly:
```bash
cd backend
./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Run the frontend (Vite + React)
```bash
cd frontend
pnpm install
pnpm dev
```
Or via Makefile:
```bash
make fe
```

### Run both (backend + frontend)
```bash
make dev
```

### Notes
- anonymization script: `configs/anonymize.sql` (executed on every clone startup)
- replication selection rules: `configs/replication-selection.yml` (policy-driven; optional runtime tooling)
- If Docker requires sudo, consider adding your user to the docker group or configuring sudoers for btrfs commands.
