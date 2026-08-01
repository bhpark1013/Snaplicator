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
#   7. stops there, at the UI: the replica is brought up from the UI, once
#      what to replicate has been chosen (--demo and START_REPLICATION=1 do
#      it here instead — there is nothing to choose about a sample database,
#      and an unattended run has no one to choose)
#
# Re-running is safe: every step detects existing state and skips.

set -euo pipefail

info() { printf '\033[1;32m[snaplicator]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[snaplicator] WARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[snaplicator] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Ask the publisher the question that decides whether this install can finish,
# and ask it before anything has been built. The answer used to arrive at the
# publication step — after a machine had been created, 150 MB of packages
# installed and a pool provisioned — and it is one round trip.
#
# What it costs to be wrong is the whole asymmetry: the check is a single
# query, and skipping it buys a four-minute walk to a message the user could
# have had immediately.
#
# soft on macOS, where a failure to connect says nothing: the machine that
# will do the real connecting does not exist yet, and this Mac may not even
# have psql. Only a definite answer is acted on there.
publisher_preflight() {
  local mode=$1 out pub super rds
  command -v psql >/dev/null 2>&1 || return 0
  if ! out=$(PGCONNECT_TIMEOUT=10 psql "$CONNSTR" -Atc "
      SELECT (SELECT count(*) FROM pg_publication WHERE pubname = '$PUBLICATION_NAME') > 0,
             (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
             coalesce((SELECT bool_or(pg_has_role(current_user, oid, 'USAGE'))
                       FROM pg_roles WHERE rolname = 'rds_superuser'), false),
             current_setting('wal_level'),
             (SELECT count(*) FROM pg_replication_slots),
             current_setting('max_replication_slots')::int,
             (SELECT rolreplication FROM pg_roles WHERE rolname = current_user),
             (SELECT count(*) FROM pg_subscription WHERE subname LIKE 'snaplicator%')" 2>&1); then
    [ "$mode" = "soft" ] && return 0
    printf '%s\n' "$out" >&2
    die "could not reach the publisher with that URI"
  fi
  IFS='|' read -r pub super rds wal slots_used slots_max repl subs <<EOF
$out
EOF

  # Whether this database can publish at all, before whether this account may.
  # wal_level is a restart-only setting, so there is nothing an install can do
  # about it and nothing later in the run that will not fail — which is why it
  # is worth a query and not a warning at the point of no return.
  if [ "$wal" != "logical" ]; then
    die "this database cannot publish logical changes: wal_level is '$wal', not 'logical'.
  It is a server setting and a restart: ALTER SYSTEM SET wal_level = 'logical'; then restart.
  (On RDS: set rds.logical_replication = 1 in the parameter group and reboot.)
  A Snaplicator clone is one of these — clones run wal_level=replica. Point at the primary."
  fi
  # A Snaplicator replica does run wal_level=logical, so nothing above catches
  # it, and replicating from one copies a copy: the same data, one hop further
  # behind, with the clones that matter left downstream of a chain.
  if [ "${subs:-0}" != "0" ] && [ "${subs:-0}" != "" ]; then
    warn "this database is itself a Snaplicator subscriber — it is a replica, not a primary."
    warn "Replicating from it works, but it copies a copy: everything arrives a hop later"
    warn "than it needs to. Point at the primary unless this is deliberate."
  fi
  if [ "${slots_used:-0}" -ge "${slots_max:-0}" ]; then
    die "the publisher has no replication slot left ($slots_used of $slots_max in use).
  Raise max_replication_slots (a restart), or drop a slot no subscriber is using:
    SELECT slot_name, active FROM pg_replication_slots;"
  fi
  # The subscriber connects back as this role, and a role without REPLICATION
  # cannot open the stream however many table privileges it has.
  if [ "$super" != "t" ] && [ "$rds" != "t" ] && [ "$repl" != "t" ]; then
    die "the account in that URI cannot replicate: it has neither superuser nor the
  REPLICATION attribute, so the subscription cannot connect.
    ALTER ROLE <user> WITH REPLICATION;"
  fi
  # rds_superuser is checked separately because RDS grants no superuser at
  # all: the managed role is what a superuser is there, and the ownership
  # checks are patched to accept it.
  if [ "$super" = "t" ] || [ "$rds" = "t" ]; then
    return 0
  fi
  if [ "$pub" = "t" ]; then
    warn "$PUBLICATION_NAME exists, so replication will work, but the account in that URI"
    warn "is not a superuser: the trigger that keeps the publication current cannot be"
    warn "installed (CREATE EVENT TRIGGER is superuser-only), so tables created later"
    warn "will have to be added by hand."
    return 0
  fi
  die "the publication $PUBLICATION_NAME does not exist and this account cannot create it.
  CREATE PUBLICATION ... FOR ALL TABLES is superuser-only — PostgreSQL offers no GRANT for it.
  Either point at a superuser account (on RDS: a member of rds_superuser), or create it
  yourself and re-run:  CREATE PUBLICATION $PUBLICATION_NAME FOR ALL TABLES;"
}

# Does the image this replica will run actually carry what the primary uses?
#
# Checked because the alternative is silence. The initial schema clone applies
# the dump with ON_ERROR_STOP=0 — it has to, or one unbuildable object would
# abort the whole clone — so a missing extension takes its tables, its indexes
# and its operator classes with it and the run still reports success. A live
# deployment was found missing pg_trgm and, with it, five trigram indexes; the
# data was complete and every query touching them planned differently.
#
# The image is asked directly rather than started: pg_config knows where the
# control files live on both Debian and Alpine builds, and listing them costs
# a container that exits immediately.
# The base image follows the primary's major version, not a pinned default.
#
# A replica exists to stand in for the primary — same major, same behaviour,
# same plans. A default baked into this script is right only for whoever set
# it, and wrong for everyone whose primary moved on. The primary already
# knows the answer, so it is asked rather than guessed. An operator who names
# an image keeps it; they are told if it will not line up.
match_image_to_primary() {
  command -v psql >/dev/null 2>&1 || return 0
  local major img_major
  major=$(PGCONNECT_TIMEOUT=10 psql "$CONNSTR" -Atc "SHOW server_version" 2>/dev/null \
    | sed -n 's/^\([0-9][0-9]*\).*/\1/p')
  [ -n "$major" ] || return 0

  if [ "${POSTGRES_IMAGE_SET:-}" != "1" ]; then
    [ "$POSTGRES_IMAGE" = "postgres:$major" ] && return 0
    info "primary is PostgreSQL $major — using postgres:$major (default was $POSTGRES_IMAGE)"
    POSTGRES_IMAGE="postgres:$major"
    return 0
  fi

  # Asked of the image rather than read off its tag: a tag says whatever its
  # author wanted, and a custom one (org/pg:15-alpine3.20-hll2.18) parses badly.
  img_major=$(docker run --rm --entrypoint sh "$POSTGRES_IMAGE" -c 'pg_config --version' 2>/dev/null \
    | sed -n 's/^PostgreSQL \([0-9][0-9]*\).*/\1/p')
  [ -n "$img_major" ] || return 0
  [ "$img_major" = "$major" ] && return 0
  warn "POSTGRES_IMAGE=$POSTGRES_IMAGE is PostgreSQL $img_major, but the primary is $major."
  warn "Logical replication permits it, but the replica will not behave like the"
  warn "primary it stands in for. Unset POSTGRES_IMAGE to follow the primary."
}

extension_preflight() {
  command -v psql >/dev/null 2>&1 || return 0
  local src avail missing=""
  src=$(PGCONNECT_TIMEOUT=10 psql "$CONNSTR" -Atc \
    "SELECT extname FROM pg_extension WHERE extname <> 'plpgsql'" 2>/dev/null) || return 0
  [ -n "$src" ] || return 0

  avail=$(docker run --rm --entrypoint sh "$POSTGRES_IMAGE" -c \
    'ls "$(pg_config --sharedir)"/extension/*.control 2>/dev/null' 2>/dev/null \
    | sed 's#.*/##; s#\.control$##') || return 0
  [ -n "$avail" ] || return 0

  while IFS= read -r ext; do
    [ -n "$ext" ] || continue
    printf '%s\n' "$avail" | grep -qxF "$ext" || missing="$missing $ext"
  done <<EOF
$src
EOF

  if [ -z "$missing" ]; then
    info "extensions: $POSTGRES_IMAGE carries everything the primary uses"
    return 0
  fi

  warn "$POSTGRES_IMAGE does not carry:$missing"

  # Built rather than refused. The missing extensions are almost always one
  # apt away — the official image installs PostgreSQL from apt.postgresql.org,
  # so that repository is already configured inside it — and stopping to make
  # the user assemble an image by hand is asking them to do a mechanical job
  # that the primary's catalog fully specifies.
  local builder="$SNAP_HOME/deploy/build-replica-image.sh" built=""
  if [ "$AUTO_BUILD_IMAGE" != "0" ] && [ -f "$builder" ]; then
    info "building an image that carries them (AUTO_BUILD_IMAGE=0 to skip)..."
    if built=$(BASE_IMAGE="$POSTGRES_IMAGE" bash "$builder" "$CONNSTR"); then
      POSTGRES_IMAGE=$built
      info "extensions: using $POSTGRES_IMAGE"
      return 0
    fi
    warn "could not build an image automatically"
  fi

  warn "The primary uses them. Their tables, indexes and operator classes will be"
  warn "skipped by the schema clone and the copy will still report success —"
  warn "so the replica would be complete-looking and quietly missing objects."
  warn "Build one yourself:  deploy/build-replica-image.sh \"\$CONNSTR\""
  if [ "$ALLOW_MISSING_EXTENSIONS" != "1" ]; then
    die "refusing to build a replica the primary's schema does not fit into.
  Override with ALLOW_MISSING_EXTENSIONS=1 if the missing ones are not needed."
  fi
}

# Run a step that has nothing to say while it works, and say something on its
# behalf. A minutes-long silence is indistinguishable from a hang, and the
# reader's only recourse is to kill an install that was fine. The seconds
# ticking up are the whole point: they are what a progress bar would be for
# work whose total is not knowable in advance (apt decides how many packages
# a machine needs, and how fast the mirror feels like being).
#
# The output is kept, not shown, and printed only if the step fails — the
# reason to hide it in the first place was that it is noise until it isn't.
run_step() {
  local label=$1; shift
  local log rc=0 pid start=$SECONDS i=0 frames='|/-\'
  log=$(mktemp "${TMPDIR:-/tmp}/snaplicator-step.XXXXXX")
  "$@" >"$log" 2>&1 &
  pid=$!
  if [ -t 2 ]; then
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r  %s %s (%ds)' "${frames:$i:1}" "$label" "$((SECONDS - start))" >&2
      i=$(( (i + 1) % 4 ))
      sleep 0.5
    done
    printf '\r\033[K' >&2
  fi
  wait "$pid" || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '\033[1;31m[snaplicator] ERROR:\033[0m %s failed after %ds\n' "$label" "$((SECONDS - start))" >&2
    tail -20 "$log" >&2
  else
    info "  $label — $((SECONDS - start))s"
  fi
  rm -f "$log"
  return "$rc"
}

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
POSTGRES_IMAGE_SET=${POSTGRES_IMAGE:+1}   # told to us, rather than defaulted
: "${POSTGRES_IMAGE:=postgres:17}"        # only a fallback; see match_image_to_primary
: "${ALLOW_MISSING_EXTENSIONS:=0}"
: "${AUTO_BUILD_IMAGE:=1}"
: "${POSTGRES_USER:=postgres}"
: "${POSTGRES_DB:=postgres}"
: "${PUBLICATION_NAME:=snaplicator_publication}"
: "${SUBSCRIPTION_NAME:=snaplicator_subscription}"
: "${DDL_SYNC_INTERVAL:=30}"
: "${DEMO_PUB_NAME:=snaplicator-demo-pub}"
: "${DEMO_PUB_PORT:=15432}"
: "${DEMO_POOL_GIB:=10}"
# ROOT_DATA_DIR: optional; unset → snaplicator-init picks the best spot

if [ -z "${POSTGRES_PASSWORD:-}" ] && [ -f "$SNAP_HOME/deploy/.env" ]; then
  # A re-run must keep the password it gave out. The replica container was
  # created with it and is not recreated here, so inventing a new one would
  # only make the .env — and everything printed from it, and every clone
  # connection string — describe a password that does not open anything.
  POSTGRES_PASSWORD=$(sed -n 's/^POSTGRES_PASSWORD=//p' "$SNAP_HOME/deploy/.env" | tail -1)
fi
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
  [ "$DEMO" = "1" ] || publisher_preflight soft

  if orbctl list 2>/dev/null | awk '{print $1}' | grep -qx "$MACHINE"; then
    info "reusing the Linux machine '$MACHINE'"
  else
    info "creating the Linux machine '$MACHINE' ($MACHINE_IMAGE)..."
    run_step "downloading and unpacking $MACHINE_IMAGE" \
      orbctl create "$MACHINE_IMAGE" "$MACHINE" || die "could not create the machine '$MACHINE'"
  fi
  orbctl start "$MACHINE" >/dev/null 2>&1 || true

  # A fresh image ships curl, python3 and ss. postgresql-client is only
  # needed when pointing at a real publisher (measuring the payload, checking
  # the publication), but installing it unconditionally costs one package and
  # spares the far side from failing its own prerequisite check after the
  # machine has already been built. The machine's root filesystem is already
  # btrfs, so the pool needs no disk of its own.
  # Split into steps that are named as they run, rather than one silent
  # command: this is the longest wait in the install (a package index and
  # ~150 MB of docker over whatever the mirror gives you), and the reader
  # should be able to see which part of it is taking the time.
  info "preparing '$MACHINE' (docker, btrfs-progs, psql)..."
  run_step "reading package lists" \
    orb -m "$MACHINE" -u root env DEBIAN_FRONTEND=noninteractive \
      apt-get update -qq || die "could not prepare '$MACHINE' (apt-get update)"
  run_step "installing docker, btrfs-progs, postgresql-client" \
    orb -m "$MACHINE" -u root env DEBIAN_FRONTEND=noninteractive \
      apt-get install -y -qq docker.io btrfs-progs postgresql-client \
      || die "could not prepare '$MACHINE' (apt-get install)"
  run_step "starting docker" \
    orb -m "$MACHINE" -u root systemctl start docker || die "could not start docker in '$MACHINE'"

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
  #
  # Handing it over means taking it back. orb puts this terminal in raw mode
  # to forward keystrokes, and a run that ends badly ends without putting it
  # back: the shell returns with echo and line editing off, and whatever the
  # far side had in flight arrives as something typed at the prompt —
  #
  #     zsh: command not found: Creating
  #
  # which is the last of an error message being executed instead of read. The
  # settings are saved here and restored on every exit, including the abrupt
  # ones, so that a failure can be seen at all.
  #
  # Everything is also kept in a log inside the machine. A terminal that has
  # scrolled, or has been overwritten by a build's progress display, is not a
  # record — and the failure worth reading is usually the one that ended the
  # run, i.e. the one furthest up.
  info "continuing inside '$MACHINE'..."
  FAR_LOG=/var/log/snaplicator-install.log
  # The far side used to fetch its own copy, which meant two downloads of the
  # same file through a CDN that caches it for five minutes — so the two sides
  # could be different versions of this script, and a fix pushed minutes ago
  # could run on one and not the other. It is fetched once here, with a query
  # string the cache has not seen, and handed over.
  SCRIPT=$(curl -fsSL "${INSTALLER_URL}?cb=$$$(date +%s)") \
    || die "could not download the installer from $INSTALLER_URL"
  # Kept off the terminal deliberately. orb restores the terminal when a
  # session ends — alternate screen off, origin mode reset, mouse and
  # bracketed-paste modes cleared, cursor put back — and it does that even for
  # a one-shot pipe that displays nothing:
  #
  #     ESC]11;? ESC[6n ESC[?1049l ESC[?6l ESC[?7h ESC[?2004l ...
  #
  # Landing between the line above and the session below, that restore puts
  # the cursor back where it was before either was written, so the far side's
  # first prompt draws over this line instead of after it. With stdout off the
  # terminal orb writes nothing at all, which is what this call has to say.
  if ! COPY_ERR=$(printf '%s' "$SCRIPT" \
      | orb -m "$MACHINE" -u root bash -c 'cat > /tmp/snaplicator-install.sh' 2>&1 >/dev/null); then
    if [ -n "$COPY_ERR" ]; then printf '%s\n' "$COPY_ERR" >&2; fi
    die "could not copy the installer into '$MACHINE'"
  fi
  FAR_CMD="bash /tmp/snaplicator-install.sh $FWD"
  # script(1) keeps a pty, so the far side still sees a terminal: its prompts
  # still find /dev/tty and its progress ticker still knows it has a reader.
  # -e returns the child's exit status rather than script's own.
  FAR_CMD="if command -v script >/dev/null 2>&1; then script -qefc \"$FAR_CMD\" $FAR_LOG; else $FAR_CMD 2>&1 | tee $FAR_LOG; fi"

  RC=0
  if { : < /dev/tty; } 2>/dev/null; then
    TTY_SAVED=$(stty -g < /dev/tty 2>/dev/null || true)
    if [ -n "$TTY_SAVED" ]; then
      trap 'stty "$TTY_SAVED" < /dev/tty 2>/dev/null || true' EXIT INT TERM
    fi
    orb -m "$MACHINE" -u root env CONNSTR="$CONNSTR" bash -c "$FAR_CMD" < /dev/tty || RC=$?
    if [ -n "$TTY_SAVED" ]; then
      stty "$TTY_SAVED" < /dev/tty 2>/dev/null || true
      trap - EXIT INT TERM
    fi
  else
    # No terminal here means orb allocates none there either, so the far side
    # sees no /dev/tty and takes its own recommendation.
    orb -m "$MACHINE" -u root env CONNSTR="$CONNSTR" bash -c "$FAR_CMD" < /dev/null || RC=$?
  fi
  if [ "$RC" != "0" ]; then
    echo >&2
    info "the install failed inside '$MACHINE'. Its output was kept — the error is near the end:" >&2
    echo "  orb -m $MACHINE tail -40 $FAR_LOG" >&2
    echo "  orb -m $MACHINE less $FAR_LOG        # all of it" >&2
    exit "$RC"
  fi

  IP=$(orbctl list 2>/dev/null | awk -v m="$MACHINE" '$1 == m {print $NF}')
  case "$IP" in [0-9]*.[0-9]*.[0-9]*.[0-9]*) ;; *) IP="" ;; esac
  # what the machine actually settled on, not what was asked for here
  FAR_PORT=$(orb -m "$MACHINE" -u root sh -c \
    "sed -n 's/^WEB_PORT=//p' '$SNAP_HOME/deploy/.env' 2>/dev/null | tail -1" 2>/dev/null)
  case "$FAR_PORT" in ''|*[!0-9]*) FAR_PORT=$WEB_PORT ;; esac

  echo
  info "this runs inside the Linux machine '$MACHINE', not on macOS directly:"
  echo "  shell into it:  orb -m $MACHINE"
  echo "  its containers: orb -m $MACHINE docker ps    (plain 'docker ps' will not show them)"
  echo

  # End on the one thing there is to do next. The machine's own summary has
  # already scrolled past several minutes of build output by now, so the
  # address is repeated here rather than left for the reader to scroll back
  # to — and opened, since a UI that has to be found is a UI that is not yet
  # doing anything for anyone. NO_BROWSER=1 declines.
  if [ -n "$IP" ]; then
    # Land on the page with the decision on it, not on a list of the clones
    # that cannot exist yet. Once the replica is up, home is the right page
    # again — so this asks the machine which of the two situations it is in.
    UI_URL="http://$IP:$FAR_PORT"
    if ! orb -m "$MACHINE" -u root docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
      UI_URL="$UI_URL/replication"
    fi
    if [ "${NO_BROWSER:-0}" = "1" ]; then
      info "open the UI:  $UI_URL"
    elif open "$UI_URL" 2>/dev/null; then
      info "opening the UI in your browser:  $UI_URL"
    else
      info "open the UI:  $UI_URL"
    fi
  else
    info "the machine reported no address — 'orbctl list' will show it once it has one"
  fi
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
# Only knowable once the target is: the demo answer arrives at the prompt, not
# only as a flag. 1 = subscribe before finishing, 0 = leave it to the UI (§7).
: "${START_REPLICATION:=$DEMO}"

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
  # Reusing the checkout is not the same as keeping it. A re-run is how this
  # installs a newer Snaplicator — the images are rebuilt from these files —
  # and skipping the fetch quietly rebuilt the same code every time, which
  # looks exactly like a fix that did not work.
  #
  # Local edits are the one thing worth more than being current, so a dirty
  # tree is left alone and said so, rather than reset over.
  if [ -d "$SNAP_HOME/.git" ] && command -v git >/dev/null; then
    BEFORE=$(git -C "$SNAP_HOME" rev-parse --short HEAD 2>/dev/null || echo '?')
    # Tracked files only. Untracked ones are not at risk — a checkout leaves
    # them alone — and this directory is full of them by design: deploy/.env
    # and its backups are written here by this script, so counting them as
    # "local changes" meant the installer blocked its own updates from the
    # second run onwards.
    if ! git -C "$SNAP_HOME" diff --quiet HEAD 2>/dev/null; then
      warn "$SNAP_HOME has edits to tracked files — building from it as it is, not updating to $SNAPLICATOR_REF"
    elif git -C "$SNAP_HOME" fetch -q --depth 1 origin "$SNAPLICATOR_REF" 2>/dev/null \
      && git -C "$SNAP_HOME" checkout -q --detach FETCH_HEAD 2>/dev/null; then
      AFTER=$(git -C "$SNAP_HOME" rev-parse --short HEAD 2>/dev/null || echo '?')
      if [ "$BEFORE" = "$AFTER" ]; then
        info "checkout already current ($SNAPLICATOR_REF @ $AFTER)"
      else
        info "updated the checkout: $BEFORE → $AFTER ($SNAPLICATOR_REF)"
      fi
    else
      warn "could not update $SNAP_HOME to $SNAPLICATOR_REF — building from what is there ($BEFORE)"
    fi
  else
    # Fetched as a tarball the first time (no git on the host): the same
    # tarball over the top is the only update available, and it overwrites.
    info "refreshing $SNAP_HOME from $SNAPLICATOR_REF..."
    curl -fsSL "https://codeload.github.com/bhpark1013/Snaplicator/tar.gz/refs/heads/$SNAPLICATOR_REF" \
      | tar xz --strip-components=1 -C "$SNAP_HOME" \
      || warn "could not refresh $SNAP_HOME — building from what is there"
  fi
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
  # Here the connection is the machine's own, so a failure to reach the
  # publisher is the answer rather than an inconclusive one.
  publisher_preflight hard
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
    # a reply generated during the handoff can arrive with no one to consume
    # it and land here, minutes later, glued to the front of the answer.
    #
    # The terminal has to leave line mode to be emptied: in line mode nothing
    # is readable until Enter, so a reply with no newline in it is invisible
    # to any read that hopes to discard it, and stays queued to be delivered
    # as part of the next line — the very line this is trying to protect.
    if command -v stty >/dev/null 2>&1 && _saved=$(stty -g < /dev/tty 2>/dev/null); then
      stty -icanon min 0 time 0 < /dev/tty 2>/dev/null || true
      while read -r -t 0.05 -n 4096 _junk < /dev/tty 2>/dev/null; do :; done
      stty "$_saved" < /dev/tty 2>/dev/null || true
    fi
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

