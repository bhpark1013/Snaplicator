#!/bin/bash
set -euo pipefail

# Load .env from repo root if present
ENV_FILE=${ENV_FILE:-"$(cd "$(dirname "$0")/.." && pwd)/.env"}
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  echo "Loaded config from $ENV_FILE"
fi

# Required envs
: "${CONTAINER_NAME:?CONTAINER_NAME is required}"
: "${NETWORK_NAME:?NETWORK_NAME is required}"
: "${HOST_PORT:?HOST_PORT is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${ROOT_DATA_DIR:?ROOT_DATA_DIR is required}"
: "${MAIN_DATA_DIR:?MAIN_DATA_DIR is required}"

# Optional envs with defaults
POSTGRES_IMAGE=${POSTGRES_IMAGE:-postgres:17}
SNAP_PARENT_DIR=${SNAP_PARENT_DIR:-"${ROOT_DATA_DIR%/}"}
CLONE_PARENT_DIR=${CLONE_PARENT_DIR:-"${ROOT_DATA_DIR%/}"}
CLONE_PREFIX=${CLONE_PREFIX:-"${MAIN_DATA_DIR}-clone"}

# Timestamp for naming
TS=$(date +%Y%m%d-%H%M%S)
CONTAINER_NAME_BASE="$CONTAINER_NAME"
CONTAINER_NAME="${CONTAINER_NAME_BASE}-${TS}"

# Ensure parent directories exist
if [ ! -d "$SNAP_PARENT_DIR" ]; then
  echo "Snapshot parent directory not found: $SNAP_PARENT_DIR" >&2
  exit 1
fi
mkdir -p "$CLONE_PARENT_DIR"

# Find the most recent subvolume whose directory name starts with ${MAIN_DATA_DIR}-snap*
echo "Searching for latest snapshot under $SNAP_PARENT_DIR (pattern: ${MAIN_DATA_DIR}-snap*)"
declare -a _candidates=()
mapfile -t _candidates < <(find "$SNAP_PARENT_DIR" -maxdepth 1 -mindepth 1 -type d -name "${MAIN_DATA_DIR}-snap*" -printf "%f\n" | sort)
SOURCE_SUBVOL=""
if [ "${#_candidates[@]}" -eq 0 ]; then
  echo "No snapshots matching ${MAIN_DATA_DIR}-snap* found in $SNAP_PARENT_DIR" >&2
  exit 1
fi
for (( idx=${#_candidates[@]}-1 ; idx>=0 ; idx-- )); do
  name="${_candidates[$idx]}"
  path="$SNAP_PARENT_DIR/$name"
  if sudo btrfs subvolume show "$path" >/dev/null 2>&1; then
    SOURCE_SUBVOL="$path"
    break
  fi
done

if [ -z "$SOURCE_SUBVOL" ]; then
  echo "No valid btrfs subvolume among candidates in $SNAP_PARENT_DIR" >&2
  exit 1
fi

echo "Selected latest snapshot: $SOURCE_SUBVOL"

# Create a new writable snapshot subvolume
TARGET_SUBVOL="$CLONE_PARENT_DIR/${CLONE_PREFIX}-$TS"

echo "Creating writable snapshot: $TARGET_SUBVOL"
sudo btrfs subvolume snapshot "$SOURCE_SUBVOL" "$TARGET_SUBVOL" >/dev/null

# Permissions for postgres container user (uid/gid 999)
echo "Setting permissions for postgres container (uid/gid 999)..."
sudo chown -R 999:999 "$TARGET_SUBVOL"
sudo chmod -R u+rwX,go-rwx "$TARGET_SUBVOL"

# Verify permissions were set correctly
if [ ! -r "$TARGET_SUBVOL" ]; then
  echo "Warning: Cannot read target subvolume after permission change" >&2
fi

# Determine PGDATA layout inside the snapshot
CONTAINER_PGDATA="/var/lib/postgresql/data"
if sudo test -f "$TARGET_SUBVOL/PG_VERSION"; then
  CONTAINER_PGDATA="/var/lib/postgresql/data"
elif sudo test -f "$TARGET_SUBVOL/pgdata/PG_VERSION"; then
  CONTAINER_PGDATA="/var/lib/postgresql/data/pgdata"
else
  echo "Could not determine PGDATA inside snapshot. Neither PG_VERSION nor pgdata/PG_VERSION found in $TARGET_SUBVOL" >&2
  exit 1
fi

echo "Using PGDATA inside container: $CONTAINER_PGDATA"

# Prepare docker network
if ! docker network ls | grep -q "${NETWORK_NAME}"; then
  docker network create "${NETWORK_NAME}"
fi

# Remove existing container if any with the same final name (unlikely due to TS)
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
fi

# Run postgres container with the cloned subvolume mounted as data dir
ABS_TARGET_SUBVOL=$(readlink -f "$TARGET_SUBVOL")

# Find available host port starting from HOST_PORT
SELECTED_HOST_PORT="$HOST_PORT"
for _i in {1..1000}; do
  if ss -ltn | grep -q ":${SELECTED_HOST_PORT} "; then
    SELECTED_HOST_PORT=$((SELECTED_HOST_PORT+1))
  else
    break
  fi
done

if ss -ltn | grep -q ":${SELECTED_HOST_PORT} "; then
  echo "Failed to find a free port starting from ${HOST_PORT}" >&2
  exit 1
fi

echo "Starting container ${CONTAINER_NAME} on network ${NETWORK_NAME}, port ${SELECTED_HOST_PORT} -> 5432"
docker run -d \
  --name "${CONTAINER_NAME}" \
  --network "${NETWORK_NAME}" \
  -p "${SELECTED_HOST_PORT}:5432" \
  -e POSTGRES_USER="${POSTGRES_USER}" \
  -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
  -e POSTGRES_DB="${POSTGRES_DB}" \
  -e PGDATA="${CONTAINER_PGDATA}" \
  -v "$ABS_TARGET_SUBVOL:/var/lib/postgresql/data" \
  "$POSTGRES_IMAGE" -c max_logical_replication_workers=0

# Wait for readiness
echo "Waiting for PostgreSQL to become ready..."
for i in {1..60}; do
  if docker exec "${CONTAINER_NAME}" pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; then
    echo "PostgreSQL ready. Container: ${CONTAINER_NAME}, Data: $ABS_TARGET_SUBVOL"
    docker logs "${CONTAINER_NAME}" --tail 50 || true
    break
  fi
  sleep 1
  if [ $i -eq 60 ]; then
    echo "Timeout waiting for container to be ready" >&2
    docker logs "${CONTAINER_NAME}" --tail 200 || true
    exit 1
  fi
done

# Immediately disable all subscriptions in the specified database to avoid slot conflicts
echo "Disabling all subscriptions in database ${POSTGRES_DB}..."
subs=$(docker exec "${CONTAINER_NAME}" psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -tAc "SELECT subname FROM pg_subscription" || true)
if [ -n "${subs}" ]; then
  while IFS= read -r sub; do
    [ -z "$sub" ] && continue
    echo "Disabling subscription: $sub"
    docker exec "${CONTAINER_NAME}" psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -c "ALTER SUBSCRIPTION \"$sub\" DISABLE;" || true
  done <<< "$subs"
else
  echo "No subscriptions found to disable."
fi

# Notes for user regarding subscriptions
cat <<'NOTE'
[INFO] Subscriptions have been disabled on this cloned instance to avoid slot conflicts
       with the original replica. If you need to re-enable, run:
         ALTER SUBSCRIPTION <name> ENABLE;
NOTE
