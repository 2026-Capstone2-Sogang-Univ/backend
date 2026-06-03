# Module 4 시뮬레이션 실험 결과 정리

**작성일:** 2026-06-01  
**목적:** 팀 교차검증 — 실험 설계(의의)가 의도대로 동작하는지, KPI 해석이 맞는지 확인  

**4축 마스터 (sweep / M3 / P* / AI 정책):** [`2026-06-01-experiment-matrix.md`](./2026-06-01-experiment-matrix.md) ← **발표·정리는 여기부터**  
**관련:** `2026-06-01-simulation-tuning-plan.md`, `docs/택시당콜비율.txt`, `docs/6주차회의_module4.txt`

---

## 1. 실험의 의의 (검증해야 할 핵심)

### 1.1 정책 목표 (Module 4)

1. **서지**로 수요·공급 불균형을 반영하고, **기사 수락**을 유도한다.
2. **PU Learning 기반 기사 모델**(`decision_function.py`)로 배차 시 수락 확률을 계산한다.
3. **역산 fare:** 셀별 `raw_surge` → 구간 **목표 매칭률 P\*** → 목표를 맞추기 위한 `required_fare` / `final_surge`.
4. **구간별 목표 P\*** (설계 고정값):

| raw_surge | 목표 P* |
|---:|---:|
| < 1.5 | 55% |
| < 2.5 | 70% |
| < 3.5 | 80% |
| ≥ 3.5 | 85% |

구현: `sumo_service/app/simulation.py` — `RAW_SURGE_BUCKETS`, `_dispatch_pricing()` (실험 모드에서 **항상 역산**).

### 1.2 KPI를 두 층으로 읽어야 함

| KPI | 의미 | 튜닝 레버 |
|-----|------|-----------|
| `avg_matching_rate_error` (= p_actual − P*) | **배차 제안이 있었을 때** 수락률 vs 구간 목표 | α, elasticity, surge_max, 역산·pool |
| `matching_success_rate` | **스폰된 승객** 중 1회라도 수락된 비율 | PASSENGER_LAMBDA, N_TAXIS, DISPATCH_MAX_CANDIDATES |
| `passengers_never_offered_rate` | 한 번도 배차 시도가 없던 승객 비율 | 과부하·택시 부족 |
| `dispatch_acceptance_rate` | 제안 대비 수락 비율 | fare·α·인센티브 |
| `surge_clamped_rate` | 역산 surge가 cap에 걸린 비율 | surge_max |
| `band_*_p_error` | **구간별** p_actual − 목표 P* | 구간 정책·cap·인센티브 |

**교차검증 포인트:** α sweep에서 `error`는 좋아지는데 `matching_success`가 0.64에 붙는 것은 **버그가 아니라 KPI 정의 차이 + 과부하**로 설명 가능해야 한다.

### 1.3 5.5:1과 300대 — 데이터는 현실, 시뮬은 스트레스

- TLC 데이터: **수락·완료 콜만** 존재 (거절·미배차 콜 없음).
- `docs/택시당콜비율.txt`: 2013 NYC 기준 **운행 중 택시 1대당 시간당 콜 비율** 전체 평균 **≈ 5.45** → 300대면 λ≈138 (5.5:1)이 **데이터와 정합**.
- 현실 맨해튼 Yellow Cab **~13,500대**가 나눠 받던 수요를 **시뮬 300대**에 넣으면 **스케일 다운 과부하**가 발생 → 발표에서는 **「극한 스트레스 시나리오」+ 「Peak 1.2~1.5:1 대조군」**으로 서술.

---

## 2. Module 3 수요 예측 API (필수 연동)

서지·역산 정책의 **수요 입력**은 Module 3 HTTP API를 써야 한다 (`sumo_service/app/prediction.py`).

| 항목 | 값 |
|------|-----|
| 기본 URL | `https://module3-ml.onrender.com/predict` |
| 인증 | 헤더 `X-API-Key` ← 환경변수 `PREDICTION_API_KEY` |
| surge에 쓰는 시점 | `demand_source=predicted` 일 때, 시뮬 시각 + 15분 horizon H3별 `predicted_demand_count` |

