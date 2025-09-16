#!/bin/bash
set -euo pipefail

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

echo "Cloning schema (DDL) from primary ${PRIMARY_HOST}:${PRIMARY_PORT}/${PRIMARY_DB} -> ${POSTGRES_DB}"

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

# 스키마만 덤프하여 replica에 적용
# --no-owner/--no-privileges로 소유권/권한 관련 오류 최소화
export PGPASSWORD="$PRIMARY_PASSWORD"
pg_dump \
  -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" \
  -U "$PRIMARY_USER" -d "$PRIMARY_DB" \
  -s --no-owner --no-privileges \
  | psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"

unset PGPASSWORD

echo "Schema clone completed" 