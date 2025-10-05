#!/bin/bash
set -euo pipefail

# Snaplicator maintenance: stop/remove ALL docker containers and
# delete ALL btrfs subvolumes under ROOT_DATA_DIR.
# Highly destructive. Use for reset only.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
ENV_FILE=${ENV_FILE:-"$ROOT_DIR/configs/.env"}

LOG_FILE=${LOG_FILE:-"$ROOT_DIR/cleanup.log"}
SCRIPT_NAME=$(basename "$0")
LOG_PREFIX="[$SCRIPT_NAME]"

log() { echo "$(date -Is) $LOG_PREFIX $*" | tee -a "$LOG_FILE" >&2; }
trap 'rc=$?; log "ERROR at line ${LINENO}: ${BASH_COMMAND}"; exit $rc' ERR

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  log "Loaded env: $ENV_FILE"
else
  log "ENV file not found: $ENV_FILE (continuing; ROOT_DATA_DIR may be required)"
fi

ROOT_DATA_DIR=${ROOT_DATA_DIR:-}
if [ -z "${ROOT_DATA_DIR}" ]; then
  read -r -p "Enter ROOT_DATA_DIR path (btrfs mount root) [/mnt/snaplicator]: " in_root
  ROOT_DATA_DIR="${in_root:-/mnt/snaplicator}"
fi
ROOT_PATH="${ROOT_DATA_DIR%/}"

echo "This will:"
echo "  1) Stop ALL running Docker containers"
echo "  2) Remove ALL Docker containers (running and stopped)"
echo "  3) Delete ALL btrfs subvolumes under $ROOT_PATH"
echo "  4) Drop replication slot on publisher (if PRIMARY_* and SUBSCRIPTION_NAME are set, and you confirm)"
read -r -p "Proceed with FULL cleanup? [Y/n] " ans
ans="${ans:-y}"
case "${ans,,}" in
  y|yes) : ;;
  *) echo "Aborting."; exit 1;;
esac

log "START full cleanup (ROOT_PATH=$ROOT_PATH)"

# 1) Stop running containers
RUNNING_IDS=$(docker ps -q || true)
if [ -n "$RUNNING_IDS" ]; then
  log "Stopping running containers..."
  while IFS= read -r id; do
    [ -z "$id" ] && continue
    name=$(docker inspect --format '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##' || true)
    log "Stopping: ${name:-$id}"
    docker stop -t 15 "$id" >/dev/null || true
  done <<< "$RUNNING_IDS"
else
  log "No running containers"
fi

# 2) Remove all containers
ALL_IDS=$(docker ps -aq || true)
if [ -n "$ALL_IDS" ]; then
  log "Removing containers..."
  while IFS= read -r id; do
    [ -z "$id" ] && continue
    name=$(docker inspect --format '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##' || true)
    log "Removing: ${name:-$id}"
    docker rm -f "$id" >/dev/null || true
  done <<< "$ALL_IDS"
else
  log "No containers to remove"
fi

