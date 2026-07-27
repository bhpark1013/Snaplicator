#!/usr/bin/env bash
# Snaplicator one-line installer (issue #19 stage 4, #10).
#
#   # nothing to look up first — it asks which database to point at:
#   curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh \
#     | sudo bash
#
# Answer the prompt with your primary's connection URI, or with "demo" to
# have it spin up a seeded sample publisher instead. Both answers can also be
# given up front, which skips the prompt (useful for unattended runs):
#
#   ... | sudo bash -s -- "postgres://user:pw@primary:5432/mydb"
#   ... | sudo bash -s -- --demo
#
# Every setting can be overridden by appending VAR=VALUE arguments, e.g.
#   ... | sudo bash -s -- --demo WEB_PORT=18080 CONTAINER_NAME=snapdemo_replica
#
# What it does, in order:
#   1. checks prerequisites (linux, docker, python3; installs the compose
#      plugin if missing), then asks what to replicate unless it was given
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

# Nothing was passed in, so ask. A connection URI carries a password, and an
# argument is the worst place to put one: it is visible in `ps` for the whole
# run and it lands in the invoking shell's history. Asking keeps it out of
# both, which makes the prompt the better default rather than a fallback.
#
# stdin is the script itself under `curl | bash`, so the answer has to be
# read from the terminal directly — the same way the pool menu below does it.
if [ "$DEMO" = "0" ] && [ -z "$CONNSTR" ]; then
  # Opening it is the only real test. Under `curl | sudo bash` with no
  # terminal the device node still exists and still passes -r for root; the
  # open is what fails, and without this the prompt loop would spin against a
  # dead fd and report "no connection URI given" instead of the real reason.
  { : < /dev/tty; } 2>/dev/null \
    || die "no terminal to ask on — pass the connection URI (postgres://user:pw@host:port/db) or --demo"
  echo >&2
  info "Point Snaplicator at the database you want clones of." >&2
  info "No database handy? Answer 'demo' and it will seed a sample one." >&2
  for _ in 1 2 3; do
    read -r -p "[snaplicator] connection URI (or 'demo'): " CONNSTR < /dev/tty || CONNSTR=""
    case "$CONNSTR" in
      demo|DEMO|Demo) DEMO=1; CONNSTR=""; break ;;
      postgres://*|postgresql://*) break ;;
      # Never echo the rejected answer back: a URI that failed the pattern
      # is usually still a URI, password and all.
      *) printf '  need postgres://user:pw@host:port/db (or demo)\n' >&2; CONNSTR="" ;;
    esac
  done
  [ "$DEMO" = "1" ] || [ -n "$CONNSTR" ] || die "no connection URI given"
fi

if [ "$DEMO" = "0" ]; then
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

# A port held by our own stack is not a conflict — it is the previous
# install, which every later step is written to detect and reuse. Checking
# only "is the port busy" made re-running fail on exactly the hosts where the
# installer had already succeeded, contradicting the promise at the top.
#
# Both services run with network_mode: host, so they publish no mappings and
# `docker ps` cannot say which port a container holds. The previous install's
# .env is the record of which ports it took; pair it with "our containers are
# actually up" so a stale .env alone never waves a real conflict through.
port_is_ours() {
  [ -f "$SNAP_HOME/deploy/.env" ] || return 1
  [ -n "$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT")" ] || return 1
  grep -qE "^(WEB_PORT|BACKEND_PORT)=$1\$" "$SNAP_HOME/deploy/.env"
}
for p in "$WEB_PORT" "$BACKEND_PORT"; do
  if ss -ltn 2>/dev/null | grep -q ":$p "; then
    port_is_ours "$p" \
      || die "port $p is already in use — override with WEB_PORT=/BACKEND_PORT= arguments"
    info "port $p is this install's own $PROJECT stack — reusing it"
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

# ── 4. btrfs pool (snaplicator-init: measure → plan → ask → apply) ───
info "surveying this host for a pool location..."
PLAN_JSON=$(mktemp /tmp/snaplicator-plan.XXXXXX)
PLAN_ARGS=(--json)
if [ -n "${ROOT_DATA_DIR:-}" ]; then
  PLAN_ARGS+=(--data-dir "$ROOT_DATA_DIR")
fi
if [ "$DEMO" = "1" ]; then
  PLAN_ARGS+=(--pool-bytes $((DEMO_POOL_GIB * 1024 * 1024 * 1024)))
else
  PLAN_ARGS+=("$CONNSTR")
fi
set +e
(cd "$SNAP_HOME/cli" && python3 -m snaplicator_init "${PLAN_ARGS[@]}") > "$PLAN_JSON"
PLAN_RC=$?
set -e
# 0 = a home exists, 1 = no-fit (a bare disk may still save the day)
[ "$PLAN_RC" -le 1 ] || { cat "$PLAN_JSON" >&2; die "host survey / payload measurement failed"; }

# Re-run: stick to the pool a previous install already chose, instead of
# asking again (and possibly picking a different spot).
if [ -z "${FORMAT_DISK:-}" ] && [ -z "${ROOT_DATA_DIR:-}" ] && [ -f "$SNAP_HOME/deploy/.env" ]; then
  PREV_ROOT=$(sed -n 's/^ROOT_DATA_DIR=//p' "$SNAP_HOME/deploy/.env" | tail -1)
  if [ -n "$PREV_ROOT" ] && [ -d "$PREV_ROOT" ]; then
    ROOT_DATA_DIR=$PREV_ROOT
    info "reusing the pool from the previous install: $ROOT_DATA_DIR (delete $SNAP_HOME/deploy/.env to choose anew)"
  fi
