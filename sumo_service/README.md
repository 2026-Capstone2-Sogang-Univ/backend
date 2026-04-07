# sumo-service

SUMO/TraCI simulation loop with FastAPI (REST + WebSocket).

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- SUMO installed and `SUMO_HOME` set (or use the `eclipse-sumo` Python package, which is included as a dependency)

## Setup

```bash
cd sumo_service
uv sync
```

Dev dependencies (pytest, httpx):

```bash
uv sync --dev
```

## Run

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

SUMO GUI 창을 열려면 (`sumo-gui` 사용):

```bash
SUMO_GUI=1 uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

서버가 뜨면 콘솔에서 단일 키로 시뮬레이션을 제어할 수 있습니다:

| 키 | 동작 |
|----|------|
| `s` | start |
| `p` | pause |
| `u` | resume |
| `r` | restart |
| `e` | end |

## REST API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/simulation/start` | 시뮬레이션 시작 |
| `POST` | `/simulation/pause` | 일시정지 |
| `POST` | `/simulation/resume` | 재개 |
| `POST` | `/simulation/restart` | 재시작 |
| `GET`  | `/simulation/status` | 현재 상태 조회 |

```bash
curl -X POST http://localhost:8000/simulation/start
curl -X POST http://localhost:8000/simulation/pause
curl -X POST http://localhost:8000/simulation/restart
curl      http://localhost:8000/simulation/status
```

## WebSocket

`ws://localhost:8000/ws` 로 연결하면 Unity 프론트엔드로 브로드캐스트되는 메시지를 수신합니다.

| Type | 전송 시점 | 내용 |
|------|-----------|------|
| `boundary` | 연결 직후 1회 | 네트워크 바운딩 박스 좌표 |
| `vehicles` | ~60 fps | 전체 차량 스냅샷 (id, x, y, angle, state) |
| `passengers` | ~60 fps | 대기 승객 목록 (id, x, y) |

Vehicle `state` 값: `car` / `empty` / `dispatched` / `occupied`

## Tests

```bash
uv run pytest
# 단일 테스트
uv run pytest tests/test_simulation.py::test_name
```

## 시뮬레이션 설정

`app/simulation.py` 상단의 상수로 동작을 조정합니다:

| 상수 | 기본값 | 설명 |
|------|--------|------|
| `SIMULATION_SPEED` | `20.0` | 실제 1초당 진행되는 시뮬레이션 초 |
| `FRAME_RATE` | `60.0` | WebSocket 브로드캐스트 fps |
| `SIM_DURATION` | `3600.0` | 시뮬레이션 총 시간(초) |
| `N_TAXIS` | `50` | 택시 수 |
| `N_BACKGROUND_CARS` | `200` | 배경 차량 수 |

SUMO 맵 설정 파일: `sumo_configs/gangnam/LargeGangNamSimulation.sumocfg`
