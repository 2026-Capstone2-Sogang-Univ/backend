# 시뮬레이션 튜닝 & 코드 수정 Plan

**작성:** 2026-06-01  
**배경:** alpha_sensitivity sweep (0.75~2.0, 택시 300대, 승객:택시 ≈ 5.5:1) 결과  
- `actual p` ↑ (0.43→0.63), `final surge` ↓ (3.7→2.9) — alpha 효과 있음  
- `error` (p − target) 항상 음수, alpha=2.0에서도 −0.14  
- `matching success` ~0.64 plateau — alpha로는 개선 안 됨  

**결론:** 병목 3층 — (1) fare 역산(평균 pool) vs 수락(개별 택시) 불일치, (2) surge cap, (3) 5.5:1 과부하.

---

## 0. 사전 준비

### 실행 환경

```powershell
cd c:\Users\Chobaa\Desktop\backend\sumo_service
uv sync
```

### 실험 러너 (권장 — WS/pacing 없이 빠름)

```powershell
uv run python scripts/run_acceptance_experiment.py `
  --elasticity 0.6 `
  --alpha-sensitivity 1.5 `
  --sim-duration 3600 `
  --csv-output ..\.temp\tuning_results.csv
```

### 기록할 KPI (스크린샷 표와 동일)

| 컬럼 | 의미 |
|---|---|
| `avg_actual_acceptance_probability` | actual p |
| `avg_target_matching_rate` | target |
| `avg_matching_rate_error` | error |
| `avg_abs_matching_rate_error` | abs error |
| `avg_required_fare_usd` | required fare |
| `avg_final_surge` | final surge |
| `avg_final_fare_usd` | final fare |
| `matching_success_rate` | matching success |

### 스크린샷(alpha sweep) 해석 — 무엇을 바꿀지

| 지표 | alpha 올릴 때 (관측) | `matching_success`와 관계 |
|------|----------------------|---------------------------|
| `avg_actual_acceptance_probability` | ↑ | **제안당** 수락률만 ↑ |
| `avg_matching_rate_error` | ↑ (0에 가까움) | P\* 추적 지표, 승객 매칭 수와 **다름** |
| `matching_success_rate` | **정체 ~0.64** | alpha와 **거의 무관** (정상일 수 있음) |

**우선 튜닝할 입력 (matching_success 올리기):**

1. **`PASSENGER_LAMBDA`** (승객:택시) — 과부하 ceiling 1순위 (`docs/택시당콜비율.txt` ratio≈5.45와 정합)
2. **`N_TAXIS`** — 공급 쪽 (수요 유지 시 ratio↓)
3. **`DISPATCH_MAX_CANDIDATES`** — 택시당 시도 승객 수·pool 크기
4. **`surge_max`** — clamp 시 수락·역산 왜곡 (`surge_clamped_rate` 확인)
5. **`policy_mode=rebalance`** — 공급 재배치 (역산은 유지)

**alpha / elasticity는** `error`, `required_fare` 튜닝용. matching plateau가 λ↓에서만 풀리면 alpha 추가 sweep은 중단해도 됨.

**진단 KPI (runner 추가):** `dispatch_acceptance_rate`, `passengers_never_offered_rate`, `surge_clamped_rate`, `avg_dispatch_decisions_per_passenger`  
→ plateau가 “거절” vs “offer 없음” vs “cap” 중 어디인지 분리.

### PASSENGER_LAMBDA ↔ 승객:택시 비율 (300대 기준)

spawn 간격 = 5 sim분(300s) → **sim 1시간당 12 interval**

```text
PASSENGER_LAMBDA = (N_TAXIS × ratio) / 12
```

| ratio | λ (300대) |
|---:|---:|
| 5.5:1 | 138 |
| 4.5:1 | 113 |
| 4.0:1 | 100 |
| 3.5:1 | 88 |
| 3.0:1 | 75 |

### sim 1회 wall-clock

```text
wall ≈ sim_duration / SIMULATION_SPEED   (기본 speed=20 → sim 1h ≈ 3분)
```

---

## 0.4 서지 구간별 목표 P* (설계 의도)

| raw_surge | 목표 매칭률 P* |
|---:|---:|
| < 1.5 | 55% |
| < 2.5 | 70% |
| < 3.5 | 80% |
| ≥ 3.5 | 85% |

구현: `RAW_SURGE_BUCKETS` → 역산 `required_fare` → `acceptance_probability(fare)`.  
**전역 `matching_success`** 와 **구간별 P\*** 는 다름: 전자는 offer/과부하, 후자는 제안된 콜의 수락률.

