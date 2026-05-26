# Acceptance Experiment Design

## 목적

목표 수락 확률 `P*`, 탄력성 계수 `elasticity`, 가격 민감도 `beta_F`를 실험 입력으로 받아 SUMO 시뮬레이션 시간 기준 1시간을 빠르게 실행하고, 기사 수락/거절 및 인센티브 정책이 핵심 KPI에 미치는 영향을 산출한다.

핵심 목적은 단순히 수락 확률 모델을 검산하는 것이 아니라, `P*`를 달성하기 위해 필요한 추가 인센티브를 역산하고, 그 결과가 매칭 성공률, 기사 시간당 수락 건수, 기사 시간당 수익, 기사 빈차 대기 시간, 인센티브 총 지출 비용에 어떤 tradeoff를 만드는지 확인하는 것이다.

## 실험 입력

### 필수 입력

- `target_p`: 목표 수락 확률 `P*`
- `elasticity`: 서지 공식의 탄력성 계수
- `beta_f`: 기존 기사 수락 모델의 `beta_F`를 실험 입력값으로 덮어쓴 값

### sweep 입력

실험 runner는 처음부터 sweep 구조로 만든다. 단일 조합도 sweep의 특수한 경우로 처리한다.

예시:

```bash
--target-p 0.85 --elasticity 0.6 --beta-f 0.006
```

```bash
--target-p-list 0.7,0.8,0.9 --elasticity-list 0.4,0.6 --beta-f-list 0.003,0.006
```

여러 리스트가 들어오면 Cartesian product로 모든 조합을 실행한다.

### seed

같은 `base seed`를 사용하되, 각 결과 row에 실제 사용 seed를 명시적으로 기록한다.

초기 설계는 모든 조합에 같은 seed를 사용한다. 이후 필요하면 `--replications`를 추가해 `seed`, `seed + 1`, `seed + 2` 방식으로 반복 평균을 낼 수 있다.

## 실행 방식

FastAPI 서버를 거치지 않고 내부 `SimulationManager`를 직접 실행한다.

각 파라미터 조합마다 다음을 새로 생성한다.

- 새 `SimulationManager`
- 새 SUMO 프로세스/TraCI 연결
- 새 메모리 이벤트 로그
- 동일 seed 기반 난수 상태

조합 간 시뮬레이션 상태, 택시 위치, 승객 큐, TraCI 연결이 섞이면 KPI 비교가 망가지므로 `SimulationManager` 재사용은 하지 않는다.

## 빠른 실행 모드

일반 모드와 별도로 실험용 빠른 실행 경로를 둔다.

### 일반 모드

- `traci.simulationStep()`
- state capture
- WebSocket broadcast queue push
- wall-clock pacing sleep

### 실험 모드

- `traci.simulationStep()`
- passenger / dispatch / trip update
- memory event log
- WebSocket broadcast off
- wall-clock sleep off

현재 코드도 이미 `traci.simulationStep()`으로 수동 step 진행을 하고 있다. 일반 실행이 느린 이유는 SUMO가 실시간에 묶여 있어서가 아니라 `time.sleep()` 기반 pacing이 걸려 있기 때문이다.

실험 모드 기본값:

```text
experiment_step_length = 1.0 simulated second
real_sleep = 0
broadcast = off
```

`--step-length`는 실험 모드 기본 `1.0`초로 둔다. `0.333`초보다 step 수가 적어 빠르고, `2.0`초 이상보다 픽업/하차 감지 및 요금 누적 왜곡 위험이 낮다.

## 서지 계산

`elasticity`는 서지 공식에만 사용한다.

```text
raw_surge = (demand / supply) ** (1 / elasticity)
surge = 1.0 if raw_surge <= 1.0 else ceil(max(raw_surge, 1.2) / 0.1) * 0.1
surge = min(surge, 4.0)
```

기존과 같이 5 simulated seconds마다 grid supply/demand를 갱신하고, 각 콜 계산에서는 최신 cached surge를 사용한다.

수요가 공급 이하이면 할인 서지를 적용하지 않고 `1.0x`를 유지한다. 초과 수요가 발생하면 서지 발동 최솟값은 `1.2x`, 상승 단위는 `0.1x`, 상한은 `4.0x`로 둔다.

서지는 pickup H3 cell 기준으로 적용한다.

