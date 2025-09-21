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

# Required variables
: "${ROOT_DATA_DIR:?ROOT_DATA_DIR is required}"
: "${MAIN_DATA_DIR:?MAIN_DATA_DIR is required}"

SOURCE_SUBVOL="${ROOT_DATA_DIR%/}/${MAIN_DATA_DIR}"
if [ ! -d "$SOURCE_SUBVOL" ]; then
  echo "Source path not found: $SOURCE_SUBVOL" >&2
  exit 1
fi

# Verify source is a btrfs subvolume
if ! sudo btrfs subvolume show "$SOURCE_SUBVOL" >/dev/null 2>&1; then
  echo "Source is not a btrfs subvolume: $SOURCE_SUBVOL" >&2
  exit 1
fi

TS=$(date +%Y%m%d-%H%M%S)
TARGET_SUBVOL="${ROOT_DATA_DIR%/}/${MAIN_DATA_DIR}-snapshot-${TS}"

if [ -e "$TARGET_SUBVOL" ]; then
  echo "Target snapshot already exists: $TARGET_SUBVOL" >&2
  exit 1
fi

echo "Creating readonly snapshot: $TARGET_SUBVOL from $SOURCE_SUBVOL"
sudo btrfs subvolume snapshot -r "$SOURCE_SUBVOL" "$TARGET_SUBVOL" >/dev/null

echo "$TARGET_SUBVOL" 