# 서지 가격 실험 가이드

이 문서는 `sumo_service`의 현재 Module 4 실험 모드를 설명한다.

현재 구현은 `RideHailingPricingEngine_fixed_v2.py`(추가구현 요구사항)의 정책과 맞춘다. 별도 기사
인센티브를 지급하지 않고, 서지 배율을 유일한 가격 조정 수단으로 사용한다.
배차 의사결정 시점에는 셀의 `raw_surge`에서 목표 매칭률을 정하고, 기사 수락
모델을 역산해 필요한 운임을 구한 뒤, 그 운임을 서지 배율로 변환한다. 이렇게
계산된 최종 운임은 기사 수락 확률 계산과 완료 trip 요금 기록에 모두 사용된다.

## 정책 흐름

pickup H3 cell 기준 흐름은 다음과 같다.

```text
raw_surge = (demand / supply) ** (1 / abs(elasticity))
target_matching_rate = get_target_matching_rate(raw_surge)
required_fare_usd = inverse_acceptance_model(target_matching_rate, candidate_driver_features)
calculated_surge = required_fare_usd / base_fare_usd
final_surge = apply_surge_limits(calculated_surge)
final_fare_usd = base_fare_usd * final_surge
```

서지 제한 정책:

- 계산된 surge가 `1.2` 미만이면 서지가 꺼진 것으로 보고 `1.0`을 사용한다.
- 활성화된 surge는 `0.1` 단위로 올림 처리한다.
- surge 상한은 `4.9`다.

목표 매칭률은 `final_surge`가 아니라 `raw_surge` 구간에서 정한다.

| raw_surge | target_matching_rate |
| --- | --- |
| `< 1.5` | `0.55` |
| `< 2.5` | `0.70` |
| `< 3.5` | `0.80` |
| `>= 3.5` | `0.85` |

runtime mode에서는 셀 단위 limited surge를 배차 판단에 바로 사용하고, 배차
시점의 surge를 trip 완료 요금에 반영한다. experiment mode에서는 위 v2
inverse-pricing 경로를 사용한다.

## 입력값

주요 sweep 입력은 다음과 같다.

- `elasticity`: demand/supply imbalance에서 raw surge를 계산할 때 쓰는 민감도.
- `alpha_sensitivity`: 기사 효용 민감도 보정값. inverse fare 계산에 사용한다.
- `beta_f`: 학습된 fare coefficient를 직접 override할 때만 사용하는 선택값.
  생략하면 `app/driver/model_coefficients.json`의 모델 계수를 사용한다.
- `demand_source`: `actual` 또는 `predicted`.
- `prediction_mode`: `none`, `sync`, `async`.
- `passenger_elasticity`: 표시된 surge에 따른 승객 수요 반응을 실험할 때 쓰는 선택값.

`target_p`는 더 이상 sweep 입력이 아니다. 목표 매칭률은 셀별 `raw_surge`
구간에서 자동으로 결정된다.

## 실행 방법

명령은 `Back/sumo_service`에서 실행한다.

actual demand 기준 단일 실행:

```powershell
uv run python scripts\run_acceptance_experiment.py `
  --elasticity 0.6 `
  --alpha-sensitivity 1.0
```

기사 민감도 sweep:

```powershell
uv run python scripts\run_acceptance_experiment.py `
  --elasticity 0.6 `
  --alpha-sensitivity-list 0.5,0.75,1.0,1.25,1.5
```

predicted demand 정책 실행:

```powershell
uv run python scripts\run_acceptance_experiment.py `
  --elasticity 0.6 `
  --demand-source predicted `
  --prediction-mode sync `
  --prediction-url https://module3-ml.onrender.com/predict `
  --prediction-horizon-min 15
```

승객 가격 탄력성 확장 실험:

```powershell
uv run python scripts\run_acceptance_experiment.py `
  --elasticity 0.6 `
  --passenger-elasticity -0.6
```

여러 조합을 CSV에 append:

```powershell
uv run python scripts\run_acceptance_experiment.py `
  --elasticity-list 0.4,0.6 `
  --alpha-sensitivity-list 0.75,1.0,1.25 `
  --csv-output ..\.temp\acceptance_experiment_results.csv
```

빠른 smoke test가 필요하면 simulated duration을 줄인다.

```powershell
uv run python scripts\run_acceptance_experiment.py `
  --elasticity 0.6 `
  --sim-duration 300
```

## 출력 형식

stdout은 JSON 배열이다. 각 row는 `status`, `reason`, `params`, `metrics`를
가진다.

```json
[
  {
    "status": "ok",
    "reason": null,
    "params": {
      "elasticity": 0.6,
      "beta_f": null,
      "seed": 42,
      "demand_source": "actual",
      "prediction_mode": "none",
      "prediction_url": "https://module3-ml.onrender.com/predict",
      "prediction_horizon_min": 15,
      "passenger_elasticity": 0.0,
      "alpha_sensitivity": 1.0,
      "weather_source": "static"
    },
    "metrics": {
      "spawned_passengers": 42,
      "unique_matched_passengers": 31,
      "matching_success_rate": 0.738,
      "accepted_dispatch_count": 34,
      "acceptances_per_driver_hour": 0.113,
      "driver_revenue_per_hour_usd": 1.42,
      "avg_actual_acceptance_probability": 0.79,
      "avg_target_matching_rate": 0.7,
      "avg_matching_rate_error": 0.02,
      "avg_abs_matching_rate_error": 0.06,
      "avg_required_fare_usd": 18.4,
      "avg_final_surge": 1.8,
      "avg_final_fare_usd": 18.0
    }
  }
]
```

## KPI 의미

`spawned_passengers`  
실험 중 생성된 전체 승객 수다.

