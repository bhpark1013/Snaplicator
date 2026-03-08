#!/bin/bash
set -euo pipefail

SCRIPT_NAME=$(basename "$0")
LOG_PREFIX="[$SCRIPT_NAME]"
LOG_FILE=${LOG_FILE:-/var/lib/postgresql/replica-init.log}

log() { echo "$(date -Is) $LOG_PREFIX $*" | tee -a "$LOG_FILE" >&2; }
log "START"

echo "Installing PostgreSQL extensions (hll, vector)..."

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"

# Check if extensions already installed
HLL_INSTALLED=$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT 1 FROM pg_extension WHERE extname='hll'" 2>/dev/null || echo "0")
VECTOR_INSTALLED=$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "SELECT 1 FROM pg_extension WHERE extname='vector'" 2>/dev/null || echo "0")

if [ "$HLL_INSTALLED" = "1" ] && [ "$VECTOR_INSTALLED" = "1" ]; then
  log "Extensions already installed, skipping"
  log "DONE"
  exit 0
fi

# Update apk and install build dependencies
log "Installing build dependencies..."
apk update >/dev/null 2>&1
apk add --no-cache git build-base postgresql-dev clang20 >/dev/null 2>&1 || true

# Create clang-19 symlinks (required by postgres build system)
ln -sf /usr/bin/clang-20 /usr/bin/clang-19 2>/dev/null || true
ln -sf /usr/bin/clang++-20 /usr/bin/clang++-19 2>/dev/null || true

# Build and install HLL extension
if [ "$HLL_INSTALLED" != "1" ]; then
  log "Building HLL extension..."
  cd /tmp && rm -rf postgresql-hll
  git clone --depth 1 https://github.com/citusdata/postgresql-hll.git >/dev/null 2>&1
  cd postgresql-hll
  make PG_CONFIG=/usr/local/bin/pg_config USE_PGXS=1 >/dev/null 2>&1 || true
  make install PG_CONFIG=/usr/local/bin/pg_config USE_PGXS=1 >/dev/null 2>&1 || true
  log "HLL extension installed"
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS hll;" 2>/dev/null || true
  rm -rf /tmp/postgresql-hll
fi

# Build and install pgvector extension
if [ "$VECTOR_INSTALLED" != "1" ]; then
  log "Building pgvector extension..."
  cd /tmp && rm -rf pgvector
  git clone --depth 1 --branch v0.8.0 https://github.com/pgvector/pgvector.git >/dev/null 2>&1
  cd pgvector
  make PG_CONFIG=/usr/local/bin/pg_config >/dev/null 2>&1 || true
  make install PG_CONFIG=/usr/local/bin/pg_config >/dev/null 2>&1 || true
  log "pgvector extension installed"
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null || true
  rm -rf /tmp/pgvector
fi

# Verify
log "Verifying extensions..."
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT extname, extversion FROM pg_extension WHERE extname IN ('hll', 'vector');" 2>/dev/null || true

log "DONE"