**이전 스크리닝이 API를 안 쓴 이유:** CLI·스크리닝 기본값이 `demand_source=actual`(대기 승객 집계)였고, Docker에 API 키가 전달되지 않았음.

**지금부터 (API 키 설정 시):**

```powershell
# repo root — .env.example 참고
$env:PREDICTION_API_KEY = "<팀 API 키>"
$env:DEMAND_SOURCE = "predicted"

python sumo_service/scripts/run_screening_parallel.py --jobs 3 --sim-duration 1000
```

단일 실험:

```powershell
uv run scripts/run_acceptance_experiment.py `
  --demand-source predicted `
  --prediction-mode sync `
  --sim-duration 1000
```

`PREDICTION_API_KEY`만 설정하면 `--demand-source` 생략 시 **predicted + sync**가 기본이다.

**정책 시계 (2026-06-01 반영):**

| 항목 | 동작 |
|------|------|
| 실험 배속 | 기본 **Lab pacing** (`SIMULATION_SPEED`, `real_sleep=1/60`) — `--fast` 시 벤치(무대기) |
| Module 3 API | **실시간 15분**마다 (`POLICY_UPDATE_INTERVAL_REAL_S=900`) — 새 예측 fetch |
| surge 재계산 | **sim 5초**마다 — 캐시된 예측 수요 + **현재 supply** (서지는 계속 변함) |
| actual baseline | **sim 5초**마다 surge (대기 승객 = demand) |

### 이중 검증 (Module 3 vs Module 4)

| 목적 | 무엇을 보나 | 지표 |
|------|-------------|------|
| **A. Module 3 검증** | 예측 시각 t → **t+horizon** 실제 그리드 수요 vs 예측 | `module3_horizon_mae_avg`, `bias`, `rmse`, `mape` |
| **B. 정책 효과** | 같은 seed·λ·α에서 **actual vs predicted** surge | `matching_success_rate`, `never_offered`, 대기·수익 등 |

```powershell
$env:PREDICTION_API_KEY = "<키>"
uv run scripts/run_policy_ab_test.py `
  --sim-duration 3600 --passenger-lambda 100 `
  --json-output ..\.temp\policy_ab.json
