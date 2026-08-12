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
  <a href="https://bhpark1013.github.io/Snaplicator/"><b>Website</b></a> ·
  <a href="https://bhpark1013.github.io/Snaplicator/#guide">Guide</a> ·
  <a href="https://bhpark1013.github.io/Snaplicator/#agent">Agents</a> ·
  <a href="DEVELOPMENT.md">Development</a>
</p>

## Install

```sh
# Linux
curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh | sudo bash

# macOS — no sudo; it builds a Linux machine first
curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh | bash
```

It asks for a connection URI to your primary. No database to hand? Append
`-s -- --demo` to start on a sample one instead.

## Two core features

**1. Data and schema replicated in real time.** Not only DML: schema changes on
the primary are reflected in real time as well.

**2. Database branching.** Save a particular state of the database and keep
restoring to that state as you test and run QA.

## 만든 이유

테스트 DB 구성을 위해 매번 데이터를 시딩해야하고, production에 버그 발생하면 test환경에서는 재현이 안되고, QA시 매번 특정 시나리오에서 테스트해야하는 상황이 번거로워서 만들었습니다.

실시간으로 primary db의 dml 및 ddl을 복제하고, 복제된 DB에 특정 상태에 스냅샷을 찍어 매번 같은 데이터로 테스트할 수 있습니다.

## Who it is for

**Use it if**

- Production is on RDS, Azure Database for PostgreSQL, Cloud SQL or a machine
  you own, and you are not moving it.
- You need real production data to test against, and it cannot leave your own
  infrastructure.
- When your agent needs to test against production data without consequences.
- When applying every schema change to the test database has become a chore.

**Don't use it if**

- You cannot enable `wal_level=logical` or create a replication slot on the
  primary.
- You already use Neon as your primary database.

## How it differs

|  | Rows follow production | Schema follows | Snapshots / branching | Your own infrastructure | Your production stays put |
|---|---|---|---|---|---|
| **Neon** | ○ logical replication | ✕ yours to keep in step | ○ copy-on-write branches | ✕ open source, but scoped to experiments | △ branch a copy hosted by Neon |
| **Supabase** | ✕ branches start with no data | ○ from your migration files | △ their cloud only | ○ documented — but without branching | ✕ Supabase-hosted projects only |
| **Aurora cloning** | ✕ a clone is fixed when taken | ○ same storage as the source | △ 15, then it is a full copy | ✕ AWS Aurora only | ○ clones the cluster you already run |
| **DBLab** | △ managed primaries are re-copied on a schedule | △ only at the next full refresh | ○ ZFS or LVM thin clones | ○ your hardware | ○ sits beside it |
| **Xata** | ○ logical replication (pgstream) | ○ event triggers replay DDL | ○ copy-on-write branches | △ self-hosted, on Kubernetes | ✕ the copy lives on the Xata platform |
| **Snaplicator** | ○ logical replication, managed included | ○ event triggers replay DDL here | ○ btrfs copy-on-write | ○ one machine you own | ○ sits beside it |

**Neon and Supabase branching** — both are excellent, and if you already run on
them, use their branching: a branch there *is* your data.

**DBLab (Postgres.ai)** — the closest thing to this, older and more capable. The
difference is how the copy keeps up. Its physical mode *"is not available for
managed cloud Postgres services such as Amazon RDS"*; against those it is
dump/restore on a schedule. Snaplicator subscribes over logical replication, so
a managed primary is followed rather than re-copied.

<sub>Checked against
[Neon branching](https://neon.com/docs/introduction/branching) ·
[Neon: replicate from RDS](https://neon.com/docs/guides/logical-replication-rds-to-neon) ·
[Supabase branching](https://supabase.com/docs/guides/deployment/branching) ·
[Aurora cloning](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Aurora.Managing.Clone.html) ·
[DBLab data sources](https://postgres.ai/docs/dblab-howtos/administration/data) ·
[Xata](https://github.com/xataio/xata) ·
[Postgres: logical replication restrictions](https://www.postgresql.org/docs/current/logical-replication-restrictions.html)</sub>

## What it installs on your primary

It asks for a superuser, so here is exactly what that is for. Schema changes are
captured with **event triggers**, and `CREATE EVENT TRIGGER` is superuser-only —
PostgreSQL provides no `GRANT` for it. Putting a newly created table into the
publication additionally needs ownership of that table.

Everything it creates is named `_snaplicator*`, so you can find every piece:

```
_snaplicator_ddl_log          table        one row per CREATE / ALTER / DROP
_snaplicator_capture_ddl      trigger      writes that row
_snaplicator_capture_drop     trigger      the same, for drops
_snaplicator_auto_add_<pub>   trigger      a new table joins the publication
<publication>                 publication  created, or one you already have
<slot>                        slot         opened by the replica, as any has
```

Your rows are read and never written. Removing all of it:

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

## License

MIT — see [`LICENSE`](LICENSE).
