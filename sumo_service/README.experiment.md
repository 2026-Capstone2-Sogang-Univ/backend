# 기사 수락 확률/인센티브 실험 가이드

이 문서는 SUMO 시뮬레이션에서 기사 수락 확률 목표값(`target_p`)을 달성하기 위해 필요한 추가 인센티브를 계산하고, 그 정책이 매칭률과 비용에 어떤 영향을 주는지 실험하는 방법을 설명한다.

## 실험 목적

이 실험은 기사 수락 모델을 단순 검산하는 용도가 아니다. 목표 수락 확률 `target_p`를 입력하면 모델을 역산해 필요한 운임 수준을 구하고, 현재 서지 운임과의 차이를 추가 인센티브로 지급했을 때 다음 KPI가 어떻게 변하는지 확인한다.

- 전체 승객 중 매칭에 성공한 비율
- 기사 1명당 시간당 수락 건수
- 기사 1명당 시간당 수익
- 기사 빈차 대기 시간
- 실제 수락된 배차에 지급된 인센티브 총액
- 인센티브 cap 때문에 목표 확률을 달성하지 못한 정도

## 핵심 입력값

필수 입력은 세 가지다.

- `target_p`: 목표 기사 수락 확률
- `elasticity`: 서지 계산 탄력성 계수
- `beta_f`: 기사 수락 모델의 운임 계수 override 값

`elasticity`는 H3 pickup cell별 서지 계산에만 사용된다.

```text
raw_surge = (demand / supply) ** (1 / elasticity)
surge = 1.0 if raw_surge <= 1.0 else ceil(max(raw_surge, 1.2) / 0.1) * 0.1
surge = min(surge, 4.0)
```

즉, 수요가 공급 이하이면 중립 배수 `1.0x`를 유지하고, 초과 수요가 발생하면 최소 `1.2x`부터 `0.1x` 단위로 상승하며 최대 `4.0x`로 제한한다.

`beta_f`는 수락 확률 정방향 계산과 목표 확률 역산에 모두 같은 값으로 사용된다.

## 실행 방식

FastAPI 서버를 띄우지 않고 `SimulationManager`를 직접 실행한다. 각 파라미터 조합마다 새로운 `SimulationManager`, SUMO 프로세스, TraCI 연결, 메모리 이벤트 로그를 만든다.

실험 스크립트는 `app/simulation.py`에 정의된 기존 SUMO 설정을 그대로 사용한다.

- SUMO config: `sumo_configs/NY/manhattan.sumocfg`
- SUMO network: `sumo_configs/NY/manhattan_car_only.net.xml`
- routable SCC cache: `sumo_configs/NY/routable_scc.json`
- parquet 승객 replay 파일: `sumo_configs/NY/trips_processed.json`

즉, 별도의 실험용 `.sumocfg`를 새로 만들지 않고 현재 Manhattan NY 시뮬레이션 설정 위에서 빠른 실행 모드만 적용한다. SUMO binary는 기본적으로 `sumo`를 사용하며, 환경변수 `SUMO_GUI=1`을 지정하면 기존 코드와 동일하게 `sumo-gui`를 사용한다.

실험 모드는 일반 WebSocket 실행과 다르게 동작한다.

- WebSocket broadcast 비활성화
- DB 기록 비활성화
- wall-clock sleep 비활성화
- SUMO step만 빠르게 진행
- KPI 집계를 위한 이벤트만 메모리에 기록

기본 실험 시간은 SUMO 시뮬레이션 시간 기준 3600초, 즉 1시간이다.

## 실행 명령

먼저 `sumo_service` 디렉터리에서 실행한다.

```powershell
cd .\sumo_service
```

단일 조합 실행:

```powershell
uv run scripts\run_acceptance_experiment.py `
  --target-p 0.85 `
  --elasticity 0.6 `
  --beta-f 0.006
```

여러 조합 sweep 실행:

```powershell
uv run scripts\run_acceptance_experiment.py `
  --target-p-list 0.7,0.8,0.9 `
  --elasticity-list 0.4,0.6 `
  --beta-f-list 0.003,0.006
```

CSV 파일에 append 저장:

```powershell
uv run scripts\run_acceptance_experiment.py `
  --target-p-list 0.7,0.8,0.9 `
  --elasticity-list 0.4,0.6 `
  --beta-f-list 0.003,0.006 `
  --csv-output ..\.temp\acceptance_experiment_results.csv
```

기본 seed는 `42`다. 변경하려면 `--seed`를 지정한다.

```powershell
uv run scripts\run_acceptance_experiment.py `
  --target-p 0.85 `
  --elasticity 0.6 `
  --beta-f 0.006 `
  --seed 123
```

실험 시간을 줄여 빠르게 smoke test를 하고 싶으면 `--sim-duration`을 줄인다.

