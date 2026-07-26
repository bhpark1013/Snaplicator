#!/usr/bin/env bash
# Snaplicator one-line installer (issue #19 stage 4, #10).
#
#   # against your own primary (URI form):
#   curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh \
#     | sudo bash -s -- "postgres://user:pw@primary:5432/mydb"
#
#   # zero-input demo: spins up a seeded sample publisher too
#   curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh \
#     | sudo bash -s -- --demo
#
# Every setting can be overridden by appending VAR=VALUE arguments, e.g.
#   ... | sudo bash -s -- --demo WEB_PORT=18080 CONTAINER_NAME=snapdemo_replica
#
# What it does, in order:
#   1. checks prerequisites (linux, docker, python3; installs the compose
#      plugin if missing)
#   2. fetches the repository to SNAP_HOME (reused on re-run)
#   3. [--demo] starts a seeded wal_level=logical publisher container
#   4. provisions the btrfs pool with snaplicator-init (measure → plan →
#      apply; idempotent, reuses an existing pool)
#   5. ensures the publication exists on the publisher
#   6. writes deploy/.env and starts the management plane (compose)
#   7. bootstraps the replica container + subscription (skipped if running)
#
# Re-running is safe: every step detects existing state and skips.

set -euo pipefail

info() { printf '\033[1;32m[snaplicator]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[snaplicator] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── argument parsing: [--demo] [CONNSTR] [VAR=VALUE ...] ─────────────
DEMO=0
CONNSTR="${CONNSTR:-}"
for a in "$@"; do
  case "$a" in
    --demo) DEMO=1 ;;
    postgres://*|postgresql://*) CONNSTR="$a" ;;
    [A-Za-z_]*=*) export "${a?}" ;;
    -*) die "unknown flag: $a" ;;
    *) CONNSTR="$a" ;;
  esac
done

# ── defaults (override with VAR=VALUE arguments) ─────────────────────
: "${SNAP_HOME:=/opt/snaplicator}"
: "${SNAPLICATOR_REF:=main}"
: "${PROJECT:=snaplicator}"
: "${WEB_PORT:=8080}"
: "${BACKEND_PORT:=8888}"
: "${HOST_PORT:=5433}"                 # replica postgres host port
: "${CONTAINER_NAME:=snaplicator_replica}"
: "${NETWORK_NAME:=snaplicator}"
: "${MAIN_DATA_DIR:=main}"
: "${POSTGRES_IMAGE:=postgres:17}"
: "${POSTGRES_USER:=postgres}"
: "${POSTGRES_DB:=postgres}"
: "${PUBLICATION_NAME:=snaplicator_publication}"
: "${SUBSCRIPTION_NAME:=snaplicator_subscription}"
: "${DDL_SYNC_INTERVAL:=30}"
: "${DEMO_PUB_NAME:=snaplicator-demo-pub}"
: "${DEMO_PUB_PORT:=15432}"
: "${DEMO_POOL_GIB:=10}"
# ROOT_DATA_DIR: optional; unset → snaplicator-init picks the best spot

if [ -z "${POSTGRES_PASSWORD:-}" ]; then
  POSTGRES_PASSWORD=$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n')
fi

# ── 1. prerequisites ─────────────────────────────────────────────────
[ "$(uname -s)" = "Linux" ] || die "Linux only (btrfs is a Linux kernel filesystem). On macOS run this inside a Linux VM (OrbStack/Lima)."
[ "$(id -u)" = "0" ] || die "run as root:  curl ... | sudo bash -s -- ..."
command -v docker >/dev/null || die "docker is required (https://docs.docker.com/engine/install/)"
command -v python3 >/dev/null || die "python3 (>= 3.10) is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || die "python3 >= 3.10 required (found $(python3 --version))"

if [ "$DEMO" = "0" ]; then
  [ -n "$CONNSTR" ] || die "pass your primary's connection URI (postgres://user:pw@host:port/db) or --demo"
  case "$CONNSTR" in postgres://*|postgresql://*) ;; *) die "URI form required: postgres://user:pw@host:port/db" ;; esac
  command -v psql >/dev/null || die "psql is required to measure/verify the publisher (apt install postgresql-client)"
fi

if ! docker compose version >/dev/null 2>&1; then
  info "installing docker compose plugin..."
  mkdir -p /usr/local/lib/docker/cli-plugins
  curl -fsSL "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-$(uname -m)" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  docker compose version >/dev/null || die "docker compose plugin installation failed"
