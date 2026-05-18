# Snaplicator

[English](README.md) | **한국어**

Snaplicator는 PostgreSQL 테스트 데이터 공급 도구입니다. 상시 떠 있는
**리플리카 컨테이너**가 운영 publication을 **네이티브 논리복제**로 구독하므로,
원본(primary)을 전혀 건드리지 않고도 데이터가 거의 실시간으로 유지됩니다.
그 리플리카 위에서 **btrfs 스냅샷**과 쓰기 가능한 **클론**으로 격리된 DB
사본을 즉시 만들 수 있습니다 — 각 클론은 자기만의 일회용 Postgres 컨테이너입니다.

논리복제가 안 되거나 불가능한 테이블(예: 외부 파이프라인이 채우는 `etl`
스키마)은 **`postgres_fdw`** 로 원본을 읽기전용으로 노출하며, 이는
`configs/fdw.yaml` 로 선언적으로 관리됩니다.

- **백엔드** — FastAPI (`backend/`), Docker + btrfs + 복제를 오케스트레이션
- **프론트엔드** — Vite + React 관리 UI (`frontend/`)
- **CLI** — `snaplicator`, Typer 기반 psql 스타일 원격 클라이언트 (`backend/cli/`)
- **MCP 서버** — REST API를 MCP 도구로 노출 (`mcp-server/`)

> 익명화(`configs/anonymize.sql`)는 선택 사항이며, **살아있는 메인 리플리카에서
> 직접 클론할 때만** 자동 적용됩니다(스냅샷에서 파생된 클론에는 적용 안 됨).

---

## 동작 원리

```
            ┌────────────────────┐  논리복제 (CREATE SUBSCRIPTION)
 primary ──►│  리플리카 컨테이너  │◄──────────────── PUBLICATION에 포함된 테이블
 (publisher)│  (Postgres, btrfs) │
            │                    │◄── postgres_fdw ─ configs/fdw.yaml의 테이블
            └─────────┬──────────┘     (실시간 읽기전용, 예: etl 스키마)
                      │ btrfs 스냅샷
              ┌───────┴────────┐
              │ 스냅샷          │  → 쓰기가능 클론, 각자 자기 포트의
              │ (읽기전용 subv) │     독립 Postgres 컨테이너
              └────────────────┘
```

- 리플리카는 Docker에서 `--network host`, `wal_level=logical` 로 실행되며,
  `PGDATA` 는 `ROOT_DATA_DIR/MAIN_DATA_DIR` 아래 btrfs 서브볼륨에 위치합니다.
- 컨테이너가 정상 기동되면 그 **안에서** 후처리 스크립트가 실행됩니다:
  `05_clone_schema.sh` → `20_create_subscription.sh` → `06_setup_fdw.sh`.
- 백엔드는 백그라운드 **DDL 자동 동기화 루프**를 돌려 구독자를 발행자와
  맞춥니다: 발행자에 auto-add 이벤트 트리거를 설치하고, 새 테이블·추가된
  컬럼·CHECK 제약·`SET SCHEMA` 이동·FDW 컬럼 드리프트를 주기적으로
  동기화합니다. 활동은 통합 동기화 로그(`/replication/sync-log`)에 기록됩니다.
- 스냅샷은 읽기전용 btrfs 서브볼륨이고, 클론은 쓰기가능 서브볼륨 + 전용
  Postgres 컨테이너로, copy-on-write 덕에 수 초 만에 생성/리셋됩니다.

`ROOT_DATA_DIR` 는 **모든 것**(메인 리플리카 + 모든 스냅샷 + 모든 클론을
형제 서브볼륨으로)을 담는 btrfs 파일시스템입니다. `MAIN_DATA_DIR` 는 그 안의
메인 리플리카 서브볼륨 이름이며, 따라서 살아있는 리플리카의 데이터 경로는
`ROOT_DATA_DIR/MAIN_DATA_DIR` 입니다.

---

## 선행 조건

**호스트 (btrfs + Docker 때문에 Linux 전용):**

- **btrfs**(`btrfs-progs`)가 있는 Linux. macOS/Windows라면 Linux VM
  (UTM, Multipass, Lima 등) 안에서 실행 — Docker Desktop 단독으로는
  btrfs 서브볼륨을 못 올립니다.
- **Docker** (리플리카와 모든 클론이 컨테이너).
- **호스트의 `psql` 클라이언트** — 백엔드가 발행자 SQL을 호스트의 `psql` 로
  실행합니다(구독자 SQL은 `docker exec` 경유).
