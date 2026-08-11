# Snaplicator

**A test database cloned straight from your production. No seeding. No dumps.**

[**Landing page →**](https://bhpark1013.github.io/Snaplicator/) — the same story with the concepts drawn, in English, Korean and Japanese.

## The problem

Setting up a test database means seeding the data again. A bug in production
will not reproduce there. QA means rebuilding the same scenario every time.
The rows that would settle all three only exist in production — and production
is not somewhere you test.

## What it does

**1. Data and schema replicated in real time.** Logical replication carries
rows and nothing else, which is why a copy of a managed primary normally has to
be taken again on a schedule. Snaplicator catches every `CREATE`, `ALTER` and
`DROP` on the primary with event triggers and replays them on the replica, so
it never drifts out of shape.

**2. Database branching.** Freeze the replica into a snapshot, then start as
many writable clones from it as you like — seconds each, effectively no disk
until something writes, and resetting one back is a single call.

## Who it is for

**Use it if**

- Production is on RDS, Azure Database for PostgreSQL, Cloud SQL or a machine
  you own, and you are not moving it.
- You need real production data to test against, and it cannot leave your own
  infrastructure.
- Your agent needs to test against production data without consequences.
- Applying every schema change to the test database has become a chore.

**Don't use it if**

- You cannot enable `wal_level=logical` or create a replication slot on the
  primary. Nothing else here works without it.
- You already use Neon as your primary database — use their branching.

## How it differs

|  | Rows follow production | Schema follows | Snapshots / branching | Your own infrastructure |
|---|---|---|---|---|
| **Neon** | ○ logical replication | ✕ yours to keep in step | ○ copy-on-write branches | ✕ open source, but scoped to experiments |
| **Supabase** | ✕ branches start with no data | ○ from your migration files | △ their cloud only | ○ documented — but without branching |
| **Aurora cloning** | ✕ a clone is fixed when taken | ○ same storage as the source | △ 15, then it is a full copy | ✕ AWS Aurora only |
| **DBLab** | △ managed primaries are re-copied on a schedule | △ only at the next full refresh | ○ ZFS or LVM thin clones | ○ |
| **Snaplicator** | ○ logical replication, managed included | ○ event triggers replay DDL here | ○ btrfs copy-on-write | ○ one machine you own |

The schema column is the one that matters. Postgres is explicit — *"The
database schema and DDL commands are not replicated"* — so following a managed
primary otherwise means re-copying it on a schedule, and the copy is only ever
as fresh as the last dump.

<sub>Checked against
[Neon branching](https://neon.com/docs/introduction/branching) ·
[Neon: replicate from RDS](https://neon.com/docs/guides/logical-replication-rds-to-neon) ·
[Supabase branching](https://supabase.com/docs/guides/deployment/branching) ·
[Supabase self-hosting](https://supabase.com/docs/guides/self-hosting) ·
[Aurora cloning](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Managing.Clone.html) ·
[DBLab data sources](https://postgres.ai/docs/dblab-howtos/administration/data) ·
[Postgres: logical replication restrictions](https://www.postgresql.org/docs/current/logical-replication-restrictions.html)</sub>

## Install

```sh
# Linux
curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh | sudo bash

# macOS — no sudo; btrfs is a Linux filesystem, so it builds a Linux machine first
curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh | bash
```

Append `-s -- --demo` to either line to start on a sample database instead of
your own. Details, running from source and troubleshooting are in
[`DEVELOPMENT.md`](DEVELOPMENT.md).

## License

MIT — see [`LICENSE`](LICENSE).