**구간 진단 KPI (runner):** `band_raw_lt_*_p_error`, `band_*_accept_rate`, `band_*_dispatch_n`  
**구간 인센티브 (실험):** `ExperimentConfig.band_incentive_usd=(0,0,1.5,4)` — 수락 시 fare에 가산(플랫폼 보조).

### 병렬 3분 스크리닝 (호스트)

```powershell
cd c:\Users\chobaa\Desktop\backend
python sumo_service/scripts/run_screening_parallel.py --jobs 3 --sim-duration 1000
# 1100대 포함: --include-heavy (느림)
```

결과: `.temp/screen/summary.json` → case별 상위 후보. 본 run은 `run_screening_one.py` + `--sim-duration 3600`.

| Set | 시나리오 ID 예 | ratio | N_TAXIS |
|-----|----------------|-------|---------|
| B | `B_stress_55` | 5.5:1 | 300 |
| A1 | `A1_peak_15`, `A1_peak_12` | 1.5 / 1.2 | 300 |
| A2 | `A2_supply_peak15` | 1.5:1 (TLC 수요) | 1100 |
| fair | `fair_ratio40` + surge/dispatch/α | 4.0:1 | 300 |

### 스크리닝 → 본 run

```powershell
cd c:\Users\chobaa\Desktop\backend
docker compose run --rm --no-deps `
  -v "${PWD}/sumo_service/scripts:/app/scripts" `
  -v "${PWD}/sumo_service/app:/app/app" `
  -v "${PWD}/.temp:/temp" `
  sumo-service bash -c "uv pip install --python /app/.venv/bin/python httpx -q && /app/.venv/bin/python /app/scripts/run_screening_sweep.py --screen-duration 1200 --final-duration 3600 --screen-csv /temp/screen_results.csv --final-csv /temp/final_results.csv"
```

- **~3분 wall:** `--screen-duration 1200` × 10 시나리오 (`run_screening_sweep.py`)
- **1h sim 본 run:** 상위 3개 `--final-duration 3600`
- Case **imbalance** / **fair** 점수로 자동 순위 → `.temp/screen_results.summary.json`

---

## 0.5 Case 1 / Case 2 실험 축 (2026-06-01 추가)

| Case | `policy_mode` | 목표 | 1순위 KPI |
|------|---------------|------|-----------|
| **공통** | 둘 다 | **PU 역산 fare** → 목표 P\* (`required_fare` / `final_surge`) | `avg_matching_rate_error`, `avg_required_fare_usd` |
| **1 — 공간 불균형** | `rebalance` | 역산 **+** 고서지 H3로 빈차 재배치 | `avg_high_surge_deficit`, `rebalance_redirect_count` |
| **2 — 가격·매칭만** | `matching` (기본) | 역산만 (재배치 없음) | `matching_success_rate`, `avg_matching_rate_error` |

### Case 1 실행 (실험 러너)

```powershell
cd c:\Users\chobaa\Desktop\backend\sumo_service

# Docker (Windows에 SUMO 없을 때)
cd ..
docker compose run --rm --no-deps `
  -v "${PWD}/sumo_service/scripts:/app/scripts" `
  -v "${PWD}/.temp:/temp" `
  -e N_TAXIS=300 -e PASSENGER_LAMBDA=100 `
  sumo-service bash -c "uv pip install --python /app/.venv/bin/python httpx && /app/.venv/bin/python /app/scripts/run_acceptance_experiment.py --policy-mode rebalance --sim-duration 3600 --seed 42 --csv-output /temp/case1_rebalance.csv"
```

**Case 1 정책 요약 (코드):**

- 승객·기사 요금: **역산 surge** (Module 4 v2, `decision_function` 역산) — `matching`과 동일
- 추가: 빈차를 `raw_surge`·(D−S) 상위 셀으로 **60s마다** `setRoute` + 크루즈 가중치
- 선택 보너스: 고서지 픽업 시 수락 확률 소폭 가산 (`rebalance_acceptance_coef`)

**튜닝 파라미터:** `--rebalance-interval-s`, `--rebalance-top-k`, `--rebalance-min-raw-surge`, `--rebalance-acceptance-coef`

Case 2는 기존 `--policy-mode matching`(기본값) + Phase A–D.

---

## 1. 실험 Phase (집에서 순서대로)

### Phase A — 병목 분리: 수요·공급 (최우선)