fi

for p in "$WEB_PORT" "$BACKEND_PORT"; do
  if ss -ltn 2>/dev/null | grep -q ":$p "; then
    die "port $p is already in use — override with WEB_PORT=/BACKEND_PORT= arguments"
  fi
done

# ── 2. fetch the repository ──────────────────────────────────────────
if [ -d "$SNAP_HOME/deploy" ]; then
  info "reusing existing checkout at $SNAP_HOME"
else
  info "fetching Snaplicator ($SNAPLICATOR_REF) to $SNAP_HOME..."
  mkdir -p "$SNAP_HOME"
  if command -v git >/dev/null; then
    git clone -q --depth 1 --branch "$SNAPLICATOR_REF" \
      https://github.com/bhpark1013/Snaplicator.git "$SNAP_HOME"
  else
    curl -fsSL "https://codeload.github.com/bhpark1013/Snaplicator/tar.gz/refs/heads/$SNAPLICATOR_REF" \
      | tar xz --strip-components=1 -C "$SNAP_HOME"
  fi
fi

# ── 3. demo publisher ────────────────────────────────────────────────
if [ "$DEMO" = "1" ]; then
  if docker inspect -f '{{.State.Running}}' "$DEMO_PUB_NAME" 2>/dev/null | grep -q true; then
    info "demo publisher already running ($DEMO_PUB_NAME)"
  else
    info "starting seeded demo publisher on port $DEMO_PUB_PORT..."
    docker rm -f "$DEMO_PUB_NAME" >/dev/null 2>&1 || true
    docker run -d --name "$DEMO_PUB_NAME" -p "$DEMO_PUB_PORT:5432" \
      -e POSTGRES_PASSWORD=snapdemo "$POSTGRES_IMAGE" -c wal_level=logical >/dev/null
    for _ in $(seq 1 30); do
      docker exec "$DEMO_PUB_NAME" pg_isready -U postgres >/dev/null 2>&1 && break
      sleep 1
    done
    sleep 2
    docker exec -i "$DEMO_PUB_NAME" psql -q -U postgres -v ON_ERROR_STOP=1 <<SQL
CREATE TABLE users (id serial PRIMARY KEY, name text, email text, created_at timestamptz DEFAULT now());
CREATE TABLE orders (id serial PRIMARY KEY, user_id int REFERENCES users(id), amount numeric(10,2), status text, created_at timestamptz DEFAULT now());
INSERT INTO users (name, email) SELECT 'user_'||g, 'user_'||g||'@example.com' FROM generate_series(1,1000) g;
INSERT INTO orders (user_id, amount, status)
  SELECT (random()*999+1)::int, round((random()*500)::numeric, 2),
         (ARRAY['paid','pending','refunded'])[1+floor(random()*3)::int]
  FROM generate_series(1,5000);
CREATE PUBLICATION $PUBLICATION_NAME FOR ALL TABLES;
SQL
    info "demo publisher seeded: 1,000 users / 5,000 orders"
  fi
  PRIMARY_HOST=127.0.0.1 PRIMARY_PORT=$DEMO_PUB_PORT PRIMARY_DB=postgres
  PRIMARY_USER=postgres PRIMARY_PASSWORD=snapdemo
else
  read -r PRIMARY_HOST PRIMARY_PORT PRIMARY_DB PRIMARY_USER PRIMARY_PASSWORD <<EOF
$(python3 - "$CONNSTR" <<'PY'
import sys
from urllib.parse import urlparse, unquote
u = urlparse(sys.argv[1])
print(u.hostname or "", u.port or 5432,
      (u.path or "").lstrip("/") or "postgres",
      unquote(u.username or "postgres"), unquote(u.password or ""))
PY
)
EOF
  [ -n "$PRIMARY_HOST" ] || die "could not parse host from: $CONNSTR"
fi

# ── 4. btrfs pool (snaplicator-init: measure → plan → apply) ─────────
info "provisioning the btrfs pool..."
INIT_ARGS=(--apply --yes)
if [ -n "${ROOT_DATA_DIR:-}" ]; then
  INIT_ARGS+=(--data-dir "$ROOT_DATA_DIR")
fi
if [ "$DEMO" = "1" ]; then
  INIT_ARGS+=(--pool-bytes $((DEMO_POOL_GIB * 1024 * 1024 * 1024)))
