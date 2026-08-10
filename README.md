# Snaplicator

**A test database cloned straight from your production. No seeding. No dumps.**

Snaplicator keeps one Postgres replica following your primary over logical
replication — schema changes included — and hands out writable, disposable
clones of it on btrfs copy-on-write. Production stays exactly where it is: RDS,
Azure Database for PostgreSQL, Cloud SQL, or a machine you own.

## Install

**Linux**

```sh
curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh | sudo bash
```

**macOS** — no `sudo`; btrfs is a Linux filesystem, so it builds a Linux machine first

```sh
curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh | bash
```

No database to hand? Append `-s -- --demo` to either line and it seeds a sample
publisher so you can watch the whole thing work.

It asks which database to point at, provisions the btrfs pool, starts the
management plane and opens the UI — then stops. **What to replicate is chosen
there**, and the first copy runs when you say so.

## Two core features

**1. Data and schema replicated in real time.** Logical replication carries
rows and nothing else, which is why a copy of a managed primary normally has to
be taken again on a schedule. Snaplicator catches every `CREATE`, `ALTER` and
`DROP` on the primary with event triggers and replays them on the replica, so
it never drifts out of shape.

**2. Database branching.** Freeze the replica into a snapshot, then start as
many writable clones from it as you like — seconds each, effectively no disk
until something writes, and resetting one back is a single call.

## Use it if

- Production is on RDS, Azure Database for PostgreSQL, Cloud SQL or a machine
  you own, and you are not moving it.
- You need real production data to test against, and it cannot leave your own
  infrastructure.
- Your agent needs to test against production data without consequences.
- Applying every schema change to the test database has become a chore.

## Don't use it if

- You cannot enable `wal_level=logical` or create a replication slot on the
  primary. Nothing else here works without it.
- You already use Neon as your primary database — use their branching.

## For an agent

Snaplicator ships an MCP server, so the loop is the agent's own. The `clones`
query parameter scopes which clones it may touch.

```json
{ "mcpServers": { "snaplicator": { "url": "http://snaplicator-host:8765/mcp?clones=5455" } } }
```

```
create_clone(description: "try #1")            → port 5455
… run the migration, run the suite, it fails …
reset_clone_to_snapshot(clone_id: "5455", snapshot_name: "before-migration-42")
delete_clone(clone_id: "5455")
```

## How it compares

|  | Rows follow production | Schema follows | Snapshots / branching | Your own infrastructure |
|---|---|---|---|---|
| **Neon** | ○ logical replication | ✕ yours to keep in step | ○ copy-on-write branches | ✕ open source, but scoped to experiments |
| **Supabase** | ✕ branches start with no data | ○ from your migration files | △ their cloud only | ○ documented — but without branching |
| **Aurora cloning** | ✕ a clone is fixed when taken | ○ same storage as the source | △ 15, then it is a full copy | ✕ AWS Aurora only |
| **DBLab** | △ managed primaries are re-copied on a schedule | △ only at the next full refresh | ○ ZFS or LVM thin clones | ○ |
| **Snaplicator** | ○ logical replication, managed included | ○ event triggers replay DDL here | ○ btrfs copy-on-write | ○ one machine you own |

Checked against [Neon branching](https://neon.com/docs/introduction/branching) ·
[Neon: replicate from RDS](https://neon.com/docs/guides/logical-replication-rds-to-neon) ·
[Supabase branching](https://supabase.com/docs/guides/deployment/branching) ·
[Supabase self-hosting](https://supabase.com/docs/guides/self-hosting) ·
[Aurora cloning](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Managing.Clone.html) ·
[DBLab data sources](https://postgres.ai/docs/dblab-howtos/administration/data) ·
[Postgres: logical replication restrictions](https://www.postgresql.org/docs/current/logical-replication-restrictions.html)

## The schema problem, and how this closes it

Postgres states the limit plainly:

> The database schema and DDL commands are not replicated.

A subscription carries row changes and nothing else. Add a column on the
primary and the replica does not have it; the first change that mentions that
column fails to apply, and replication stops there. That is why a copy of a
managed primary is normally taken again on a schedule — and why the copy is
only ever as fresh as the last dump, schema included.

Snaplicator puts event triggers on the primary that write every DDL statement
to an outbox table. That table is itself a member of the publication, so the
DDL arrives as ordinary replicated rows and is replayed on the replica.

Three things are worth knowing about it:

- `CREATE INDEX CONCURRENTLY` cannot run inside the apply worker's transaction,
  so it is queued to `_snaplicator_ddl_deferred` and run outside it.
- A statement that fails here is written to `_snaplicator_ddl_failures` and left
  there — not retried, not patched over.
- A watermark stops anything older than the trigger install from being replayed.

## Anonymization

`configs/anonymize.sql` runs inside every clone built from the live replica,
before the port is handed out. If it fails, the clone is destroyed rather than
served. Copy the example to start:

```sh
cp configs/anonymize-example.sql configs/anonymize.sql
```

> A clone taken from an existing snapshot does **not** run it — only clones
> spawned from the live replica do.

## More

- [`DEVELOPMENT.md`](DEVELOPMENT.md) — running from source, how replication
  works in detail, the API, troubleshooting
- [`deploy/README.md`](deploy/README.md) — running two installs against one primary

## License

MIT — see [`LICENSE`](LICENSE).