- **무비밀번호 sudo** — 도구가 호출하는 작업들:
  `btrfs`, `chown`, `chmod`, `mkdir`, `mv`, `mount`, (btrfs 프로비저닝 시)
  LVM 도구. 백엔드가 `sudo -n …` 으로 부르므로 sudoers를 설정하거나
  이미 권한이 있는 사용자로 실행하세요.
- btrfs 프로비저닝에 쓰이는 `util-linux` / LVM 유틸:
  `findmnt`, `lsblk`, `blkid`, `pvcreate`, `vgcreate`, `lvcreate`, `mkfs.btrfs`.
- `make`.

**백엔드:**

- `venv` 가능한 Python **3.10+**. 의존성(`backend/requirements.txt`에 고정):
  FastAPI, Uvicorn, pydantic-settings, python-dotenv, PyYAML.

**프론트엔드:**

- Node.js + **pnpm**.

**데이터베이스:**

- 논리복제가 켜진(`wal_level = logical`) 원본 PostgreSQL과 **publication**.
- `CREATE SUBSCRIPTION` 가능한 복제 권한 롤.
- *(선택)* `postgres_fdw` 용 별도 **읽기전용 롤** — 복제와 다른
  호스트/포트(bastion, pgbouncer)를 가리켜도 됨.

> 호스트/DB가 준비됐는지 모르겠다면 `make doctor` 를 실행하세요 — 위 항목을
> 전부 점검하고 무엇이 빠졌는지·어떻게 고치는지 정확히 알려줍니다.

---

## 설정

모든 설정은 **`configs/.env`** 에 있습니다(셸 스크립트와 백엔드가
pydantic-settings로 함께 읽음). `make setup` 이 대화형으로 이 파일을 만들어
주며, 손으로 하려면 템플릿에서 시작하세요:

```bash
cp configs/.env.example configs/.env
$EDITOR configs/.env
```

| 변수 | 필수 | 용도 |
|---|---|---|
| `CONTAINER_NAME` | ✓ | 리플리카 컨테이너 이름 |
| `NETWORK_NAME` | ✓ | Docker 네트워크 이름(리플리카는 host 네트워크 강제) |
| `HOST_PORT` | ✓ | 리플리카 Postgres 포트 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | ✓ | 리플리카 슈퍼유저/DB |
| `POSTGRES_IMAGE` | – | Postgres 이미지(기본 `postgres:17`; 확장 필요 시 커스텀 이미지) |
| `ROOT_DATA_DIR` | ✓ | 리플리카+모든 스냅샷/클론을 담는 btrfs 루트 |
| `MAIN_DATA_DIR` | ✓ | `ROOT_DATA_DIR` 아래 메인 리플리카 서브볼륨 이름 |
| `PRIMARY_HOST` / `PRIMARY_PORT` / `PRIMARY_DB` | ✓ | 발행자 접속 |
| `PRIMARY_USER` / `PRIMARY_PASSWORD` | ✓ | 복제 롤 자격증명 |
| `PGSSLMODE` | – | 예: `require` (기본 `prefer`) |
| `PUBLICATION_NAME` | ✓ | 구독할 publication |
| `SUBSCRIPTION_NAME` | ✓ | 리플리카에 생성할 subscription 이름 |
| `DUMP_SCHEMAS` | – | 원본에서 스키마 복제(DDL)할 스키마들, 쉼표구분(기본 `public`) |
| `PRECREATED_SLOT_NAME` | – | `CREATE SUBSCRIPTION`이 만들게 두지 않고 미리 만든 슬롯 재사용 |
| `FDW_USER` / `FDW_PASSWORD` | – | postgres_fdw 롤; **비우면 FDW 전체 비활성** |
| `FDW_HOST` / `FDW_PORT` / `FDW_DB` | – | FDW 대상; 비우면 `PRIMARY_*` 로 폴백 |
| `DDL_SYNC_INTERVAL` | – | 자동 동기화 루프 주기(초, 기본 30; `0`이면 끔) |

`configs/` 의 다른 설정 파일:

- **`fdw.yaml`** — `postgres_fdw` 의 단일 진실 공급원(SoT). 외부
  `schema.table` 목록과 서버 옵션을 담음. Replication UI(권장) 또는 손으로
  편집; 손으로 고쳤으면 `POST /replication/fdw/regenerate` 가
  `fdw_setup.generated.sql` 을 다시 렌더링·재적용. `.generated.sql` 은
  파생물이니 직접 수정 금지.