```text
surge = cached_surge[pickup_h3] or 1.0
surged_fare = expected_fare_usd * surge
```

`elasticity`는 지역별 수요/공급 불균형이 기본 운임 또는 base surge에 얼마나 반영되는지만 조절한다. 역산 인센티브 공식에는 직접 넣지 않는다.

## 가격 역산 및 인센티브

`target_p`는 실제로 역산에 사용한다.

흐름:

```text
P* -> required_fare 역산 -> incentive 산출 -> fare_amount 결정
-> 정방향 acceptance probability 재계산 -> 난수로 수락/거절
```

역산 결과는 최종 운임이 아니라 `surged_fare`에 추가되는 인센티브 기준으로 해석한다.

```text
base_fare_usd = expected_fare / 100
surged_fare = base_fare_usd * surge
required_fare = inverse_fare_for_target_p(...)
raw_incentive = required_fare - surged_fare
incentive_cap = min(10.00, base_fare_usd * 1.0)
incentive = clamp(raw_incentive, 0, incentive_cap)
fare_amount = surged_fare + incentive
```

`required_fare <= surged_fare`이면 인센티브는 0으로 둔다.

```text
raw_incentive = required_fare - surged_fare
incentive = max(0, raw_incentive)
```

인센티브 총 지출 비용은 서지 운임을 제외하고 추가 인센티브만 합산한다.

```text
incentive_cost_total_usd = sum(incentive_usd for accepted dispatches)
```

## PU 보정 반영

현재 정방향 수락 모델은 다음 구조다.

```text
P = sigmoid(z) / c
```

따라서 목표가 최종 수락확률 `target_p`라면 역산은 PU 보정 상수 `c`를 반영한다.

```text
sigmoid(z_target) = target_p * c
z_target = log((target_p * c) / (1 - target_p * c))
```

`target_p * c >= 1`이면 수학적으로 불가능하므로 해당 조합은 invalid로 기록하고 시뮬레이션을 건너뛴다.

## decision_function 리팩터링 원칙

역산과 정방향 수락 확률 계산이 어긋나지 않도록 기존 `acceptance_probability()` 내부 계산을 쪼개서 재사용한다.

권장 구조:

```python
def acceptance_features(...):
    return AcceptanceFeatures(
        dV_without_fare=...,
        t_pu=...,
        t_c=...,
        z_without_fare=...
    )

def probability_from_z(z, c):
    return clip(sigmoid(z) / c, 0, 1)

def acceptance_probability(...):
    features = acceptance_features(...)
    z = features.z_without_fare + beta_f * fare_amount
    return probability_from_z(z, c)

def required_fare_for_target_p(...):
    features = acceptance_features(...)
    z_target = logit(target_p * c)
    return (z_target - features.z_without_fare) / beta_f
```

`beta_f`가 0에 가까우면 역산이 불가능하다.

```text
if abs(beta_f) < 1e-9:
    status = "invalid"
    reason = "beta_f too close to zero for inverse fare calculation"
```

`beta_f < 0`은 금지하지 않는다. 기존 학습 결과에서 가격 계수 부호가 불안정한 맥락이 있으므로, 음수 실험도 분석 가능하게 둔다.

## cap 처리

인센티브 cap은 역산 공식이 산출한 `required_fare`를 무제한으로 지급하지 않기 위해 둔다.

목표 수락 확률 `P*`를 높게 잡거나, pickup distance가 길거나, 기사 수락 모델의 가격 민감도 `beta_f`가 낮으면 `required_fare`가 비현실적으로 커질 수 있다. 이 값을 그대로 인센티브로 지급하면 실험 결과가 "돈을 충분히 주면 수락률이 오른다"는 자명한 결론으로 수렴하고, 정책 비용의 현실성도 사라진다.

따라서 cap은 다음 역할을 한다.

- 정책 예산 제약을 시뮬레이션에 반영한다.
- 과도한 `target_p` 조합이 현실적으로 달성 가능한지 검증한다.
- 매칭률 개선과 인센티브 비용 사이의 tradeoff를 드러낸다.
- 모델이 요구하는 가격 인상이 운영 정책으로 감당 가능한 수준인지 판단하게 한다.

현재 cap은 다음처럼 정의한다.

```text
incentive_cap = min(10.00, base_fare_usd * 1.0)
```

