#!/usr/bin/env bash
# Build a replica image that fits the primary's schema.
#
#   deploy/build-replica-image.sh "postgres://user:pw@primary:5432/db"
#
# Reads the extensions the primary actually has, works out which ones the base
# image is missing, and installs them — rather than letting the schema clone
# skip whatever will not build. The generated image name is printed on stdout,
# so a caller can do:
#
#   POSTGRES_IMAGE=$(deploy/build-replica-image.sh "$CONNSTR")
#
# Why an image and not `docker exec apt-get install` into the running replica:
# clones are separate containers started from POSTGRES_IMAGE. A package
# installed by hand into the replica is absent from every clone, so the replica
# would read the data fine and each clone would fail on the same columns —
# and bootstrap recreates the replica container anyway, taking it with it.
#
# Env:
#   BASE_IMAGE   override the base (default: postgres:<primary's major>)
#   IMAGE_NAME   override the output tag
#   EXTRA_PKGS   extra apt packages, space separated

set -euo pipefail

info() { printf '\033[1;32m[build-image]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[build-image] WARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[build-image] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

CONNSTR=${1:-${CONNSTR:-}}
[ -n "$CONNSTR" ] || die "usage: $0 <publisher-connstr>"
command -v psql >/dev/null 2>&1 || die "psql is required"
command -v docker >/dev/null 2>&1 || die "docker is required"

# ── what the primary actually runs and uses ──────────────────────────
SRC=$(PGCONNECT_TIMEOUT=15 psql "$CONNSTR" -Atc \
  "SELECT extname FROM pg_extension WHERE extname <> 'plpgsql' ORDER BY 1") \
  || die "could not read extensions from the primary"
MAJOR=$(PGCONNECT_TIMEOUT=15 psql "$CONNSTR" -Atc "SHOW server_version" | sed 's/\..*//') \
  || die "could not read server_version from the primary"
[ -n "$MAJOR" ] || die "could not determine the primary's major version"

BASE_IMAGE=${BASE_IMAGE:-postgres:$MAJOR}

# A package is named for the major of the installation it lands in, not the
# primary's. Those are the same only when the base image was defaulted above;
# a caller that pins one (install.sh passes POSTGRES_IMAGE) can hand us any
# major, and naming the packages after the primary then puts the .so under a
# path the running server never reads.
BASE_MAJOR=$(docker run --rm --entrypoint sh "$BASE_IMAGE" -c 'pg_config --version' 2>/dev/null \
  | sed -n 's/^PostgreSQL \([0-9]*\).*/\1/p')
[ -n "$BASE_MAJOR" ] || die "could not read the PostgreSQL major version of $BASE_IMAGE"

info "primary is PostgreSQL $MAJOR; base image $BASE_IMAGE (PostgreSQL $BASE_MAJOR)"
if [ "$BASE_MAJOR" != "$MAJOR" ]; then
  warn "the replica would run PostgreSQL $BASE_MAJOR against a PostgreSQL $MAJOR primary."
  warn "Logical replication permits it, but a replica meant to stand in for the"
  warn "primary should match it. Pass BASE_IMAGE=postgres:$MAJOR to line them up."
fi
if [ -z "$SRC" ]; then
  info "the primary uses no extensions beyond plpgsql — $BASE_IMAGE is enough"
  printf '%s\n' "$BASE_IMAGE"
  exit 0
fi
info "primary uses: $(printf '%s ' $SRC)"

# ── which of them the base image already carries ─────────────────────
# Asked of the image rather than assumed: contrib moves between releases, and
# a wrong assumption here is exactly the silent gap this script exists to close.
HAVE=$(docker run --rm --entrypoint sh "$BASE_IMAGE" -c \
  'ls "$(pg_config --sharedir)"/extension/*.control 2>/dev/null' \
  | sed 's#.*/##; s#\.control$##') || die "could not inspect $BASE_IMAGE"

NEED=""
for ext in $SRC; do
  printf '%s\n' "$HAVE" | grep -qxF "$ext" || NEED="$NEED $ext"
done
NEED=$(printf '%s' "$NEED" | xargs || true)

if [ -z "$NEED" ] && [ -z "${EXTRA_PKGS:-}" ]; then
  info "$BASE_IMAGE already carries everything — nothing to build"
  printf '%s\n' "$BASE_IMAGE"
  exit 0
fi
info "missing from the base image: $NEED"

# ── resolve extension names to PGDG package names ────────────────────
# The official image installs PostgreSQL from apt.postgresql.org, so that
# repository is already configured and most extensions are one apt away. The
# package name usually follows the extension name, but not always — so each
# candidate is put to apt rather than guessed and hoped for.
RESOLVER='
set -e
apt-get update -qq >/dev/null 2>&1 || true
for ext in $EXTS; do
  alias=""
  case "$ext" in
    vector)        alias="pgvector" ;;
    postgis)       alias="postgis-3" ;;
    pg_cron)       alias="cron" ;;
    pg_partman)    alias="partman" ;;
    pg_repack)     alias="repack" ;;
    pg_similarity) alias="similarity" ;;
    pg_squeeze)    alias="squeeze" ;;
    pgaudit)       alias="pgaudit" ;;
  esac
  found=""
  for cand in $alias "$ext" "$(printf %s "$ext" | tr _ -)" "$(printf %s "$ext" | sed s/^pg_//)"; do
    [ -n "$cand" ] || continue
    pkg="postgresql-$PGMAJOR-$cand"
    if apt-cache policy "$pkg" 2>/dev/null | grep -q "Candidate: [^(]"; then
      found=$pkg; break
    fi
  done
  if [ -n "$found" ]; then echo "OK $ext $found"; else echo "NOPKG $ext"; fi
