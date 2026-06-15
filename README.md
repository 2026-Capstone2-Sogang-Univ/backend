# DigitalTwin Backend

Manhattan SUMO 네트워크 위에서 택시, 승객, 배경 차량을 시뮬레이션하고 프론트엔드에 REST API와 WebSocket 데이터를 제공하는 백엔드 저장소다. 현재 실행 중심 서비스는 `sumo_service`이며, 배차, 요금, surge, KPI, 수동 승객/택시 생성, 실험 모드를 함께 담당한다.

---

## 시스템 구성

현재 백엔드는 `sumo_service`가 시뮬레이션 상태의 기준이다. 프론트엔드는 REST API로 제어/조회 요청을 보내고, WebSocket protobuf 스트림으로 실시간 상태를 받는다. AI 수요 예측 API는 실험 모드에서 선택적으로 호출한다.

```text
+------------------------------------------------------------------+
|                      Web Frontend                                |
|  - REST control/query client                                     |
|  - WebSocket protobuf client                                     |
+------------------------------+-----------------------------------+
                               |
                               | REST API
                               | WebSocket protobuf
                               v
+------------------------------------------------------------------+
|                         SUMO Service                             |
|  - FastAPI REST API                                              |
|  - WebSocket broadcaster                                         |
|  - SUMO/TraCI simulation loop                                    |
|  - taxi/passenger/background vehicle state management            |
|  - dispatch, fare, surge, KPI logic                              |
|  - manual passenger/taxi creation                                |
+---------------+-------------------------------+------------------+
                |                               |
                | optional DB write/query        | experiment mode only
                v                               v
+-------------------------------+    +-----------------------------+
| PostgreSQL / TimescaleDB      |    | External Prediction API      |
|  - run/trip/dispatch records  |    |  - demand prediction by H3   |
|  - vehicle state/log storage  |    |  - selected horizon request  |
+-------------------------------+    +-----------------------------+
```

### SUMO Service

- SUMO/TraCI 기반 Manhattan 도로망 시뮬레이션
- 택시, 승객, 배경 차량 생성과 상태 전이 관리
- parquet replay 또는 random 승객 생성 지원
- H3 cell 단위 supply/demand/surge 계산
- 기사 수락률 기반 v2 pricing과 effective fare 계산
- REST API와 WebSocket protobuf로 프론트엔드 연동
- PostgreSQL/TimescaleDB 저장 지원
- 실험 스크립트를 통한 actual/predicted demand 비교 지원

---

## 기술 스택

| 항목 | 기술 |
| --- | --- |
| 시뮬레이터 | SUMO + TraCI |
| 웹 프레임워크 | FastAPI + Uvicorn |
| WebSocket | FastAPI WebSocket, protobuf binary |
| H3 격자 | h3 |
| DB | PostgreSQL 16 + TimescaleDB |
| DB 드라이버 | asyncpg |
| 언어 | Python 3.11 |
| 패키지 관리 | uv |
| 배포 | Docker, Docker Compose |

---

## 실행

### 로컬 실행