즉, 추가 인센티브는 최대 10달러이면서 동시에 기본 예상 운임의 100%를 넘지 못한다. 예를 들어 기본 예상 운임이 7달러면 cap은 7달러이고, 기본 예상 운임이 18달러면 cap은 10달러다.

인센티브가 cap에 걸려 `p_actual < target_p`가 되는 경우는 정상 실행한다.

이 상황은 invalid가 아니라 현실성 제약 때문에 목표 수락확률을 만족하지 못한 중요한 실험 결과다.

cap이 KPI에 미치는 영향은 다음과 같다.

- `p_actual`이 `target_p`보다 낮아질 수 있다.
- `matching_success_rate`가 `target_p` 증가폭만큼 오르지 않을 수 있다.
- `capped_dispatch_attempt_rate`가 높을수록 목표 수락확률을 달성하려는 시도가 예산 제약에 자주 막혔다는 뜻이다.
- `avg_target_gap_when_capped`가 클수록 cap 때문에 실제 수락확률이 목표에서 크게 벗어났다는 뜻이다.
- `incentive_cost_total_usd`는 cap 때문에 무한히 커지지 않지만, 그만큼 매칭률 개선도 제한될 수 있다.
- `driver_revenue_per_hour_usd`는 서지와 인센티브로 증가할 수 있으나, cap에 자주 걸리면 목표 수락확률 기반 기대치보다 낮게 나타날 수 있다.

따라서 cap은 단순한 방어 로직이 아니라 실험 해석의 핵심 변수다. `target_p`를 높였는데 KPI가 개선되지 않는다면 먼저 `capped_dispatch_attempt_rate`와 `avg_target_gap_when_capped`를 확인해, 실패 원인이 기사 수락 모델의 구조인지, 수요/공급 불균형인지, 아니면 cap으로 인한 정책 예산 제약인지 분리해야 한다.

각 dispatch decision 이벤트에는 다음 값을 기록한다.

```json
{
  "target_p": 0.85,
  "p_actual": 0.72,
  "capped": true,
  "target_gap": 0.13
}
```

정의:

```text
capped = incentive != raw_incentive after clamp
target_gap = target_p - p_actual
```

## 메모리 이벤트 로그

실험 모드는 DB에 쓰지 않고 내부 메모리 로그로 집계한다.

필수 이벤트:

- `passenger_spawned`
- `dispatch_attempted`
- `dispatch_decision`
- `trip_completed`

권장 필드:

### passenger_spawned

- `sim_time`
- `passenger_id`
- `pickup_h3`
- `dropoff_h3`
- `expected_fare_usd`
- `expected_distance_m`

### dispatch_decision

- `sim_time`
- `dispatch_id`
- `passenger_id`
- `taxi_id`
- `accepted`
- `target_p`
- `p_actual`
- `base_fare_usd`
- `surge`
- `surged_fare_usd`
- `required_fare_usd`
- `raw_incentive_usd`
- `incentive_usd`
- `capped`
- `target_gap`
- `estimated_pickup_distance_m`

### trip_completed

- `sim_time`
- `passenger_id`
- `taxi_id`
- `dispatch_id`
- `dispatch_sim_time`
- `pickup_sim_time`
- `dropoff_sim_time`
- `fare_usd`
- `completion`

## KPI 정의

### 매칭 성공률

생성된 전체 승객 중 배차 수락된 승객 비율.

동일 승객이 여러 기사에게 제안될 수 있으므로 수락된 dispatch 수가 아니라 unique passenger 기준으로 계산한다.

```text
matching_success_rate =
    unique_passengers_with_accepted_dispatch / spawned_passengers
```

### 기사 시간당 수락 건수

전체 택시 대수 기준으로 계산한다.

```text
acceptances_per_driver_hour =
    accepted_dispatch_count / (N_TAXIS * (SIM_DURATION / 3600))
```

### 기사 시간당 수익

완료된 trip의 실제 운임 합계를 전체 택시 대수와 실험 시간으로 나눈다.

```text
driver_revenue_per_hour_usd =
    sum(fare_usd for completed trips) / (N_TAXIS * (SIM_DURATION / 3600))
```

실험 종료 시점에 강제 완료된 trip은 완료 기반 운임 지표에서 제외한다.

### 기사 빈차 대기 시간

기사 관점의 search time이다.

