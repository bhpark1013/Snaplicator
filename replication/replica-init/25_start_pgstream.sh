#!/bin/bash
set -euo pipefail

# Trace & logging setup
SCRIPT_NAME=$(basename "$0")
LOG_PREFIX="[$SCRIPT_NAME]"
LOG_FILE=${LOG_FILE:-/var/lib/postgresql/replica-init.log}

if [[ "${TRACE:-0}" == "1" ]]; then
  PS4='+ ['"$SCRIPT_NAME"':${LINENO}] '
  set -x
fi

log() { echo "$(date -Is) $LOG_PREFIX $*" | tee -a "$LOG_FILE" >&2; }
trap 'rc=$?; log "ERROR at line ${LINENO}: ${BASH_COMMAND}"; exit $rc' ERR
log "START"

echo "Starting pgstream replication..."

# 필수 환경변수 검증
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${PRIMARY_HOST:?PRIMARY_HOST is required}"
: "${PRIMARY_PORT:?PRIMARY_PORT is required}"
: "${PRIMARY_DB:?PRIMARY_DB is required}"
: "${PRIMARY_USER:?PRIMARY_USER is required}"
: "${PRIMARY_PASSWORD:?PRIMARY_PASSWORD is required}"

# pgstream 관련 변수
PGSTREAM_SLOT_NAME=${PGSTREAM_SLOT_NAME:-pgstream_slot}
PGSTREAM_LOG_LEVEL=${PGSTREAM_LOG_LEVEL:-info}
PGSTREAM_CONFIG_FILE=${PGSTREAM_CONFIG_FILE:-/var/lib/postgresql/pgstream.env}
PGSTREAM_LOG_FILE=${PGSTREAM_LOG_FILE:-/var/lib/postgresql/pgstream.log}

# Primary 연결 문자열 구성
SSL_MODE=${PGSSLMODE:-disable}
PRIMARY_URL="postgresql://${PRIMARY_USER}:${PRIMARY_PASSWORD_URLENCODED:-$PRIMARY_PASSWORD}@${PRIMARY_HOST}:${PRIMARY_PORT}/${PRIMARY_DB}?sslmode=${SSL_MODE}"
REPLICA_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD:-$POSTGRES_USER}@127.0.0.1:5432/${POSTGRES_DB}?sslmode=disable"

log "Primary: ${PRIMARY_HOST}:${PRIMARY_PORT}/${PRIMARY_DB}"
log "Replica: 127.0.0.1:5432/${POSTGRES_DB}"

# IMPORTANT: Disable FK constraints on replica for incremental replication
# This is required because replica has schema but no data, so FK constraints would fail
log "Setting session_replication_role='replica' on database (disables FK constraints)"
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER DATABASE $POSTGRES_DB SET session_replication_role = 'replica';" 2>/dev/null || true

# pgstream 설치 확인
if ! command -v pgstream &> /dev/null; then
  log "pgstream not found, installing..."
  # Install wget if not available
  if ! command -v wget &> /dev/null; then
    apk add --no-cache wget >/dev/null 2>&1 || true
  fi
  wget -q -O /tmp/pgstream "https://github.com/xataio/pgstream/releases/download/v0.9.6/pgstream.linux.amd64"
  chmod +x /tmp/pgstream
  mv /tmp/pgstream /usr/local/bin/
  log "pgstream installed"
fi

# Replica에 이미 데이터가 있는지 확인 (테이블 수 체크)
TABLE_COUNT=$(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE';" 2>/dev/null || echo "0")

log "Replica table count: $TABLE_COUNT"

# pgstream 설정 파일 생성
log "Creating pgstream config: $PGSTREAM_CONFIG_FILE"
cat > "$PGSTREAM_CONFIG_FILE" << ENVEOF
# Listener (Publisher)
PGSTREAM_POSTGRES_LISTENER_URL=${PRIMARY_URL}
PGSTREAM_POSTGRES_REPLICATION_SLOT_NAME=${PGSTREAM_SLOT_NAME}
PGSTREAM_POSTGRES_SNAPSHOT_STORE_URL=${PRIMARY_URL}

# Processor (Replica)
PGSTREAM_POSTGRES_WRITER_TARGET_URL=${REPLICA_URL}
PGSTREAM_POSTGRES_WRITER_SCHEMALOG_STORE_URL=${PRIMARY_URL}
PGSTREAM_INJECTOR_STORE_POSTGRES_URL=${PRIMARY_URL}
ENVEOF

# pgstream init (이미 초기화되어 있으면 스킵)
log "Checking pgstream initialization on publisher..."
export PGPASSWORD="$PRIMARY_PASSWORD"
SLOT_EXISTS=$(psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" -At \
  -c "SELECT 1 FROM pg_replication_slots WHERE slot_name = '${PGSTREAM_SLOT_NAME}'" 2>/dev/null || echo "0")

if [ "$SLOT_EXISTS" != "1" ]; then
  log "Creating replication slot on publisher: $PGSTREAM_SLOT_NAME"
  psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" \
    -c "SELECT pg_create_logical_replication_slot('${PGSTREAM_SLOT_NAME}', 'wal2json');" 2>/dev/null || true
  log "Replication slot created"
else
  log "pgstream slot already exists: $PGSTREAM_SLOT_NAME"
fi
unset PGPASSWORD

# pgstream 실행 결정 (스키마만 있는 경우 초기 스냅샷 불필요 - incremental만 진행)
log "Replica has schema ( tables), running pgstream in incremental mode"

# 기존 pgstream 프로세스 종료
pkill -f "pgstream run" 2>/dev/null || true
sleep 2

# pgstream 백그라운드 실행
log "Starting pgstream..."
nohup pgstream run --config "$PGSTREAM_CONFIG_FILE" --log-level "$PGSTREAM_LOG_LEVEL" > "$PGSTREAM_LOG_FILE" 2>&1 &
PGSTREAM_PID=$!
log "pgstream started with PID: $PGSTREAM_PID"

# 잠시 대기 후 상태 확인
sleep 5
if ps -p $PGSTREAM_PID > /dev/null 2>&1; then
  log "pgstream is running"
  tail -20 "$PGSTREAM_LOG_FILE" | while read line; do log "pgstream: $line"; done
else
  log "WARNING: pgstream may have stopped. Check $PGSTREAM_LOG_FILE"
  tail -50 "$PGSTREAM_LOG_FILE" | while read line; do log "pgstream: $line"; done
fi

echo "pgstream replication started"
log "DONE"
