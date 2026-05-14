#!/bin/bash
set -euo pipefail

# Initialize postgres_fdw on the replica from the SoT-rendered SQL.
# The SQL file is produced by backend/app/services/fdw.py from configs/fdw.yaml
# and is copied into /opt/replica-init/ by scripts/run-replica-postgres.sh.

SCRIPT_NAME=$(basename "$0")
LOG_PREFIX="[$SCRIPT_NAME]"
LOG_FILE=${LOG_FILE:-/var/lib/postgresql/replica-init.log}

if [[ "${TRACE:-0}" == "1" ]]; then
  PS4='+ ['"$SCRIPT_NAME"':${LINENO}] '
  set -x
fi

log() { echo "$(date -Is) $LOG_PREFIX $*" | tee -a "$LOG_FILE" >&2; }
trap 'rc=$?; log "ERROR at line ${LINENO}: ${BASH_COMMAND} (rc=$rc)"; exit $rc' ERR
log "START"

SQL_FILE=${FDW_SQL_FILE:-/opt/replica-init/fdw_setup.generated.sql}

if [ ! -f "$SQL_FILE" ]; then
  log "no $SQL_FILE present, skipping FDW setup (yaml-managed FDW not configured)"
  log "DONE"
  exit 0
fi

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"

# FDW connection target: FDW_HOST/PORT/DB if set (e.g. a readonly bastion),
# otherwise fall back to PRIMARY_HOST/PORT/DB (direct to the primary cluster).
EFF_FDW_HOST=${FDW_HOST:-${PRIMARY_HOST:-}}
EFF_FDW_PORT=${FDW_PORT:-${PRIMARY_PORT:-}}
EFF_FDW_DB=${FDW_DB:-${PRIMARY_DB:-}}

if [ -z "$EFF_FDW_HOST" ] || [ -z "$EFF_FDW_PORT" ] || [ -z "$EFF_FDW_DB" ]; then
  log "FDW host/port/db unresolved (set FDW_HOST/PORT/DB or PRIMARY_*); skipping FDW setup"
  log "DONE"
  exit 0
fi

# FDW credentials are separate from PRIMARY_* (typically a readonly role).
if [ -z "${FDW_USER:-}" ] || [ -z "${FDW_PASSWORD:-}" ]; then
  log "FDW_USER / FDW_PASSWORD not set in env; skipping FDW setup"
  log "DONE"
  exit 0
fi

log "applying $SQL_FILE (server=prod_fdw, target=$EFF_FDW_HOST:$EFF_FDW_PORT/$EFF_FDW_DB)"

psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 \
  -v primary_host="$EFF_FDW_HOST" \
  -v primary_port="$EFF_FDW_PORT" \
  -v primary_db="$EFF_FDW_DB" \
  -v fdw_user="$FDW_USER" \
  -v fdw_password="$FDW_PASSWORD" \
  -f "$SQL_FILE" 2>&1 | tee -a "$LOG_FILE"

log "DONE"