```text
empty_wait_time =
    next_accepted_dispatch_sim_time - previous_dropoff_sim_time
```

첫 승객 전 대기시간은 제외한다.

실험 종료 시점에 완료되지 않은 진행 중 trip은 빈차 시간 계산에서 제외한다.

기본 출력은 평균값이고, 가능하면 p50/p95도 추가할 수 있다.

### 인센티브 총 지출 비용

서지 운임을 제외하고 추가 인센티브만 합산한다.

```text
incentive_cost_total_usd = sum(incentive_usd for accepted dispatches)
```

정책 비용 관점에서는 실제 수락된 dispatch에 지급된 인센티브만 비용으로 본다.

## 진행 중 trip 처리

1시간 실험 종료 시 아직 완료되지 않은 trip은 다음처럼 처리한다.

포함:

- 매칭 성공률
- 수락 건수

제외:

- 기사 빈차 대기 시간
- 운임 완료 기반 지표 (`driver_revenue_per_hour_usd` 포함)

이유는 배차 수락이 발생했다면 매칭 성공으로 보는 정의를 택했지만, 빈차 시간과 수익은 완료된 trip의 `dropoff_sim_time`과 `fare_usd`가 있어야 계산 가능하기 때문이다.

`forced_at_end` 완료도 빈차 시간 계산에서는 제외한다.

## 출력 메트릭

핵심 KPI:

- `spawned_passengers`
- `unique_matched_passengers`
- `matching_success_rate`
- `accepted_dispatch_count`
- `acceptances_per_driver_hour`
- `driver_revenue_per_hour_usd`
- `avg_empty_wait_time_s`
- `incentive_cost_total_usd`

진단 지표:

- `capped_dispatch_attempt_rate`
- `avg_target_gap_when_capped`
- `avg_actual_acceptance_probability`
- `avg_required_fare_usd`
- `avg_incentive_usd`

진단 지표는 `P*`를 올렸는데 매칭률이 기대만큼 오르지 않는 이유가 cap 때문인지, 모델의 가격 민감도 때문인지 해석하는 데 필요하다.

## invalid 조합 처리

다음 조건은 시뮬레이션을 실행하지 않고 invalid row만 기록한다.

- `target_p * c >= 1`
- `abs(beta_f) < 1e-9`

결과 예시:

```json
{
  "status": "invalid",
  "reason": "target_p * c must be < 1",
  "params": {
    "target_p": 0.99,
    "elasticity": 0.6,
    "beta_f": 0.006,
    "seed": 42
  },
  "metrics": null
}
```

CSV에도 invalid row를 남긴다. `status`, `reason` 컬럼으로 원인을 표시하고 metric 컬럼은 비워둔다.

## 출력 형식

stdout은 항상 JSON 배열로 출력한다. 단일 조합도 배열 한 개짜리로 출력한다.

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

CSV 저장은 선택 옵션으로 둔다.

```bash
--csv-output .temp/acceptance_experiment_results.csv
```

CSV에는 sweep 결과를 append할 수 있게 한다.

권장 컬럼:

```text
status, reason, target_p, elasticity, beta_f, seed,
spawned_passengers, unique_matched_passengers, matching_success_rate,
accepted_dispatch_count, acceptances_per_driver_hour, driver_revenue_per_hour_usd,
avg_empty_wait_time_s, incentive_cost_total_usd,
capped_dispatch_attempt_rate, avg_target_gap_when_capped,
avg_actual_acceptance_probability, avg_required_fare_usd, avg_incentive_usd
```

## 구현 파일 계획

### 수정 대상

- `sumo_service/app/driver/decision_function.py`
  - feature extraction / probability / inverse fare 계산 함수로 분리
  - 실험 입력 `beta_f` override 지원

- `sumo_service/app/simulation.py`
  - experiment mode config 추가
  - broadcast off / sleep off 경로 추가
  - memory event log hook 추가
  - cached pickup-cell surge 기반 fare/incentive 계산 적용

### 신규 파일

- `sumo_service/scripts/run_acceptance_experiment.py`
  - CLI 인자 parsing
  - 단일 조합 및 sweep 실행
  - invalid 사전 검증
  - 각 조합마다 새 `SimulationManager` / SUMO 실행
  - KPI 집계
  - stdout JSON 배열 출력
  - 선택적 CSV append