- **`anonymize.sql`** *(gitignore 대상; `anonymize-example.sql` 에서 복사)* —
  clone-from-main 시에만 자동 실행되는 마스킹 SQL.
- **`replication_check.example.sql`** — 웹에서 편집 가능한 복제 점검 쿼리의
  기본 시드. 실제 환경별 버전은 repo 밖
  **`~/.snaplicator/replication_check.sql`** 에 저장(재클론/리셋에도 생존;
  Replication UI에서 편집, 쓰기 차단 읽기전용).

---

## 시작하기

### 가장 쉬움: 원커맨드 셋업

```bash
git clone <repo-url> Snaplicator && cd Snaplicator
make setup
```

`make setup` 은 의존성을 설치하고, 합리적 기본값과 함께 `configs/.env` 를
대화형으로 작성(Enter로 기본값 수락)하고, 프리플라이트 **doctor** 를 돌린 뒤
— 전부 초록이면 — 리플리카 기동과 API+UI 시작을 제안합니다. 재실행은
안전합니다(기존 `configs/.env` 는 백업).

호스트 선행 조건과 발행자 준비(`wal_level=logical` + publication)는 여전히
필요합니다. `make doctor` 가 무엇이 빠졌고 어떻게 고치는지 언제든 알려줍니다:

```bash
make doctor          # 빨강/초록 체크리스트 + 복붙 가능한 해결책
```

### 수동 단계 (`make setup` 이 자동화하는 것)

```bash
git clone <repo-url> Snaplicator && cd Snaplicator

# 1. 설정
cp configs/.env.example configs/.env && $EDITOR configs/.env
# (선택) cp configs/anonymize-example.sql configs/anonymize.sql
# (선택) postgres_fdw 테이블용 configs/fdw.yaml 편집

# 2. 원본(발행자)에서 한 번만:
#    ALTER SYSTEM SET wal_level = logical;   -- 이후 재시작
#    CREATE PUBLICATION <PUBLICATION_NAME> FOR TABLES IN SCHEMA public;
#    -- 복제 롤에 REPLICATION + 필요한 SELECT 권한 부여

# 3. 의존성 설치
make server-prepare              # 백엔드 venv + pip install
( cd frontend && pnpm install )  # 프론트 의존성

# 4. 준비 상태 점검
make doctor                      # ✘ 있으면 먼저 해결

# 5. 리플리카 기동 (필요 시 btrfs 대화형 프로비저닝,
#    컨테이너 시작, 스키마복제 + 구독 + FDW)
make replica

# 6. API + UI 실행
make dev                         # 백엔드 :8888 + 프론트 :3000 동시
#   또는 따로:  make server   /   make fe

# 7. UI 열기
#    http://localhost:3000   (UI가 /api → http://localhost:8888 프록시)
#    API 문서: http://localhost:8888/docs
```

`ROOT_DATA_DIR` 가 아직 btrfs 위가 **아니면** 최초의 `make replica` 는
대화형입니다: LVM 기반 btrfs 볼륨을 초기화할 수 있습니다(파괴적 단계 전
확인 프롬프트). 실패 시 컨테이너 안의 `replica-init.log` 를 repo 루트로
복사하니, 재시도 전에 확인하세요.

---

## Make 타깃

| 타깃 | 하는 일 |
|---|---|
| `make setup` | **원커맨드 최초 구동**: 의존성 + 대화형 `configs/.env` + `doctor` + 선택적 기동 |
| `make doctor` | 구동 전 환경 점검 — 빨강/초록 체크리스트 + 복붙 해결책(서버 불필요) |
| `make replica` | btrfs 프로비저닝(필요 시) + 리플리카 컨테이너 실행 + 후처리(스키마복제, 구독, FDW) |
| `make server-prepare` | `backend/.venv` 생성 + Python 의존성 설치(최초 1회) |
| `make server` | FastAPI 서버를 `0.0.0.0:8888` 에서 실행(`--reload`) |
| `make fe` | 프론트 `pnpm install` + `pnpm dev` (`:3000`) |
| `make dev` | `server` 와 `fe` 동시 실행 |

---

## CLI

Typer 기반 원격 클라이언트가 실행 중인 Snaplicator API와 통신합니다:

```bash
pip install -e backend            # `snaplicator` 명령 설치
export SNAPLICATOR_URL=http://localhost:8888

snaplicator health
snaplicator clones ...            # 클론 관리
snaplicator snap ...              # 스냅샷 관리
snaplicator repl ...              # 복제 모니터링/관리
```

`--host/-H` 가 `SNAPLICATOR_URL` 보다 우선합니다.

