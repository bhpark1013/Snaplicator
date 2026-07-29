#!/usr/bin/env bash
# Snaplicator one-line installer (issue #19 stage 4, #10).
#
#   # nothing to look up first — it asks which database to point at:
#   curl -fsSL https://raw.githubusercontent.com/bhpark1013/Snaplicator/main/deploy/install.sh \
#     | sudo bash
#
#   # on macOS, the same line (without sudo — it does not touch the Mac):
#   curl -fsSL .../install.sh | bash
#   It provisions a Linux machine with OrbStack and continues inside it,
#   because btrfs cannot exist on macOS. MACHINE= names it.
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

# Ask which database to point at, unless it was given. A connection URI
# carries a password, and an argument is the worst place to put one: it is
# visible in `ps` for the whole run and it lands in the invoking shell's
# history. Asking keeps it out of both, which makes the prompt the better
# default rather than a fallback.
#
# stdin is the script itself under `curl | bash`, so the answer is read from
# the terminal directly — the same way the pool menu below does. Opening
# /dev/tty is the only real test that one exists: the device node passes -r
# for root even with no terminal behind it, and the open is what fails.
ask_target() {
  if [ "$DEMO" = "1" ] || [ -n "$CONNSTR" ]; then return 0; fi
  { : < /dev/tty; } 2>/dev/null \
    || die "no terminal to ask on — pass the connection URI (postgres://user:pw@host:port/db) or --demo"
  echo >&2
  info "Point Snaplicator at the database you want clones of." >&2
  info "No database handy? Answer 'demo' and it will seed a sample one." >&2
  for _ in 1 2 3; do
    read -r -p "[snaplicator] connection URI (or 'demo'): " CONNSTR < /dev/tty || CONNSTR=""
    case "$CONNSTR" in
      demo|DEMO|Demo) DEMO=1; CONNSTR=""; return 0 ;;
      postgres://*|postgresql://*) return 0 ;;
      # Never echo the rejected answer back: something that failed the
      # postgres:// pattern is usually still a URI, password and all.
      *) printf '  need postgres://user:pw@host:port/db (or demo)\n' >&2; CONNSTR="" ;;
    esac
  done
  die "no connection URI given"
}

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