```

출력: `policy_comparison` (predicted−actual), `module3_validation`, `policy_kpi_table`.

**아직 API 미연동:** Lab WebSocket(`docker compose up` 일반 start)은 surge에 **actual**만 사용 — 실험 러너와 분리된 경로.

---

## 3. 실험 인프라 (재현 방법)

| 항목 | 내용 |
|------|------|
| 러너 | `scripts/run_acceptance_experiment.py` |
| 병렬 스크리닝 | `scripts/run_screening_parallel.py` (Docker, `--jobs 3`) |
| 단일 시나리오 | `scripts/run_screening_one.py` |
| 시나리오 정의 | `scripts/screening_scenarios.py` |
| 정책 모드 | `matching` (역산만) / `rebalance` (역산 + 빈차 고서지 재배치) |
| 공통 고정 | seed=42, elasticity=0.6, α=1.5 (시나리오별 예외 명시), 역산 ON |

**Docker (Windows, SUMO 없을 때):**

```powershell
cd c:\Users\chobaa\Desktop\backend
python sumo_service/scripts/run_screening_parallel.py --jobs 3 --sim-duration 1000
```

**결과 파일:**

- 요약: `.temp/screen/summary.json`
- 시나리오별: `.temp/screen/{scenario_id}.json`

---

## 4. 선행 실험 (alpha sweep — 스크린샷 baseline)

**조건:** N_TAXIS=300, PASSENGER_LAMBDA≈5.5:1, `matching` + 역산, sim 3600s 수준.

| alpha | actual p | target | error | matching success |
|------:|---------:|-------:|------:|-----------------:|
| 0.75 | 0.426 | 0.757 | −0.331 | 0.656 |
| 1.00 | 0.464 | 0.767 | −0.303 | 0.649 |
| 1.25 | 0.493 | 0.767 | −0.275 | 0.638 |
| 1.50 | 0.531 | 0.769 | −0.238 | 0.641 |
| 2.00 | 0.628 | 0.770 | −0.142 | 0.643 |

**해석 (팀 검증용):**

- α ↑ → **제안 수준** `actual p` ↑, `error` 개선 → **역산·PU 경로는 동작**.
- `matching_success` **~0.64 정체** → α로는 **승객 전체 매칭률**을 올리기 어려움 → **λ / 택시 수 / offer 부족** 축으로 가야 함.

---

## 5. 병렬 스크리닝 (2026-06-01 실행)

**조건:** sim_duration=**1000s**, seed=42, 3-way 병렬 Docker, 14 시나리오 (~13분 wall).

### 4.1 전체 결과 요약표

| scenario_id | case | ratio (label) | matching | never_offered | error (P*) | dispatch 수락 | surge_clamped |
|-------------|------|---------------|----------|---------------|------------|---------------|---------------|
| **B_stress_55** | stress | 5.5:1 | **0.70** | **0.29** | +0.003 | 0.77 | 1.0 |
| fair_ratio40 | fair | 4.0:1 | 0.88 | 0.12 | −0.090 | 0.71 | 1.0 |
| **fair_dispatch10** | fair | 4.0:1 K10 | **0.92** | **0.08** | −0.076 | 0.71 | 1.0 |
| fair_ratio35 | fair | 3.5:1 | 0.92 | 0.08 | −0.074 | 0.71 | 1.0 |
| fair_ratio30 | fair | 3.0:1 | 1.00 | 0.00 | −0.178 | 0.63 | 1.0 |
| A1_peak_15 | fair | 1.5:1 | 1.00 | 0.00 | −0.260 | 0.52 | 1.0 |
| A1_peak_12 | fair | 1.2:1 | 1.00 | 0.00 | −0.277 | 0.45 | 1.0 |
| fair_surge6 | fair | 4.0:1 cap6 | 0.88 | 0.12 | −0.089 | 0.71 | 1.0 |
| fair_alpha20 | fair | 4.0:1 α2 | 0.91 | 0.09 | −0.111 | 0.69 | 1.0 |
| fair_alpha125 | fair | 4.0:1 α1.25 | 0.91 | 0.08 | −0.093 | 0.72 | 1.0 |
| fair_elast08 | fair | 4.0:1 e0.8 | 0.88 | 0.12 | −0.088 | 0.71 | 1.0 |
| imb_rebalance_40 | imbalance | 4.0:1 reb | 0.90 | 0.10 | +0.078 | **0.87** | 1.0 |
| imb_combo | imbalance | 4.0:1 combo | 0.88 | 0.12 | +0.095 | **0.88** | 1.0 |
| imb_band_inc | imbalance | 4.0:1 band | 0.80 | 0.20 | −0.193 | 0.62 | 1.0 |

### 4.2 구간별 P* 달성 (대표: fair_dispatch10, B_stress_55)

**fair_dispatch10** (4.0:1, K=10) — 배차 제안 기준:

| 구간 | target P* | avg p_actual | p_error | dispatch_n |
|------|----------:|-------------:|--------:|-----------:|
| <1.5 | 0.55 | 0.87 | **+0.32** | 91 |
| <2.5 | 0.70 | — | — | 0 |
| <3.5 | 0.80 | 0.94 | +0.14 | 8 |
| ≥3.5 | 0.85 | 0.68 | **−0.17** | 421 |

**B_stress_55** (5.5:1):

| 구간 | target P* | avg p_actual | p_error |
|------|----------:|-------------:|--------:|
| <1.5 | 0.55 | 0.95 | +0.40 |
| ≥3.5 | 0.85 | 0.75 | **−0.10** |

**교차검증 포인트:**

- 대부분 run에서 **배차의 대부분이 raw_surge ≥3.5 구간**에 몰림 → 중간 구간(70%, 80%) 표본 적음.
- **저서지(<1.5)에서 P* 초과**, **고서지(≥3.5)에서 P* 미달** 패턴 반복 → surge cap·β_F·pool 평균 feature 이슈와 일치.
- **모든 run `surge_clamped_rate = 1.0`** → `surge_max=6` 설정만으로는 final surge 분포가 ratio40과 거의 동일했음 (역산 입력·step 정책 추가 확인 필요).

### 4.3 기타 단발 run

| run | 조건 | matching | 비고 |
|-----|------|----------|------|
| phase_a_quick_138 | λ=138, sim 1200s | (JSON 일부) | error ≈ +0.05, 과부하 |
| screen_partial | λ=100 vs 138 vs band | 0.82 / 0.64 / 0.74 | λ 효과 확인 |
| case1_smoke | rebalance, sim 300s | 1.0 | 짧은 구간, 참고용 |

---

## 6. 결론 (현 시점)

### 5.1 실험 의의가 살아 있는가?

| 질문 | 판단 |
|------|------|
| PU + 역산이 돌아가는가? | **예** — 제안 시 `p_actual`, `required_fare`, 구간 `band_*` 지표 출력 |
| 구간 P* 정책이 반영되는가? | **예** — `target_matching_rate`가 raw_surge 구간별로 다름 |
| α가 P* 추적에 영향? | **예** (스크린샷) |
| α가 전역 matching에 영향? | **약함** — λ/택시 수가 지배 |
| 5.5:1이 “비현실”인가? | **데이터는 현실; 300대 시뮬은 의도적 스트레스** |

### 5.2 추천 운영 시나리오 (스크리닝 기준)

| 목적 | 추천 ID | 이유 |
|------|---------|------|
| **Case 2 — 승객·기사·PU 균형** | `fair_dispatch10` | matching 0.92, never_offered 8% |
| 대안 (단순) | `fair_ratio35` | 3.5:1, 비슷한 수준 |
| **Case 1 — 공간 불균형** | `imb_rebalance_40` | rebalance + 역산, 수락률 0.87 |
| **스트레스 대조** | `B_stress_55` | 5.5:1, matching 0.70 |
| **발표용 “너무 쉬운” 구간** | A1_peak_* | matching 100% but P* error 큼 → 본실험 제외 권장 |

---

## 7. 팀 교차검증 체크리스트

- [ ] `matching_success` = unique matched passengers / spawned — `run_acceptance_experiment.py` `_aggregate()`
- [ ] 실험 모드에서 `policy_mode=rebalance`여도 **역산 fare 사용** (`use_inverse = experiment_config is not None`)
- [ ] PASSENGER_LAMBDA = round(N_TAXIS × ratio / 12) — `screening_scenarios.lambda_for_ratio()`
- [ ] B_stress_55: never_offered ≈ 29%, matching ≈ 70% 재현 가능한가
- [ ] fair_dispatch10: matching ≈ 92% 재현 가능한가
- [ ] 구간 KPI: 고서지(≥3.5)에서 p_error < 0 반복이 cap/모델 한계로 설명 가능한가
- [ ] `docs/6주차회의_module4.txt` First-dispatch + acceptance 모델과 코드 일치하는가

**재현 명령 (1개만):**

```powershell
docker compose run --rm --no-deps `
  -v "${PWD}/sumo_service/scripts:/app/scripts" `
  -v "${PWD}/sumo_service/app:/app/app" `
  -v "${PWD}/.temp:/temp" `
  sumo-service bash -c "uv pip install --python /app/.venv/bin/python httpx -q && /app/.venv/bin/python /app/scripts/run_screening_one.py --scenario-id fair_dispatch10 --case fair --passenger-lambda 100 --n-taxis 300 --dispatch-max-candidates 10 --alpha-sensitivity 1.5 --sim-duration 1000 --json-output /temp/screen/fair_dispatch10_verify.json"