```powershell
uv run scripts\run_acceptance_experiment.py `
  --target-p 0.85 `
  --elasticity 0.6 `
  --beta-f 0.006 `
  --sim-duration 300
```

## 출력 형식

stdout은 항상 JSON 배열이다. 단일 조합을 실행해도 배열 하나짜리로 출력된다.

```json
[
  {
    "status": "ok",
    "reason": null,
    "params": {
      "target_p": 0.85,
      "elasticity": 0.6,
      "beta_f": 0.006,
      "seed": 42
    },
    "metrics": {
      "spawned_passengers": 42,
      "unique_matched_passengers": 31,
      "matching_success_rate": 0.738,
      "accepted_dispatch_count": 34,
      "acceptances_per_driver_hour": 0.113,
      "driver_revenue_per_hour_usd": 1.42,
      "avg_empty_wait_time_s": 284.2,
      "p50_empty_wait_time_s": 210.0,
      "p95_empty_wait_time_s": 820.0,
      "incentive_cost_total_usd": 126.5,
      "capped_dispatch_attempt_rate": 0.18,
      "avg_target_gap_when_capped": 0.11,
      "avg_actual_acceptance_probability": 0.79,
      "avg_required_fare_usd": 18.4,
      "avg_incentive_usd": 3.72
    }
  }
]
```

SUMO 초기화 로그는 JSON 파싱을 방해하지 않도록 stderr로 분리된다.

## KPI 설명

`spawned_passengers`  
실험 중 생성된 전체 승객 수다.

`unique_matched_passengers`  
한 번 이상 수락된 배차를 받은 unique 승객 수다. 같은 승객에게 여러 번 배차 시도가 있어도 승객 기준으로 한 번만 센다.

`matching_success_rate`  
생성 승객 중 매칭 성공 승객 비율이다.

```text
unique_matched_passengers / spawned_passengers
```

`accepted_dispatch_count`  
기사에게 제안된 배차 중 실제 수락된 배차 수다.

`acceptances_per_driver_hour`  
전체 택시 수와 실험 시간을 기준으로 환산한 기사 1명당 시간당 수락 건수다.

```text
accepted_dispatch_count / (N_TAXIS * (SIM_DURATION / 3600))
```

`driver_revenue_per_hour_usd`  
완료된 trip의 실제 운임 합계를 전체 택시 수와 실험 시간으로 나눈 기사 1명당 시간당 수익이다. 실험 종료 시 강제 완료된 trip은 완료 기반 운임 지표에서 제외한다.

```text
sum(fare_usd for completed trips) / (N_TAXIS * (SIM_DURATION / 3600))
```

`avg_empty_wait_time_s`  
기사의 이전 하차 시점부터 다음 배차 수락 시점까지의 평균 빈차 대기 시간이다. 첫 승객 전 대기시간은 제외한다.

`p50_empty_wait_time_s`, `p95_empty_wait_time_s`  
빈차 대기 시간의 중앙값과 95퍼센타일이다. 표본이 부족하면 `null`일 수 있다.

`incentive_cost_total_usd`  
실제로 수락된 배차에 지급된 추가 인센티브 총액이다. 서지로 올라간 운임은 정책 비용에 포함하지 않는다.

`capped_dispatch_attempt_rate`  
전체 dispatch decision 중 인센티브 cap에 걸린 비율이다.

`avg_target_gap_when_capped`  
cap에 걸린 decision에서 `target_p - p_actual`의 평균이다. 값이 클수록 목표 수락 확률을 cap 때문에 달성하지 못했다는 뜻이다.

`avg_actual_acceptance_probability`  
실험 중 계산된 실제 수락 확률 `p_actual`의 평균이다.

`avg_required_fare_usd`  
목표 수락 확률을 만족하기 위해 모델이 요구한 운임의 평균이다.

`avg_incentive_usd`  
제안된 dispatch decision 기준 추가 인센티브 평균이다. 실제 비용은 `incentive_cost_total_usd`처럼 수락된 배차만 합산한다.

## 인센티브 계산 방식

각 배차 decision에서 다음 순서로 계산한다.

```text
base_fare_usd = expected_fare / 100
surge = cached_surge[pickup_h3] or 1.0
surged_fare_usd = base_fare_usd * surge
required_fare_usd = inverse_fare_for_target_p(...)
raw_incentive_usd = required_fare_usd - surged_fare_usd
incentive_usd = clamp(raw_incentive_usd, 0, min(10.0, base_fare_usd))
fare_amount = surged_fare_usd + incentive_usd
p_actual = acceptance_probability(..., fare_amount)
```

`required_fare_usd <= surged_fare_usd`이면 추가 인센티브는 0이다.

인센티브 cap은 다음 값이다.

```text
min(10.0 USD, base_fare_usd)
```