## 현재 구현 정책과 AI 예측 기반 정책 비교를 위한 추가 구현

현재 구현 내용은 **현재 수요 기반 정책(actual-demand baseline)** 으로 본다.

현재 수요 기반 정책은 시뮬레이션 루프에서 매 step마다 관측되는 대기 승객을 H3 pickup cell별로 집계해 `grid_demand`를 만들고, 이 값을 서지 계산의 `demand`로 사용한다.

```text
current waiting passengers -> grid_demand[pickup_h3]
raw_surge = (grid_demand[pickup_h3] / current_supply[pickup_h3]) ** (1 / elasticity)
surge = apply_surge_policy(raw_surge)
```

AI 수요 예측 기반 정책(predicted-demand policy)은 이 `demand` 입력만 Module 3의 미래 수요 예측값으로 바꾼다.

```text
Module 3 predicted demand at t + horizon -> predicted_demand[pickup_h3]
raw_surge = (predicted_demand[pickup_h3] / current_supply[pickup_h3]) ** (1 / elasticity)
surge = apply_surge_policy(raw_surge)
```

비교 실험의 목적은 같은 `target_p`, `elasticity`, `beta_f`, `seed`, 승객 replay, 택시 초기 상태에서 **서지 계산에 쓰는 수요 입력만 바꿨을 때** KPI가 개선되는지 확인하는 것이다.

### Module 3 예측 결과 파일 계약

실시간 모델 서빙은 필수 구현 범위로 두지 않는다. 우선 Module 3가 실험 기간 전체에 대한 예측 결과를 offline batch로 생성하고, 실험 runner가 이를 lookup한다.

권장 파일:

```text
.temp/module3_predictions.parquet
```

필수 컬럼:

```text
target_time, h3, predicted_demand
```

여러 horizon을 한 파일에 담을 경우:

```text
base_time, target_time, horizon_min, h3, predicted_demand
```

정의:

- `base_time`: 예측을 수행한 기준 시각
- `target_time`: 예측 대상 시각 (`base_time + horizon_min`)
- `horizon_min`: 예측 horizon. 기본값은 15
- `h3`: H3 level 9 pickup cell
- `predicted_demand`: 해당 target time과 H3 cell의 예측 수요량

실험 runner는 `sim_time`을 `SIM_BASE_DATETIME + sim_time`으로 변환한 뒤, `prediction_horizon_min`을 더해 `target_time` bucket을 조회한다.

```text
lookup_time = floor_to_15min(SIM_BASE_DATETIME + sim_time + prediction_horizon_min)
predicted_demand = predictions[(lookup_time, pickup_h3)]
```

예측값이 없는 cell은 기본값을 명시적으로 정한다.

```text
missing predicted demand -> 0.0
```

다만 이 정책은 예측 파일의 커버리지 문제를 KPI에 직접 반영하므로, 진단 지표로 missing rate를 함께 기록한다.

### 실시간 추론이 필요한 경우

현재 acceptance experiment의 비교 목적만 놓고 보면 실시간 추론은 필수 조건이 아니다. 시뮬레이션은 과거 승객 데이터를 replay하고, Module 3 예측 대상도 `target_time`, `h3` 기준의 미래 수요이므로 실험 기간 전체 예측값을 미리 생성해 lookup할 수 있다.

```text
Module 3 offline batch inference
-> module3_predictions.parquet
-> experiment runner lookup
-> predicted-demand policy 실행
```

실시간 추론은 다음 조건 중 하나 이상을 만족할 때 필요하다.

- 모델 입력에 시뮬레이션 중 동적으로 변하는 상태가 포함되는 경우
  - 예: 현재 빈 택시 공급, 현재 미매칭 승객 수, 최근 정책 적용 결과, 현재 서지/인센티브, 현재 매칭 실패율
- 정책이 수요를 바꿀 수 있다고 가정하고, 바뀐 정책 상태를 다시 모델 입력으로 넣는 closed-loop 구조를 실험하는 경우
  - 예: 인센티브를 올린 결과 특정 cell의 공급이 증가하고, 그 공급 변화가 다음 15분 수요/매칭 예측에 반영되어야 하는 경우
- 실험이 과거 replay가 아니라 온라인 생성 환경인 경우
  - 예: 승객 발생이 고정 파일이 아니라 시뮬레이션 상태, 외부 이벤트, 사용자 입력에 따라 매번 달라지는 경우