# Run once docker and psql are both present, and before anything is built out
# of the image. Order matters: the major is settled first, then the extensions
# are checked against the image that choice landed on.
if [ "$DEMO" = "0" ]; then
  match_image_to_primary
  extension_preflight
fi

# ── 5. publication on the publisher (non-demo) ───────────────────────
# Reported here, decided in the UI. What a publication covers is the whole
# question this install cannot answer — it has no table names on screen and
# no way to show what an existing one already targets. So a run that ends at
# the UI leaves the publication alone, and the UI creates it from what was
# chosen when the copy starts.
#
# An unattended run has nobody to ask, so it keeps the old behaviour: there
# the default is the only answer available.
if [ "$DEMO" = "0" ]; then
  PUB_EXISTS=$(psql "$CONNSTR" -Atc "SELECT count(*) FROM pg_publication WHERE pubname='$PUBLICATION_NAME'")
  if [ "$PUB_EXISTS" != "0" ]; then
    PUB_TABLES=$(psql "$CONNSTR" -Atc "SELECT count(*) FROM pg_publication_tables WHERE pubname='$PUBLICATION_NAME'" 2>/dev/null)
    info "publication $PUBLICATION_NAME already exists (${PUB_TABLES:-?} tables) — the UI shows what it covers"
  elif [ "$START_REPLICATION" = "1" ]; then
    info "creating publication $PUBLICATION_NAME (FOR ALL TABLES)..."
    psql "$CONNSTR" -q -c "CREATE PUBLICATION $PUBLICATION_NAME FOR ALL TABLES" \
      || die "could not create the publication — create it manually and re-run"
  else
    info "no publication yet — the UI will create it from what you choose"
  fi