else
  INIT_ARGS+=("$CONNSTR")
fi
POOL_OUT=$(cd "$SNAP_HOME/cli" && python3 -m snaplicator_init "${INIT_ARGS[@]}") \
  || { printf '%s\n' "$POOL_OUT" >&2; die "pool provisioning failed — see the plan above (attach a disk or pass ROOT_DATA_DIR=/path)"; }
printf '%s\n' "$POOL_OUT"
POOL_DIR=$(printf '%s\n' "$POOL_OUT" | sed -n 's/^pool ready: \(.*\) (.*/\1/p' | tail -1)
[ -n "$POOL_DIR" ] || die "could not determine the pool directory from snaplicator-init output"
ROOT_DATA_DIR=$POOL_DIR

# ── 5. publication on the publisher (non-demo) ───────────────────────
if [ "$DEMO" = "0" ]; then
  if [ "$(psql "$CONNSTR" -Atc "SELECT count(*) FROM pg_publication WHERE pubname='$PUBLICATION_NAME'")" = "0" ]; then
    info "creating publication $PUBLICATION_NAME (FOR ALL TABLES)..."
    psql "$CONNSTR" -q -c "CREATE PUBLICATION $PUBLICATION_NAME FOR ALL TABLES" \
      || die "could not create the publication — create it manually and re-run"
  else
    info "publication $PUBLICATION_NAME already exists"
  fi
fi

# ── 6. management plane ──────────────────────────────────────────────
ENV_FILE="$SNAP_HOME/deploy/.env"
[ -f "$ENV_FILE" ] && cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
cat > "$ENV_FILE" <<EOF
WEB_PORT=$WEB_PORT
BACKEND_PORT=$BACKEND_PORT
ROOT_DATA_DIR=$ROOT_DATA_DIR
MAIN_DATA_DIR=$MAIN_DATA_DIR
CONTAINER_NAME=$CONTAINER_NAME
NETWORK_NAME=$NETWORK_NAME
HOST_PORT=$HOST_PORT
POSTGRES_IMAGE=$POSTGRES_IMAGE
POSTGRES_USER=$POSTGRES_USER
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
POSTGRES_DB=$POSTGRES_DB
PRIMARY_HOST=$PRIMARY_HOST
PRIMARY_PORT=$PRIMARY_PORT
PRIMARY_DB=$PRIMARY_DB
PRIMARY_USER=$PRIMARY_USER
PRIMARY_PASSWORD=$PRIMARY_PASSWORD
PGSSLMODE=${PGSSLMODE:-prefer}
PUBLICATION_NAME=$PUBLICATION_NAME
SUBSCRIPTION_NAME=$SUBSCRIPTION_NAME
DDL_SYNC_INTERVAL=$DDL_SYNC_INTERVAL
EOF

info "starting the management plane (first build takes a few minutes)..."
( cd "$SNAP_HOME/deploy" && docker compose -p "$PROJECT" up -d --build )

for _ in $(seq 1 30); do
  curl -fsS "127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "127.0.0.1:$BACKEND_PORT/health" >/dev/null || die "backend did not become healthy — check: docker compose -p $PROJECT logs manager"

# ── 7. replica bootstrap ─────────────────────────────────────────────
if docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
  info "replica $CONTAINER_NAME already running — skipping bootstrap"
else
  info "bootstrapping the replica (schema clone + subscription; may take a while)..."
  ( cd "$SNAP_HOME/deploy" && docker compose -p "$PROJECT" exec -T manager bash scripts/run-replica-postgres.sh </dev/null ) \
    || die "replica bootstrap failed — check $SNAP_HOME logs and re-run this installer"
fi

# ── done ─────────────────────────────────────────────────────────────
# the machine's outbound source address (hostname -I can lead with a
# docker bridge IP, which is useless to the user)
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="src") print $(i+1)}' | head -1)
[ -n "$IP" ] || IP=$(hostname -I 2>/dev/null | awk '{print $1}')
info "done!"
echo
echo "  UI:        http://${IP:-<host>}:$WEB_PORT"
echo "  replica:   postgres://$POSTGRES_USER:$POSTGRES_PASSWORD@${IP:-<host>}:$HOST_PORT/$POSTGRES_DB"
echo "  clone one: curl -X POST 127.0.0.1:$BACKEND_PORT/clones -H 'Content-Type: application/json' -d '{}'"
echo "  pool:      $ROOT_DATA_DIR"
echo
