# PRD: SUMO 기반 택시 수요 예측 및 동적 배차 교통 시뮬레이션 백엔드

## Problem Statement

도시 내 택시 수요는 시간대·날씨·이벤트 등에 따라 지역별로 불균형하게 발생한다. 빈 택시는 수요가 없는 곳에 머물고, 수요가 집중된 지역에는 택시가 부족한 상황이 반복된다. 이를 해결하기 위해서는 단기 수요를 예측하고, 예측 결과를 기반으로 빈 택시를 수요 집중 지역으로 유도하는 실시간 동적 배차 시스템이 필요하다. 또한 이 시스템의 효과를 검증할 수 있는 시뮬레이션 환경과, 결과를 3D Digital Twin으로 시각화할 수 있는 데이터 파이프라인이 필요하다.

## Solution

SUMO(Simulation of Urban MObility) 시뮬레이터를 백엔드 핵심 엔진으로 사용하여 역삼동 실제 도로 네트워크 위에서 택시·승객·일반 차량의 이동을 시뮬레이션한다. 딥러닝 기반 수요 예측 결과를 5분 주기로 반영하여 승객을 동적으로 생성하고, 수급 불균형 지표에 따라 빈 택시에 확률론적 인센티브를 부여해 수요 지역으로 유도한다. 시뮬레이션 상태는 WebSocket을 통해 Unity 기반 3D Digital Twin 프론트엔드에 실시간으로 전달된다. 전체 시스템은 세 개의 독립된 마이크로서비스(SUMO Service, Prediction Service, Dispatch Service)로 구성되며 gRPC로 내부 통신한다.

## User Stories

### 시뮬레이션 제어
1. As a frontend operator, I want to start the simulation via an API call, so that the SUMO simulation begins running and streaming data.
2. As a frontend operator, I want to pause the simulation via an API call, so that I can freeze the current state for inspection.
3. As a frontend operator, I want to restart the simulation via an API call, so that I can reset to the initial state and run again.
4. As a frontend operator, I want the simulation to automatically stop after 1 simulated hour, so that each demo run has a defined end.
5. As a frontend operator, I want to receive a notification via WebSocket when the simulation ends, so that the UI can display a completion state.
6. As a developer, I want to start the simulation by typing a command in the console, so that I can run the simulation without a frontend client.
7. As a developer, I want to pause and restart the simulation by typing a command in the console, so that I can control simulation state during local development and debugging.

### WebSocket 데이터 수신 (Unity / Digital Twin)
6. As a Unity client, I want to receive the network boundary coordinates once on connection, so that I can correctly map simulation coordinates to 3D world space.
7. As a Unity client, I want to receive a snapshot of all vehicles every 100ms, so that I can render smooth vehicle movement in the 3D scene.
8. As a Unity client, I want each vehicle's state (car / empty / dispatched / occupied) included in the snapshot, so that I can color-code vehicles appropriately.
9. As a Unity client, I want to receive the full list of waiting passengers every 100ms, so that I can render and remove passenger markers automatically.
10. As a Unity client, I want passengers that have been picked up to disappear from the passenger list, so that the scene stays consistent with simulation state.

### 수요 예측 연동
11. As the dispatch service, I want to receive demand predictions (t+1 ~ t+6, 5-minute intervals) from the prediction service every 5 simulated minutes, so that I can plan incentives ahead of time.
12. As the prediction service, I want to be triggered automatically on a simulated-time schedule, so that predictions stay aligned with simulation progress without manual intervention.
13. As the prediction service, I want to receive the current simulation timestamp from the SUMO service via gRPC, so that predictions are temporally consistent with the simulation.

### 동적 배차 및 인센티브
14. As the dispatch service, I want to compute a supply-demand imbalance score per zone every 5 simulated minutes, so that I can identify where incentives are needed.
15. As the dispatch service, I want to calculate an incentive level proportional to the imbalance score, so that higher imbalance areas attract more taxis.
16. As the dispatch service, I want to send incentive levels and rerouting commands to the SUMO service via gRPC, so that taxis respond to demand signals.
17. As the SUMO service, I want to apply probabilistic rerouting to empty taxis based on incentive levels, so that the simulation reflects realistic driver behavior.
18. As the SUMO service, I want to automatically assign the nearest available empty taxi to a waiting passenger, so that pickups happen without manual dispatch.
19. As a simulation analyst, I want to observe changes in vehicle distribution after incentives are applied, so that I can validate the effectiveness of the dispatch algorithm.
20. As a simulation analyst, I want to measure average passenger waiting time with and without the incentive system, so that I can quantify its impact.