cap에 걸리면 `p_actual`이 `target_p`보다 낮을 수 있다. 이것은 invalid가 아니라 정책 비용 제약 때문에 목표 확률을 달성하지 못한 정상 결과다.

## Invalid 조합

다음 조합은 SUMO를 실행하지 않고 invalid row만 출력한다.

- `target_p * c >= 1`
- `abs(beta_f) < 1e-9`

예시:

```json
[
  {
    "status": "invalid",
    "reason": "beta_f too close to zero for inverse fare calculation",
    "params": {
      "target_p": 0.8,
      "elasticity": 0.6,
      "beta_f": 0.0,
      "seed": 42
    },
    "metrics": null
  }
]
```

CSV에도 invalid row가 남고 metric 컬럼은 비워진다.

## 결과 해석 방법

`target_p`를 올렸는데 `matching_success_rate`가 크게 오르지 않는다면 먼저 `capped_dispatch_attempt_rate`와 `avg_target_gap_when_capped`를 확인한다. 두 값이 높으면 운임 모델상 더 큰 인센티브가 필요하지만 cap 때문에 지급하지 못한 상황이다.

`avg_required_fare_usd`가 비정상적으로 크거나 음수 방향으로 움직이면 `beta_f` 설정을 확인해야 한다. `beta_f`는 실험 입력으로 음수도 허용되지만, 음수 값에서는 운임을 올릴수록 수락 확률이 낮아지는 해석이 가능하므로 결과를 별도로 검토해야 한다.

`incentive_cost_total_usd`는 실제 수락된 배차만 비용으로 계산한다. 정책 예산 관점에서는 이 값을 보고, 제안 자체가 얼마나 비싸졌는지는 `avg_incentive_usd`를 함께 본다.

`avg_empty_wait_time_s`는 완료된 trip 이후 다음 수락까지의 시간만 반영한다. 실험 종료 시점에 아직 완료되지 않은 trip은 매칭 성공률과 수락 건수에는 포함되지만, 빈차 대기 시간 계산에는 포함되지 않는다.

## 구현 위치

- `app/driver/decision_function.py`: 수락 확률 계산, PU 보정, 목표 확률 기반 운임 역산
- `app/grid.py`: 서지 계산과 `elasticity` 적용
- `app/simulation.py`: 실험 모드 실행, 인센티브 계산, 메모리 이벤트 로그
- `scripts/run_acceptance_experiment.py`: CLI 인자 파싱, sweep 실행, KPI 집계, JSON/CSV 출력
- `tests/test_decision_function.py`: 역산 함수와 invalid 조건 테스트

## QnA

### 1. 승객의 콜이 거절당했을 때, 다른 택시한테 콜이 요청되는가?

그렇다. 승객은 배차 제안이 거절되면 계속 `waiting` 상태로 남는다. 거절 이벤트는 `dispatch_decision`에 `accepted=false`로 기록되지만, 승객 상태를 `assigned`로 바꾸거나 대기열에서 제거하지 않는다.

따라서 같은 step에서 뒤이어 처리되는 다른 empty taxi가 같은 승객을 다시 후보로 볼 수 있고, 이후 step에서도 계속 재시도될 수 있다. 승객이 대기열에서 빠지는 시점은 어떤 택시가 해당 콜을 수락해 `assigned` 상태가 되었을 때다.

### 2. 첫 번째 택시가 콜을 수락했을 때에만 "매칭 성공"으로 간주되는가?

아니다. 매칭 성공은 첫 번째 택시의 수락 여부가 아니라, 해당 승객에게 `accepted=true`인 dispatch decision이 한 번이라도 있었는지로 판단한다.

즉 첫 번째 택시가 거절해도 나중에 다른 택시가 수락하면 그 승객은 매칭 성공에 포함된다. KPI 계산에서는 수락된 dispatch decision들의 `passenger_id`를 set으로 모아 unique 승객 수를 세고, 이를 전체 생성 승객 수로 나누어 `matching_success_rate`를 계산한다.

### 3. 승객의 콜은 몇 분 동안 지속되는가?

현재 구현 기준으로 승객 콜 자체에는 별도 만료 시간이 없다. 승객이 `waiting` 상태인 동안에는 수락될 때까지 대기열에 남고, 계속 다른 택시들의 후보로 올라갈 수 있다.

다만 어떤 택시가 콜을 수락해 승객이 `assigned` 상태가 된 뒤에는 배차 타임아웃이 적용된다. 택시가 `DISPATCH_TIMEOUT_S = 600.0`초, 즉 10분 안에 픽업하지 못하면 해당 배차는 타임아웃되고 승객은 삭제되지 않은 채 다시 `waiting` 상태로 돌아간다.

따라서 콜 요청 유효시간 관점에서는 무기한이고, 한 번 수락된 배차가 픽업을 기다리는 시간은 10분이다.
