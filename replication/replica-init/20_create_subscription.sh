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

echo "Creating logical replication subscription..."
log "Creating subscription: ${SUBSCRIPTION_NAME} to publication ${PUBLICATION_NAME} on ${PRIMARY_HOST}:${PRIMARY_PORT}/${PRIMARY_DB}"

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

# 간결화: 사전 테이블 검사 제거

# 연결 문자열 구성: URI가 아닌 libpq key=value 형태로 강제(특수문자 안전)
# NOTE: password에 특수문자가 포함되어도 key=value 형태는 인코딩이 필요 없음
SSL_MODE=${PGSSLMODE:-prefer}
# 세션 레벨 타임아웃을 퍼블리셔 연결에 함께 전달해 lock timeout 회피
CONNSTR="host=${PRIMARY_HOST} port=${PRIMARY_PORT} dbname=${PRIMARY_DB} user=${PRIMARY_USER} password=${PRIMARY_PASSWORD} sslmode=${SSL_MODE} target_session_attrs=read-write options='-c lock_timeout=0 -c statement_timeout=0'"

# 리플리케이션 연결 GUC 검증: publisher에 실제로 반영되는지 확인(프록시 환경 진단)
PUB_STMT_TO=$(psql -At "$CONNSTR" -c "SHOW statement_timeout" 2>/dev/null || true)
if [ -n "$PUB_STMT_TO" ]; then
  log "Publisher SHOW statement_timeout -> $PUB_STMT_TO"
  if [ "$PUB_STMT_TO" != "0" ] && [ "$PUB_STMT_TO" != "0ms" ]; then
    log "WARNING: statement_timeout is not 0 on publisher session; options parameter may be ignored by proxy."
  fi
else
  log "WARNING: could not verify publisher session GUCs via connstr"
fi

# 슬롯 이름 결정: PRECREATED_SLOT_NAME 우선, 없으면 기존 규칙으로 생성
export PGPASSWORD="$PRIMARY_PASSWORD"
PRECREATED_SLOT_NAME=${PRECREATED_SLOT_NAME:-}
CREATE_SLOT_FLAG="true"
sanitize_slot() {
  local raw="$1"; local low="${raw,,}"; local safe
  safe=$(echo "$low" | sed -E 's/[^a-z0-9_]+/_/g' | sed -E 's/^_+|_+$//g')
  echo "${safe:0:63}"
}
if [ -n "$PRECREATED_SLOT_NAME" ]; then
  CHOSEN_SLOT_NAME=$(sanitize_slot "$PRECREATED_SLOT_NAME")
  CREATE_SLOT_FLAG="false"
  log "Using pre-created slot from env: $CHOSEN_SLOT_NAME (create_slot=false)"
else
  RAW_BASE="${SLOT_NAME:-$SUBSCRIPTION_NAME}"
  LOW_BASE="${RAW_BASE,,}"
  SAFE_BASE=$(echo "$LOW_BASE" | sed -E 's/[^a-z0-9_]+/_/g' | sed -E 's/^_+|_+$//g')
  BASE_TRIM=${SAFE_BASE:0:45}
  CHOSEN_SLOT_NAME="$BASE_TRIM"
  for _i in 1 2 3 4 5; do
    EXISTS=$(psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" -At \
      -c "SELECT 1 FROM pg_replication_slots WHERE slot_name = '$CHOSEN_SLOT_NAME'" 2>/dev/null || true)
    if [ "$EXISTS" != "1" ]; then break; fi
    SUFFIX="_$(date +%s)_$RANDOM"
    MAX_BASE_LEN=$((63 - ${#SUFFIX}))
    [ $MAX_BASE_LEN -lt 1 ] && MAX_BASE_LEN=1
    CHOSEN_SLOT_NAME="${BASE_TRIM:0:$MAX_BASE_LEN}${SUFFIX}"
  done
  log "Using replication slot name: $CHOSEN_SLOT_NAME (create_slot=true)"
fi

# 공통 SQL 본문(변수는 psql -v로 바인딩)
create_sql() {
  cat <<'SQL'
\set connection_string :connstr
CREATE SUBSCRIPTION :subscription_name 
  CONNECTION :'connection_string' 
  PUBLICATION :publication_name 
  WITH (slot_name = :'slot_name', copy_data = true, create_slot = :create_slot);
SQL
}

# 이미 구독이 존재하면 종료
if psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT 1 FROM pg_subscription WHERE subname = '$SUBSCRIPTION_NAME'" | grep -q 1; then
  echo "Subscription '$SUBSCRIPTION_NAME' already exists. Skipping."
  exit 0
fi

# create_slot=true로 구독 생성 (퍼블리셔가 슬롯을 직접 생성)
set +e
TMP_BASE=$(mktemp -t sub_create_XXXXXX 2>/dev/null || mktemp)
SQL_FILE="${TMP_BASE}.sql"
create_sql > "$SQL_FILE"
log "Creating subscription with create_slot=${CREATE_SLOT_FLAG} and slot: $CHOSEN_SLOT_NAME"
OLD_ERR_TRAP=$(trap -p ERR || true); trap - ERR
psql -v ON_ERROR_STOP=1 \
     -U "$POSTGRES_USER" \
     -d "$POSTGRES_DB" \
     -v subscription_name="$SUBSCRIPTION_NAME" \
     -v slot_name="$CHOSEN_SLOT_NAME" \
     -v connstr="$CONNSTR" \
     -v publication_name="$PUBLICATION_NAME" \
     -v create_slot="$CREATE_SLOT_FLAG" \
     -f "$SQL_FILE"
rc=$?
if [ -n "$OLD_ERR_TRAP" ]; then eval "$OLD_ERR_TRAP"; fi
if [ $rc -ne 0 ]; then
  rm -f "$SQL_FILE" "$TMP_BASE"
  set -e
  echo "Failed to create subscription"
  exit 1
fi
rm -f "$SQL_FILE" "$TMP_BASE"
set -e
echo "Subscription setup completed"
log "DONE"
exit 0