# 2.5) Drop replication slot on publisher (optional)
if [ -n "${SUBSCRIPTION_NAME:-}" ] && [ -n "${PRIMARY_HOST:-}" ] && [ -n "${PRIMARY_PORT:-}" ] \
   && [ -n "${PRIMARY_DB:-}" ] && [ -n "${PRIMARY_USER:-}" ] && [ -n "${PRIMARY_PASSWORD:-}" ]; then
  read -r -p "Drop replication slot '${SUBSCRIPTION_NAME}' on publisher ${PRIMARY_HOST}:${PRIMARY_PORT}/${PRIMARY_DB}? [Y/n] " ans_slot
  ans_slot="${ans_slot:-y}"
  case "${ans_slot,,}" in
    y|yes)
      log "Attempting to drop replication slot '${SUBSCRIPTION_NAME}' on publisher..."
      export PGPASSWORD="$PRIMARY_PASSWORD"
      # Check existence
      exists=$(psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" -At \
        -c "SELECT 1 FROM pg_replication_slots WHERE slot_name = '$SUBSCRIPTION_NAME'" 2>/dev/null || true)
      if [ "$exists" = "1" ]; then
        # Terminate active backend if needed
        active_pid=$(psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" -At \
          -c "SELECT active_pid FROM pg_replication_slots WHERE slot_name = '$SUBSCRIPTION_NAME'" 2>/dev/null || true)
        if [ -n "${active_pid}" ] && [ "${active_pid}" != "" ] && [ "${active_pid}" != "0" ]; then
          log "Terminating active replication PID ${active_pid}"
          psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" -At \
            -c "SELECT pg_terminate_backend(${active_pid})" >/dev/null 2>&1 || true
        fi
        # Drop slot
        if psql -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$PRIMARY_USER" -d "$PRIMARY_DB" -At \
             -c "SELECT pg_drop_replication_slot('$SUBSCRIPTION_NAME')" >/dev/null 2>&1; then
          log "Dropped replication slot '${SUBSCRIPTION_NAME}'"
        else
          log "Failed to drop replication slot '${SUBSCRIPTION_NAME}'. You may need superuser privileges."
        fi
      else
        log "Replication slot '${SUBSCRIPTION_NAME}' not found on publisher (nothing to drop)"
      fi
      unset PGPASSWORD
      ;;
    *) log "Skip replication slot drop" ;;
  esac
else
  log "Slot drop skipped (missing PRIMARY_* or SUBSCRIPTION_NAME in env)"
fi

# 3) Delete btrfs subvolumes under ROOT_PATH
FSTYPE=""
if command -v findmnt >/dev/null 2>&1; then
  FSTYPE=$(findmnt -no FSTYPE -T "$ROOT_PATH" || true)
fi
if [ "$FSTYPE" != "btrfs" ]; then
  log "ROOT_PATH is not btrfs (detected: '$FSTYPE'). Skipping btrfs cleanup."
  log "DONE (containers only)"
  exit 0
fi

MOUNTPOINT=$(findmnt -no TARGET -T "$ROOT_PATH" || true)
if [ -z "$MOUNTPOINT" ]; then
  log "Failed to resolve mountpoint for $ROOT_PATH"; exit 1
fi

log "Enumerating btrfs subvolumes under $ROOT_PATH..."
mapfile -t SUB_PATHS < <(sudo btrfs subvolume list -o "$ROOT_PATH" | awk '{$1=$1; print}' | awk '{for(i=9;i<=NF;i++)printf $i" "; print ""}' | sed 's/[[:space:]]*$//' )

if [ "${#SUB_PATHS[@]}" -eq 0 ]; then
  log "No subvolumes found under $ROOT_PATH"
  log "DONE"
  exit 0
fi

# Build absolute paths and sort by depth (longest first)
ABS_LIST=()
for p in "${SUB_PATHS[@]}"; do
  abs="$MOUNTPOINT/$p"
  # ensure it's within ROOT_PATH boundary
  case "$abs" in
    "$ROOT_PATH"/*) ABS_LIST+=("$abs") ;;
  esac
done

if [ "${#ABS_LIST[@]}" -eq 0 ]; then
  log "No subvolumes resolved within $ROOT_PATH"
  log "DONE"
  exit 0
fi

# Sort by descending path length
IFS=$'\n' read -r -d '' -a ABS_SORTED < <(printf '%s\n' "${ABS_LIST[@]}" | awk '{print length, $0}' | sort -rn | cut -d' ' -f2- && printf '\0')

read -r -p "Delete ${#ABS_SORTED[@]} subvolumes under $ROOT_PATH? [Y/n] " ans2
ans2="${ans2:-y}"
case "${ans2,,}" in
  y|yes) : ;;
  *) log "Skip btrfs subvolume deletion"; log "DONE"; exit 0;;
esac

for sv in "${ABS_SORTED[@]}"; do
  if sudo btrfs subvolume show "$sv" >/dev/null 2>&1; then
    # Unmount any mountpoints at/under this subvolume (deepest-first)
    MOUNT_LIST=$(findmnt -Rno TARGET -- "$sv" 2>/dev/null || true)
    if [ -n "$MOUNT_LIST" ]; then
      while IFS= read -r mp; do
        [ -z "$mp" ] && continue
        log "Unmounting: $mp"
        sudo umount "$mp" >/dev/null 2>&1 || true
      done < <(printf '%s\n' "$MOUNT_LIST" | sort -r)
    fi

    log "Deleting subvolume: $sv"
    if ! sudo btrfs subvolume delete "$sv" >/dev/null 2>&1; then
      log "Failed to delete $sv. Checking if busy..."
      (command -v fuser >/dev/null 2>&1 && sudo fuser -vm "$sv" || true)
      # Try unmount again just in case
      MOUNT_LIST=$(findmnt -Rno TARGET -- "$sv" 2>/dev/null || true)
      if [ -n "$MOUNT_LIST" ]; then
        while IFS= read -r mp; do
          [ -z "$mp" ] && continue
          log "Unmounting (retry): $mp"
          sudo umount "$mp" >/dev/null 2>&1 || true
        done < <(printf '%s\n' "$MOUNT_LIST" | sort -r)
      fi
      log "Retrying delete: $sv"
      sudo btrfs subvolume delete "$sv"
    fi
  else
    log "Skip (not a subvolume or missing): $sv"
  fi
done

log "Syncing btrfs subvolume deletions... (this may take a while)"
sudo btrfs subvolume sync "$MOUNTPOINT" >/dev/null 2>&1 || sudo btrfs subvolume sync "$ROOT_PATH" >/dev/null 2>&1 || true

log "DONE"


