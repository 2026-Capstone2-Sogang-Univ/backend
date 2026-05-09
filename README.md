# SUMO 기반 택시 수요 예측 및 동적 배차 시뮬레이션 백엔드

뉴욕 맨해튼 실제 도로 네트워크 위에서 택시·승객·일반 차량의 이동을 시뮬레이션하고,
딥러닝 기반 수요 예측을 실시간으로 반영하여 동적 배차를 수행하는 백엔드 시스템입니다.
시뮬레이션 결과는 WebSocket을 통해 웹 기반 Digital Twin 프론트엔드에 스트리밍됩니다.

---

## 시스템 구성

세 개의 독립된 마이크로서비스로 구성되며, 내부 통신은 gRPC를 사용합니다.

```
┌─────────────────────────────────────────────────────────────────┐
│                    Web Frontend (Digital Twin)                   │
│                         WebSocket Client                         │
└──────────────────────────────┬──────────────────────────────────┘
                               │ WebSocket
                               │ boundary / vehicles / passengers
                               │ surge / fare_update / finished
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                         SUMO Service                             │
│  - SUMO/TraCI 시뮬레이션 제어 (맨해튼 도로망)                   │
│  - FastAPI REST API  (시뮬레이션 제어 + 조회)                   │
│  - WebSocket 서버    (60 fps 상태 스트리밍)                     │
│  - CLI 콘솔 제어                                                 │
└───────────────┬──────────────────────────────┬──────────────────┘
                │ gRPC (시뮬레이션 상태)        │ gRPC (인센티브/재라우팅)
                ▼                              ▲
┌───────────────────────┐      ┌───────────────────────────────────┐
│   Prediction Service  │      │          Dispatch Service          │
│   [미구현]            │─────▶│          [미구현]                  │
│   - 딥러닝 추론       │      │   - 수급 불균형 계산               │
│   - 5분 주기 트리거   │      │   - 인센티브 레벨 결정             │
└───────────────────────┘      │   - 최근접 택시 배차               │
  gRPC (예측 수요)              └───────────────────────────────────┘
```

### SUMO Service [구현 완료]

- SUMO 1.26 시뮬레이터를 TraCI API로 제어하는 핵심 서비스
- 맨해튼 OSM 기반 도로 네트워크(`manhattan_car_only.net.xml`) 사용
- 초기 상태: 빈 택시 50대 + 배경 일반 차량 200대 (TraCI로 동적 생성)
- 시뮬레이션 속도: 20× 가속 모드 (실제 1초 = 시뮬레이션 20초)
- 종료 조건: 시뮬레이션 시간 3,600초(1시간) 도달 시 자동 종료
- 승객 생성: 5분 시뮬 주기마다 Poisson 분포(λ=5), `random` / `parquet` 이중 모드
- 요금 계산: 거리 + 저속 시간 기반 (서울 택시 요금 체계 근사)
- H3 격자(해상도 9) 기반 공급/수요 집계 및 서지 계수(surge coefficient) 계산

### Prediction Service [미구현]

- 딥러닝 모델의 추론(inference)을 담당 (학습 제외)
- 시뮬레이션 시계 기준 5분마다 자동 트리거
- 출력: t+1 ~ t+6 각 5분 구간별 예측 호출 수

### Dispatch Service [미구현]

- 수급 불균형 지표 계산: `imbalance = predicted_demand - available_taxis`
- 불균형 심각도에 비례한 인센티브 레벨 결정 (0.0 ~ 1.0)
- 빈 택시 재라우팅: 인센티브 레벨에 따른 확률론적 재배정
- 배차: 대기 승객과 가장 가까운 빈 택시 자동 매칭

---

## 기술 스택

| 항목 | 기술 |
|------|------|
| 시뮬레이터 | SUMO 1.26 + TraCI |
| 웹 프레임워크 | FastAPI + Uvicorn |
| WebSocket | FastAPI WebSocket (Starlette) |
| H3 격자 | h3 4.x |
| 내부 통신 | gRPC (예정) |
| 배포 | Docker, Docker Compose |
| 언어 | Python 3.11 |
| 패키지 관리 | uv |

---

## 빠른 시작

### 요구사항

- Docker, Docker Compose

### 전체 시스템 실행

```bash
cd Back
docker compose up --build
```

### 시뮬레이션 제어

**REST API:**

