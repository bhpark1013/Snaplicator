# Development

Running Snaplicator from source, how the pieces fit, and what to check when
something is off. The short version — install and use — is in
[`README.md`](README.md).

## Running the installer again

#### Running it again

Nothing on the second run destroys anything.

```
[snaplicator] Snaplicator is already installed here.
[snaplicator]   path:    /opt/snaplicator
[snaplicator]   primary: db.example.com:5432/appdb
[snaplicator]   replica: running

  1. open it (default)
  2. add a new install beside it — this one is left alone
```

Answer **1** and it brings up what is already there, taking the target from
that install's own `.env`. Answer **2** and it builds a second, complete
install with its own path, ports, pool, containers and publication — chosen
automatically from what is free:

```
[snaplicator] adding a new install:
[snaplicator]   path:        /opt/snaplicator2   (pool /snaplicator2)
[snaplicator]   ports:       UI 8081, api 8889, replica 5434
[snaplicator]   publication: snaplicator_publication2
```

A machine with nothing installed is not asked at all — there is no choice to
make. Neither is a run that already names a target on the command line: asking
somewhere new to point is what someone does when they want another install.

Discarding a copy is deliberate and off the menu: `REPOINT=1` drops the
subscription (releasing the publisher's replication slot), removes the replica
and its clones, and empties the pool before rebuilding.

Two installs can share one primary — they take separate publications,
subscriptions, slots and DDL-capture triggers, and neither can turn the
other's off. See [`deploy/README.md`](deploy/README.md) for the settings each
one owns.

### Project map
- `backend/`: FastAPI services plus Docker/btrfs orchestration
- `frontend/`: management UI (Vite + React, powered by `pnpm`)
- `cli/`: `snaplicator` CLI — psql-style remote client for the REST API
- `mcp-server/`: MCP server that wraps the REST API for agentic clients
- `replication/replica-init/`: container init scripts (schema clone, extensions, FDW, subscription)
- `scripts/`: helper utilities for running the replica container and managing snapshots/clones
- `configs/`: `.env`, `anonymize.sql`, `fdw.yaml`, and misc SQL helpers

### Prerequisites

For the one-line install, only the last item is yours to arrange:

- **Linux**: Docker. The installer adds `btrfs-progs`, `postgresql-client` and
  the compose plugin if they are missing.
- **macOS**: a Linux machine. The installer offers to create one with OrbStack
  and will `brew install --cask orbstack` if you say yes.

  > **OrbStack is free for personal, non-commercial use only.** Commercial use
  > needs a paid licence — see [orbstack.dev/pricing](https://orbstack.dev/pricing).
  > The installer says so before it offers, and defaults to *no*. Any Linux VM
  > of your own works instead: run the Linux one-liner inside Colima, Lima, UTM,
  > or a Linux box, and nothing else changes.
- A primary Postgres with logical replication available: `wal_level=logical`,
  a role that may replicate, and either `CREATE PUBLICATION` privilege or a
  publication someone made for you (the installer checks all of this before it
  changes anything, and says which part is missing).
  - On RDS/Aurora that means `rds.logical_replication=1` and the
    `rds_replication` role.

Building from source additionally needs Python 3.10+ with `python3 -m venv`,
`pnpm`, and `make`.

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
     log table rides the data publication)     arriving row and EXECUTEs ddl_text
                                               at its exact position in the stream
```

Because the log row commits in the same transaction as the DDL, it arrives **in commit order between the surrounding DML** — the subscriber applies `ALTER TABLE` at exactly the point the publisher did. No LSN bookkeeping, no polling, no ordering heuristics.

The log table travels in the publication the subscription already reads, so
there is one publication and the subscription is never told a second name.
That puts it inside the object the selection screen rewrites — narrowing a
publication is a `DROP` + `CREATE`, because PostgreSQL has no `ALTER` from
`FOR ALL TABLES` to anything narrower — so the log table is named on every
path that recreates it, and hidden from every screen that lists tables. Losing
it would be silent: rows keep arriving and only DDL stops.

Safety properties (all covered by tests in `backend/tests/`):

- **Capture guards** — `_snaplicator_*` objects are never captured (recursion), DCL and publication/subscription DDL are filtered (publisher-only concepts), one log row per `(txid, query)` (dedupe).
- **Watermark** — the subscriber skips log rows with `id <=` the install-time watermark, so clone artifacts and re-subscriptions never re-execute history. The mark is taken *after* capture is installed and *before* the schema clone, which is what makes it mean "the clone starts here".
- **Catch-up for copied rows** — a subscription's first pass over a table is `COPY`, and row triggers do not see `COPY`. The log table is a table like any other, so whatever it held when its copy ran arrives present, above the watermark, and unexecuted. Once every table has finished syncing, the loop runs those rows in id order under the same rules as the trigger. Nothing runs twice: both paths claim the row in `_snaplicator_ddl_applied` first.
- **One auto-add trigger per publication** — capture is two shared triggers that name no publication (so every install writes them identically) plus `_snaplicator_auto_add_<publication>`, which does the `ALTER PUBLICATION ... ADD TABLE` for new tables and is scoped to what that publication covers. That split is what lets two installs share a primary: a fixed name meant the last one to start owned it, and the other's new tables quietly stopped being added.
- **Failures are loud, never fatal, never retried** — a DDL that cannot apply (e.g. subscriber-local drift) is recorded in `_snaplicator_ddl_failures` (with `search_path` for manual replay) and the stream keeps flowing; the apply trigger never re-raises, because a re-raise would crash-loop the apply worker. Resolution is a human decision.
- **`CONCURRENTLY` is deferred** — `CREATE INDEX CONCURRENTLY` cannot run inside the apply transaction, so it is queued in `_snaplicator_ddl_deferred` for one-shot out-of-band execution.

Known limitations (shared with every replay-based approach, including the [withdrawn core patch](https://commitfest.postgresql.org/patch/3595/)):

- **Volatile statements diverge** — DDL whose effect depends on volatile functions replays with per-node results: `ADD COLUMN ... DEFAULT now()/gen_random_uuid()/nextval()` backfills existing rows with different values on each node, and `CREATE TABLE AS SELECT` materializes different contents. Accepted for a test-data replica; rows later UPDATEd on the publisher self-heal (logical replication re-sends whole-row images). The correction for a table that matters is a **manual table resync**: remove it from the publication, `TRUNCATE` it on the subscriber, re-add it, then `ALTER SUBSCRIPTION ... REFRESH PUBLICATION` — tablesync re-copies the publisher's data wholesale. Automated detection (volatile-pattern scan of the DDL log → checksum confirmation → resync recommendation in the sync log) is tracked in [#17](https://github.com/bhpark1013/Snaplicator/issues/17).
- DDL hidden inside function calls replays the outer statement (`SELECT migrate()` requires the function to exist and behave on the subscriber); plain/Alembic-style migrations and `DO` blocks replay correctly.
- `command_tag` for `CREATE EXTENSION` records the first inner command of the extension script (`ddl_text` is intact and replays correctly).

Context: PostgreSQL core has tried to ship this (commitfest [#3595](https://commitfest.postgresql.org/patch/3595/), 2022–2024, withdrawn; a narrower "take2" is in progress), and [pgl_ddl_deploy](https://github.com/enova/pgl_ddl_deploy) implements the same event-trigger + queue pattern as an extension. Managed services (Aurora/RDS) don't allow that extension, which is why Snaplicator implements the pattern in plain SQL managed by the backend.

**Status**: fully wired. Capture triggers install before the schema clone and self-heal in the 30s loop. The subscriber-side switch is `DDL_APPLY_ENABLED` in `configs/.env` — connecting the stream is idempotent (apply infra + watermark seed + log table in the publication + `REFRESH` only if it just joined). The code default is off, so importing the backend alone changes no replication behaviour; **`install.sh` writes `DDL_APPLY_ENABLED=1`**, so an installed deployment has it on. Deferred `CONCURRENTLY` statements are executed once, out of band, by the loop; new apply failures surface in the sync log (and Slack, if configured).

Two smoke environments, both real containers and real logical replication:

```bash
cd backend
python3 scripts/ddl_smoke.py setup            # one pair: publisher + subscriber
python3 scripts/ddl_smoke.py status
python3 scripts/ddl_smoke.py teardown

python3 scripts/two_instances_smoke.py        # two installs against one primary
```

`two_instances_smoke.py` is the one that pins multi-install behaviour: phase 1
puts both on one publication (a new table auto-joins, both replicas
materialise it through in-stream DDL, both get its rows, `ALTER TABLE` applies
on both); phase 2 gives them different publications and checks each takes only
its own schema — including that the first install still auto-adds after the
second was installed, which is exactly what used to break.

---

## Clones

A clone is a btrfs snapshot of the replica's data directory with a postgres
container on top of it. The snapshot is O(1) and costs only what later
diverges, so a clone of a 300 GB replica is instant and nearly free.

Building one is not instant, though — the container has to start, subscriptions
have to be dropped, sequences synced, `configs/anonymize.sql` run — so the UI
names the stage it is in rather than saying "creating…" for a minute:

```
checkpoint → snapshot → permissions → container → ready
           → subscriptions → sequences → anonymize → user
```

**Refresh replaces a clone without taking it away.** The new copy is built
beside the old one, on its own port, and only once it is serving does the swap
happen — the old container is stopped with the image's `STOPSIGNAL` (postgres
reads `SIGINT` as *fast shutdown*: disconnect, roll back, checkpoint) rather
than killed, so clients get `FATAL: terminating connection due to administrator
command` instead of a socket that goes quiet. Measured on a clone with a 20s
anonymize step: **23s of work, 0.4s unreachable**, where it used to be 23s of
downtime.

A `docker rm -f` in that position is a `SIGKILL` plus the container's network
namespace disappearing, which blackholes every open client socket — the client
learns nothing until TCP keepalive fires, two hours later by Linux default.
That is the one failure mode worth engineering around, and the reason a plain
container replacement is not enough.

**Space is checked when you start, not when you install.** The installer runs
before anything is selected, so its only honest scope is the whole database —
refusing there turned a forecast about the largest possible choice into a gate
on every smaller one. The check now happens where the selection exists, and
splits in two: whether the data can land at all (payload × 1.1, the only thing
worth refusing over) and whether room is left for the snapshots and clones that
follow (× 1.5, which is said and not enforced — a btrfs subvolume shares its
filesystem's free space and reserves nothing).

---

## Quick Start (from source)

The steps the installer above performs for you, for when you are developing on
Snaplicator itself or want to place each piece by hand.

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

You do not have to: the UI offers the publications the primary already has, or
creates one. Choosing an existing one is a promise never to rewrite it, and
that promise is kept — narrowing is refused for a publication this install did
not create.

The backend installs DDL capture event triggers on the publisher (see
`GET /replication/trigger-status`, which asks about *this* publication). One of
them auto-adds new tables — every schema the publication covers follows its
future tables by default, and the exceptions are what get stored, because a
list written today is silent about the schema someone creates tomorrow.

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
- DDL capture triggers missing on publisher: hit `POST /replication/trigger-install` (also reinstalled automatically by the 30s loop if they go missing)
- FDW table looks stale: confirm the table is listed in `configs/fdw.yaml`; the drift detector only reconciles configured targets. Schema-level entries pick up new tables on the next re-import.
- Running out of btrfs space: delete old subvolumes under `MAIN_DATA_DIR` with `sudo btrfs subvolume delete ...`
- macOS reminder: keep actual data on the Linux VM's btrfs mount; Docker Desktop alone cannot host btrfs snapshots.

Keep `configs/.env`, `configs/anonymize.sql`, and `configs/fdw.yaml` in sync with your environment, and feel free to extend the Makefile/scripts to automate your own workflows.
