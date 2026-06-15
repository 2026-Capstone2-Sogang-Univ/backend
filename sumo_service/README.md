# SUMO Service

`sumo_service`는 FastAPI 서버와 SUMO/TraCI 시뮬레이션 루프를 함께 실행하는 백엔드 서비스다. Manhattan 네트워크 위에서 택시, 승객, 배경 차량을 생성하고 배차, 요금, surge, KPI, 프론트엔드 인터랙션 이벤트를 제공한다.

## 실행

```powershell
cd Back\sumo_service
uv sync --group dev
uv run uvicorn app.main:app --host 0.0.0.0 --port 8080
```

상태 확인:

```powershell
curl http://127.0.0.1:8080/simulation/status
```

## 주요 환경변수

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SUMO_GUI` | `0` | `1`이면 `sumo-gui`, 아니면 `sumo` 사용 |
| `SUMO_ROUTING_ALGORITHM` | `astar` | SUMO routing algorithm |
| `SUMO_REROUTING_THREADS` | `0` | SUMO rerouting thread 수 |
| `ROUTE_REROUTING_PROBABILITY` | `0.3` | travel time 수집용 rerouting device 비율 |
| `ROUTE_ADAPTATION_INTERVAL_S` | `60` | SUMO travel time adaptation interval |
| `ROUTE_ADAPTATION_WEIGHT` | `0.5` | travel time adaptation weight |
| `SIM_DURATION` | `3600` | 시뮬레이션 종료 시간, 초 |
| `SIMULATION_SPEED` | `20` | 실제 1초당 진행할 시뮬레이션 초 |
| `SIM_PROFILE` | `0` | profiling 로그 출력 여부 |
| `N_TAXIS` | `300` | 초기 택시 수 |
| `N_BACKGROUND_CARS` | `800` | 초기 배경 차량 수 |
| `PASSENGER_SOURCE` | `parquet` | `random` 또는 `parquet` |
| `PASSENGERS_PER_5MIN` | `80` | 5분 bucket당 승객 수 |
| `TRIPS_FILE` | `sumo_configs/NY/trips_processed.json` | parquet replay 입력 파일 |
| `DATABASE_URL` | 없음 | PostgreSQL 연결 URL |
| `MANUAL_COMMAND_TIMEOUT_S` | `5.0` | 수동 생성/quote 명령 응답 대기 시간 |
| `PASSENGER_WAIT_TIMEOUT_S` | `900.0` | 대기 승객 timeout |
| `TAXI_DISPATCH_COOLDOWN_S` | `5.0` | 택시 재배차 cooldown |
| `PAIR_DISPATCH_COOLDOWN_S` | `60.0` | 같은 승객-택시 pair 재시도 cooldown |
| `DISPATCH_DELAY_S` | `5.0` | 승객 생성 후 배차 후보 편입 지연 시간 |
| `DISPATCH_DELAY_MANUAL` | `1` | 수동 승객에도 배차 지연을 적용할지 여부 |
| `DISPATCH_MAX_CANDIDATES` | `3` | 승객별 배차 후보 평가 상한 |

`PASSENGER_LAMBDA`는 호환용 alias다. 현재 코드는 `PASSENGERS_PER_5MIN`을 우선 사용하고, 값이 없을 때만 fallback으로 읽는다.

## 승객 생성 모드

`random` 모드는 5분마다 `PASSENGERS_PER_5MIN` 기준으로 승객을 무작위 생성한다.

`parquet` 모드는 전처리된 `trips_processed.json`의 pickup/dropoff/time 정보를 replay한다. 이 모드에서도 `PASSENGERS_PER_5MIN`이 적용되며, 각 5분 bucket에서 목표 수만큼 생성된다. replay 파일 길이를 넘는 장기 실행은 loop 방식으로 이어진다.

기본값은 `parquet`이다. Docker 개발 override는 편의를 위해 `PASSENGER_SOURCE=random`으로 덮어쓴다.

## REST API

모든 endpoint는 `/simulation` prefix 아래에 있다.

| Method | Path | 설명 |
| --- | --- | --- |
| `POST` | `/start` | 시뮬레이션 시작 |
| `POST` | `/pause` | 일시정지 |
| `POST` | `/resume` | 재개 |
| `POST` | `/restart` | 중지 후 재시작 |
| `POST` | `/stop` | 시뮬레이션 중지 |
| `POST` | `/shutdown` | 서버 종료 |
| `GET` | `/status` | 현재 상태와 스냅샷 요약 |
| `GET` | `/kpi` | matching/wait/fare KPI |
| `GET` | `/surge` | H3 cell별 supply/demand/surge |
| `GET` | `/h3-regions` | H3 ID to display name map |
| `GET` | `/passengers` | 현재 승객 목록 |
| `POST` | `/passengers/quote` | 수동 승객 생성 전 예상 요금 조회 |
| `POST` | `/passengers` | 수동 승객 생성 |
| `POST` | `/taxis` | 수동 택시 생성 |
| `GET` | `/taxis/{taxi_id}/standby` | 빈 택시 standby 정보 |
| `GET` | `/taxis/{taxi_id}/call` | 배차된 택시의 현재 call detail |
| `GET` | `/fare/{passenger_id}` | 완료 trip 요금 조회 |

## Start/Restart body

`POST /simulation/start`와 `/restart`는 다음 값을 선택적으로 받는다.

```json
{
  "duration": 3600,
  "seed": 42,
  "passenger_source": "parquet",
  "taxi_count": 200,
  "background_vehicle_count": 600,
  "passengers_per_5min": 50,
  "simulation_speed": 20,
  "initial_passenger_count": 0,
  "target_matching_rates": {
    "raw_lt_1_5": 0.55,
    "raw_lt_2_5": 0.70,
    "raw_lt_3_5": 0.80,
    "raw_gte_3_5": 0.85
  },
  "pricing_policy": {
    "epsilon": -0.6,
    "surge_min": 1.2,
    "surge_max": 4.9,
    "alpha_sensitivity": 1.0
  }
}
```

주요 validation:

- `duration <= 604800`
- `passenger_source`는 `random`, `parquet`만 허용
- `taxi_count <= 1000`
- `background_vehicle_count <= 3000`
- `passengers_per_5min <= 1000`
- `simulation_speed <= 120`
- `initial_passenger_count <= 5000`
- target matching rate는 0 이상 1 이하
- `pricing_policy.surge_min <= surge_max`
- `pricing_policy.alpha_sensitivity > 0`

## 수동 인터랙션 API

수동 승객 생성 요청:

```json
{
  "pickup": {"lat": 40.758, "lng": -73.9855},
  "dropoff": {"lat": 40.7506, "lng": -73.9935},
  "incentive_limit": 500
}
```

`incentive_limit`은 승객이 허용하는 추가 지불 상한이며 cent 단위다. quote 응답의 `surge_multiplier`는 시스템 surge와 cap을 모두 적용한 effective surge다.

`POST /simulation/passengers/quote` 응답:

```json
{
  "expected_fare": 1480,
  "expected_distance_m": 3000,
  "estimated_wait_sec": 120,
  "surge_multiplier": 1.33,
  "incentive_limit": 500,
  "total_amount": 1980
}
```

수동 명령은 시뮬레이션 loop에서 처리된다. `MANUAL_COMMAND_TIMEOUT_S` 안에 처리되지 않으면 400 응답과 `simulation_busy` error가 반환된다.

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

## Surge와 요금

기본 pricing policy:

```json
{
  "epsilon": -0.6,
  "surge_min": 1.2,
  "surge_max": 4.9,
  "alpha_sensitivity": 1.0
}
```

raw surge는 supply/demand 불균형에서 계산한다. v2 pricing은 기사 수락률 목표를 맞추기 위해 필요한 fare를 역산한 뒤 `surge_min`, `surge_max`, 승객의 `incentive_limit`을 적용한다. 수동 승객의 경우 `incentive_limit`은 최종 추가 지불 상한으로 동작한다.

기본 meter fare는 2013 NYC TLC 기준 일부를 사용한다. 기본요금, 거리요금, 저속 시간요금, MTA surcharge가 적용되며 tip과 시간대 surcharge는 적용하지 않는다.

## 데이터 파일

- `sumo_configs/NY/manhattan.sumocfg`: SUMO 설정
- `sumo_configs/NY/manhattan_car_only.net.xml`: Manhattan 차량 네트워크
- `sumo_configs/NY/trips_processed.json`: 기본 parquet replay 변환 결과
- `sumo_configs/NY/routable_scc.json`: routing 가능한 largest SCC edge 목록
- `sumo_configs/NY/supported_h3.json`: 지원 H3 cell 목록

## 테스트

```powershell
cd Back\sumo_service
uv run pytest -q
```
