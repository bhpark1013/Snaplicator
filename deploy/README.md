# Snaplicator — docker deployment

The management plane (FastAPI backend + web UI) runs in containers; the
replica postgres and its clones stay **sibling containers** that the
manager creates through the host docker socket — that is the product, so
compose deliberately does not own them.

## One line

```sh
# Linux
curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh | sudo bash

# macOS — no sudo; creates an OrbStack Linux machine and continues inside it
curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh | bash
```

`install.sh` does everything below — pool, `.env`, compose, replica bootstrap
— and asks which database to point at. Answer with a connection URI, or with
`demo` to have it seed a sample publisher first.

Both answers can be given up front instead, which skips the prompt and is
what unattended runs want:

```sh
... | sudo bash -s -- "postgres://user:pw@primary:5432/mydb"
... | sudo bash -s -- --demo
```

It stops at the UI. What to replicate is chosen there, and the first copy
starts when you say so — `START_REPLICATION=1` replicates everything without
asking.

## Running it again

The installer looks before it asks. With nothing installed at `SNAP_HOME`
there is no question to put, so it just installs. With something there:

```
[snaplicator] Snaplicator is already installed here.
[snaplicator]   path:    /opt/snaplicator
[snaplicator]   primary: db.example.com:5432/appdb
[snaplicator]   replica: running

  1. open it (default)
  2. add a new install beside it — this one is left alone
```

| answer | what happens |
|---|---|
| `1`, Enter, anything unrecognised | brings up what is there, target read from its own `.env` |
| `2` | builds a second install in the first free slot |
| a URI or `--demo` on the command line | treated as `2` — naming a target is what someone does when they want another install |
| no terminal to ask on | treated as `1`; pass `NEW_INSTALL=1` to say otherwise in advance |

Nothing on that menu deletes anything, so a mis-typed answer cannot cost a
replica. Discarding is deliberate and separate:

```sh
... | sudo bash -s -- REPOINT=1 "postgres://user:pw@other:5432/otherdb"
```

which drops the subscription (releasing the publisher's replication slot),
removes the replica and its clones, empties the pool, and drops the state
volume before rebuilding. A replica cannot be re-aimed without that: the copy
on disk stays what it is, and pointing the configuration elsewhere would leave
the manager talking to a primary the data never came from.

## A second install

`2` picks the first free slot and prints it:

```
[snaplicator] adding a new install:
[snaplicator]   path:        /opt/snaplicator2   (pool /snaplicator2)
[snaplicator]   ports:       UI 8081, api 8889, replica 5434
[snaplicator]   publication: snaplicator_publication2
```

Everything that would otherwise be shared is indexed:

| setting | first | second |
|---|---|---|
| `SNAP_HOME` | `/opt/snaplicator` | `/opt/snaplicator2` |
| `PROJECT` (compose) | `snaplicator` | `snaplicator2` |
| `ROOT_DATA_DIR` | `/snaplicator` | `/snaplicator2` |
| `WEB_PORT` / `BACKEND_PORT` / `HOST_PORT` | 8080 / 8888 / 5433 | 8081 / 8889 / 5434 |
| `CONTAINER_NAME`, `NETWORK_NAME` | `snaplicator_replica`, `snaplicator` | `snaplicator2_replica`, `snaplicator2` |
| `PUBLICATION_NAME` | `snaplicator_publication` | `snaplicator_publication2` |

Values you pass yourself are left alone — an explicit `WEB_PORT=` is an
instruction, not a coincidence to route around.

The publication has to differ: two installs sharing one share a table list,
and either narrowing it narrows the other's replica. Their DDL capture is
separate too — the logging triggers are shared and identical, while the
`ALTER PUBLICATION ... ADD TABLE` trigger is named after the publication, so
neither install can take the other's off.

`PROJECT` is recorded in the `.env` as `COMPOSE_PROJECT`, because nothing else
on disk says which compose stack belongs to which install — and something that
takes one apart must not take down the other's.

## Anything else

Any setting is overridable by appending `VAR=VALUE`, e.g. `WEB_PORT=18080`,
`ROOT_DATA_DIR=/mnt/pool`, `FORMAT_DISK=/dev/sdX`, `SNAPLICATOR_REF=<branch>`,
`MACHINE=<orbstack machine>` on macOS. Re-running is safe: every step detects
existing state and reuses it, and the checkout is refreshed so a re-run is how
you install a newer Snaplicator.

### By hand

```sh
# 0. provision the btrfs pool (picks/creates the pool)
cd cli && sudo python3 -m snaplicator_init "postgres://…" --apply && cd ..

# 1. configure
cd deploy
cp .env.example .env      # fill in publisher + pool settings

# 2. run the management plane
docker compose up -d --build

# 3. bootstrap the replica (one-time; creates container + subscription)
docker compose exec manager bash scripts/run-replica-postgres.sh
```

UI: `http://<host>:8080` (`WEB_PORT`) · API: `:8888` (`BACKEND_PORT`).

## Why the manager container is shaped like this

| choice | reason |
|---|---|
| `network_mode: host` | it scans host listeners (`ss -ltn`) for free ports and reaches replica/clones via host-published ports |
| `privileged` | btrfs subvolume/snapshot ioctls on the bind-mounted pool (it already holds the docker socket, which is root-equivalent on the host) |
| pool mounted at the **same path** | the manager passes pool paths to `docker run -v`; the host daemon resolves them on the host side |
| `./.env → /app/configs/.env` | one file feeds compose interpolation, backend Settings (dotenv) and the replica bootstrap script |

`~/.snaplicator` (sync-event history) lives in the `snaplicator-state`
volume and survives rebuilds. `configs/fdw.yaml` edits made through the
UI live in the container filesystem — bind-mount `../configs` over
`/app/configs` instead if you want them to survive image rebuilds
(then keep `.env` inside that directory).

## Tests

`test-install.sh` drives the installer's decisions without running it: the
functions are lifted out of `install.sh` verbatim by `awk`, so what is
exercised is the code that ships rather than a restatement of it.

```sh
bash deploy/test-install.sh
```

Linux only for the install itself (btrfs + host networking); macOS reaches it
through the OrbStack machine.