- 예측 feature가 실행 시점의 외부 데이터에 의존하는 경우
  - 예: 실시간 날씨 API, 실시간 교통량, 이벤트 API, 장애/공사 정보
- 최종 데모 요구사항이 "현재 시뮬레이션 상태를 모델 서버에 보내고 즉시 예측 응답을 받는다"는 시스템 연동 자체를 보여주는 것인 경우
- 실제 운영 시스템처럼 미래 기간의 입력 데이터를 사전에 모두 알 수 없는 경우

위 조건이 없다면 실시간 gRPC/API 서빙은 실험의 필수 구현 범위가 아니라 선택 확장으로 둔다. 우선순위는 다음과 같이 잡는다.

```text
1. offline prediction lookup으로 actual vs predicted KPI 비교
2. 필요 시 같은 입출력 계약을 유지한 채 file lookup을 API/gRPC client로 교체
3. closed-loop 실시간 추론은 최종 확장 실험으로 분리
```

### demand source 옵션 추가

`ExperimentConfig`에 수요 입력 정책을 추가한다.

```python
@dataclass(frozen=True)
class ExperimentConfig:
    target_p: float
    elasticity: float
    beta_f: float
    seed: int = 42
    sim_duration: float = SIM_DURATION
    step_length: float = 1.0
    real_sleep: float = 0.0
    broadcast: bool = False
    demand_source: str = "actual"  # actual | predicted
    prediction_path: str | None = None
    prediction_horizon_min: int = 15
```

CLI 옵션:

```bash
--demand-source actual
```

```bash
--demand-source predicted \
  --prediction-path .temp/module3_predictions.parquet \
  --prediction-horizon-min 15
```

`demand_source=predicted`인데 `prediction_path`가 없거나 파일을 읽을 수 없으면 invalid row가 아니라 실행 전 오류로 처리한다. 이는 파라미터 조합의 수학적 invalid가 아니라 실험 설정 오류이기 때문이다.

### Demand provider 추가

신규 파일:

```text
sumo_service/app/demand_provider.py
```

역할:

```python
class PredictionDemandProvider:
    def demand_by_h3(self, sim_time: float) -> dict[str, float]:
        ...
```

책임:

- Parquet 또는 CSV 예측 파일 로드
- `target_time`, `h3` 기준 lookup table 생성
- `sim_time + prediction_horizon_min`에 해당하는 H3별 예측 수요 반환
- missing prediction count / lookup count 기록

`actual` 정책은 기존 `_capture_state()`가 반환하는 `grid_demand`를 그대로 사용하므로 별도 provider가 없어도 된다.

### surge 계산 경로 변경

`sumo_service/app/simulation.py`의 `_build_surge_cells`는 현재 `grid_supply`, `grid_demand`만 입력받는다. 비교 실험을 위해 `sim_time`을 함께 받고, 실험 설정에 따라 서지 계산용 demand를 선택하게 바꾼다.

```python
def _build_surge_cells(self, grid_supply, grid_demand, sim_time):
    demand_for_surge = grid_demand

    if (
        self.experiment_config is not None
        and self.experiment_config.demand_source == "predicted"
    ):
        demand_for_surge = self._prediction_demand_provider.demand_by_h3(sim_time)

    for cell in set(grid_supply) | set(demand_for_surge):
        surge = compute_surge(
            grid_supply.get(cell, 0),
            demand_for_surge.get(cell, 0),
            elasticity=elasticity,
        )
```

공정한 비교를 위해 `supply`는 두 정책 모두 현재 시뮬레이션의 실제 빈 택시 공급을 사용한다.

```text
actual-demand baseline:
  demand = current waiting passengers by H3
  supply = current empty taxis by H3

predicted-demand policy:
  demand = Module 3 predicted demand by H3 at t + horizon
  supply = current empty taxis by H3
```

### paired comparison 실행

비교는 같은 파라미터 조합과 같은 seed로 actual/predicted를 쌍으로 실행한다.

예시:

```bash
python sumo_service/scripts/run_acceptance_experiment.py \
  --target-p-list 0.7,0.8,0.9 \
  --elasticity-list 0.4,0.6 \
  --beta-f-list 0.003,0.006 \
  --seed 42 \
  --demand-source actual \
  --csv-output .temp/acceptance_actual.csv
```

