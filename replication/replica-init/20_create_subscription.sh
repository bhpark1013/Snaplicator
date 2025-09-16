#!/bin/bash
set -euo pipefail

echo "Creating logical replication subscription..."

# 필수 환경변수 검증
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${SUBSCRIPTION_NAME:?SUBSCRIPTION_NAME is required}"
: "${PRIMARY_HOST:?PRIMARY_HOST is required}"
: "${PRIMARY_PORT:?PRIMARY_PORT is required}"
: "${PRIMARY_DB:?PRIMARY_DB is required}"
: "${PRIMARY_USER:?PRIMARY_USER is required}"
: "${PRIMARY_PASSWORD:?PRIMARY_PASSWORD is required}"
: "${PUBLICATION_NAME:?PUBLICATION_NAME is required}"

# 이미 구독이 존재하면 종료 (구독 테이블 확인)
if psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT 1 FROM pg_subscription WHERE subname = '$SUBSCRIPTION_NAME'" | grep -q 1; then
  echo "Subscription '$SUBSCRIPTION_NAME' already exists. Skipping."
  exit 0
fi

# 공통 SQL 본문(변수는 psql -v로 바인딩)
create_sql() {
  cat <<'SQL'
\set connection_string 'host=' :primary_host ' port=' :primary_port ' dbname=' :primary_db ' user=' :primary_user ' password=' :primary_password
CREATE SUBSCRIPTION :subscription_name 
  CONNECTION :'connection_string' 
  PUBLICATION :publication_name 
  WITH (copy_data = true, create_slot = :create_slot);
SQL
}

# 1차 시도: create_slot = true
set +e
psql -v ON_ERROR_STOP=1 \
     -U "$POSTGRES_USER" \
     -d "$POSTGRES_DB" \
     -v subscription_name="$SUBSCRIPTION_NAME" \
     -v primary_host="$PRIMARY_HOST" \
     -v primary_port="$PRIMARY_PORT" \
     -v primary_db="$PRIMARY_DB" \
     -v primary_user="$PRIMARY_USER" \
     -v primary_password="$PRIMARY_PASSWORD" \
     -v publication_name="$PUBLICATION_NAME" \
     -v create_slot="true" \
     -f <(create_sql)
rc=$?
set -e

if [ $rc -ne 0 ]; then
  echo "First attempt failed, retrying with create_slot=false (slot may already exist on publisher)"
  psql -v ON_ERROR_STOP=1 \
       -U "$POSTGRES_USER" \
       -d "$POSTGRES_DB" \
       -v subscription_name="$SUBSCRIPTION_NAME" \
       -v primary_host="$PRIMARY_HOST" \
       -v primary_port="$PRIMARY_PORT" \
       -v primary_db="$PRIMARY_DB" \
       -v primary_user="$PRIMARY_USER" \
       -v primary_password="$PRIMARY_PASSWORD" \
       -v publication_name="$PUBLICATION_NAME" \
       -v create_slot="false" \
       -f <(create_sql)
fi

echo "Subscription setup completed"
