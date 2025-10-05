#!/bin/bash
set -euo pipefail

# Basic error tracing
trap 'rc=$?; echo "[run-replica-postgres] ERROR at line ${LINENO}: ${BASH_COMMAND}" >&2; exit $rc' ERR
if [[ "${TRACE:-0}" == "1" ]]; then
  PS4='+ [run-replica-postgres:${LINENO}] '
  set -x
fi

# .env 파일 로드 (있으면 값 주입)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
ENV_FILE=${ENV_FILE:-"$ROOT_DIR/configs/.env"}
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
  # btrfs 초기화 여부 컨펌 (LVM LV 기반)
  read -r -p "Initialize a btrfs filesystem on an LVM logical volume mounted at $ROOT_PATH now? [Y/n] " ans
  ans="${ans:-y}"
  case "${ans,,}" in
    y|yes)
      LV_NAME_DEFAULT="snapdata"
      LV_SIZE_DEFAULT="20"

      # 1) 기존 VG 목록(여유공간 포함)에서 선택
      mapfile -t VG_LIST < <(sudo vgs --noheadings -o vg_name,vg_free --units g --nosuffix 2>/dev/null | awk '{gsub(/^ +| +$/,"",$0); print}')
      VG_NAME=""
      if [ ${#VG_LIST[@]} -gt 0 ]; then
        echo "Select a Volume Group to use (or 'n' to create a new VG):"
        idx=1
        for line in "${VG_LIST[@]}"; do
          echo "  [$idx] $line"
          idx=$((idx+1))
        done
        read -r -p "Enter number or 'n': " vg_sel
        case "$vg_sel" in
          n|N)
            ;;
          *)
            if [[ "$vg_sel" =~ ^[0-9]+$ ]] && [ "$vg_sel" -ge 1 ] && [ "$vg_sel" -le ${#VG_LIST[@]} ]; then
              choice="${VG_LIST[$((vg_sel-1))]}"
              VG_NAME="${choice%% *}"
              VG_FREE_RAW="${choice#* }"  # may contain decimals
            else
              echo "Invalid selection."; exit 1
            fi
            ;;
        esac
      fi

      # 2) 새 VG 생성이 필요한 경우
      if [ -z "$VG_NAME" ]; then
        VG_NAME_DEFAULT="snaplicator-vg"
        read -r -p "New VG name [$VG_NAME_DEFAULT]: " vg_in
        VG_NAME="${vg_in:-$VG_NAME_DEFAULT}"

        # 후보 디스크 나열(기존 PV 제외)
        mapfile -t EXIST_PVS < <(sudo pvs --noheadings -o pv_name 2>/dev/null | awk '{gsub(/^ +| +$/,"",$0); print}')
        mapfile -t CANDS < <(lsblk -drpno NAME,SIZE,TYPE 2>/dev/null | awk '$3=="disk"{print $1" "$2}')
        # 필터링: 기존 PV 및 그 부모 디스크 제외
        FILTERED=()
        for line in "${CANDS[@]}"; do
          dev="${line%% *}"
          skip="0"
          # 해당 디스크의 모든 파티션을 찾아 PV와 매칭되면 제외
          mapfile -t PARTS < <(lsblk -rpno NAME,TYPE "$dev" | awk '$2=="part"{print $1}')
          for pv in "${EXIST_PVS[@]}"; do
            if [ "$pv" = "$dev" ]; then skip="1"; break; fi
            for p in "${PARTS[@]}"; do
              if [ "$pv" = "$p" ]; then skip="1"; break 2; fi
            done
          done
          if [ "$skip" = "0" ]; then FILTERED+=("$line"); fi
        done
        if [ ${#FILTERED[@]} -eq 0 ]; then
          echo "No candidate disks found. You can enter a device path manually."
          read -r -p "PV device path (e.g., /dev/sdb): " PV_DEV
          if [ -z "${PV_DEV:-}" ]; then echo "No PV device provided. Aborting."; exit 1; fi
        else
          echo "Select a device to initialize as PV:"
          idx=1
          for line in "${FILTERED[@]}"; do
            echo "  [$idx] $line"
            idx=$((idx+1))
          done
          read -r -p "Enter number (or 'm' to enter path manually): " sel
          case "$sel" in
            m|M)
              read -r -p "PV device path (e.g., /dev/sdb): " PV_DEV
              ;;
            *)
              if [[ "$sel" =~ ^[0-9]+$ ]] && [ "$sel" -ge 1 ] && [ "$sel" -le ${#FILTERED[@]} ]; then
                choice="${FILTERED[$((sel-1))]}"
                PV_DEV="${choice%% *}"
              else
                echo "Invalid selection."; exit 1
              fi
              ;;
          esac
          if [ -z "${PV_DEV:-}" ]; then echo "No PV device provided. Aborting."; exit 1; fi
        fi
        echo "WARNING: This will initialize $PV_DEV for LVM (data loss)."
        read -r -p "Proceed? [Y/n] " confirm_pv
        confirm_pv="${confirm_pv:-y}"
        case "${confirm_pv,,}" in
          y|yes)
            sudo pvcreate "$PV_DEV"
            sudo vgcreate "$VG_NAME" "$PV_DEV"
            ;;
          *) echo "Aborting."; exit 1;;
        esac
        # 새 VG는 전체 용량이 여유공간이므로 VG_FREE_RAW를 갱신
        VG_FREE_RAW=$(sudo vgs --noheadings -o vg_free --units g --nosuffix "$VG_NAME" 2>/dev/null | awk '{gsub(/^ +| +$/,"",$0); print}')
      else
        # 기존 VG의 여유공간 조회
        VG_FREE_RAW=$(sudo vgs --noheadings -o vg_free --units g --nosuffix "$VG_NAME" 2>/dev/null | awk '{gsub(/^ +| +$/,"",$0); print}')
      fi

      # LV 이름/크기 입력 및 검증
      read -r -p "LV name [$LV_NAME_DEFAULT]: " lv_in
      read -r -p "LV size in GiB (number) [$LV_SIZE_DEFAULT]: " size_in
      LV_NAME="${lv_in:-$LV_NAME_DEFAULT}"
      LV_SIZE="${size_in:-$LV_SIZE_DEFAULT}"
      case "$LV_SIZE" in
        ''|*[!0-9]*) echo "Invalid size: '$LV_SIZE' (must be an integer GiB)"; exit 1;;
      esac

      # LV 존재 시 사용할지 여부 확인 루프
      USE_EXISTING_LV=0
      while true; do
        if sudo lvs --noheadings -o lv_name,vg_name --separator '|' 2>/dev/null \
          | awk -F'|' '{gsub(/^ +| +$/, "", $1); gsub(/^ +| +$/, "", $2); print $1"|"$2}' \
          | grep -Fxq "$LV_NAME|$VG_NAME"; then
          read -r -p "Logical Volume $VG_NAME/$LV_NAME already exists. Use existing? [Y/n] " use_ans
          use_ans="${use_ans:-y}"
          case "${use_ans,,}" in
            y|yes)
              echo "Using existing LV: $VG_NAME/$LV_NAME"
              USE_EXISTING_LV=1
              break
              ;;
            *)
              read -r -p "Enter a new LV name [$LV_NAME_DEFAULT]: " lv_in2
              LV_NAME="${lv_in2:-$LV_NAME_DEFAULT}"
              ;;
          esac
        else
          break
        fi
      done

      # 새로 생성이 필요한 경우에만 여유공간 검증 및 생성
      if [ "$USE_EXISTING_LV" -eq 0 ]; then
        VG_FREE_INT="0"
        if [ -n "$VG_FREE_RAW" ]; then
          VG_FREE_INT="${VG_FREE_RAW%.*}"
          [ -z "$VG_FREE_INT" ] && VG_FREE_INT="0"
        fi
        if [ "$LV_SIZE" -gt "$VG_FREE_INT" ]; then
          echo "Not enough free space in VG '$VG_NAME': request ${LV_SIZE}G, free ${VG_FREE_RAW:-0G}"
          exit 1
        fi

        echo "Creating LV: $VG_NAME/$LV_NAME (${LV_SIZE}G)"
        sudo lvcreate -n "$LV_NAME" -L "${LV_SIZE}G" "$VG_NAME"
      fi

      DEVICE="/dev/$VG_NAME/$LV_NAME"
      # 안전장치: 대상이 반드시 LVM LV여야 함
      DEV_TYPE=$(lsblk -ndo TYPE "$DEVICE" 2>/dev/null || true)
      if [ "$DEV_TYPE" != "lvm" ]; then
        echo "Refusing to format non-LV device: $DEVICE (type='$DEV_TYPE')"
        exit 1
      fi

      # 이미 마운트되어 있으면 처리
      CUR_MNT=$(findmnt -S "$DEVICE" -no TARGET 2>/dev/null || true)
      if [ -n "$CUR_MNT" ] && [ "$CUR_MNT" != "$ROOT_PATH" ]; then
        echo "Device $DEVICE is mounted at $CUR_MNT. It must be unmounted to format."
        read -r -p "Unmount $CUR_MNT now? [Y/n] " um_ans
        um_ans="${um_ans:-y}"
        case "${um_ans,,}" in
          y|yes) sudo umount "$CUR_MNT" || { echo "Failed to unmount $CUR_MNT"; exit 1; } ;;
          *) echo "Aborting."; exit 1 ;;
        esac
      fi

      # 기존 파일시스템 확인
      EXIST_FS=$(blkid -o value -s TYPE "$DEVICE" 2>/dev/null || true)
      if [ -n "$EXIST_FS" ] && [ "$EXIST_FS" != "btrfs" ]; then
        echo "WARNING: $DEVICE has existing filesystem: $EXIST_FS"
        read -r -p "Overwrite with btrfs? [Y/n] " ow_ans
        ow_ans="${ow_ans:-y}"
        case "${ow_ans,,}" in
          y|yes) : ;; 
          *) echo "Aborting."; exit 1 ;;
        esac
      fi
      echo "Formatting btrfs on $DEVICE"
      sudo mkfs.btrfs -f "$DEVICE"
      echo "Mounting $DEVICE on $ROOT_PATH"
      sudo mount "$DEVICE" "$ROOT_PATH"

      # 재확인
      FSTYPE=""
      if command -v findmnt >/dev/null 2>&1; then
        FSTYPE=$(findmnt -no FSTYPE -T "$ROOT_PATH" || true)
      fi
      if [ "$FSTYPE" != "btrfs" ]; then
        echo "Failed to mount btrfs at $ROOT_PATH (detected: '$FSTYPE'). Aborting."
        exit 1
      fi
      BTRFS_AVAILABLE=1
      echo "btrfs mounted at $ROOT_PATH via LVM ($DEVICE)"
      ;;
    *)
      read -r -p "Proceed without btrfs snapshot support? [Y/n] " ans2
      ans2="${ans2:-y}"
      case "${ans2,,}" in
        y|yes) BTRFS_AVAILABLE=0 ;;
        *) echo "Aborting."; exit 1 ;;
      esac
      ;;
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
        ans="${ans:-y}"
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
      read -r -p "Use as plain directory (no snapshots) anyway? [Y/n] " ans
      ans="${ans:-y}"
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
CONTAINER_ID=$(docker run -d \
  --name "${CONTAINER_NAME}" \
  --network "${NETWORK_NAME}" \
  -p "${HOST_PORT}:5432" \
  --env-file "${ENV_FILE}" \
  "${DATA_MOUNT_ARGS[@]}" \
  "${VOLUME_ARGS[@]}" \
  postgres:17)
echo "$CONTAINER_ID"

# 준비 대기 및 상태 출력
echo "Waiting for PostgreSQL to become ready..."
for i in {1..60}; do
  # 컨테이너 상태 확인
  if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    # 아직 기동 중이거나 실패했을 수 있음
    status=$(docker inspect -f '{{.State.Status}}' "${CONTAINER_NAME}" 2>/dev/null || true)
    echo "Container state: ${status:-unknown} (attempt $i/60)"
  else
    if docker exec "${CONTAINER_NAME}" pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; then
      echo "Replica ready on port ${HOST_PORT}"
      docker logs "${CONTAINER_NAME}" --tail 50 || true
      exit 0
    else
      echo "Waiting... attempt $i/60"
    fi
  fi
  sleep 1
done

echo "Timeout waiting for replica container to be ready"
docker logs "${CONTAINER_NAME}" --tail 200 || true
docker inspect -f '{{.State.Status}} {{.State.ExitCode}} {{.State.Error}}' "${CONTAINER_NAME}" 2>/dev/null || true
exit 1