```

---

## 8. 추후 진행 목록

### 7.1 우선순위 높음 (본 run sim 3600s)

| # | 작업 | 목적 |
|---|------|------|
| 1 | **본 run 5종** — `fair_dispatch10`, `fair_ratio35`, `B_stress_55`, `imb_rebalance_40`, `fair_ratio40` × sim 3600s | 발표·논문용 확정 KPI |
| 2 | **surge_max sweep** — 4.9 / 6 / 8 at ratio 4.0:1 | `surge_clamped_rate`↓ 및 구간 p_error 개선 |
| 3 | **Set A2** — `A2_supply_peak15` (N_TAXIS=1100, λ=138) × 1 run | TLC 수요 강도 유지 Peak 대조 |

### 7.2 정책·코드 (실험 틀 유지)

| # | 작업 | 목적 |
|---|------|------|
| 4 | 구간 P* **연속화** (계단 → logistic) | 계단 경계 artifact 완화 |
| 5 | **pool vs 개별 택시** feature 정합 (§3.3 옵션 A) | error 음수·required fare 왜곡 |
| 6 | REST Lab **inverse pricing** 연동 (§3.2) | 프론트·실험 정책 일치 |
| 7 | `surge_clamped_rate` 원인 분석 — calculated vs final surge 로깅 | cap이 100%인 이유 분리 |

### 7.3 분석·문서

| # | 작업 | 목적 |
|---|------|------|
| 8 | 구간별 **한계 효용** 표/그래프 (band p_error vs 인센티브·α) | Case 1/2 정답 시나리오 논증 |
| 9 | surge ON vs OFF 대조 (6주차 1.2.2) | 서지 효과 입증 |
| 10 | 빈차시간 vs NYC TLC (가능 시) | 외부 타당도 |
| 11 | CSV에 `band_*` 컬럼 추가 | 스프레드시트 분석 편의 |

### 7.4 실행 명령 (본 run 예시)

```powershell
cd c:\Users\chobaa\Desktop\backend
$finalists = @("fair_dispatch10","fair_ratio35","B_stress_55","imb_rebalance_40","fair_ratio40")
foreach ($id in $finalists) {
  # manifest.json / screening_scenarios.py 에서 λ·옵션 확인 후 run_screening_one.py 호출
}
```

또는 `run_screening_parallel.py`에 `--final-duration 3600 --scenario-ids ...` 확장 (미구현 시 수동 반복).

---

## 8. 완료 run 스냅샷 (2026-06-01)

공통: `seed=42`, `EXPERIMENT_FAST=1`, Module 3 API OK, `surge_clamped_rate≈1` (cap 이슈는 별도 서술).

### 8.1 Run 목록

| Run | sim | 시나리오 | 산출물 | 상태 |
|-----|-----|----------|--------|------|
| **Sweep** | 1000s | 14 (predicted) | `.temp/screen/` | ✅ 14/14 ok |
| **M3 trial** | 1000s | 5 (m3_*) | `.temp/m3_validation/` | ✅ 5/5 ok |
| **Triple-arm** | 1000s | 6 finalist × actual+predicted | `.temp/triple_arm/` | ✅ 12/12 ok |

**본 검증(long)은 아직 없음** — 아래 §8.5 길이 권장.

### 8.2 Sweep → finalist 6

| scenario_id | ratio/특징 | matching | never_offered | error |
|-------------|------------|----------|---------------|-------|
| fair_ratio35 | 3.5:1 | 0.97 | 0.03 | −0.16 |
| fair_ratio40 | 4.0:1 | 0.81 | 0.19 | −0.31 |
| fair_dispatch10 | 4.0:1 K10 | 0.90 | 0.10 | −0.10 |
| B_stress_55 | 5.5:1 | 0.69 | 0.30 | +0.06 |
| imb_rebalance_40 | rebalance | 0.89 | 0.11 | +0.12 |
| imb_combo | reb+cap+band | 0.92 | 0.08 | +0.08 |

Peak(`A1_peak_*`)는 matching 1.0이나 P* error 큼 → **발표 메인 후보에서 제외**.

### 8.3 Triple-arm 1000s — ①②③ (finalist 6)

**① 알고리즘** — `actual/`, `ai_policy/*.json`에 `band_*`, matching, error 풀셋.

**② 모델** — `ai_forecast/` (= predicted 동일 run):

| scenario | horizon evals | MAE | bias | API |
|----------|----------------|-----|------|-----|
| fair_dispatch10 | 1 | 0.38 | −0.18 | 2/2 |
| fair_ratio35 | 1 | 0.41 | −0.21 | 2/2 |
| B_stress_55 | 1 | 0.52 | −0.33 | 2/2 |
| imb_combo | 1 | 0.40 | −0.20 | 2/2 |

→ **1000s는 ② 본평가 불가** (`eval_count=1`). long run 필수.

**③ AI 정책** — actual vs predicted (Δ matching):

| scenario | actual | predicted | Δ | 해석 |
|----------|--------|-----------|---|------|
| fair_ratio35/40, dispatch10, B_stress | = | = | 0 | surge 차이 거의 없음 |
| imb_rebalance_40 | 0.87 | 0.84 | −0.03 | 소폭 악화 |
| **imb_combo** | 0.83 | **0.89** | **+0.06** | AI 정책 개선 후보 |

`policy_ab` arm은 코드 반영됨 — **다음 long run**부터 `summary.arms.policy_ab`에 `policy_net_improved` 포함.

### 8.4 명령 (long 본검증)

```powershell
$env:PREDICTION_API_KEY = "..."
$env:EXPERIMENT_FAST = "1"
$env:DOCKER_MAX_JOBS = "6"
python sumo_service/scripts/run_three_arm_parallel.py --jobs 6 --sim-duration 43200
```

### 8.5 sim 길이 — “하루”가 필요한가?

| 목적 | 권장 sim | sim 시각 | horizon eval 횟수 (900s 간격) | wall (fast, jobs=6, 12 runs) |
|------|----------|----------|------------------------------|------------------------------|
| ① 알고리즘/P* | **7200–14400** | 2–4h | 8–16 | ~30–60분 |
| ② M3 본평가 | **43200** | **12h** | ~48 | **~1.5–2.5h** |
| ③ AI 정책 Δ | **7200+** (①과 동일 run) | 2h+ | — | (합쳐서) |
| NYC 1일 재현 | 86400 | 24h | ~96 | ~3–4h |

**결론:** **시뮬 “하루(86400s)”까지 필수는 아님.** 팀 계획대로 **43200s(12 sim시간)** 이면 ①②③을 한 번에 본검증 가능. 24h는 일주기·피크 패턴이 논문 요구일 때만.

Parquet 본 replay(2013-07-08~15)는 별도 `SIM_DURATION`/데이터 창 이슈 — 튜닝 plan Phase F.

---

## 9. 변경 이력 (코드)

| 날짜 | 변경 |
|------|------|
| 2026-06-01 | `policy_mode=rebalance`에서도 역산 유지 |
| 2026-06-01 | 진단 KPI: never_offered, dispatch_acceptance, surge_clamped |
| 2026-06-01 | `ExperimentConfig`: passenger_lambda, n_taxis, surge_max, band_incentive_usd, dispatch_max_candidates |
| 2026-06-01 | `experiment_metrics.py` — 구간별 band KPI |
| 2026-06-01 | `run_screening_parallel.py` — 14 시나리오 병렬 스크리닝 |

---

## 10. 문의 시 참고 파일

| 파일 | 내용 |
|------|------|
| `.temp/screen/summary.json` | 최신 병렬 스크리닝 전체 결과 |
| `.temp/triple_arm/summary.json` | 6 finalist × actual/predicted/M3 요약 |
| `.temp/m3_validation/summary.json` | M3 전용 5 시나리오 trial |
| `2026-06-01-simulation-tuning-plan.md` | 튜닝 Phase·코드 수정 plan |
| `sumo_service/README.experiment.v2-surge.md` | v2 역산 정책 설명 |
| `docs/기사의사결정 진행방식 설명.txt` | PU Learning·β 계수 |
| `docs/택시당콜비율.txt` | ratio 5.45 근거 |
