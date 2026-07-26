# Snaplicator — docker deployment (issue #19, stage 3)

The management plane (FastAPI backend + web UI) runs in containers; the
replica postgres and its clones stay **sibling containers** that the
manager creates through the host docker socket — that is the product, so
compose deliberately does not own them.

## Quickstart

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
