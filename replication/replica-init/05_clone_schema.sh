#!/bin/bash
set -o errtrace
# set -euo pipefail

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
# Do not exit the entrypoint on ERR; just log it
trap 'rc=$?; log "ERROR at line ${LINENO}: ${BASH_COMMAND} (rc=$rc)"' ERR
log "START"

# Schema-only DDL 복제 스크립트 (컨테이너 최초 초기화 시 1회 실행)
# 20_create_subscription.sh 이전

# 필수 환경변수 검증
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${PRIMARY_HOST:?PRIMARY_HOST is required}"
: "${PRIMARY_PORT:?PRIMARY_PORT is required}"
: "${PRIMARY_DB:?PRIMARY_DB is required}"
: "${PRIMARY_USER:?PRIMARY_USER is required}"
: "${PRIMARY_PASSWORD:?PRIMARY_PASSWORD is required}"

echo "Cloning schema (DDL) from primary ${PRIMARY_HOST}:${PRIMARY_PORT}/${PRIMARY_DB} -> ${POSTGRES_DB}"
log "Primary: ${PRIMARY_HOST}:${PRIMARY_PORT}/${PRIMARY_DB}"

# Ensure subscriber database exists (some images may not create POSTGRES_DB at init)
log "Ensuring subscriber database exists: ${POSTGRES_DB}"
DB_EXISTS=$(psql -U "$POSTGRES_USER" -d postgres -At -c "SELECT 1 FROM pg_database WHERE datname='${POSTGRES_DB}'" 2>/dev/null || true)
if [ "$DB_EXISTS" != "1" ]; then
  psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"${POSTGRES_DB}\" OWNER \"${POSTGRES_USER}\" TEMPLATE template0;" >/dev/null 2>&1 || true
  DB_EXISTS=$(psql -U "$POSTGRES_USER" -d postgres -At -c "SELECT 1 FROM pg_database WHERE datname='${POSTGRES_DB}'" 2>/dev/null || true)
  if [ "$DB_EXISTS" = "1" ]; then
    log "Created database ${POSTGRES_DB}"
  else
    log "WARNING: Failed to ensure database ${POSTGRES_DB}; subsequent steps may fail"
  fi
fi

# Inherit-errexit guard: entrypoint runs with set -e; avoid unintended exits
_OLD_HAS_E=0; case $- in *e*) _OLD_HAS_E=1;; esac; set +e

# Primary 접속 가능 대기 (최대 60초)
for i in {1..60}; do
  READY=1
  if pg_isready -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" >/dev/null 2>&1; then
    READY=0
  else
    # pg_isready 실패 시 실제 쿼리로 재확인 (키=값 형태, 특수문자 안전)
    if PGPASSWORD="$PRIMARY_PASSWORD" PGSSLMODE="${PGSSLMODE:-prefer}" \
       psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" -At -c 'SELECT 1' >/dev/null 2>&1; then
      READY=0
    fi
  fi
  if [ $READY -eq 0 ]; then break; fi
  log "Primary is not ready, waiting for ${i} seconds"
  sleep 1
  if [ $i -eq 60 ]; then
    echo "Primary is not ready, skipping schema clone"
    log "Primary not ready after wait; skipping"
    return 0
  fi
done

# 전체 스키마를 schema-only로 덤프 후 replica에 적용
export PGPASSWORD="$PRIMARY_PASSWORD"

log "Primary is ready"
# Preflight: DNS & TCP connectivity to primary
log "DNS resolve for $PRIMARY_HOST"
RESOLVE_OUT=$(getent hosts "$PRIMARY_HOST" 2>&1 || true)
log "getent hosts -> ${RESOLVE_OUT:-<no output>}"
log "TCP check to $PRIMARY_HOST:$PRIMARY_PORT"
OLD_E_SET=0; case $- in *e*) OLD_E_SET=1;; esac; set +e
timeout 5 bash -lc "/bin/true < /dev/tcp/$PRIMARY_HOST/$PRIMARY_PORT" >/dev/null 2>&1
TCP_RC=$?
if [ $OLD_E_SET -eq 1 ]; then set -e; fi
if [ $TCP_RC -eq 0 ]; then
  log "TCP connectivity OK to $PRIMARY_HOST:$PRIMARY_PORT"
else
  log "TCP connectivity FAILED to $PRIMARY_HOST:$PRIMARY_PORT (rc=$TCP_RC)"
fi
log "Preparing to dump all schemas (schema-only) from primary"

# replica 측 선행 스키마 생성은 필요하지 않음 (pg_dump 출력에 포함)

# 확장/함수 선처리는 일반화 단계에서는 생략 (pg_dump 결과에 따라 적용)

# 확장 동기화는 생략 (필요 시 별도 스크립트에서 처리 권장)