```bash
python sumo_service/scripts/run_acceptance_experiment.py \
  --target-p-list 0.7,0.8,0.9 \
  --elasticity-list 0.4,0.6 \
  --beta-f-list 0.003,0.006 \
  --seed 42 \
  --demand-source predicted \
  --prediction-path .temp/module3_predictions.parquet \
  --prediction-horizon-min 15 \
  --csv-output .temp/acceptance_predicted.csv
```

runner에 비교 모드를 추가할 경우:

```bash
--compare-demand-sources actual,predicted
```

이 모드는 같은 `(target_p, elasticity, beta_f, seed)`마다 actual run과 predicted run을 모두 실행하고, 결과 row에 `demand_source`를 기록한다.

### 비교 결과 출력 컬럼

기존 CSV 컬럼에 다음 params를 추가한다.

```text
demand_source, prediction_horizon_min, prediction_path
```

predicted 정책의 진단 지표:

```text
prediction_lookup_count
prediction_missing_count
prediction_missing_rate
avg_predicted_demand_for_surge
avg_actual_demand_for_surge
avg_demand_bias
avg_abs_demand_error
avg_surge
```

`avg_actual_demand_for_surge`는 predicted 정책에서도 같은 시점의 실제 `grid_demand`를 함께 기록해 비교한다.

paired comparison 요약 테이블은 별도 후처리로 만든다.

```text
target_p, elasticity, beta_f, seed,
actual_matching_success_rate,
predicted_matching_success_rate,
delta_matching_success_rate,
actual_incentive_cost_total_usd,
predicted_incentive_cost_total_usd,
delta_incentive_cost,
actual_avg_empty_wait_time_s,
predicted_avg_empty_wait_time_s,
delta_avg_empty_wait_time_s,
actual_acceptances_per_driver_hour,
predicted_acceptances_per_driver_hour,
delta_acceptances_per_driver_hour,
actual_driver_revenue_per_hour_usd,
predicted_driver_revenue_per_hour_usd,
delta_driver_revenue_per_hour_usd
```

정책 비용 대비 효과를 보기 위해 다음 파생 지표도 권장한다.

```text
cost_per_matched_passenger =
    incentive_cost_total_usd / unique_matched_passengers
```

### 공정성 조건

actual-demand baseline과 predicted-demand policy 비교에서 다르게 둘 수 있는 것은 하나뿐이다.

```text
surge 계산에 들어가는 demand source
```

고정해야 하는 값:

- `target_p`
- `elasticity`
- `beta_f`
- `seed`
- 승객 replay 데이터
- 택시 초기 생성 및 이동 난수
- `sim_duration`
- `step_length`
- 인센티브 cap
- acceptance probability 계산식
- dispatch matching 방식

이 조건을 지키지 않으면 KPI 차이가 AI 예측 수요 때문인지, 난수나 시뮬레이션 상태 차이 때문인지 분리할 수 없다.

### 해석 기준

predicted-demand policy가 개선됐다고 주장하려면 최소한 다음 중 하나 이상이 actual baseline보다 좋아야 한다.

- 같은 또는 더 낮은 `incentive_cost_total_usd`에서 `matching_success_rate` 상승
- 같은 또는 더 낮은 비용에서 `avg_empty_wait_time_s` 감소
- `cost_per_matched_passenger` 감소
- `acceptances_per_driver_hour` 상승
- `driver_revenue_per_hour_usd` 상승

반대로 predicted 정책의 KPI가 나빠졌다면 다음 진단 지표를 확인한다.

- prediction missing rate가 높은지
- predicted demand가 actual demand보다 체계적으로 낮은지 (`avg_demand_bias < 0`)
- 초고수요 cell에서 과소예측이 발생했는지
- surge가 과도하게 높아져 인센티브 비용만 증가했는지
- incentive cap 때문에 `target_p`를 달성하지 못했는지

## 남은 구현 전 확인 사항

- 실험 모드에서 `SimulationManager`를 직접 실행할 때 async lifecycle을 어떻게 단순화할지
- normal mode와 experiment mode의 코드 중복을 어디까지 허용할지
- cached surge를 passenger pickup H3별로 조회할 수 있도록 현재 `_build_surge_cells` 결과 구조를 dict로도 보관할지
- offered incentive 기준 비용 진단 지표를 별도로 둘지