# ── 0. macOS: get a Linux, then continue inside it ───────────────────
# btrfs is a Linux kernel filesystem, so there is nothing here to install
# onto. The Mac's own Docker does not help: its containers run in a VM whose
# mount namespace you cannot add to, and the daemon resolves every -v path
# there — so a pool mounted anywhere else is invisible to it and clones would
# silently receive an empty directory as their PGDATA. What is needed is a
# Linux the user owns, with the daemon living in it. Rather than making that
# the reader's homework, do it.
if [ "$(uname -s)" = "Darwin" ]; then
  : "${MACHINE:=snaplicator}"
  : "${MACHINE_IMAGE:=ubuntu}"
  : "${INSTALLER_URL:=https://raw.githubusercontent.com/bhpark1013/Snaplicator/${SNAPLICATOR_REF}/deploy/install.sh}"

  if ! command -v orbctl >/dev/null; then
    if command -v brew >/dev/null && { : < /dev/tty; } 2>/dev/null; then
      info "OrbStack provides the Linux machine this needs (and replaces Docker Desktop)."
      read -r -p "[snaplicator] install OrbStack with brew? [Y/n] " reply < /dev/tty || reply=""
      case "$reply" in
        ""|y|Y|yes|YES) brew install --cask orbstack || die "brew install failed" ;;
        *) die "install OrbStack (https://orbstack.dev), or run this inside a Linux VM of your own" ;;
      esac
    else
      die "OrbStack is required on macOS: brew install --cask orbstack  (or run this inside a Linux VM of your own)"
    fi
  fi

  # Ask here, where the user actually is. The handoff below runs without a
  # terminal of its own, so a prompt on the far side would have nothing to
  # read from.
  ask_target

  if orbctl list 2>/dev/null | awk '{print $1}' | grep -qx "$MACHINE"; then
    info "reusing the Linux machine '$MACHINE'"
  else
    info "creating the Linux machine '$MACHINE' ($MACHINE_IMAGE)..."
    orbctl create "$MACHINE_IMAGE" "$MACHINE" >/dev/null || die "could not create the machine '$MACHINE'"
  fi
  orbctl start "$MACHINE" >/dev/null 2>&1 || true

  # A fresh image ships curl, python3 and ss. postgresql-client is only
  # needed when pointing at a real publisher (measuring the payload, checking
  # the publication), but installing it unconditionally costs one package and
  # spares the far side from failing its own prerequisite check after the
  # machine has already been built. The machine's root filesystem is already
  # btrfs, so the pool needs no disk of its own.
  info "preparing '$MACHINE' (docker, btrfs-progs, psql)..."
  orb -m "$MACHINE" -u root bash -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq >/dev/null 2>&1
    apt-get install -y -qq docker.io btrfs-progs postgresql-client >/dev/null 2>&1
    systemctl start docker' || die "could not prepare '$MACHINE'"

  # Hand off to the same script, running as root inside the machine. The URI
  # travels in the environment, not argv: this script already honours a
  # preset CONNSTR, and argv would publish the password to that machine's
  # process list for the length of the install.
  # A plain string, not an array: macOS ships bash 3.2, where expanding an
  # empty array under `set -u` is itself an "unbound variable" error — which
  # is exactly the common case here, a run with only a URI to pass.
  FWD=""
  if [ "$DEMO" = "1" ]; then FWD="--demo"; fi
  for a in "$@"; do
    case "$a" in [A-Za-z_]*=*) FWD="$FWD $a" ;; esac
  done

  # The terminal has to be handed over explicitly. Under `curl | bash` this
  # script's stdin is the pipe, so that is what orb would inherit — and orb
  # decides whether to forward keystrokes from its own stdin. It gives the
  # far side a terminal either way (an openable, isatty-true /dev/tty), so
  # the pool menu over there prints its prompt and then waits on a terminal
  # nothing types into: the install hangs with no way to answer it, and no
  # test on the far side can tell the two cases apart.
  info "continuing inside '$MACHINE'..."
  if { : < /dev/tty; } 2>/dev/null; then
    orb -m "$MACHINE" -u root env CONNSTR="$CONNSTR" \
      bash -c "curl -fsSL '$INSTALLER_URL' | bash -s -- $FWD" < /dev/tty || exit $?
  else
    # No terminal here means orb allocates none there either, so the far side
    # sees no /dev/tty and takes its own recommendation.
    orb -m "$MACHINE" -u root env CONNSTR="$CONNSTR" \
      bash -c "curl -fsSL '$INSTALLER_URL' | bash -s -- $FWD" < /dev/null || exit $?
  fi

  IP=$(orbctl list 2>/dev/null | awk -v m="$MACHINE" '$1 == m {print $NF}')
  case "$IP" in [0-9]*.[0-9]*.[0-9]*.[0-9]*) ;; *) IP="" ;; esac
  echo
  info "this runs inside the Linux machine '$MACHINE', not on macOS directly:"
  echo "  shell into it:  orb -m $MACHINE"
  echo "  its containers: orb -m $MACHINE docker ps    (plain 'docker ps' will not show them)"
  [ -n "$IP" ] && echo "  reachable at:   $IP"
  echo
  exit 0
fi

# ── 1. prerequisites ─────────────────────────────────────────────────
[ "$(uname -s)" = "Linux" ] || die "Linux or macOS only (btrfs is a Linux kernel filesystem)."
[ "$(id -u)" = "0" ] || die "run as root:  curl ... | sudo bash -s -- ..."
command -v docker >/dev/null || die "docker is required (https://docs.docker.com/engine/install/)"
command -v python3 >/dev/null || die "python3 (>= 3.10) is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || die "python3 >= 3.10 required (found $(python3 --version))"

ask_target

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
  # -r is not the test: the device node passes it for root even with no
  # terminal behind it, and opening is what fails. Same check as ask_target.
  if { : < /dev/tty; } 2>/dev/null; then
    choice=""
    # Empty the terminal's input queue first. A terminal emulator answers the
    # queries programs make of it (background colour, cursor position) by
    # writing the reply into that queue, and whatever is sitting there when
    # this prompt opens is read as if it had been typed — over an orb session
    # a reply generated during the handoff arrives with no one to consume it,
    # and lands here minutes later glued to the front of the answer.
    while read -r -t 0 _junk < /dev/tty 2>/dev/null; do
      read -r -t 0.2 _junk < /dev/tty 2>/dev/null || break
    done
    case "$REC_ARG" in
      format:*)
        # a destructive option is never the enter-key default
        read -r -p "[snaplicator] where should the pool live? (type a number) " choice < /dev/tty || choice=""
        [ -n "$choice" ] || die "an explicit choice is required when the option formats a disk" ;;
      *)
        read -r -p "[snaplicator] where should the pool live? [${REC}] " choice < /dev/tty || choice=""
        [ -n "$choice" ] || choice=$REC ;;
    esac
    # The answer is about to become part of a sed expression, so it has to be
    # a number before it gets there: anything else is a sed syntax error
    # instead of the message written for exactly this mistake.
    choice=${choice%$'\r'}
    case "$choice" in
      ''|*[!0-9]*) die "not a number: pick one of the listed options and re-run" ;;
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