### 승객 생성 및 운행
21. As the SUMO service, I want to dynamically generate passengers every 5 simulated minutes based on Poisson-distributed demand from prediction results, so that passenger volume matches the predicted demand.
22. As the SUMO service, I want unserved passengers to remain waiting until a taxi is assigned, so that demand accumulation is correctly modeled.
23. As the SUMO service, I want a taxi that has dropped off a passenger to automatically return to the `empty` state, so that it becomes available for the next dispatch cycle.
24. As the SUMO service, I want each passenger's drop-off point to be set to a random edge within the road network, so that taxi trips are spatially distributed across the simulation area.

### 인프라 및 운영
23. As a developer, I want each service to run in its own Docker container, so that services can be developed and deployed independently.
24. As a developer, I want Docker Compose to manage all services together, so that the full system can be started with a single command.
25. As a developer, I want gRPC proto definitions shared across services, so that interface contracts are consistent and versioned.

## Implementation Decisions

### 마이크로서비스 구성

**1. SUMO Service**
- SUMO 시뮬레이터를 TraCI API로 제어하는 핵심 서비스
- OSM에서 추출한 역삼동 경계 기준 도로 네트워크 사용 (`netconvert` 변환)
- 시뮬레이션 초기 상태: 빈 택시 50대(랜덤 배치), 배경 일반 차량 200대
- 시뮬레이션 속도: 가속 모드 (실제 1초 = 시뮬레이션 1분)
- 종료 조건: 시뮬레이션 시계 기준 1시간 경과 시 자동 종료
- WebSocket 서버 역할: 프론트엔드에 boundary / vehicles / passengers 메시지 스트리밍
- REST API 역할: 시뮬레이션 시작 / 일시정지 / 재시작 엔드포인트 제공
- CLI 역할: 콘솔에서 `start` / `pause` / `restart` 명령어 입력으로 동일한 제어 가능 (별도 스레드에서 stdin 읽기)
- gRPC 서버 역할: 현재 시뮬레이션 상태(차량 위치, 빈 택시 수, 현재 시각) 노출
- gRPC 클라이언트 역할: Dispatch Service로부터 인센티브 레벨 및 재라우팅 명령 수신

**2. Prediction Service**
- 별도 팀이 개발한 딥러닝 모델의 추론(inference)만 담당 (학습 제외)
- 시뮬레이션 시계 기준 5분마다 자동 트리거
- 입력: 현재 시뮬레이션 시각 (SUMO Service에서 gRPC로 수신)
- 출력: t+1~t+6 각 5분 구간별 역삼동 예측 호출 수 배열
- gRPC 서버 역할: 예측 결과를 Dispatch Service에 제공
- **주의**: 모델 입출력 상세 스펙은 모델 팀과 협의 후 확정 필요

**3. Dispatch Service**
- gRPC 클라이언트로 SUMO Service(시뮬레이션 상태)와 Prediction Service(예측 수요) 모두 소비
- 5분 주기로 수급 불균형 지표 계산: `imbalance = predicted_demand - available_taxis`
- 불균형 심각도에 비례한 인센티브 레벨 결정 (0.0 ~ 1.0 정규화)
- 빈 택시의 재라우팅 여부를 인센티브 레벨에 따른 확률로 결정 (확률론적)
- 배차: 대기 승객과 가장 가까운 빈 택시를 자동 매칭 (유클리드 거리 기반)
- gRPC 서버 역할: 인센티브 레벨 및 재라우팅 명령을 SUMO Service에 전달

**탑승 후 운행 (변경 가능)**
- 승객의 하차 지점은 역삼동 도로 네트워크 내 랜덤 엣지로 설정
- 택시가 하차 지점에 도달하면 즉시 `empty` 상태로 복귀하여 다음 배차 대기
- 하차 지점은 시각화에 불필요하므로 WebSocket `passengers` 메시지에 포함하지 않음
- *추후 실제 운행 데이터 기반 목적지 분포로 교체 가능*

### gRPC 통신 흐름

```
[Prediction Service] --(예측 수요: t+1~t+6)--> [Dispatch Service]
[SUMO Service] --(시뮬레이션 상태: 차량 위치, 빈 택시 수, 시각)--> [Dispatch Service]
[Dispatch Service] --(인센티브 레벨, 재라우팅 대상 택시 ID 목록)--> [SUMO Service]
[SUMO Service] --(현재 시뮬레이션 시각)--> [Prediction Service]
```