fi

# ── 6. management plane ──────────────────────────────────────────────
ENV_FILE="$SNAP_HOME/deploy/.env"
# One backup, and only when there is something to back up. A timestamped copy
# per run left a growing pile inside the checkout — of files nobody reads,
# holding passwords, in a directory this script also asks git about.
cat > "$ENV_FILE.new" <<EOF
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
if [ -f "$ENV_FILE" ] && cmp -s "$ENV_FILE" "$ENV_FILE.new"; then
  rm -f "$ENV_FILE.new"
else
  [ -f "$ENV_FILE" ] && mv "$ENV_FILE" "$ENV_FILE.bak"
  mv "$ENV_FILE.new" "$ENV_FILE"
fi

info "starting the management plane (first build takes a few minutes)..."
( cd "$SNAP_HOME/deploy" && docker compose -p "$PROJECT" up -d --build )

for _ in $(seq 1 30); do
  curl -fsS "127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "127.0.0.1:$BACKEND_PORT/health" >/dev/null || die "backend did not become healthy — check: docker compose -p $PROJECT logs manager"

# ── 7. replica bootstrap ─────────────────────────────────────────────
# Not run here by default. The subscription's initial copy is the point of no
# return — what gets replicated is decided by the publication as it stands the
# moment that copy begins — and the installer is the wrong place to ask, with
# no table names on screen and a terminal that cannot show hundreds of them.
# So the install ends at the UI, where the choice is made, and the copy starts
# from there.
#
# START_REPLICATION=1 keeps the old end-to-end behaviour for unattended runs.
# --demo defaults to it: a seeded sample database has nothing to decide.
BOOTSTRAPPED=0
if docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null | grep -q true; then
  info "replica $CONTAINER_NAME already running — skipping bootstrap"
  BOOTSTRAPPED=1
