#!/bin/bash
set -euo pipefail

# Trace & logging setup
SCRIPT_NAME=$(basename "$0")
LOG_PREFIX="[$SCRIPT_NAME]"
LOG_FILE=${LOG_FILE:-/var/lib/postgresql/replica-init.log}

# Enable bash trace when TRACE=1
if [[ "${TRACE:-0}" == "1" ]]; then
  PS4='+ ['"$SCRIPT_NAME"':${LINENO}] '
  set -x
fi

log() { echo "$(date -Is) $LOG_PREFIX $*" | tee -a "$LOG_FILE" >&2; }
trap 'rc=$?; log "ERROR at line ${LINENO}: ${BASH_COMMAND}"; exit $rc' ERR
log "START"

# Schema-only DDL 복제 스크립트 (컨테이너 최초 초기화 시 1회 실행)
# 실행 순서: 01_wait_for_db.sh 이후, 20_create_subscription.sh 이전

# 필수 환경변수 검증
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${PRIMARY_HOST:?PRIMARY_HOST is required}"
: "${PRIMARY_PORT:?PRIMARY_PORT is required}"
: "${PRIMARY_DB:?PRIMARY_DB is required}"
: "${PRIMARY_USER:?PRIMARY_USER is required}"
: "${PRIMARY_PASSWORD:?PRIMARY_PASSWORD is required}"
: "${PUBLICATION_NAME:?PUBLICATION_NAME is required}"

echo "Cloning schema (DDL) from primary ${PRIMARY_HOST}:${PRIMARY_PORT}/${PRIMARY_DB} -> ${POSTGRES_DB}"
log "Primary: ${PRIMARY_HOST}:${PRIMARY_PORT}/${PRIMARY_DB}, Publication: ${PUBLICATION_NAME}"

# Primary 접속 가능 대기 (최대 60초)
for i in {1..60}; do
  if pg_isready -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
  if [ $i -eq 60 ]; then
    echo "Primary is not ready, aborting schema clone"
    exit 1
  fi
done

# Publication 대상만 스키마/테이블 덤프하여 replica에 적용
# - publication에 포함된 테이블 목록을 조회해 해당 테이블 DDL만 덤프(-t)
# - 필요한 스키마는 선제 생성
export PGPASSWORD="$PRIMARY_PASSWORD"

# publication에 포함된 테이블 목록 (schema.table 형식, 식별자 안전 인용)
mapfile -t PUB_TABLES < <(
  psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" -At \
    -c "SELECT DISTINCT format('%I.%I', schemaname, tablename) FROM pg_publication_tables WHERE pubname = '$PUBLICATION_NAME';"
)

if [ "${#PUB_TABLES[@]}" -eq 0 ]; then
  echo "No tables found in publication '$PUBLICATION_NAME'. Skipping schema clone."
  log "No tables in publication; nothing to apply"
  unset PGPASSWORD
  exit 0
fi

# 필요한 스키마 목록
mapfile -t PUB_SCHEMAS < <(
  psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" -At \
    -c "SELECT DISTINCT quote_ident(schemaname) FROM pg_publication_tables WHERE pubname = '$PUBLICATION_NAME';"
)

# replica DB에 스키마 사전 생성
for s in "${PUB_SCHEMAS[@]}"; do
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE SCHEMA IF NOT EXISTS $s;"
done

# pg_dump 인자 구성 (-t로 publication 대상 테이블만)
DUMP_ARGS=(
  -h "$PRIMARY_HOST" -p "$PRIMARY_PORT"
  -U "$PRIMARY_USER" -d "$PRIMARY_DB"
  -s --no-owner --no-privileges
)
for t in "${PUB_TABLES[@]}"; do
  DUMP_ARGS+=( -t "$t" )
done

pg_dump "${DUMP_ARGS[@]}" | psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"

unset PGPASSWORD

echo "Schema clone completed"
log "DONE"