## schema-only 전체 덤프 및 적용 (환경 호환적인 임시파일 생성)
# SSL 모드 기본값(필요 시 .env에서 PGSSLMODE 오버라이드)
export PGSSLMODE=${PGSSLMODE:-prefer}
# 로컬 psql 작업들은 타임아웃/락타임아웃 비활성화 (스키마 적용 시 장시간 작업 보호)
export PGOPTIONS="${PGOPTIONS:- -c statement_timeout=0 -c lock_timeout=0 }"
TMP_BASE=$(mktemp -t all_schema_XXXXXX 2>/dev/null || mktemp)
TMP_SQL="${TMP_BASE}.sql"
TMP_ERR="${TMP_BASE}.err"
: >"$TMP_SQL"; : >"$TMP_ERR"
log "Running pg_dump (schema-only)"
PGDUMP_ARGS=( -s --no-owner --no-privileges -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" )
# 선택 스키마 덤프: 환경변수 DUMP_SCHEMAS(콤마 구분) 지정 시 해당 스키마만
if [ -n "${DUMP_SCHEMAS:-}" ]; then
  IFS=',' read -r -a __SCHEMAS <<< "$DUMP_SCHEMAS"
  __LIST=()
  for __s in "${__SCHEMAS[@]}"; do
    __t=$(echo "${__s}" | awk '{$1=$1;print}')
    [ -z "$__t" ] && continue
    PGDUMP_ARGS+=( -n "$__t" )
    __LIST+=("$__t")
  done
  if [ ${#__LIST[@]} -gt 0 ]; then
    log "Limiting dump to schemas: ${__LIST[*]}"
  fi
fi
OLD_ERR_TRAP=$(trap -p ERR || true); trap - ERR; OLD_E=0; case $- in *e*) OLD_E=1;; esac; set +e
pg_dump "${PGDUMP_ARGS[@]}" 1>"$TMP_SQL" 2>"$TMP_ERR"
PG_DUMP_RC=$?
if [ $PG_DUMP_RC -ne 0 ]; then
  if [ -s "$TMP_ERR" ] && [ -f "$TMP_ERR" ]; then log "pg_dump stderr (head): $(head -n 20 "$TMP_ERR")"; fi
  log "pg_dump failed (rc=$PG_DUMP_RC). Aborting schema apply step."
  rm -f "$TMP_SQL" "$TMP_ERR" "$TMP_BASE"
  if [ $OLD_E -eq 1 ]; then set -e; fi; if [ -n "$OLD_ERR_TRAP" ]; then eval "$OLD_ERR_TRAP"; fi
  unset PGPASSWORD
  return 0
fi
if [ $OLD_E -eq 1 ]; then set -e; fi; if [ -n "$OLD_ERR_TRAP" ]; then eval "$OLD_ERR_TRAP"; fi

# 1) Primary의 확장 목록을 동기화 시도 (주요 확장은 덤프 -n 제한 시 누락될 수 있음)
log "Syncing extensions from primary (best-effort)"
_OLD_ERR_TRAP=$(trap -p ERR || true); trap - ERR; _OLD_E=0; case $- in *e*) _OLD_E=1;; esac; set +e
mapfile -t PRIMARY_EXTS < <(psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" -At \
  -c "select e.extname from pg_extension e where e.extname not in ('plpgsql')" 2>/dev/null || true)
if [ ${#PRIMARY_EXTS[@]} -gt 0 ]; then
  mapfile -t AVAIL_SUB < <(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select lower(name) from pg_available_extensions" 2>/dev/null || true)
  for ext in "${PRIMARY_EXTS[@]}"; do
    _ok=0; for a in "${AVAIL_SUB[@]}"; do [ "$a" = "$ext" ] && { _ok=1; break; }; done
    if [ $_ok -eq 1 ]; then
      __EXT_ERR=$(mktemp -t ext_sync_XXXX 2>/dev/null || mktemp)
      psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS \"$ext\";" 1>/dev/null 2>"$__EXT_ERR"
      rc=$?
      if [ $rc -eq 0 ]; then
        log "[ext-sync] ensured '$ext'"
      else
        if [ -s "$__EXT_ERR" ]; then
          log "[ext-sync] failed '$ext': $(head -n 5 "$__EXT_ERR" | tr '\n' ' ' | sed 's/  */ /g')"
        else
          log "[ext-sync] failed '$ext' with no stderr"
        fi
      fi
      rm -f "$__EXT_ERR"
    else
      log "[ext-sync] '$ext' not available on subscriber"
    fi
  done
fi
if [ $_OLD_E -eq 1 ]; then set -e; fi; if [ -n "$_OLD_ERR_TRAP" ]; then eval "$_OLD_ERR_TRAP"; fi

# 확장 처리: 설치 시도 후 실패하는 확장만 스킵
log "Processing extensions: try install, skip only failures"
_OLD_ERR_TRAP=$(trap -p ERR || true); trap - ERR; _OLD_E=0; case $- in *e*) _OLD_E=1;; esac; set +e
EXT_NAMES=()
while IFS= read -r name; do EXT_NAMES+=("$name"); done < <(grep -Eio "CREATE[[:space:]]+EXTENSION( IF NOT EXISTS)?[[:space:]]+\"?[A-Za-z0-9_]+\"?" "$TMP_SQL" | awk '{print tolower($NF)}' | sed 's/\"//g' || true)
while IFS= read -r name; do EXT_NAMES+=("$name"); done < <(grep -Eio "ALTER[[:space:]]+EXTENSION[[:space:]]+\"?[A-Za-z0-9_]+\"?" "$TMP_SQL" | awk '{print tolower($NF)}' | sed 's/\"//g' || true)
while IFS= read -r name; do EXT_NAMES+=("$name"); done < <(grep -Eio "COMMENT[[:space:]]+ON[[:space:]]+EXTENSION[[:space:]]+\"?[A-Za-z0-9_]+\"?" "$TMP_SQL" | awk '{print tolower($NF)}' | sed 's/\"//g' || true)
readarray -t SQL_EXTS < <(printf '%s\n' "${EXT_NAMES[@]}" | awk 'NF' | sort -u)
if [ ${#SQL_EXTS[@]} -gt 0 ]; then
  mapfile -t AVAIL_EXTS < <(psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "select lower(name) from pg_available_extensions" 2>/dev/null || true)
  for ext in "${SQL_EXTS[@]}"; do
    # 가용하면 설치 시도
    available=0
    for a in "${AVAIL_EXTS[@]}"; do [ "$a" = "$ext" ] && { available=1; break; }; done
    if [ $available -eq 1 ]; then
      psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS \"$ext\";" >/dev/null 2>&1
      rc=$?
      if [ $rc -ne 0 ]; then
        log "Extension '$ext' install failed; commenting out its statements"
        sed -i -E "s/^(CREATE[[:space:]]+EXTENSION( IF NOT EXISTS)?[[:space:]]+\"?$ext\"?)/-- skipped: \1/Ig" "$TMP_SQL"
        sed -i -E "s/^(ALTER[[:space:]]+EXTENSION[[:space:]]+\"?$ext\"?)/-- skipped: \1/Ig" "$TMP_SQL"
        sed -i -E "s/^(COMMENT[[:space:]]+ON[[:space:]]+EXTENSION[[:space:]]+\"?$ext\"?)/-- skipped: \1/Ig" "$TMP_SQL"
      else
        log "Extension '$ext' ensured"
      fi
    else
      log "Extension '$ext' not available; commenting out its statements"
      sed -i -E "s/^(CREATE[[:space:]]+EXTENSION( IF NOT EXISTS)?[[:space:]]+\"?$ext\"?)/-- skipped: \1/Ig" "$TMP_SQL"
      sed -i -E "s/^(ALTER[[:space:]]+EXTENSION[[:space:]]+\"?$ext\"?)/-- skipped: \1/Ig" "$TMP_SQL"
      sed -i -E "s/^(COMMENT[[:space:]]+ON[[:space:]]+EXTENSION[[:space:]]+\"?$ext\"?)/-- skipped: \1/Ig" "$TMP_SQL"
    fi
  done
fi
if [ $_OLD_E -eq 1 ]; then set -e; fi; if [ -n "$_OLD_ERR_TRAP" ]; then eval "$_OLD_ERR_TRAP"; fi

# CREATE SCHEMA public; → IF NOT EXISTS로 치환 (이미 존재 시 오류 회피)
sed -i -E 's/^(CREATE[[:space:]]+SCHEMA[[:space:]]+)public;$/\1IF NOT EXISTS public;/I' "$TMP_SQL"

log "Applying schema to replica database"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$TMP_SQL" 1>/dev/null 2>>"$TMP_ERR"
PSQL_APPLY_RC=$?
if [ $PSQL_APPLY_RC -ne 0 ]; then
  log "psql apply failed (rc=$PSQL_APPLY_RC)"
  if [ -s "$TMP_ERR" ] && [ -f "$TMP_ERR" ]; then log "psql stderr (tail): $(tail -n 30 "$TMP_ERR")"; fi
else
  log "Schema apply completed successfully"
fi
rm -f "$TMP_SQL" "$TMP_ERR" "$TMP_BASE"

:

unset PGPASSWORD

# Restore inherited errexit if it was originally set
if [ $_OLD_HAS_E -eq 1 ]; then set -e; fi

echo "Schema clone completed"
log "DONE"