```bash
curl -X POST http://localhost:8080/simulation/start
curl -X POST http://localhost:8080/simulation/pause
curl -X POST http://localhost:8080/simulation/resume
curl -X POST http://localhost:8080/simulation/restart
```

**콘솔 (컨테이너 stdout):**

```
s=start  p=pause  u=resume  r=restart  e=end  q=quit server
```

### 로컬 개발 (sumo_service 단독)

```bash
cd Back/sumo_service

# 의존성 설치 (개발 도구 포함)
uv sync --group dev

# 단위 테스트 실행
uv run pytest -v

# 개발 서버 (SUMO 없이 API 레이어만 확인)
uv run uvicorn app.main:app --reload --port 8080
```

---

## REST API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| POST | `/simulation/start` | 시뮬레이션 시작 |
| POST | `/simulation/pause` | 일시정지 |
| POST | `/simulation/resume` | 재개 |
| POST | `/simulation/restart` | 초기화 후 재시작 |
| POST | `/simulation/stop` | 시뮬레이션 중단 (서버 유지) |
| POST | `/simulation/shutdown` | 시뮬레이션 중단 후 서버 프로세스 종료 |
| GET  | `/simulation/status` | 현재 상태 + 차량/승객 스냅샷 |
| GET  | `/simulation/passengers` | 대기 중 승객 목록 |
| GET  | `/simulation/surge` | H3 셀별 공급/수요/서지 계수 |
| GET  | `/simulation/fare/{passenger_id}` | 완료된 트립 요금 조회 (없으면 404) |

---

## WebSocket 프로토콜

**엔드포인트:** `ws://localhost:8080/ws`

### 메시지 타입 요약

| 타입 | 전송 빈도 | 설명 |
|------|----------|------|
| `boundary` | 연결당 1회 | SUMO 내부 좌표 + WGS84 지리 경계 |
| `snapshot` | 60 fps | 차량 전체 + 대기/배차 승객 목록 (단일 메시지) |
| `surge` | 5 시뮬 초마다 | H3 셀별 공급·수요·서지 계수 |
| `fare_update` | 하차 시 1회 | 실제 요금 및 이동 거리 기록 |
| `finished` | 시뮬 종료 시 1회 | 시뮬레이션 3,600초 도달 알림 |

### boundary

클라이언트 접속 시 1회 전송합니다. SUMO 내부 좌표계(미터)와 WGS84 좌표계 경계를 함께 제공합니다.

```json
{
  "type": "boundary",
  "sumo": {
    "minX": 0.0,
    "minY": 0.0,
    "maxX": 10000.0,
    "maxY": 20000.0
  },
  "geo": {
    "minLat": 40.700,
    "minLng": -74.020,
    "maxLat": 40.880,
    "maxLng": -73.910
  }
}
```

### snapshot

매 스텝(60 fps) 전송. 차량과 승객을 하나의 메시지로 묶어 전송합니다.

```json
{
  "type": "snapshot",
  "sim_time": 300.0,
  "vehicles": [
    {"id": "taxi_0", "lat": 40.716, "lng": -74.001, "angle": 90.0, "speed": 5.2, "state": "empty"}
  ],
  "passengers": [
    {"id": "p_0", "lat": 40.715, "lng": -74.002, "expected_fare": 5900, "expected_distance_m": 2100.5}
  ]
}
```

| state | 대상 | 의미 |
|-------|------|------|
| `car` | 배경 차량 | 일반 차량 |
| `empty` | 택시 | 승객 없음, 배차 대기 중 |
| `dispatched` | 택시 | 승객 픽업 이동 중 |
| `occupied` | 택시 | 승객 탑승, 목적지 이동 중 |

`passengers`는 대기(`waiting`) 및 배차됨(`assigned`) 상태만 포함합니다. 탑승 후 목록에서 제거됩니다.

### surge

5 시뮬 초마다 전송. 빈 택시(공급) 또는 대기 승객(수요)이 존재하는 H3 셀만 포함합니다.

```json
{
  "type": "surge",
  "h3_resolution": 9,
  "cells": [
    {
      "h3": "892830828cbffff",
      "supply": 4,
      "demand": 2,
      "surge": 0.63,
      "center": {"lat": 40.7128, "lng": -74.006}
    }
  ],
  "sim_time": 300.0
}
```

서지 계수 계산:

| 조건 | 값 |
|------|----|
| supply = 0, demand = 0 | 1.0 |
| supply = 0, demand > 0 | 5.0 (최대) |
| supply > 0, demand = 0 | 0.0 |
| supply > 0, demand > 0 | `min((demand / supply)^1.667, 5.0)` |

