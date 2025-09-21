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
: "${ROOT_DATA_DIR:?ROOT_DATA_DIR is required}"
: "${MAIN_DATA_DIR:?MAIN_DATA_DIR is required}"

# btrfs 및 서브볼륨 프리플라이트 체크
ROOT_PATH="${ROOT_DATA_DIR%/}"
MAIN_PATH="$ROOT_PATH/${MAIN_DATA_DIR}"

# ROOT_DATA_DIR 준비
if [ ! -d "$ROOT_PATH" ]; then
  echo "Preparing ROOT_DATA_DIR: $ROOT_PATH"
  sudo mkdir -p "$ROOT_PATH"
fi

# 파일시스템 타입 확인 (btrfs 권장)
FSTYPE=""
if command -v findmnt >/dev/null 2>&1; then
  FSTYPE=$(findmnt -no FSTYPE -T "$ROOT_PATH" || true)
fi
if [ -z "$FSTYPE" ]; then
  # fallback
  FSTYPE=$(stat -f -c %T "$ROOT_PATH" 2>/dev/null || true)
fi

BTRFS_AVAILABLE=1
if [ "$FSTYPE" != "btrfs" ]; then
  echo "[WARN] ROOT_DATA_DIR is not on btrfs (detected: '$FSTYPE'). Snapshots won't be available."
  read -r -p "Proceed without btrfs snapshot support? [y/N] " ans
  case "${ans,,}" in
    y|yes) BTRFS_AVAILABLE=0 ;;
    *) echo "Aborting."; exit 1 ;;
  esac
fi

# MAIN_DATA_DIR 서브볼륨 확인/처리
if sudo btrfs subvolume show "$MAIN_PATH" >/dev/null 2>&1; then
  echo "MAIN_DATA_DIR subvolume exists: $MAIN_PATH"
else
  if [ -d "$MAIN_PATH" ]; then
    if [ -z "$(ls -A "$MAIN_PATH" 2>/dev/null || true)" ]; then
      echo "Empty directory detected at $MAIN_PATH"
      if [ "$BTRFS_AVAILABLE" -eq 1 ]; then
        read -r -p "Convert to btrfs subvolume now? [Y/n] " ans
        case "${ans,,}" in
          n|no) echo "Keeping as plain directory (no subvolume)." ;;
          *)
            sudo rmdir "$MAIN_PATH"
            echo "Creating btrfs subvolume: $MAIN_PATH"
            sudo btrfs subvolume create "$MAIN_PATH" >/dev/null
            ;;
        esac
      else
        echo "Non-btrfs filesystem: keeping as plain directory."
      fi
    else
      echo "Non-empty directory at $MAIN_PATH and not a subvolume."
      read -r -p "Use as plain directory (no snapshots) anyway? [y/N] " ans
      case "${ans,,}" in
        y|yes) echo "Proceeding with plain directory." ;;
        *) echo "Aborting."; exit 1 ;;
      esac
    fi
  else
    # Path missing
    if [ "$BTRFS_AVAILABLE" -eq 1 ]; then
      echo "Creating btrfs subvolume (path missing): $MAIN_PATH"
      sudo btrfs subvolume create "$MAIN_PATH" >/dev/null
    else
      echo "Creating plain directory (non-btrfs): $MAIN_PATH"
      sudo mkdir -p "$MAIN_PATH"
    fi
  fi
fi

# 권한 설정 (컨테이너 postgres 사용자 uid/gid 999)
sudo chown -R 999:999 "$MAIN_PATH"
sudo chmod 700 "$MAIN_PATH" || true

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

# 데이터 디렉토리 마운트 설정
DATA_MOUNT_ARGS=( -v "$MAIN_PATH:/var/lib/postgresql/data" -e PGDATA=/var/lib/postgresql/data/pgdata )

# 컨테이너 실행 (postgres:17)
docker run -d \
  --name "${CONTAINER_NAME}" \
  --network "${NETWORK_NAME}" \
  -p "${HOST_PORT}:5432" \
  --env-file "${ENV_FILE}" \
  "${DATA_MOUNT_ARGS[@]}" \
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