**목적:** matching ~0.64가 과부하 ceiling인지 확인.  
**고정:** `alpha=1.5`, `elasticity=0.6`, `seed=42`, `sim-duration=3600`, `N_TAXIS=300`

| Run | PASSENGER_LAMBDA | ratio | 기대 matching |
|---|---:|---:|---|
| A1 | 138 | 5.5:1 (baseline) | ~0.64 |
| A2 | 113 | 4.5:1 | ↑ 시작 |
| A3 | 100 | 4.0:1 | ~0.70? |
| A4 | 88 | 3.5:1 | ~0.75? |
| A5 | 75 | 3.0:1 | ~0.80? |

```powershell
$env:N_TAXIS="300"
$env:PASSENGER_LAMBDA="100"   # A3 예시

uv run python scripts/run_acceptance_experiment.py `
  --elasticity 0.6 --alpha-sensitivity 1.5 --seed 42 --sim-duration 3600 `
  --csv-output ..\.temp\phase_a.csv
```

**판단:**
- ratio ↓ → matching ↑ 이면 → **3층(과부하) 확정**. 이후 baseline ratio를 4.0~3.5로 낮추거나 택시 수 ↑.
- flat이면 → dispatch 구조(1층) 또는 cap(2층) 쪽 Phase B/C로.

**대안 (수요 유지, 공급 ↑):**

| Run | N_TAXIS | LAMBDA | ratio |
|---|---:|---:|---|
| A6 | 400 | 183 | 5.5:1 |
| A7 | 400 | 133 | 4.0:1 |

```powershell
$env:N_TAXIS="400"
$env:PASSENGER_LAMBDA="133"
```

---

### Phase B — P* gap (`error`) 줄이기: 정책 파라미터

**전제:** Phase A에서 ratio **4.0:1** (λ=100)을 “튜닝용 baseline”으로 채택.  
**고정:** `N_TAXIS=300`, `PASSENGER_LAMBDA=100`, `alpha=1.5`, `seed=42`

#### B1 — elasticity (CLI 지원 ✅)

raw_surge·target P*·grid surge 모두 변함.

```powershell
uv run python scripts/run_acceptance_experiment.py `
  --elasticity-list 0.4,0.6,0.8 `
  --alpha-sensitivity 1.5 --sim-duration 3600 `
  --csv-output ..\.temp\phase_b1_elasticity.csv
```

| elasticity | 기대 |
|---|---|
| 0.4 | raw_surge ↑, target ↑, surge ↑ |
| 0.8 | raw_surge ↓, target ↓, error 개선 가능 |

#### B2 — surge_max (CLI 미지원 ⚠️ → §3 코드 수정 후)

| surge_max | 비고 |
|---:|---|
| 4.9 | baseline |
| 6.0 | cap 완화 |
| 8.0 | required fare gap 확인용 |

#### B3 — beta_f (CLI 지원 ✅)

```powershell
uv run python scripts/run_acceptance_experiment.py `
  --beta-f-list 0.003,0.006 `
  --alpha-sensitivity 1.5 --elasticity 0.6 `
  --csv-output ..\.temp\phase_b3_beta.csv
```

(미지정 시 `app/driver/model_coefficients.json` 학습값 사용)

#### B4 — alpha (미세 조정, Phase B 이후)

Phase A/B 후 **error가 −0.05 이내**로 들어온 뒤:

```powershell
uv run python scripts/run_acceptance_experiment.py `
  --alpha-sensitivity-list 1.25,1.5,1.75,2.0,2.5 `
  --elasticity 0.6 --sim-duration 3600 `
  --csv-output ..\.temp\phase_b4_alpha.csv
```

---

### Phase C — dispatch 구조 파라미터 (env)

**고정:** Phase A/B best 조합

| Run | DISPATCH_MAX_CANDIDATES | 기대 |
|---|---:|---|
| C1 | 3 (default) | baseline |
| C2 | 5 | pool·후보 확대 |
| C3 | 10 | fare-feature mismatch 완화 |

```powershell
$env:DISPATCH_MAX_CANDIDATES="10"
```

영향 범위:
- `_candidate_driver_average_features()` — 가격 pool 크기
- `_update_taxi_states()` — 택시당 시도 승객 수

---

### Phase D — 검증 run (길게)

best 1~2 조합 × `sim-duration 7200` (wall ~6분) 또는 `14400` (wall ~12분).

```powershell
uv run python scripts/run_acceptance_experiment.py `
  --elasticity 0.6 --alpha-sensitivity 1.5 --sim-duration 7200 `
  --csv-output ..\.temp\phase_d_validate.csv