```powershell
cd Back\sumo_service
uv sync --group dev
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### Docker 실행

```powershell
cd Back
docker compose up --build
```

`docker-compose.override.yml`이 있으면 Docker Compose가 자동으로 함께 읽는다. 이 override는 개발 편의를 위해 코드 볼륨 마운트, `--reload`, `PASSENGER_SOURCE=random`, `DATABASE_URL` 등을 적용한다. 배포 설정에 가깝게 확인하려면 override 없이 실행한다.

```powershell
cd Back
docker compose -f docker-compose.yml up --build
```

---

## 주요 기본값

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `PASSENGER_SOURCE` | `parquet` | 승객 생성 모드 |
| `TRIPS_FILE` | `sumo_configs/NY/trips_processed.json` | parquet replay 입력 파일 |
| `PASSENGERS_PER_5MIN` | `80` | 5분 bucket당 승객 수. random/parquet 공통 |
| `SIM_DURATION` | `3600` | 시뮬레이션 시간, 초 |
| `SIMULATION_SPEED` | `20` | 실제 1초당 진행할 시뮬레이션 초 |
| `N_TAXIS` | `300` | 초기 택시 수 |
| `N_BACKGROUND_CARS` | `800` | 초기 배경 차량 수 |
| `SUMO_ROUTING_ALGORITHM` | `astar` | SUMO routing algorithm |
| `DATABASE_URL` | 없음 | 없으면 DB writer 없이 동작 |

`PASSENGER_LAMBDA`는 호환용 alias다. 현재 코드는 `PASSENGERS_PER_5MIN`을 우선 사용하고, 값이 없을 때만 fallback으로 읽는다.

---

## REST API

주요 REST API prefix는 `/simulation`이다.

| Method | Endpoint | 설명 |
| --- | --- | --- |
| `POST` | `/simulation/start` | 시뮬레이션 시작 |
| `POST` | `/simulation/pause` | 일시정지 |
| `POST` | `/simulation/resume` | 재개 |
| `POST` | `/simulation/restart` | 재시작 |
| `POST` | `/simulation/stop` | 시뮬레이션 중지 |
| `POST` | `/simulation/shutdown` | 서버 종료 |
| `GET` | `/simulation/status` | 현재 상태 조회 |
| `GET` | `/simulation/kpi` | KPI 조회 |
| `GET` | `/simulation/surge` | H3 cell별 supply/demand/surge |
| `GET` | `/simulation/h3-regions` | H3 ID to display name map |
| `GET` | `/simulation/passengers` | 현재 승객 목록 |
| `POST` | `/simulation/passengers/quote` | 수동 승객 생성 전 예상 요금 조회 |
| `POST` | `/simulation/passengers` | 수동 승객 생성 |
| `POST` | `/simulation/taxis` | 수동 택시 생성 |
| `GET` | `/simulation/taxis/{taxi_id}/standby` | 빈 택시 standby 정보 |
| `GET` | `/simulation/taxis/{taxi_id}/call` | 배차된 택시의 call detail |
| `GET` | `/simulation/fare/{passenger_id}` | 완료 trip 요금 조회 |

### Start/Restart body 예시

```json
{
  "duration": 3600,
  "seed": 42,
  "passenger_source": "parquet",
  "taxi_count": 200,
  "background_vehicle_count": 600,
  "passengers_per_5min": 50,
  "simulation_speed": 20,
  "initial_passenger_count": 0
}
```

---

## WebSocket

WebSocket endpoint는 `/ws`이며 JSON이 아니라 protobuf binary를 전송한다. proto 기준은 `proto/ws_messages.proto` 또는 프로젝트 루트의 `ws_messages.proto` 복사본이다.

주요 메시지:

- `boundary`: 접속 시 1회, SUMO/geo boundary
- `snapshot`: 차량/승객 스냅샷
- `surge`: H3 cell별 supply/demand/surge
- `fare_update`: trip 완료 요금
- `finished`: 시뮬레이션 종료
- `passenger_created`, `taxi_created`, `passenger_creation_failed`
- `dispatch_assigned`, `passenger_boarded`, `passenger_cancelled`

---

## 승객 replay 데이터

기본 replay 파일은 `sumo_service/sumo_configs/NY/trips_processed.json`이다. 런타임에서는 `PASSENGERS_PER_5MIN` 또는 start body의 `passengers_per_5min` 값으로 5분 bucket당 생성량을 조절한다. replay 파일 길이를 넘는 장기 실행은 loop 방식으로 이어진다.

테스트용 `trips_processed_600ph_sample.json`은 로컬 검증 편의를 위한 샘플 파일이다. 배포나 기본 실행은 `trips_processed.json`을 기준으로 한다.

---

## 실험 모드

v2 surge와 기사 수락률 기반 실험은 다음 문서를 기준으로 한다.

- `sumo_service/README.experiment.v2-surge.md`
- `sumo_service/scripts/run_acceptance_experiment.py`

예측 수요 실험은 `--demand-source predicted`와 `PREDICTION_API_KEY`가 필요하다.

---

## 검증

```powershell
cd Back\sumo_service
uv run pytest -q
```
