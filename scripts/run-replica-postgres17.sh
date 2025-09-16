#!/bin/bash
set -euo pipefail

# .env 파일 로드 (있으면 값 주입)
ENV_FILE=${ENV_FILE:-"$(cd "$(dirname "$0")/.." && pwd)/.env"}
if [ -f "$ENV_FILE" ]; then
  set -a  # 모든 변수를 자동으로 export
  source "$ENV_FILE"
  set +a  # export 자동화 해제
  echo "Loaded config from $ENV_FILE"
fi

# 필수 환경변수 검증 (모든 값은 .env에서 제공되어야 함)
: "${CONTAINER_NAME:?CONTAINER_NAME is required}"
: "${NETWORK_NAME:?NETWORK_NAME is required}"
: "${HOST_PORT:?HOST_PORT is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${PRIMARY_HOST:?PRIMARY_HOST is required}"
: "${PRIMARY_PORT:?PRIMARY_PORT is required}"
: "${PRIMARY_DB:?PRIMARY_DB is required}"
: "${PRIMARY_USER:?PRIMARY_USER is required}"
: "${PRIMARY_PASSWORD:?PRIMARY_PASSWORD is required}"
: "${SUBSCRIPTION_NAME:?SUBSCRIPTION_NAME is required}"
: "${PUBLICATION_NAME:?PUBLICATION_NAME is required}"

# 네트워크 준비
if ! docker network ls | grep -q "${NETWORK_NAME}"; then
  docker network create "${NETWORK_NAME}"
fi

# 기존 컨테이너 정리
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
fi

# 볼륨 마운트 설정
BASE_DIR=$(cd "$(dirname "$0")/.." && pwd)
VOLUME_ARGS=( -v "$BASE_DIR/replication/replica-init:/docker-entrypoint-initdb.d:ro" )

# 컨테이너 실행 (postgres:17)
docker run -d \
  --name "${CONTAINER_NAME}" \
  --network "${NETWORK_NAME}" \
  -p "${HOST_PORT}:5432" \
  --env-file "${ENV_FILE}" \
  "${VOLUME_ARGS[@]}" \
  postgres:17

# 준비 대기 및 상태 출력
echo "Waiting for PostgreSQL to become ready..."
for i in {1..60}; do
  if docker exec "${CONTAINER_NAME}" pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; then
    echo "Replica ready on port ${HOST_PORT}"
    docker logs "${CONTAINER_NAME}" --tail 50
    exit 0
  fi
  sleep 1
done

echo "Timeout waiting for replica container to be ready"
docker logs "${CONTAINER_NAME}" --tail 200
exit 1