`unique_matched_passengers`  
한 번 이상 accepted dispatch decision을 받은 unique 승객 수다.

`matching_success_rate`  

```text
unique_matched_passengers / spawned_passengers
```

`accepted_dispatch_count`  
기사에게 제안된 배차 중 실제 수락된 배차 수다.

`acceptances_per_driver_hour`  

```text
accepted_dispatch_count / (N_TAXIS * (sim_duration / 3600))
```

`driver_revenue_per_hour_usd`  
완료된 trip의 최종 청구 운임을 택시-시간으로 나눈 값이다. 최종 청구 운임을
사용하므로 dispatch 시점 surge가 포함된다. 시뮬레이션 종료 시 강제 완료된
trip은 제외한다.

```text
sum(fare_usd for completed trips) / (N_TAXIS * (sim_duration / 3600))
```

`avg_actual_acceptance_probability`  
배차 판단 시점에 계산된 기사 수락 확률 `p_actual`의 평균이다.

`avg_target_matching_rate`  
`raw_surge` 구간에서 결정된 목표 매칭률의 평균이다.

`avg_matching_rate_error`  

```text
mean(p_actual - target_matching_rate)
```

`avg_abs_matching_rate_error`  

```text
mean(abs(p_actual - target_matching_rate))
```

`avg_required_fare_usd`  
surge limit을 적용하기 전, inverse acceptance model이 요구한 운임의 평균이다.

`avg_final_surge`  
배차 판단에 사용된 최종 surge의 평균이다.

`avg_final_fare_usd`  
배차 판단에 사용된 최종 예상 운임의 평균이다.

predicted-demand mode에서는 다음 prediction diagnostics가 함께 기록된다.

- `prediction_request_count`
- `prediction_success_count`
- `prediction_failure_count`
- `prediction_latency_ms_avg`
- `prediction_latency_ms_p95`
- `prediction_fallback_count`
- `prediction_stale_use_count`
- `prediction_missing_h3_rate`
- `avg_actual_demand_for_surge`
- `avg_predicted_demand_for_surge`
- `avg_demand_bias`
- `avg_abs_demand_error`

## Invalid row

입력값이 수학적으로 위험하거나 finite하지 않으면 SUMO를 실행하지 않고 invalid
row를 출력한다.

현재 validation:

- `beta_f`를 명시한 경우 finite해야 한다.
- `alpha_sensitivity`는 양수이고 finite해야 한다.

`beta_f`를 생략하면 학습된 모델 계수를 그대로 사용한다. 이 경로가 권장 기본값이다.

## DB 저장 필드

runtime DB 기록은 dispatch/trip 단위 scalar 값만 저장한다. 후보 기사별 상세 로그나
전체 grid snapshot은 저장하지 않는다.

`dispatch`에는 다음 값을 저장한다.

- `raw_surge`
- `target_matching_rate`
- `calculated_surge`
- `final_surge`
- `final_fare_estimate_usd`
- `p_actual`
- `accepted`

`trip`에는 다음 값을 저장한다.

- `meter_fare`: surge 적용 전 NYC TLC meter fare. 단위는 cents.
- `surge`: 해당 trip에 적용된 dispatch-time surge.
- `fare`: 최종 청구 운임. 단위는 cents.
- `expected_fare`: 기존 meter estimate. 단위는 cents.

기존 DB volume을 사용하는 경우를 위해 startup 시
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migration을 실행한다.

## 구현 위치

- `app/pricing.py`: raw surge, target matching-rate band, final surge limit.
- `app/grid.py`: WebSocket heatmap과 runtime cache에 쓰는 cell-level limited surge.
- `app/driver/decision_function.py`: 기사 수락 확률과 inverse fare 계산.
- `app/fare.py`: meter fare와 최종 청구 fare 계산.
- `app/simulation.py`: dispatch pricing, acceptance decision, trip fare accounting.
- `scripts/run_acceptance_experiment.py`: CLI sweep runner와 KPI 집계.

## 비교 방법

predicted demand 효과를 보려면 같은 seed와 같은 파라미터에서 actual demand와 비교한다.

```powershell
uv run python scripts\run_acceptance_experiment.py `
  --elasticity 0.6 `
  --demand-source actual `
  --json-output ..\.temp\actual.json

uv run python scripts\run_acceptance_experiment.py `
  --elasticity 0.6 `
  --demand-source predicted `
  --prediction-mode sync `
  --json-output ..\.temp\predicted.json
```

predicted demand는 같은 조건에서 다음 중 하나 이상을 개선할 때 의미가 있다.

- 더 높은 `matching_success_rate`
- 더 낮은 `avg_empty_wait_time_s`
- 더 낮은 `avg_abs_matching_rate_error`
- 더 높은 `acceptances_per_driver_hour`
- 더 높거나 안정적인 `driver_revenue_per_hour_usd`

predicted mode가 나쁘게 나오면 먼저 `prediction_missing_h3_rate`,
`avg_demand_bias`, `avg_abs_demand_error`, `avg_surge`, `avg_final_surge`를
확인한다.

## 주의사항

- passenger `expected_fare`는 기존 meter estimate이며 surge를 포함하지 않는다.
- completed trip의 `fare`는 dispatch-time surge가 반영된 최종 청구 운임이다.
- frontend 호환성을 위해 WebSocket `fare_update` payload 구조는 유지한다.
- WebSocket `surge` payload는 heatmap 표시용 limited cell surge를 계속 제공한다.
- 장기 predicted-demand 실험에서는 `last_prediction` fallback이 필요할 수 있지만,
  현재 기본값은 `error`다. 예측 실패가 validation run에 조용히 섞이지 않게 하기
  위한 선택이다.
