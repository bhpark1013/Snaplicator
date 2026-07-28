# Snaplicator — docker deployment (issue #19, stage 3)

The management plane (FastAPI backend + web UI) runs in containers; the
replica postgres and its clones stay **sibling containers** that the
manager creates through the host docker socket — that is the product, so
compose deliberately does not own them.

## Quickstart

```sh
curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh | sudo bash
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

Any setting is overridable by appending `VAR=VALUE`, e.g. `WEB_PORT=18080`,
`ROOT_DATA_DIR=/mnt/pool`, `FORMAT_DISK=/dev/sdX`, `SNAPLICATOR_REF=<branch>`.
Re-running is safe: every step detects existing state and reuses it.

### By hand

```sh
# 0. provision the btrfs pool (stage 1–2 tool; picks/creates the pool)
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

Linux only (btrfs + host networking).
