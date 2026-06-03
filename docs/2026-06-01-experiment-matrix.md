# Module 4 시뮬레이션 — 실험 결과 마스터 정리 (4축)

**작성일:** 2026-06-01  
**목적:** sweep / Module 3 / P* / AI 정책 효과를 **한곳에서** 추적·교차검증  
**상세 수치·과거 run:** [`2026-06-01-experiment-results.md`](./2026-06-01-experiment-results.md)  
**지표·A/B·sweep 선정:** [`2026-06-02-experiment-metrics-and-comparison.md`](./2026-06-02-experiment-metrics-and-comparison.md)  
**튜닝 계획:** [`../2026-06-01-simulation-tuning-plan.md`](../2026-06-01-simulation-tuning-plan.md)  
**알고리즘 트러블슈팅:** [`2026-06-02-algorithm-troubleshooting.md`](./2026-06-02-algorithm-troubleshooting.md)

---

## 0. 한눈에 보기

| # | 실험 축 | 질문 | demand | 대표 KPI | 결과 파일 | 상태 |
|---|---------|------|--------|----------|-----------|------|
| **1** | **Sweep** | 어떤 λ·α·정책이 후보인가? | predicted | `matching_success`, `score` | `.temp/screen/` | ✅ 14/14 (1000s) |
| **2** | **Module 3 검증** | 예측 API·horizon이 맞는가? | predicted only | `module3_horizon_mae_avg`, `eval_count` | `.temp/m3_validation/` | ⏳ long run 대기/진행 |
| **3** | **목표 P* (역산)** | 구간별 수락률이 P*에 가까운가? | actual or predicted | `band_*_p_error`, `avg_matching_rate_error` | sweep + 본 run | 🔄 sweep에 포함 |
| **4** | **AI 정책 효과** | 예측 수요 surge가 **실제보다 나은가?** | **actual vs predicted** | `policy_comparison`, KPI Δ | `.temp/triple_arm_14k/` | ✅ 12/12 (2026-06-03) |
| **5** | **P\* 2×2 그리드** | cap×λ×target P\* | predicted | band_err, match, rev | `.temp/pstar_grid/` | ✅ 12/12 (2026-06-03) |

**공통 전제:** 실험 모드 = PU 역산 ON, `PREDICTION_API_KEY` + `demand_source=predicted`(기본), Lab pacing(`SIMULATION_SPEED`, `--fast` 제외).

**벤치 처리량 (2026-06-02, §4 본 run):** Docker/overnight는 `--fast` + `BENCH_STEP_LENGTH=2`, `N_BACKGROUND_CARS=200` — **ED·K·역산·수요는 동일**, TraCI step·부가 I/O만 축소. 상세: [`2026-06-01-issues-and-resolutions.md`](./2026-06-01-issues-and-resolutions.md) §13.

---

## 1. Sweep — 시나리오 후보 추리

### 1.1 질문

- 과부하(5.5:1) vs 운영(4:1)·Peak(1.2~1.5:1) 중 **어디서 Module 4를 검증할 것인가?**
- α, dispatch K, surge cap, rebalance, band 인센티브 중 **본 run 후보**는?

### 1.2 설정

| 항목 | 값 |
|------|-----|
| 시나리오 | 14개 (`screening_scenarios.py`) |
| sim | 1000s (짧은 스크리닝) |
| seed | 42 |
| 병렬 | `--jobs 3` (RAM 여유 시) |

```powershell
$env:PREDICTION_API_KEY = "<키>"
python sumo_service/scripts/run_screening_parallel.py --jobs 3 --sim-duration 1000
python sumo_service/scripts/pick_finalists_from_screen.py
```

### 1.3 기록할 표 (채우기)

| scenario_id | ratio | match | never_offered | error | clamp | score | 본 run? |
|-------------|-------|------:|--------------:|------:|------:|------:|:-------:|
| fair_dispatch10 | 4:1 K10 | | | | | | ☐ |
| fair_ratio35 | 3.5:1 | | | | | | ☐ |
| fair_ratio40 | 4:1 | | | | | | ☐ |
| B_stress_55 | 5.5:1 | | | | | | ☐ |
| imb_rebalance_40 | reb | | | | | | ☐ |
| imb_combo | combo | | | | | | ☐ |

**2026-06-01 sweep (predicted, 1000s) 참고값** — `summary.json`에서 갱신:

| scenario_id | matching | never_offered | error |
|-------------|----------|---------------|-------|
| fair_dispatch10 | 0.92 | 0.08 | −0.07 |
| fair_ratio35 | 0.92 | 0.08 | −0.06 |
| B_stress_55 | 0.70 | 0.29 | +0.02 |
| imb_combo | 0.93 | 0.07 | +0.05 |
| A1_peak_* | 1.0 | 0 | ~−0.26 |