```

---

### Phase E — REST/Lab 연동 (선택)

실험 결과를 프론트 Lab에서 보려면 Docker + REST:

```powershell
cd c:\Users\Chobaa\Desktop\backend
# docker-compose.override.yml 에 SIM_DURATION=3600 권장
docker compose up --build
```

```powershell
curl -X POST http://localhost:8080/simulation/start `
  -H "Content-Type: application/json" `
  -d '{"duration":3600,"taxi_count":300,"pricing_policy":{"alpha_sensitivity":1.5,"surge_max":6.0,"epsilon":-0.6}}'
```

⚠️ **현재 REST 운영 모드는 inverse pricing 미적용** (§3.2 참고). Lab UI 확인용.

---

## 2. 추천 sweep 매트릭스 (한 번에)

시간 있을 때 Cartesian product (약 3×3×3 = 27 run × 3분 ≈ 80분):

| 축 | 값 |
|---|---|
| PASSENGER_LAMBDA | 100, 88, 75 |
| elasticity | 0.4, 0.6, 0.8 |
| alpha_sensitivity | 1.25, 1.5, 2.0 |

```powershell
$env:N_TAXIS="300"
# LAMBDA는 run마다 수동 변경 필요 (runner가 env만 읽음)
# → §3.1 CLI `--passenger-lambda` 추가 후 아래처럼 자동화 가능
```

---

## 3. 코드 수정 Plan

우선순위 순. 집에서 **Phase B2/C 전에** 3.1만 해도 sweep 편해짐.

### 3.1 [P1] 실험 CLI 확장 — env 없이 sweep

**파일:** `sumo_service/scripts/run_acceptance_experiment.py`, `sumo_service/app/simulation.py`

**추가 CLI 인자:**

```text
--passenger-lambda-list
--n-taxis
--dispatch-max-candidates
--surge-max-list
--surge-min
```

**변경 요약:**

1. `ExperimentConfig`에 `pricing_policy: dict | None` 또는 `surge_max: float` 필드 추가
2. `SimulationManager.fresh_experiment()` / `_reset_run_state()`에서 `_pricing_policy` 반영
3. `_run_one()`에서 env 대신 config로 `N_TAXIS`·`PASSENGER_LAMBDA` override  
   (현재 `N_TAXIS`·`PASSENGER_LAMBDA`는 **모듈 import 시점** 상수 → run마다 `os.environ` 설정 또는 SimulationManager 인스턴스 필드로 옮겨야 함)

**최소 패치 대안 (CLI 안 고칠 때):**

```powershell
# run마다 env 설정 후 1조합씩
$env:PASSENGER_LAMBDA="100"
$env:DISPATCH_MAX_CANDIDATES="10"
# surge_max는 simulation.py DEFAULT_PRICING_POLICY["surge_max"] 임시 수정
```

---

### 3.2 [P1] REST start → inverse pricing 연결

**문제:** `_dispatch_pricing()` inverse path가 `experiment_config is not None`일 때만 동작.

```python
# simulation.py L1705
if self.experiment_config is not None and base_fare_usd > 0:
    ...
else:
    final_surge = self._surge_by_h3.get(...)  # grid surge만
```

**수정 방향:**

```python
use_inverse_pricing = (
    self.experiment_config is not None
    or self._pricing_policy.get("use_inverse_pricing", False)
)
```

또는 `pricing_policy`에 `alpha_sensitivity`가 start body로 들어오면 **자동으로 inverse path** 사용.

**영향:** Lab `POST /simulation/start` body의 `pricing_policy`가 실험과 동일하게 동작.

---

### 3.3 [P2] fare–acceptance feature 정합 (1층 구조 문제)

**문제:** fare는 pool **평균** feature, 수락은 **시도 택시 개별** feature.

**파일:** `simulation.py` `_dispatch_pricing()`, `_update_taxi_states()`

**옵션 A — 최소 수정 (pool 대표 택시):**

- pool 내 **가장 가까운 empty taxi** feature로 역산 (평균 대신 min D_pu taxi)
- 수락 판단도 **같은 택시**에서만 offer

**옵션 B — 의도에 맞는 수정 (콜 중심 dispatch):**

```
for each waiting passenger P (sim step):
  pool = nearest K empty taxis to P
  fare = inverse(target P*, average or min features of pool)
  for taxi in pool (nearest first):
    if random() < acceptance_probability(taxi, fare): assign; break
```