## MCP 서버

`mcp-server/server.py` 가 REST API를 stdio MCP 도구로 노출합니다(클론,
스냅샷, 복제). venv에 `mcp` 와 `httpx` 패키지가 필요하고
`SNAPLICATOR_URL`(기본 `http://localhost:8888`)을 읽습니다:

```bash
mcp-server/.venv/bin/python mcp-server/server.py
```

---

## API 스모크 테스트

```bash
curl -s localhost:8888/health | jq .
curl -s 'localhost:8888/setup/preflight' | jq .          # `make doctor` 와 동일 점검
curl -s 'localhost:8888/setup/preflight?deep=false' | jq .  # 네트워크 호출 생략(빠름)
curl -s localhost:8888/snapshots | jq .
curl -s -X POST localhost:8888/snapshots -H 'content-type: application/json' \
     -d '{"description":"before migration"}' | jq .
curl -s -X POST localhost:8888/snapshots/<snapshot_name>/clone | jq .
curl -s localhost:8888/replication/lag | jq .
curl -s localhost:8888/replication/sync-log | jq .
```

라우트 그룹: `/health`, `/setup` (`/setup/preflight`), `/snapshots`,
`/clones`, `/replication` (`/replication/fdw*`, `/replication/sync-log`,
`/replication/check-sql` 포함). 전체 스키마는 `/docs`.

---

## 스크립트

- `scripts/setup.sh` — `make setup` 의 본체: 의존성, 대화형 `configs/.env`,
  프리플라이트, 선택적 기동.
- `scripts/run-replica-postgres.sh` — `make replica` 의 핵심: btrfs/LVM
  프로비저닝, 컨테이너 실행, 컨테이너 내 후처리.
- `scripts/create_main_snapshot.sh` — 메인 리플리카 스냅샷.
- `scripts/create-clone-from-snapshot-postgres.sh` — 클론 컨테이너 기동.
- `scripts/maintenance/cleanup_all.sh` — 오래된 클론/컨테이너 정리.
- `replication/replica-init/*.sh` — 컨테이너 내 초기화 단계
  (`01_wait_for_db`, `03_install_extensions`, `05_clone_schema`,
  `06_setup_fdw`, `20_create_subscription`).

`backend/app/services/preflight.py` 가 doctor 로직을 담고 있으며 단독
실행 가능(`python -m app.services.preflight`)하고 `GET /setup/preflight`
로도 제공됩니다.

---

## 트러블슈팅

- **구동 전 무엇이든** — `make doctor` 실행; 빠진 선행 조건(env 키, Docker,
  psql, btrfs, sudo, 발행자 `wal_level`/publication)을 복붙 해결책과 함께
  콕 짚어줍니다.
- **리플리카 초기화 실패** — repo 루트의 `replica-init.log`(실패 시 컨테이너
  밖으로 복사됨)와 `docker logs <CONTAINER_NAME>` 확인.
- **클론 컨테이너가 안 뜸** — `docker logs <clone-container>`.
- **btrfs 공간 부족** — 오래된 서브볼륨 삭제:
  `sudo btrfs subvolume delete <ROOT_DATA_DIR>/<subvol>`.
- **구독이 안 생김** — 원본의 `wal_level=logical` 과 publication, 복제 롤
  접속 가능 여부, (설정 시) `PRECREATED_SLOT_NAME` 존재 확인.
- **FDW 미설정** — `FDW_USER`/`FDW_PASSWORD` 가 있고 host/port/db가
  해석되지 않으면(`FDW_*` 또는 `PRIMARY_*`) `06_setup_fdw.sh` 는 무동작.
  `POST /replication/fdw/regenerate` 로 재렌더/재적용.
- **DDL 변경이 전파 안 됨** — 자동 동기화 루프는 `DDL_SYNC_INTERVAL` 초마다
  실행; `/replication/sync-log` 와 `snaplicator.ddl_sync` 로그 확인.
  auto-add 트리거 설치 여부(`/replication/trigger-status`) 확인.
- **`sudo` 프롬프트 / 권한 거부** — 백엔드는 `sudo -n`(비대화형) 사용;
  btrfs/chown/mount/LVM에 무비밀번호 sudo 설정.
- **macOS** — `ROOT_DATA_DIR` 를 Linux VM의 btrfs 마운트에 두세요.

`configs/.env`, `configs/fdw.yaml`, `configs/anonymize.sql` 을 환경에
맞게 유지하고, 필요하면 Makefile/스크립트를 확장해서 워크플로를 자동화하세요.