**본 run 후보 (권장 5~6):** `fair_dispatch10`, `fair_ratio35`, `fair_ratio40`, `B_stress_55`, `imb_rebalance_40`, `imb_combo` — Peak(A1)는 P* 검증용이지 발표 메인 X.

### 1.4 산출물

- `.temp/screen/summary.json`, `{scenario_id}.json`
- `.temp/screen/finalists_for_overnight.json`

---

## 2. Module 3 검증 — AI 수요 예측이 맞는가

### 2.1 질문

- HTTP API가 안정적으로 동작하는가?
- **t 시점 예측**이 **t+15분(horizon)** 실제 H3 수요와 얼마나 맞는가?
- (부가) `history_missing_rate`, API latency

### 2.2 설정

| 항목 | 값 |
|------|-----|
| demand | **predicted only** (정책 검증과 분리) |
| sim | **길게** (예: 43200s = 12 sim시간, paced) |
| 병렬 | **`--jobs 1`** (RAM) |
| sweep **이후** | `run_m3_after_screen.py` |

```powershell
python sumo_service/scripts/run_m3_after_screen.py --jobs 1 --sim-duration 43200
```

### 2.3 KPI (runner 자동 집계)

| KPI | 해석 |
|-----|------|
| `prediction_success_count` / `prediction_request_count` | API 성공률 |
| `module3_horizon_eval_count` | horizon 도달 평가 횟수 (길수록 ↑) |
| `module3_horizon_mae_avg` | H3별 \|예측−실제\| (↓ 좋음) |
| `module3_horizon_bias_avg` | 과대(+) / 과소(−) |
| `module3_horizon_rmse_avg`, `module3_horizon_mape_avg` | 보조 |
| `history_missing_rate` | 입력 이력 빈칸 (초반 ↑ 정상) |

**주의:** `avg_demand_bias`(surge 진단)는 **같은 시점** 비교라 horizon 검증과 다름 → **§2 KPI만** Module 3 판정에 사용.

### 2.4 통과 기준 (팀 합의용 초안)

| 등급 | 조건 |
|------|------|
| OK | API 실패 0, `horizon_eval_count` ≥ 1, MAE가 팀 baseline 이하 |
| 주의 | bias 한쪽 치우침, MAE 높지만 정책(§4)은 개선 |
| 실패 | API 실패 다수, eval 0 |

### 2.5 기록 표

| run_id | sim | m3_evals | mae | bias | pred_ok | 비고 |
|--------|-----|----------|-----|------|---------|------|
| m3_fair40 | 43200 | | | | | |
| m3_stress55 | | | | | | |
| … | | | | | | |

**산출물:** `.temp/m3_validation/summary.json`

---

## 3. 목표 수락률 P* — 역산이 구간 목표를 맞추는가

### 3.1 질문

- **역산 fare + PU 모델**이 raw_surge 구간별 **목표 P***(55/70/80/85%)에 가까운가?
- 이건 **전역 matching**과 별개 (제안이 있는 콜만).

### 3.2 설계 고정값

| raw_surge | 목표 P* |
|---:|---:|
| < 1.5 | 55% |
| < 2.5 | 70% |
| < 3.5 | 80% |
| ≥ 3.5 | 85% |

### 3.3 KPI

| KPI | 의미 |
|-----|------|
| `avg_matching_rate_error` | 전체 제안: p_actual − P* |
| `avg_abs_matching_rate_error` | \|error\| |
| `band_raw_lt_*_p_error` | **구간별** 오차 |
| `band_*_dispatch_n` | 구간 표본 수 (0이면 미판정) |
| `surge_clamped_rate` | cap 때문에 역산 실패 |

### 3.4 통과 기준 (초안)

| 항목 | 목표 |
|------|------|
| 고서지(≥3.5) | `band_raw_gte_3_5_p_error` → 0 근처 (지금은 음수 많음 → cap/β_F 이슈) |
| 전역 | `avg_abs_matching_rate_error` < 0.15 (λ·α 튜닝 후) |
| 과부하 | B_stress에서 error 좋아도 matching 0.7 — **P* OK ≠ matching OK** |

### 3.5 기록

- sweep·본 run JSON의 `band_*` 필드 복사
- α sweep 스크린샷과 함께 **§3 vs §4 분리** 서술

**산출물:** sweep/finalists JSON 내 metrics (별도 폴더 불필요)

---

## 4. AI 정책 효과 — 예측 surge가 의미 있었는가

### 4.1 질문

- **같은 seed·λ·α·택시**에서 surge 수요만 바꿨을 때:
  - matching·대기·수익이 **actual 대비** 나아졌는가?
