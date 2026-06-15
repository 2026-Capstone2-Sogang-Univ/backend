# v2 Surge Experiment Guide

현재 코드 기준 v2 surge와 driver acceptance 실험 실행 방법을 정리한 문서다. 실험은 FastAPI 서버를 통하지 않고 `SimulationManager`를 직접 실행하며, WebSocket 표시가 아니라 JSON/CSV 결과 산출을 목적으로 한다.

## 목적

- H3 cell별 supply/demand 불균형이 surge에 미치는 영향 확인
- v2 pricing이 목표 기사 수락률에 얼마나 가까워지는지 확인
- 실제 수요 기반 정책과 AI 예측 수요 기반 정책 비교
- `alpha_sensitivity`, `epsilon`, passenger elasticity 변화에 따른 matching/fare/wait KPI 비교

## 기본 정책

기본 pricing policy:

```json
{
  "epsilon": -0.6,
  "surge_min": 1.2,
  "surge_max": 4.9,
  "alpha_sensitivity": 1.0
}
```

기본 target matching rate bucket:

| raw surge 구간 | target matching rate |
| --- | --- |
| `< 1.5` | `0.55` |
| `< 2.5` | `0.70` |
| `< 3.5` | `0.80` |
| `>= 3.5` | `0.85` |

raw surge는 supply/demand 불균형에서 계산한다. v2 pricing은 후보 기사와 승객 feature를 기준으로 목표 수락률에 필요한 fare를 역산하고, `surge_min`, `surge_max`, 승객 cap을 적용한다.

## 승객 replay와 생성량

실험의 기본 승객 소스는 코드 기본값인 `parquet`이다. `TRIPS_FILE`의 전처리된 승객 목록을 사용하며, `PASSENGERS_PER_5MIN` 또는 CLI의 `--passengers-per-5min` 값으로 5분 bucket당 생성 수를 제한한다.

현재 코드 기본값은 `PASSENGERS_PER_5MIN=80`이다. 같은 seed와 같은 입력 파일을 사용하면 sampling 결과가 재현되도록 설계되어 있다. replay 파일 길이를 넘는 장기 실험은 loop 방식으로 이어진다.

pickup/dropoff edge가 SUMO 네트워크에서 유효하지 않거나 route를 찾지 못하면 해당 trip은 skip되고, 결과의 `parquet_replay` diagnostics에 집계된다.

## AI 예측 수요 사용

기본 실험은 `--demand-source actual`이며 외부 예측 API를 호출하지 않는다.

예측 수요를 사용하려면 다음처럼 실행한다.

```powershell
$env:PREDICTION_API_KEY="..."
uv run python scripts\run_acceptance_experiment.py --demand-source predicted --prediction-mode sync --prediction-horizon-min 15
```

현재 스크립트는 한 번에 하나의 `--prediction-horizon-min` 값을 사용한다. 15/30/45/60분 horizon을 비교하려면 horizon별로 실험을 따로 실행한다.

예측 API 관련 주요 옵션:

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--prediction-url` | `https://module3-ml.onrender.com/predict` | 외부 예측 API endpoint |
| `--prediction-horizon-min` | `15` | 예측 horizon |
| `--prediction-mode` | `none` | `none`, `sync`, `async` |
| `--demand-source` | `actual` | `actual` 또는 `predicted` |

예측 호출 실패와 fallback은 결과의 `prediction_failure_count`, `prediction_fallback_count`, `prediction_missing_h3_rate` 등으로 확인한다.

## 실행 예시

기본 단일 실험:

```powershell
cd Back\sumo_service
uv run python scripts\run_acceptance_experiment.py --json-output experiment_out.json --csv-output experiment_out.csv
```

시간당 600명 수준의 승객 생성:

```powershell
uv run python scripts\run_acceptance_experiment.py --passengers-per-5min 50 --json-output experiment_600ph.json --csv-output experiment_600ph.csv
```

`alpha_sensitivity` sweep:

```powershell
uv run python scripts\run_acceptance_experiment.py --elasticity 0.6 --alpha-sensitivity-list 0.5,1.0,1.5,2.0 --json-output sweep.json --csv-output sweep.csv
```

예측 수요 실험:

```powershell
$env:PREDICTION_API_KEY="..."
uv run python scripts\run_acceptance_experiment.py --demand-source predicted --prediction-mode sync --prediction-horizon-min 15 --json-output predicted.json --csv-output predicted.csv
```

## 주요 CLI 옵션

| 옵션 | 설명 |
| --- | --- |
| `--elasticity`, `--elasticity-list` | 수요/가격 탄력성 |
| `--beta-f`, `--beta-f-list` | 기사 fare 민감도 |
| `--alpha-sensitivity`, `--alpha-sensitivity-list` | 목표 수락률 error를 surge에 반영하는 민감도 |
| `--seed` | 난수 seed |
| `--sim-duration` | 실험 시뮬레이션 시간 |
| `--step-length` | SUMO step length |
| `--passengers-per-5min` | 5분 bucket당 승객 수 |
| `--demand-source` | `actual` 또는 `predicted` |
| `--prediction-mode` | `none`, `sync`, `async` |
| `--prediction-horizon-min` | 예측 horizon |
| `--passenger-elasticity` | 승객 가격 탄력성 제거 모델 |
| `--json-output` | JSON 결과 파일 |
| `--csv-output` | CSV 결과 파일 |

## 주요 결과 지표

JSON/CSV 결과에는 다음 값이 포함된다.

- `spawned_passengers`
- `unique_matched_passengers`
- `matching_success_rate`
- `accepted_dispatch_count`
- `acceptances_per_driver_hour`
- `driver_revenue_per_hour_usd`
- `avg_empty_wait_time_s`
- `p50_empty_wait_time_s`
- `p95_empty_wait_time_s`
- `avg_actual_acceptance_probability`
- `avg_target_matching_rate`
- `avg_matching_rate_error`
- `avg_abs_matching_rate_error`
- `avg_required_fare_usd`
- `avg_final_surge`
- `avg_final_fare_usd`
- `avg_actual_demand_for_surge`
- `avg_predicted_demand_for_surge`
- `avg_demand_bias`
- `avg_abs_demand_error`
- `parquet_replay`
- `matching.by_raw_bucket`
- `cells.by_raw_bucket`

`avg_required_fare_usd`는 목표 수락률을 맞추기 위해 역산된 fare 평균이다. `avg_final_surge`와 `avg_final_fare_usd`는 cap과 surge 상한 적용 후 실제 의사결정에 가까운 값이다.

## 해석 주의사항

- `avg_target_matching_rate`가 높고 `matching_success_rate`가 낮으면 surge 상한, 후보 기사 부족, pickup 거리, acceptance model 중 하나가 병목일 수 있다.
- `avg_final_surge`가 4.9에 붙어 있으면 정책 상한에 걸린 상태다.
- `parquet_replay.skipped_pickup`, `skipped_dropoff`, `route_failed`가 높으면 전처리 데이터와 SUMO edge 매칭을 먼저 확인한다.
- predicted mode가 actual보다 나쁘면 `prediction_missing_h3_rate`, `avg_demand_bias`, `avg_abs_demand_error`를 먼저 확인한다.
