<p align="center">
  <a href="https://bhpark1013.github.io/Snaplicator/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/docs/logo-dark.svg">
      <img src="https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/docs/logo-light.svg" alt="Snaplicator" width="88" height="88">
    </picture>
  </a>
</p>

<h1 align="center">Snaplicator</h1>

<p align="center">
  A test database cloned straight from your production. No seeding. No dumps.
</p>

<p align="center">
  <a href="https://bhpark1013.github.io/Snaplicator/"><b>Read the landing page &rarr;</b></a>
</p>

## 만든 이유

테스트 DB 구성을 위해 매번 데이터를 시딩해야하고, production에 버그 발생하면 test환경에서는 재현이 안되고, QA시 매번 특정 시나리오에서 테스트해야하는 상황이 번거로워서 만들었습니다.

실시간으로 primary db의 dml 및 ddl을 복제하고, 복제된 DB에 특정 상태에 스냅샷을 찍어 매번 같은 데이터로 테스트할 수 있습니다.

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

## What it asks of your primary, and why

It asks for a superuser, which is a lot to ask, so here is exactly what that
buys and exactly what lands in your production database.

**Why superuser.** Schema changes are captured with **event triggers**, and
`CREATE EVENT TRIGGER` is superuser-only — PostgreSQL provides no `GRANT` for
it, so there is no narrower role that can install one. Adding a newly created
table to the publication additionally requires owning that table. A
replication-only role gets through neither step. This is a property of
PostgreSQL, not a shortcut taken here.

**What it creates**, all named `_snaplicator*` so you can find every piece:

| Object | Kind | What it is for |
|---|---|---|
| `_snaplicator_ddl_log` | table (+ sequence, PK) | One row per `CREATE` / `ALTER` / `DROP`. This is the outbox. |
| `_snaplicator_capture_ddl` | function + event trigger (`ddl_command_end`) | Writes that row. |
| `_snaplicator_capture_drop` | function + event trigger (`sql_drop`) | Same, for drops, which the other event cannot see. |
| `_snaplicator_auto_add_<publication>` | function + event trigger | Adds a newly created table to the publication so it starts replicating. |
| your publication | publication | Created, or an existing one you pick. |
| a replication slot | slot | Created by the subscriber, as any logical replica does. |

**What it writes.** One row per schema change, into its own log table. Your
tables are read and never written — the replica is downstream of them and has
no path back.

**What you have to set yourself.** `wal_level=logical` and a role that may
replicate (`rds_replication` on RDS/Aurora). The installer checks both before
it changes anything and tells you which is missing.

**Removing it** leaves nothing behind:

```sql
DROP EVENT TRIGGER IF EXISTS _snaplicator_capture_ddl;
DROP EVENT TRIGGER IF EXISTS _snaplicator_capture_drop;
DROP EVENT TRIGGER IF EXISTS _snaplicator_auto_add_<publication>;
DROP FUNCTION IF EXISTS public._snaplicator_capture_ddl(),
                        public._snaplicator_capture_drop(),
                        public._snaplicator_auto_add_<publication>();
DROP TABLE IF EXISTS public._snaplicator_ddl_log;   -- sequence and index go with it
DROP PUBLICATION IF EXISTS <publication>;
SELECT pg_drop_replication_slot('<slot>');          -- do not skip this one
```

The slot is the one that matters. A slot nobody reads holds WAL on the primary
until the disk is gone, so drop it if you stop using Snaplicator.

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

|  | Rows follow production | Schema follows | Snapshots / branching | Your own infrastructure | Your production stays put |
|---|---|---|---|---|---|
| **Neon** | ○ logical replication | ✕ yours to keep in step | ○ copy-on-write branches | ✕ open source, but scoped to experiments | △ branch a copy hosted by Neon |
| **Supabase** | ✕ branches start with no data | ○ from your migration files | △ their cloud only | ○ documented — but without branching | ✕ Supabase-hosted projects only |
| **Aurora cloning** | ✕ a clone is fixed when taken | ○ same storage as the source | △ 15, then it is a full copy | ✕ AWS Aurora only | ○ clones the cluster you already run |
| **DBLab** | △ managed primaries are re-copied on a schedule | △ only at the next full refresh | ○ ZFS or LVM thin clones | ○ | ○ sits beside it |
| **Xata** | ○ logical replication (pgstream) | ○ event triggers replay DDL | ○ copy-on-write branches | △ self-hosted, on Kubernetes | ✕ the copy lives on the Xata platform |
| **Snaplicator** | ○ logical replication, managed included | ○ event triggers replay DDL here | ○ btrfs copy-on-write | ○ one machine you own | ○ sits beside it |

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
[Xata](https://github.com/xataio/xata) ·
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