- Module 3이 조금 틀려도 **정책적으로 이득**일 수 있음 → §2와 **반드시 같이** 보고.

### 4.2 설정 (paired A/B)

| arm | `demand_source` |
|-----|-----------------|
| A | `actual` (대기 승객) |
| B | `predicted` (Module 3) |

```powershell
# 시나리오 1개
uv run scripts/run_policy_ab_test.py --sim-duration 3600 --passenger-lambda 100 `
  --json-output ..\.temp\policy_ab\fair40.json

# 본 run 후보 일괄
python sumo_service/scripts/run_finalists_overnight.py --jobs 2 --sim-duration 7200
```

### 4.3 KPI (predicted − actual)

| KPI | 개선 방향 |
|-----|-----------|
| `matching_success_rate` | ↑ |
| `passengers_never_offered_rate` | ↓ |
| `avg_empty_wait_time_s`, `p95_empty_wait_time_s` | ↓ |
| `acceptances_per_driver_hour`, `driver_revenue_per_hour_usd` | ↑ |
| `avg_abs_matching_rate_error` | ↓ (부가) |

`policy_comparison` in JSON: `policy_improved_keys`, `policy_net_improved`.

### 4.4 통과 기준 (초안)

| 판정 | 조건 |
|------|------|
| **AI 유의미** | stress 제외 최소 2개 KPI 개선 + matching ↑ 또는 never_offered ↓ |
| **중립** | matching 비슷, MAE만 다름 |
| **무의미** | predicted가 전 지표 악화 |

### 4.5 기록 표

| scenario_id | Δ matching | Δ never_offered | Δ wait | net_improved | §2 MAE |
|-------------|------------|-----------------|--------|--------------|--------|
| fair_dispatch10 | | | | | |
| fair_ratio40 | | | | | |
| B_stress_55 | | | | | |

**산출물:** `.temp/finalists/{id}_ab.json`, `summary.json`

---

## 5. 실행 순서 (RAM·API·Docker)

```powershell
# 1) 고아 컨테이너 정리
python sumo_service/scripts/docker_cleanup.py

# 2) Sweep → M3 순차 (각 단계 병렬 최대 4)
$env:PREDICTION_API_KEY = "<키>"
$env:DOCKER_MAX_JOBS = "4"
python sumo_service/scripts/run_screening_then_m3.py --jobs 4

# 또는 sweep만 먼저
python sumo_service/scripts/run_screening_parallel.py --jobs 4 --sim-duration 1000
```

| 규칙 | 값 |
|------|-----|
| 동시 `docker compose run` | **≤4** (`DOCKER_MAX_JOBS`, `clamp_docker_jobs`) |
| Module 3 `/predict` | 실시간 **15분마다 1회/컨테이너** → 4병렬 ≈ 최대 4동시 POST (Render 일반적으로 OK) |
| screening 1000s | run당 ~1~2회 API / wall ~50s @ speed 20 |
| M3 long 43200s | run당 ~3회 API / wall ~36min |

**스크립트를 동시에 여러 개 띄우지 말 것** (sweep + M3 + finalists 병렬 실행 금지).

---

## 6. 발표/논문용 한 줄 메시지 (초안)

1. **Sweep:** 4:1·K10에서 matching ~0.9, 5.5:1 스트레스 ~0.7.  
2. **Module 3:** horizon MAE/bias = … (§2 채움).  
3. **P*:** 구간 역산은 α로 error 개선, cap·고서지 구간 미달.  
4. **AI 정책:** predicted surge 시 matching/대기가 actual 대비 … (§4 채움).

---

## 7. 체크리스트 (팀 교차검증)

- [ ] 1 Sweep: 14 시나리오 JSON + summary 최신 (predicted)
- [ ] 2 Module 3: long run summary, `horizon_eval_count` > 0
- [ ] 3 P*: band 표 + clamp rate 해석
- [ ] 4 AI: 최소 3 후보 × A/B, `policy_net_improved` 기록
- [ ] KPI 혼동 없음: matching ≠ error ≠ horizon MAE
- [ ] 5.5:1 = 스트레스 서술, Peak = 대조

---

## 8. 관련 스크립트

| 스크립트 | 축 |
|----------|-----|
| `run_screening_parallel.py` | 1 |
| `pick_finalists_from_screen.py` | 1 |
| `run_module3_validation_parallel.py` | 2 |
| `run_m3_after_screen.py` | 2 (after 1) |
| `run_policy_ab_test.py` | 4 |
| `run_finalists_overnight.py` | 4 (batch) |
| `run_acceptance_experiment.py` | 1–4 공통 runner |

---

*수치 갱신: 각 run 완료 후 해당 JSON → 이 문서 표 복사 또는 `experiment-results.md` §5 갱신.*