### fare_update

택시가 하차 지점 30m 이내에 도달하는 순간 전송합니다.

```json
{
  "type": "fare_update",
  "passenger_id": "p_42",
  "taxi_id": "taxi_7",
  "fare": 6200,
  "expected_fare": 5900,
  "distance_m": 2100.5,
  "sim_time": 480.0
}
```

요금 계산 방식 (단위: USD 센트):

```
기본요금       $3.00  (승차 즉시)
거리 추가      $0.70  (1/5마일 = 약 322m 마다)
저속 추가      $0.70  (시속 12마일 미만 시 60초 마다)
─────────────────────────────────────
개선 부담금    $1.00  (모든 운행)
MTA 할증료     $0.50  (맨해튼 운행, 항상 적용)
─────────────────────────────────────
미적용 항목    NYS 혼잡 할증료 $2.50 (96번가 남쪽)
               CBD 혼잡 통행료 $0.75 (60번가 남쪽)
               야간 할증 $1.00 / 러시아워 할증 $2.50
```

---

## 승객 생성 모드

`PASSENGER_SOURCE` 환경변수로 모드를 전환합니다.

| 모드 | 환경변수 값 | 동작 |
|------|-----------|------|
| random (기본) | `random` | 5분 시뮬 주기마다 Poisson(λ=5) 샘플링으로 승객 생성 |
| parquet | `parquet` | 전처리된 NYC 실제 택시 데이터 기반으로 승객 생성 |

**parquet 모드 사전 준비 — 전처리 스크립트 실행:**

`bash` / Git Bash:

```bash
cd Back
python scripts/preprocess_trips.py \
  --input  real_taxi_data/od_month=07/consolidated.parquet \
  --net    sumo_service/sumo_configs/NY/manhattan_car_only.net.xml \
  --output sumo_service/sumo_configs/NY/trips_processed.json \
  --start  "2013-07-08 08-00-00" \
  --end    "2013-07-08 09-00-00" \
  --workers 8 \
  --sample 5000
```

PowerShell:

```powershell
cd Back
python scripts/preprocess_trips.py `
  --input  real_taxi_data/od_month=07/consolidated.parquet `
  --net    sumo_service/sumo_configs/NY/manhattan_car_only.net.xml `
  --output sumo_service/sumo_configs/NY/trips_processed.json `
  --start  "2013-07-08 08-00-00" `
  --end    "2013-07-08 09-00-00" `
  --workers 8 `
  --sample 5000
```

필요 패키지 (서비스 컨테이너 외부 전용):

```bash
pip install pandas pyarrow sumolib pyproj rtree
```

---

## 프로젝트 구조

```
Back/
├── sumo_service/                  # SUMO 시뮬레이션 서비스 [구현 완료]
│   ├── app/
│   │   ├── main.py                # FastAPI 앱 + WebSocket 엔드포인트 + CLI 스레드
│   │   ├── simulation.py          # SimulationManager (TraCI 루프, 상태머신, 요금 누적)
│   │   ├── connection_manager.py  # WebSocket 연결 관리 + 브로드캐스트
│   │   ├── coord.py               # SUMO <-> WGS84 좌표 변환 (TraCI 정밀 변환)
│   │   ├── fare.py                # 요금 계산 (TripAccumulator, calculate_fare)
│   │   ├── grid.py                # H3 격자 조회 + compute_surge
│   │   ├── passenger.py           # Passenger 데이터클래스
│   │   └── routers/
│   │       └── simulation.py      # REST 라우터 (start/pause/resume/restart/status/fare/surge/passengers)
│   ├── sumo_configs/
│   │   └── NY/                    # 맨해튼 SUMO 네트워크 설정 파일
│   ├── tests/                     # 단위 테스트 61개 (pytest, SUMO 없이 실행 가능)
│   ├── Dockerfile
│   └── pyproject.toml
├── scripts/
│   └── preprocess_trips.py        # NYC 택시 parquet → trips_processed.json 변환 (1회성)
├── prediction_service/            # [미구현]
├── dispatch_service/              # [미구현]
├── proto/                         # gRPC 공통 proto 정의 [미구현]
├── docs/
│   ├── PRD.md
│   └── project-proposal.md
└── CLAUDE.md                      # Claude Code 작업 가이드
```
