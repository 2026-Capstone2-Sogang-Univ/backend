# SUMO 기반 택시 수요 예측 및 동적 배차 시뮬레이션 백엔드

서울 역삼동 실제 도로 네트워크 위에서 택시·승객·일반 차량의 이동을 시뮬레이션하고, 딥러닝 기반 수요 예측을 실시간으로 반영하여 동적 배차를 수행하는 백엔드 시스템입니다. 시뮬레이션 결과는 WebSocket을 통해 Unity 기반 3D Digital Twin 프론트엔드에 스트리밍됩니다.

## 시스템 구성

세 개의 독립된 마이크로서비스로 구성되며, 내부 통신은 gRPC를 사용합니다.

```
┌─────────────────────────────────────────────────────────┐
│                    Unity (Digital Twin)                  │
│                  WebSocket Client                        │
└───────────────────────┬─────────────────────────────────┘
                        │ WebSocket (boundary / vehicles / passengers)
                        │
┌───────────────────────▼─────────────────────────────────┐
│                    SUMO Service                          │
│  - SUMO/TraCI 시뮬레이션 제어                            │
│  - FastAPI REST API (시뮬레이션 제어)                    │
│  - WebSocket 서버 (상태 스트리밍)                        │
│  - CLI 콘솔 제어                                         │
└──────────┬──────────────────────────┬────────────────────┘
           │ gRPC (시뮬레이션 상태)    │ gRPC (인센티브/재라우팅 명령)
           ▼                          ▲
┌──────────────────────┐   ┌──────────────────────────────┐
│  Prediction Service  │   │       Dispatch Service        │
│  - 딥러닝 추론 실행  │──▶│  - 수급 불균형 계산           │
│  - 5분 주기 스케줄링 │   │  - 인센티브 레벨 결정         │
└──────────────────────┘   │  - 최근접 택시 배차           │
  gRPC (예측 수요)          └──────────────────────────────┘
```

### SUMO Service

- SUMO 시뮬레이터를 TraCI API로 제어하는 핵심 서비스
- 역삼동 OSM 기반 도로 네트워크 위에서 시뮬레이션 실행
- 시뮬레이션 초기 상태: 빈 택시 50대, 배경 일반 차량 200대
- 시뮬레이션 속도: 가속 모드 (실제 1초 = 시뮬레이션 1분), 시뮬레이션 1시간 후 자동 종료
- WebSocket으로 프론트엔드에 100ms 간격 상태 스트리밍

### Prediction Service

- 별도 팀이 개발한 딥러닝 모델의 추론(inference)만 담당
- 시뮬레이션 시계 기준 5분마다 자동 트리거
- 출력: t+1~t+6 각 5분 구간별 역삼동 예측 호출 수

### Dispatch Service

- 수급 불균형 지표 계산: `imbalance = predicted_demand - available_taxis`
- 불균형 심각도에 비례한 인센티브 레벨 결정 (0.0 ~ 1.0)
- 빈 택시 재라우팅: 인센티브 레벨에 따른 확률론적 결정
- 배차: 대기 승객과 가장 가까운 빈 택시 자동 매칭

## 기술 스택

| 항목 | 기술 |
|------|------|
| 시뮬레이터 | SUMO + TraCI |
| 웹 프레임워크 | FastAPI (REST API + WebSocket) |
| 내부 통신 | gRPC |
| 딥러닝 | TensorFlow / PyTorch |
| 배포 | Docker, Docker Compose |
| 언어 | Python |
| 패키지 관리 | uv |

## 빠른 시작

### 요구사항

- Docker, Docker Compose
- SUMO (로컬 개발 시)
- [uv](https://docs.astral.sh/uv/) (로컬 개발 시)

### 로컬 개발 환경 설정

각 서비스는 `pyproject.toml`로 의존성을 관리합니다.

```bash
# 서비스 디렉토리에서 의존성 설치 (예: dispatch-service)
cd dispatch-service
uv sync

# 테스트 실행
uv run pytest
```

### 전체 시스템 실행

```bash
docker compose up
```

### 시뮬레이션 제어

**REST API:**
```bash
curl -X POST http://localhost:8000/simulation/start
curl -X POST http://localhost:8000/simulation/pause
curl -X POST http://localhost:8000/simulation/restart
```

**콘솔 (SUMO Service 컨테이너 내):**
```
> start
> pause
> restart
```

## WebSocket 프로토콜

WebSocket 엔드포인트: `ws://localhost:8000/ws`

### 메시지 타입

**`boundary`** — 연결 직후 1회 전송. Unity가 맵 좌표계를 초기화하는 데 사용합니다.

```json
{
  "type": "boundary",
  "minX": 0.0,
  "minY": 0.0,
  "maxX": 1928.66,
  "maxY": 1902.15
}
```

**`vehicles`** — 100ms 간격. 현재 도로 위 모든 차량의 스냅샷입니다.

```json
{
  "type": "vehicles",
  "vehicles": [
    { "id": "veh_0", "x": 964.3, "y": 951.1, "angle": 90.0, "state": "empty" }
  ]
}
```

| state | 의미 |
|-------|------|
| `car` | 일반 차량 |
| `empty` | 빈 택시 (배차 대기) |
| `dispatched` | 승객 픽업 이동 중 |
| `occupied` | 승객 탑승 중 |

**`passengers`** — 100ms 간격. 현재 대기 중인 승객 전체 목록입니다. 목록에서 사라진 승객은 Unity가 자동 제거합니다.

```json
{
  "type": "passengers",
  "passengers": [
    { "id": "p_0", "x": 970.1, "y": 940.5 }
  ]
}
```

## 프로젝트 구조

```
backend/
├── sumo-service/        # SUMO 시뮬레이션 + FastAPI + WebSocket
├── prediction-service/  # 딥러닝 추론 스케줄러
├── dispatch-service/    # 동적 배차 및 인센티브 알고리즘
├── proto/               # 공유 gRPC proto 정의
├── docs/
│   ├── PRD.md
│   └── project-proposal.md
└── docker-compose.yml
```