elif [ "$START_REPLICATION" = "1" ]; then
  info "bootstrapping the replica (schema clone + subscription; may take a while)..."
  ( cd "$SNAP_HOME/deploy" && docker compose -p "$PROJECT" exec -T manager bash scripts/run-replica-postgres.sh </dev/null ) \
    || die "replica bootstrap failed — check $SNAP_HOME logs and re-run this installer"
  BOOTSTRAPPED=1
fi

# ── done ─────────────────────────────────────────────────────────────
# the machine's outbound source address (hostname -I can lead with a
# docker bridge IP, which is useless to the user)
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="src") print $(i+1)}' | head -1)
[ -n "$IP" ] || IP=$(hostname -I 2>/dev/null | awk '{print $1}')
info "done!"
echo
echo "  UI:        http://${IP:-<host>}:$WEB_PORT"
if [ "$BOOTSTRAPPED" = "1" ]; then
  echo "  replica:   postgres://$POSTGRES_USER:$POSTGRES_PASSWORD@${IP:-<host>}:$HOST_PORT/$POSTGRES_DB"
  echo "  clone one: curl -X POST 127.0.0.1:$BACKEND_PORT/clones -H 'Content-Type: application/json' -d '{}'"
else
  # No replica address to print: nothing has been copied yet, and saying
  # otherwise would be advertising a database that does not exist.
  echo "  next:      http://${IP:-<host>}:$WEB_PORT/replication — choose what to replicate, then start it"
  echo "             (unattended: re-run with START_REPLICATION=1 to replicate everything)"
fi
echo "  pool:      $ROOT_DATA_DIR"
echo