### WebSocket 메시지 스펙

**boundary** (연결 직후 1회 전송)
```json
{
  "type": "boundary",
  "minX": 0.0, "minY": 0.0,
  "maxX": 1928.66, "maxY": 1902.15
}
```

**vehicles** (100ms 간격)
```json
{
  "type": "vehicles",
  "vehicles": [
    { "id": "veh_0", "x": 964.3, "y": 951.1, "angle": 90.0, "state": "empty" }
  ]
}
```
state 값: `car` (일반 차량) / `empty` (빈 택시) / `dispatched` (픽업 이동 중) / `occupied` (승객 탑승 중)

**passengers** (100ms 간격)
```json
{
  "type": "passengers",
  "passengers": [
    { "id": "p_0", "x": 970.1, "y": 940.5 }
  ]
}
```
목록에서 사라진 승객은 Unity 측에서 자동 제거.

### 인프라
- 각 서비스는 독립된 Docker 컨테이너로 배포
- Docker Compose로 전체 시스템 단일 명령 기동
- gRPC proto 파일은 공유 디렉토리에서 관리하여 서비스 간 인터페이스 일관성 유지
- 기술 스택: Python, SUMO/TraCI, FastAPI (REST API + WebSocket), gRPC, TensorFlow 또는 PyTorch
- TraCI는 동기(blocking) API이므로 FastAPI의 async 이벤트 루프와 분리하여 별도 스레드(`asyncio.run_in_executor()`)에서 실행

## Testing Decisions

**좋은 테스트의 기준**: 외부에서 관찰 가능한 동작(출력값, 상태 변화)만 검증하며 내부 구현 세부사항에 의존하지 않는다. 단위 테스트는 외부 의존성(SUMO, gRPC, 딥러닝 모델) 없이 독립 실행 가능해야 한다.

**테스트 대상 모듈: Dispatch Service**

- **수급 불균형 지표 계산**: 예측 수요와 빈 택시 수를 입력으로 받아 imbalance score를 올바르게 계산하는지 검증
- **인센티브 레벨 결정**: imbalance score 범위별로 인센티브 레벨이 0.0~1.0 사이에서 적절하게 산출되는지 검증
- **확률론적 재라우팅**: 인센티브 레벨 0.0일 때 재라우팅 확률 0, 1.0일 때 최대 확률임을 통계적으로 검증 (다수 반복 시뮬레이션)
- **최근접 택시 배차**: 여러 빈 택시와 승객이 주어졌을 때 항상 가장 가까운 택시가 배차되는지 검증 (유클리드 거리 기반)
- **엣지 케이스**: 빈 택시 0대일 때 배차 없음, 승객 0명일 때 인센티브 불필요 등

## Out of Scope

- **Module 2 (피처 엔지니어링)**: 택시 호출 데이터의 전처리 및 피처 생성은 별도 팀 담당
- **딥러닝 모델 학습**: 모델 개발 및 학습은 별도 팀 담당. 이 레포는 추론 호출만 수행
- **실제 실시간 데이터 연동**: 기상청 API, 실시간 교통 API 등 외부 데이터 수집은 범위 외
- **사용자 인증 및 권한 관리**: API 인증/인가 시스템
- **모니터링 및 알람 시스템**: Prometheus, Grafana 등 운영 모니터링
- **CI/CD 파이프라인**: 자동화된 빌드/배포 파이프라인
- **다중 지역 지원**: 역삼동 외 다른 지역으로의 확장

## Further Notes

- **딥러닝 모델 I/O 스펙 미확정**: Prediction Service의 gRPC 인터페이스 설계는 모델 팀과의 협의 완료 후 진행해야 한다. 이 부분이 전체 파이프라인의 critical path임.
- **시뮬레이션 시계 동기화**: 세 서비스 모두 SUMO의 시뮬레이션 시계를 기준 시각으로 사용하므로, SUMO Service가 항상 시각 정보의 단일 진실 공급원(single source of truth)이 되어야 한다.
- **OSM 네트워크 전처리**: `netconvert`로 생성한 `.net.xml`에서 택시가 진입 불가한 도로 유형(보행자 전용 등) 필터링이 필요할 수 있다.
- **Poisson 승객 생성 파라미터**: 예측 호출 수를 lambda로 사용하는 Poisson 분포로 각 5분 구간 승객 수를 샘플링한다. 이 로직은 SUMO Service 내부에 구현된다.