fi

# Selection UX: show every viable option with the recommendation marked
# and let the user pick; with no viable option, show the remediation and
# stop. Presets (FORMAT_DISK= / ROOT_DATA_DIR=) skip the menu, and a
# TTY-less run takes the recommendation (never a destructive format).
CHOSEN=$(python3 -c '
import json, sys
p = json.load(open(sys.argv[1]))
print("" if p["chosen"] is None else p["chosen"]["target"])
' "$PLAN_JSON")

if [ -z "${FORMAT_DISK:-}" ] && [ -z "${ROOT_DATA_DIR:-}" ]; then
  # machine lines on stdout ("N|datadir:/path" / "N|format:/dev/x"),
  # the human menu on stderr (recommended first, planner ranking)
  OPTIONS=$(python3 -c '
import json, sys
p = json.load(open(sys.argv[1]))
GiB = 2 ** 30

def human(n):
    if n >= GiB:
        return f"{n / GiB:.1f} GiB"
    return f"{n // 2**20} MiB"

fit = [c for c in p["candidates"] if c["fits"]]
chosen = p["chosen"]
rec = 1
if chosen:
    for i, c in enumerate(fit, 1):
        if c["target"] == chosen["target"] and c["kind"] == chosen["kind"]:
            rec = i
            break
req = p["required_bytes"]
pay = p["payload_bytes"]
hdr = "[snaplicator] pool size needed: " + human(req)
if pay:
    hdr += "  (payload " + human(pay) + " × 2, floor 10 GiB)"
print(hdr, file=sys.stderr)
if fit:
    print("[snaplicator] locations with that much room:", file=sys.stderr)
for i, c in enumerate(fit, 1):
    target = c["target"]
    if c["priority"] == 3:
        size = c["size_bytes"] // GiB
        desc = "format " + target + " (empty disk, " + str(size) + " GiB) — DESTRUCTIVE: everything on it is erased"
        arg = "format:" + target
    else:
        pool = target.rstrip("/") + "/snaplicator"
        free = c["avail_bytes"] // GiB
        if c["priority"] == 1:
            how = "btrfs subvolume (nothing to format)"
        else:
            how = "loopback file on " + str(c["fstype"]) + " (slight I/O overhead)"
        desc = pool + " (" + str(free) + " GiB free) — " + how
        arg = "datadir:" + pool
    mark = "   [recommended]" if i == rec else ""
    print(str(i) + "|" + arg)
    print("  " + str(i) + ". " + desc + mark, file=sys.stderr)
for c in p["candidates"]:
    if not c["fits"]:
        print("  ✗  " + c["target"] + " — only " + human(c["avail_bytes"])
              + " free (< " + human(req) + " needed)", file=sys.stderr)
if fit:
    print("REC|" + str(rec))
' "$PLAN_JSON")
  if [ -z "$OPTIONS" ]; then
    (cd "$SNAP_HOME/cli" && python3 -m snaplicator_init --plan "$PLAN_JSON") >&2 || true
    die "no viable pool location — follow the remediation above, then re-run"
  fi
  REC=$(printf '%s\n' "$OPTIONS" | sed -n 's/^REC|//p')
  REC_ARG=$(printf '%s\n' "$OPTIONS" | sed -n "s/^${REC}|//p")
  if [ -r /dev/tty ]; then
    choice=""
    case "$REC_ARG" in
      format:*)
        # a destructive option is never the enter-key default
        read -r -p "[snaplicator] where should the pool live? (type a number) " choice < /dev/tty || choice=""
        [ -n "$choice" ] || die "an explicit choice is required when the option formats a disk" ;;
      *)
        read -r -p "[snaplicator] where should the pool live? [${REC}] " choice < /dev/tty || choice=""
        [ -n "$choice" ] || choice=$REC ;;
    esac
    SELECTED=$(printf '%s\n' "$OPTIONS" | sed -n "s/^${choice}|//p")
    [ -n "$SELECTED" ] || die "no option ${choice} — re-run and pick a listed number"
  else
    SELECTED=$(printf '%s\n' "$OPTIONS" | sed -n "s/^${REC}|//p")
    case "$SELECTED" in
      format:*) die "recommended option is a destructive format but there is no TTY to confirm — pass FORMAT_DISK=${SELECTED#format:}" ;;
    esac
    info "no TTY — taking the recommendation: ${SELECTED#datadir:}"
  fi
  case "$SELECTED" in
    format:*)  FORMAT_DISK=${SELECTED#format:} ;;
    datadir:*) ROOT_DATA_DIR=${SELECTED#datadir:} ;;
  esac
fi

if [ -n "${FORMAT_DISK:-}" ]; then
  info "pool target: $FORMAT_DISK (format as btrfs)"
else
  info "pool target: ${ROOT_DATA_DIR:-$CHOSEN}"
fi
info "provisioning the btrfs pool..."
# re-plan at apply time with the selection pinned — a frozen plan cannot
# change its chosen candidate, a --data-dir/--format-disk pin can
INIT_ARGS=(--apply --yes)
if [ -n "${ROOT_DATA_DIR:-}" ]; then
  INIT_ARGS+=(--data-dir "$ROOT_DATA_DIR")
fi
if [ -n "${FORMAT_DISK:-}" ]; then
  INIT_ARGS+=(--format-disk "$FORMAT_DISK")
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