done
'
RES=$(docker run --rm --entrypoint sh -e EXTS="$NEED" -e PGMAJOR="$BASE_MAJOR" \
  "$BASE_IMAGE" -c "$RESOLVER") || die "could not resolve packages inside $BASE_IMAGE"

PKGS=""
UNRESOLVED=""
while IFS=' ' read -r status ext pkg; do
  case "$status" in
    OK)    PKGS="$PKGS $pkg"; info "  $ext -> $pkg" ;;
    NOPKG) UNRESOLVED="$UNRESOLVED $ext"; warn "  $ext -> no PGDG package found" ;;
  esac
done <<EOF
$RES
EOF

if [ -n "$UNRESOLVED" ]; then
  warn "no package for:$UNRESOLVED"
  warn "These need a package name or a source build. Pass one with EXTRA_PKGS=,"
  warn "or point BASE_IMAGE at an image that already carries them."
  [ "${IGNORE_UNRESOLVED:-0}" = "1" ] || die "refusing to build an image that still would not fit"
fi

PKGS=$(printf '%s %s' "$PKGS" "${EXTRA_PKGS:-}" | xargs || true)
[ -n "$PKGS" ] || die "nothing to install after resolution"

# ── build ────────────────────────────────────────────────────────────
TAG=${IMAGE_NAME:-snaplicator-postgres:$BASE_MAJOR-$(printf '%s' "$PKGS" | shasum | cut -c1-8)}
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT
cat > "$BUILD_DIR/Dockerfile" <<EOF
FROM $BASE_IMAGE
RUN apt-get update \\
 && apt-get install -y --no-install-recommends $PKGS \\
 && rm -rf /var/lib/apt/lists/*
EOF
info "building $TAG"
docker build -q -t "$TAG" "$BUILD_DIR" >/dev/null || die "docker build failed"

# ── verify, because building is not the same as fitting ──────────────
# The whole point of this script is that a missing extension is silent later.
# Trusting the build to have worked would put that silence right back.
BUILT=$(docker run --rm --entrypoint sh "$TAG" -c \
  'ls "$(pg_config --sharedir)"/extension/*.control 2>/dev/null' \
  | sed 's#.*/##; s#\.control$##')
STILL=""
for ext in $SRC; do
  printf '%s\n' "$BUILT" | grep -qxF "$ext" || STILL="$STILL $ext"
done
if [ -n "$STILL" ]; then
  die "$TAG was built but still does not carry:$STILL"
fi

info "verified: $TAG carries all $(printf '%s\n' $SRC | wc -l | xargs) extension(s) the primary uses"
printf '%s\n' "$TAG"