**옵션 C — 수락만 pool 평균 feature로:**

- fare 역산은 유지, `acceptance_probability`에 **avg D_pu, avg dV** 사용 (개별 대신)  
- 빠르지만 물리적 의미는 약함

**권장:** Phase A/B sweep 후 error가 여전히 −0.1 이상이면 **옵션 A**부터.

---

### 3.4 [P2] KPI: 시도당 vs 승객당 분리

**파일:** `run_acceptance_experiment.py` `_aggregate()`

추가 metrics:

```text
avg_dispatch_attempts_per_passenger
passengers_never_offered_rate
surge_clamped_rate  (calculated_surge != final_surge 비율)
```

**목적:** matching plateau가 “offer 없음” vs “거절” 중 어디인지 분리.

---

### 3.5 [P3] target_matching_rates 실험 CLI 연동

**현재:** `ExperimentConfig` / runner에 `target_matching_rates` 없음.  
**수정:** start body와 동일 4구간 override를 experiment에서도 sweep.

---

## 4. 결과 해석 Decision Tree

```text
Phase A: ratio 3.5:1 → matching > 0.75?
  ├─ YES → 과부하가 주원인. 운영 ratio 4.0~3.5 또는 N_TAXIS↑. alpha는 보조.
  └─ NO  → Phase B/C (구조·cap)

Phase B: surge_max 8.0 → error > −0.05?
  ├─ YES → cap이 주원인. surge_max·elasticity 튜닝으로 마감.
  └─ NO  → §3.3 dispatch 구조 수정 필요.

Phase C: DISPATCH_MAX_CANDIDATES 10 → error 개선?
  ├─ YES → pool 크기·택시순회 문제. §3.3 옵션 A/B.
  └─ NO  → acceptance model / beta_f / feature 정의 재검토.

최종: matching > 0.70 AND |error| < 0.05 → Phase D validation run
```

---

## 5. 현재 baseline 스크린샷 (참고)

| alpha | actual p | target | error | required fare | final surge | final fare | matching |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.75 | 0.426 | 0.757 | −0.331 | $359.40 | 3.720 | $30.11 | 0.656 |
| 1.00 | 0.464 | 0.767 | −0.303 | $193.44 | 3.600 | $28.49 | 0.649 |
| 1.25 | 0.493 | 0.767 | −0.275 | $127.98 | 3.526 | $28.50 | 0.638 |
| 1.50 | 0.531 | 0.769 | −0.238 | $94.25 | 3.325 | $25.90 | 0.641 |
| 2.00 | 0.628 | 0.770 | −0.142 | $49.29 | 2.930 | $23.05 | 0.643 |

---

## 6. 집에서 첫 30분 체크리스트

- [ ] `uv sync` + 실험 1회 smoke test (`--sim-duration 300`)
- [ ] Phase A: λ = 138, 100, 88 세 번 (alpha=1.5 고정)
- [ ] matching success가 ratio에 따라 오르는지 표로 정리
- [ ] best ratio 확정 → Phase B1 elasticity 3종
- [ ] (선택) `DEFAULT_PRICING_POLICY["surge_max"]` = 6.0 임시 수정 후 1 run
- [ ] 결과 CSV를 `.temp/`에 저장, 스크린샷 표와 동일 컬럼 비교

---

## 7. 관련 파일

| 파일 | 역할 |
|---|---|
| `sumo_service/app/simulation.py` | dispatch, pricing, spawn, env 상수 |
| `sumo_service/app/pricing.py` | raw_surge, surge limit, target P* band |
| `sumo_service/app/driver/decision_function.py` | acceptance / inverse fare |
| `sumo_service/scripts/run_acceptance_experiment.py` | sweep runner |
| `sumo_service/README.experiment.v2-surge.md` | v2 정책 설명 |
| `docker-compose.override.yml` | SIM_DURATION=604800 (1주) — **튜닝 시 3600으로 변경** |

---

## 8. Open Questions (다음 세션)

1. Phase A에서 ratio 3.5:1 matching ceiling은 얼마인가?
2. surge_max 6.0만으로 error가 −0.05 이내로 들어오는가?
3. §3.3 옵션 A(최소 dispatch 수정) vs B(콜 중심) — 어느 쪽으로 PR할지?
4. REST Lab inverse pricing (§3.2) — 프론트 연동 일정과 맞출지?
