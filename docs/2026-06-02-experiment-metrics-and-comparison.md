# Module 4 실험 — 지표·비교 정리

**작성일:** 2026-06-02  
**목적:** sweep 선정 근거, actual vs predicted(AI) A/B 지표, sweep 변수 타당성을 한 문서에서 참조  
**관련:** [`2026-06-01-experiment-matrix.md`](./2026-06-01-experiment-matrix.md), [`2026-06-01-experiment-results.md`](./2026-06-01-experiment-results.md), [`../2026-06-01-simulation-tuning-plan.md`](../2026-06-01-simulation-tuning-plan.md), [`2026-06-02-algorithm-troubleshooting.md`](./2026-06-02-algorithm-troubleshooting.md) (증상·KPI·역산·배차)

---

## 0. 전제 — 두 arm이 비교하는 것

두 arm은 **시나리오(λ, α, K, surge cap, rebalance 등)는 동일**하고, **수요 입력만** 다릅니다.

| arm | 결과 폴더 | `demand_source` | AI (Module3) |
|-----|-----------|-----------------|--------------|
| Baseline | `.temp/triple_arm_14k/actual/` | parquet **실제 OD** | **없음** (`prediction_mode=none`) |
| AI 정책 | `.temp/triple_arm_14k/ai_policy/` | Module3 **`/predict`** | **있음** (15분 horizon → surge·역산 입력) |

**비교 질문 (축 4):** 같은 역산·PU·배차 알고리즘에서, surge에 쓰는 수요만 AI로 바꿨을 때 **matching·대기·수익·P\*** 가 어떻게 달라지는가?

---

## 1. Sweep으로 고른 6개 시나리오와 이유

### 1.1 Sweep이 한 일

| 항목 | 값 |
|------|-----|
| 시나리오 수 | 14 (`screening_scenarios.py`) |
| sim | **1000s** (짧은 스크리닝) |
| 수요 | **`demand_source=predicted`** (Module3) |
| seed | 42 |
| 선정 스크립트 | `pick_finalists_from_screen.py` |

선정 규칙:

- case(`fair` / `imbalance` / `stress`)별 **score 상위 2개**
- **`B_stress_55` 강제 포함** (스트레스 대조군)
- 제외: `A1_peak_12`, `A1_peak_15`, `fair_ratio30` 등 (본 run 메인 X)

### 1.2 최종 6개 본 run 후보

| scenario_id | sweep에서 바꾼 축 | 선정 이유 |
|-------------|-------------------|-----------|
| **fair_dispatch10** | 4:1 + **K=10** | fair 상위권; **배차 pool 크기** 효과 분리 |
| **fair_ratio35** | **3.5:1** (λ=88) | fair score 1위급 (1000s matching ~0.97); 4:1보다 덜 과부하 |
| **fair_ratio40** | **4.0:1** (λ=100) | 운영 목표 **4:1** 대표 |
| **B_stress_55** | **5.5:1** (λ=138) | TLC **~5.45:1** 정합 **스트레스** 대조군 |
| **imb_rebalance_40** | 4:1 + **`policy_mode=rebalance`** | imbalance 상위; **빈차 재배치**만 추가 |
| **imb_combo** | rebalance + K10 + surge cap6 + band 인센티브 | imbalance **복합 레버** (실전형) |

### 1.3 의도적으로 본 run에서 뺀 sweep ID

| ID | 이유 |
|----|------|
| `A1_peak_12`, `A1_peak_15` | Peak(1.2~1.5:1) — P*·현실 대조, **발표 메인 아님** |
| `fair_ratio30`, `fair_alpha*`, `fair_elast08`, `fair_surge6`, `imb_band_inc` | 단일 축 실험; 6개에 **흡수**되거나 top-2 밖 |
| `A2_supply_peak15` | N=1100 공급 스케일 — 별도 질문 |

### 1.4 Score 정의 (`screening_scenarios.score_scenario`)

| case | 식 (요지) |
|------|-----------|
| **fair / stress** | `matching − 0.4·|P* error| − 0.002·fare − 0.1·never_offered − 0.05·band_penalty` |
| **imbalance** | `matching + 0.5·(1−never_offered) − 0.15·deficit − 0.05·band_penalty` |

→ sweep 1위 = 전역 matching만이 아니라 **과부하(never_offered)** 와 **구간 P\*** 도 반영.

### 1.5 Sweep 전체 14개 결과 (predicted, 1000s, seed=42)

출처: `.temp/screen/summary.json` (2026-06-01). `err` = `avg_matching_rate_error` (p_actual − P*).

| scenario_id | case | λ·비고 | match | never_off | err | score | 본 run |
|-------------|------|--------|------:|----------:|----:|------:|:------:|
| A1_peak_12 | fair | 1.2:1 | 1.000 | 0.000 | −0.271 | 0.827 | |
| A1_peak_15 | fair | 1.5:1 | 1.000 | 0.000 | −0.314 | 0.798 | |
| fair_ratio30 | fair | 3.0:1 | 1.000 | 0.000 | −0.203 | 0.837 | |
| fair_ratio35 | fair | 3.5:1 | 0.974 | 0.026 | −0.156 | **0.841** | ✓ |
| fair_dispatch10 | fair | 4:1 K10 | 0.899 | 0.101 | −0.096 | 0.773 | ✓ |
| fair_ratio40 | fair | 4:1 | 0.809 | 0.191 | −0.305 | 0.574 | ✓ |
| fair_surge6 | fair | 4:1 cap6 | 0.831 | 0.169 | −0.457 | 0.515 | |
| fair_alpha125 | fair | 4:1 α1.25 | 0.843 | 0.149 | −0.212 | 0.663 | |
| fair_alpha20 | fair | 4:1 α2.0 | 0.826 | 0.174 | −0.257 | 0.612 | |
| fair_elast08 | fair | 4:1 e0.8 | 0.809 | 0.191 | −0.305 | 0.574 | |
| imb_combo | imbalance | combo | 0.921 | 0.079 | +0.080 | **1.343** | ✓ |
| imb_rebalance_40 | imbalance | reb | 0.885 | 0.115 | +0.121 | 1.280 | ✓ |
| imb_band_inc | imbalance | band $ | 0.809 | 0.191 | −0.330 | 1.153 | |
| B_stress_55 | stress | 5.5:1 | 0.693 | 0.303 | +0.060 | 0.584 | ✓ |

> `A2_supply_peak15`(N=1100)는 `screening_scenarios.py`에 정의돼 있으나 이번 `summary.json` 14건에는 미포함.

---

## 1A. 본 run 14k 결과 (actual + predicted)

조건: **sim=14400s**, seed=42, bench fast (`BENCH_STEP_LENGTH=2` 등).  
**상태 (2026-06-03):** actual **6/6**, predicted **6/6** — `.temp/triple_arm_14k/{actual,ai_policy}/`

### actual 6/6 (`demand_source=actual`, AI 없음)

| scenario | match | never_off | err | dispatch_acc | rev $/hr |
|----------|------:|----------:|----:|-------------:|---------:|
| fair_ratio35 | 0.623 | 0.376 | +0.025 | 0.868 | 9.2 |
| imb_rebalance_40 | 0.617 | 0.383 | +0.148 | 0.989 | 10.6 |
| fair_ratio40 | 0.608 | 0.390 | +0.039 | 0.881 | 9.3 |
| fair_dispatch10 | 0.591 | 0.406 | +0.064 | 0.901 | 11.9 |
| imb_combo | 0.578 | 0.422 | +0.154 | 0.995 | 11.6 |
| B_stress_55 | 0.534 | 0.464 | +0.065 | 0.904 | 11.2 |

### predicted 6/6 (`demand_source=predicted`, Module3)

| scenario | match | never_off | err | dispatch_acc | m3 MAE |
|----------|------:|----------:|----:|-------------:|-------:|
| fair_ratio35 | 0.633 | 0.366 | +0.041 | 0.863 | 0.684 |
| fair_dispatch10 | 0.599 | 0.399 | +0.081 | 0.906 | 0.733 |
| fair_ratio40 | 0.593 | 0.405 | +0.051 | 0.877 | 0.732 |
| imb_rebalance_40 | 0.590 | 0.409 | +0.156 | 0.982 | 0.730 |
| B_stress_55 | 0.530 | 0.469 | +0.079 | 0.905 | 0.857 |
| **imb_combo** | **0.600** | **0.400** | **+0.164** | **0.989** | **0.734** |

### A/B 요약 (6/6, Δ = predicted − actual)

| scenario | Δ match | Δ never_off | 해석 |
|----------|--------:|------------:|------|
| fair_ratio35 | +0.010 | −0.010 | AI 소폭 유리 |
| fair_dispatch10 | +0.008 | −0.007 | AI 소폭 유리 |
| fair_ratio40 | −0.014 | +0.015 | 중립~약악화 |
| B_stress_55 | −0.004 | +0.005 | 중립 |
| imb_rebalance_40 | −0.026 | +0.026 | rebalance+AI는 본 run에서 약악화 |
| **imb_combo** | **+0.022** | **−0.022** | rebalance+combo에서도 AI **소폭 유리** (matching·never_off) |

---

## 1B. P\* 2×2 그리드 12 run (2026-06-02~03)

조건: **sim=7200s**, seed=42, bench fast, `demand_source=predicted`, `policy_mode=matching`,  
`target_p_bucket=raw_gte_3_5`, `target_p ∈ {0.80, 0.85, 0.90}`.  
**상태:** **12/12 ok** — `.temp/pstar_grid/summary.json` (2026-06-03T02:37 UTC).  
상세 실행: [`2026-06-02-pstar-grid-experiment.md`](./2026-06-02-pstar-grid-experiment.md)

### 2×2 요약 (고서지 band 기준)

| 셀 | λ | cap | P\*↑ 시 matching | P\*↑ 시 `band_gte_3_5_p_error` | rev $/hr (P\* 0.85) |
|----|---|-----|------------------|-------------------------------|---------------------|
| **S1** fair35 | 88 | 4.9 | 0.71→0.73→0.71 (비단조) | +0.03→+0.05 | ~17.6 |
| **S3** fair35 | 88 | 6.0 | 0.72→**0.74**→0.73 | +0.03→+0.07 | ~14.7 |
| **S2** stress55 | 138 | 4.9 | ~0.56–0.57 정체 | +0.06→+0.09 | ~22.2 |
| **S4** stress55 | 138 | 6.0 | ~0.56–0.58 정체 | +0.07→+0.10 | ~22.8 |

→ **fair:** cap 6.0에서 matching·P\* 추적 여유; **stress:** matching 천장, P\*↑·cap 완화는 **수익·band error** 쪽 변화가 큼.

### 전체 12 run

| run_id | λ | cap | target P\* | match | never_off | band_err† | rev $/hr | band_n† |
|--------|---|-----|------------|------:|----------:|----------:|---------:|--------:|
| pgrid_fair35_cap49_p80 | 3.5:1 | 4.9 | 0.80 | 0.712 | 0.286 | +0.026 | 16.0 | 3050 |
| pgrid_fair35_cap49_p85 | 3.5:1 | 4.9 | 0.85 | 0.727 | 0.271 | +0.034 | 17.6 | 3007 |
| pgrid_fair35_cap49_p90 | 3.5:1 | 4.9 | 0.90 | 0.710 | 0.289 | +0.049 | 16.8 | 2973 |
| pgrid_fair35_cap60_p80 | 3.5:1 | 6.0 | 0.80 | 0.718 | 0.281 | +0.027 | 16.1 | 3043 |
| pgrid_fair35_cap60_p85 | 3.5:1 | 6.0 | 0.85 | **0.736** | 0.263 | +0.044 | 14.7 | 2940 |
| pgrid_fair35_cap60_p90 | 3.5:1 | 6.0 | 0.90 | 0.726 | 0.272 | +0.069 | 17.0 | 2916 |
| pgrid_stress55_cap49_p80 | 5.5:1 | 4.9 | 0.80 | 0.559 | 0.440 | +0.063 | 19.8 | 3081 |
| pgrid_stress55_cap49_p85 | 5.5:1 | 4.9 | 0.85 | 0.568 | 0.431 | +0.079 | 22.2 | 3035 |
| pgrid_stress55_cap49_p90 | 5.5:1 | 4.9 | 0.90 | 0.570 | 0.429 | +0.089 | 21.1 | 3008 |
| pgrid_stress55_cap60_p80 | 5.5:1 | 6.0 | 0.80 | 0.558 | 0.441 | +0.071 | 21.1 | 3008 |
| pgrid_stress55_cap60_p85 | 5.5:1 | 6.0 | 0.85 | 0.571 | 0.428 | +0.089 | 22.8 | 2980 |
| pgrid_stress55_cap60_p90 | 5.5:1 | 6.0 | 0.90 | **0.584** | 0.415 | +0.101 | 21.6 | 2987 |

† `band_raw_gte_3_5_p_error` = p_actual − target_p (고서지); `band_n` = `band_raw_gte_3_5_dispatch_n`.

**해석 초안:** target P\* 0.85→0.90에서 fair·stress 모두 **band_err 양수 확대**(실제 수락 > 목표). stress에서만 P\* 0.90이 matching **소폭 최고**(0.584). cap 4.9→6.0은 fair matching에 큰 차이 없음; stress는 rev·band 추적에 영향.

---

## 2. actual vs predicted — 짝지어 볼 지표

### 2.1 축 4: AI 정책 효과 (양 arm 공통 JSON `metrics`)

같은 `scenario_id`에 대해 **Δ = predicted − actual**.

| KPI | JSON 필드 | 의미 | AI가 나을 때 |
|-----|-----------|------|--------------|
| **매칭 성공률** | `matching_success_rate` | 스폰 승객 중 1회라도 수락 | **Δ > 0** |
| **미배차 비율** | `passengers_never_offered_rate` | 한 번도 배차 시도 없음 | **Δ < 0** |
| **배차 수락률** | `dispatch_acceptance_rate` | 제안 대비 수락 | **Δ > 0** (보조) |
| **빈차 대기** | `avg_empty_wait_time_s`, `p50_*`, `p95_*` | 택시 공회전 대기 | **Δ < 0** |
| **기사 생산성** | `acceptances_per_driver_hour` | 시간당 수락 | **Δ > 0** |
| **기사 수익** | `driver_revenue_per_hour_usd` | 시간당 매출 (USD) | **Δ > 0** |
| **승객당 배차 시도** | `avg_dispatch_decisions_per_passenger` | pool·거절 구조 | 맥락별 해석 |

**판정 초안 (matrix §4.4):**

| 판정 | 조건 |
|------|------|
| **AI 유의미** | stress 제외, matching ↑ 또는 never_offered ↓ 등 **핵심 2개+** 개선 |
| **중립** | matching 비슷, MAE만 다름 |
| **무의미** | predicted가 핵심 KPI 전부 악화 |

자동 집계: `run_policy_ab_test.py` → `policy_comparison`, `policy_improved_keys`.

### 2.2 14k A/B 표

상세 수치는 **§1A**. sweep(1000s) matching ~0.8–1.0 vs 본 run(14400s) ~0.53–0.62 는 **sim 길이·actual parquet** 차이 — **A/B는 14k·동일 seed 내에서만** 비교.

### 2.3 predicted 전용 — Module 3 검증 (축 2)

actual arm: `module3_horizon_eval_count = 0`, API 호출 없음.

| KPI | JSON 필드 | 의미 |
|-----|-----------|------|
| Horizon MAE | `module3_horizon_mae_avg` | t 예측 vs **t+15min** 실제 H3 수요 |
| Bias | `module3_horizon_bias_avg` | 과대(+) / 과소(−) |
| RMSE / MAPE | `module3_horizon_rmse_avg`, `module3_horizon_mape_avg` | 보조 |
| 평가 횟수 | `module3_horizon_eval_count` | horizon 도달 횟수 (sim 길수록 ↑) |
| API | `prediction_request_count`, `prediction_success_count` | 안정성 |

**주의:** `avg_demand_bias`(surge 진단)는 **동시점** 비교 → horizon 검증과 다름. **§2.3 KPI만** Module 3 판정에 사용.

### 2.4 양 arm 공통 — P\* 역산 (축 3)

**「제안이 있었던 배차」** 기준. 전역 `matching_success`와 **별개**.

| KPI | JSON 필드 |
|-----|-----------|
| 전역 P* 오차 | `avg_matching_rate_error`, `avg_abs_matching_rate_error` |
| 구간별 | `band_raw_lt_1_5_p_error` … `band_raw_gte_3_5_p_error` |
| 구간 표본 | `band_*_dispatch_n` (0이면 미판정) |
| Cap | `surge_clamped_rate` |

**설계 고정 목표 P\*** (`simulation.py`):

| raw_surge | 목표 P* |
|---:|---:|
| < 1.5 | 55% |
| < 2.5 | 70% |
| < 3.5 | 80% |
| ≥ 3.5 | 85% |

### 2.5 보조·진단 KPI

| KPI | 용도 |
|-----|------|
| `spawned_passengers`, `unique_matched_passengers` | 수요 규모·λ |
| `avg_final_surge`, `avg_final_fare_usd`, `avg_required_fare_usd` | 역산·fare |
| `rebalance_redirect_count` | rebalance 시나리오 |
| `score` | sweep 종합점수; **14k A/B는 matching·never_offered 우선** |

---

## 3. Sweep 변수 변경 이유와 타당성

공통 고정: **N_TAXIS=300**, **α=1.5**, **elasticity=0.6**, **PU 역산 ON**, seed=42.

| sweep 축 | 대표 scenario_id | 왜 바꿨나 | 타당성 |
|----------|------------------|-----------|--------|
| **승객:택시 ratio (λ)** | ratio30/35/40, B_stress_55, A1_peak | TLC **~5.45:1** vs 운영 **4:1** vs Peak **1.2–1.5:1** | matching plateau **1순위 = 과부하**; `docs/택시당콜비율.txt`, tuning plan |
| **dispatch K** | fair_dispatch10, imb_combo | 택시당 후보 승객 수 | α만으로 matching 안 오를 때 **pool·거절** 분리 |
| **α** | fair_alpha125/20 | PU 민감도 | **P* error·fare** 튜닝; matching과 분리 |
| **elasticity** | fair_elast08 | spawn 탄력성 | 고 surge 수요 억제 |
| **surge_max** | fair_surge6, imb_combo | cap | `surge_clamped_rate`, 역산 왜곡 진단 |
| **rebalance** | imb_rebalance_40, imb_combo | 빈차 → 고수요 셀 | 역산만으로 공급 위치 부족 |
| **band 인센티브 (USD)** | imb_band_inc, imb_combo | 구간별 추가 보상 | 저/고 surge **P\*** 보조 |
| **공급 스케일** | A2_supply_peak15 | N=1100 | “택시만 늘리면?” — 메인 6개와 별도 |
| **스트레스** | B_stress_55 | 데이터 ratio 재현 | **한계 성능·발표 narrative** |

### 3.1 α sweep 교훈 (튜닝 plan)

| 관측 | 함의 |
|------|------|
| α ↑ → `avg_matching_rate_error` 개선 가능 | **P* 추적** 지표 |
| α와 무관하게 `matching_success` ~0.64 정체 | **λ 과부하** ceiling |
| | 본 run 6개는 **λ·K·rebalance·combo** 중심, α≈1.5 고정 |

### 3.2 Sweep vs 본 run (14k) 해석 주의

| | Sweep | 본 run |
|---|--------|--------|
| sim | 1000s | 14400s |
| sweep 수요 | predicted only | **actual + predicted** 짝 |
| matching 규모 | ~0.7–1.0 | actual ~0.53–0.62 |

→ sweep = **「어떤 레버 조합이 후보인가」**  
→ 14k = **「실제 수요 baseline vs AI 수요 A/B」**

### 3.3 λ ↔ PASSENGER_LAMBDA (300대)

spawn 간격 5 sim분 → sim 1시간당 12 interval:

```text
PASSENGER_LAMBDA = round(N_TAXIS × ratio / 12)
```

| ratio | λ (N=300) |
|---:|---:|
| 5.5:1 | 138 |
| 4.0:1 | 100 |
| 3.5:1 | 88 |
| 3.0:1 | 75 |

---

## 4. 산출물 경로

| 단계 | 경로 |
|------|------|
| Sweep | `.temp/screen/summary.json`, `{scenario_id}.json` |
| 본 run actual | `.temp/triple_arm_14k/actual/{scenario_id}.json` |
| 본 run predicted | `.temp/triple_arm_14k/ai_policy/{scenario_id}.json` |
| 통합 summary | `.temp/triple_arm_14k/summary.json` (디스크 재빌드 필요 시 `--summary-only`) |
| **P\* 2×2 그리드** | `.temp/pstar_grid/pgrid_*.json`, `summary.json` |
| M3 long (43k) | `.temp/triple_arm_43k/` (14k 완료 후) |

---

## 5. 한 줄 요약

1. **6개 후보** = fair(λ·K) + stress(5.5:1) + imbalance(rebalance·combo); sweep score·case별 top-2 + stress 필수.
2. **14k A/B** = **12/12 완료** (imb_combo predicted 포함); fair 계열 AI 소폭 유리, imb_rebalance는 약악화.
3. **P\* 그리드** = **12/12 완료** (7200s, predicted); fair는 P\*·cap 민감, stress는 matching 정체·수익·band_err 민감.
4. **Sweep 변수** = λ(과부하), K, α/e, cap, rebalance, 인센티브를 **한 축씩** 검증 후 본 run으로 압축.

---

## 6. 추후: 선정 알고리즘에서 P\*를 조절하며 한계효용 탐색 — 유효한가?

### 6.1 결론

**유효하다.** 다만 **6개 전부 × 14k**를 다시 돌리기보다, **finalist 1~2개**에 대해 **P\* 그리드(또는 α)** 를 짧은 sim으로 스윕하는 **2차 실험**이 적절하다.

### 6.2 이 코드에서 P\*가 의미하는 것

- 배차 시 `raw_surge` 구간마다 **목표 수락률 P\*** 가 고정 (`simulation.py` `RAW_SURGE_BUCKETS`):

  | raw_surge | P\* |
  |---:|---:|
  | < 1.5 | 55% |
  | < 2.5 | 70% |
  | < 3.5 | 80% |
  | ≥ 3.5 | 85% |

- PU 역산: `target P*` → `required_fare` → `final_surge` → 기사 수락 확률 (`README.experiment.v2-surge.md`).
- **`target_p`는 더 이상 CLI sweep 입력이 아님** — 구간 표를 바꾸거나 `SimulationOptions.target_matching_rates`로 주입.

### 6.3 “한계효용”을 어떻게 읽을지

| 축 | 조절 | 관측 (한계) |
|----|------|-------------|
| **P\* ↑** (구간별) | 고서지 목표 85%→90% 등 | `band_*_p_error`↓ vs `avg_final_fare`·`driver_revenue`↑, `matching_success`는 λ에 묶임 |
| **α** (`alpha_sensitivity`) | PU 민감도 | P\* 추적 vs fare (sweep `fair_alpha*` 참고) |
| **band_incentive_usd** | 구간별 USD 보정 (`imb_combo`) | 역산 P\* 외 **유효 수락률** 보조 — P\* 그리드와 **분리** 해석 |

**한계효용 곡선 예:** 고서지 구간만 P\* ∈ {0.80, 0.85, 0.90} × 2~3 sim 길이 → (Δ matching, Δ revenue, Δ \|p_error\|) / ΔP\*.

### 6.4 본 run이 말해 주는 선행 조건

- 14k에서 `err`가 대체로 **양수** (실제 수락 > 목표 P\*에 가깝거나 cap 영향) — P\*를 **올리면** fare·surge 경로가 바뀜.
- `matching_success`는 P\*와 **약하게 연동** (튜닝 plan: α로 error만 좋아지고 matching 정체) → P\* 스윕의 **1차 목표는 band P\* 추적 + fare/revenue**, matching은 **2차·λ 고정 하에서** 봄.
- `surge_clamped_rate` ≈ 1 — cap이 역산을 자주 막음 → P\* 스윕 시 **`surge_max` 완화** 또는 고서지 구간만 조절하는 것이 타당.

### 6.5 권장 설계 (실행 가능)

1. **대상:** `fair_ratio35` 또는 `fair_dispatch10` 1개 + (선택) `B_stress_55` 스트레스 검증.
2. **고정:** λ, K, rebalance, `demand_source` (actual vs predicted는 **별도 arm**으로 유지).
3. **스윕:** raw_surge ≥3.5 구간 P\*만 {0.80, 0.85, 0.90} 또는 4구간 전부 ±5%p.
4. **sim:** 3600~7200s (14k 전체는 비용 큼).
5. **KPI:** `band_raw_gte_3_5_p_error`, `avg_abs_matching_rate_error`, `matching_success_rate`, `driver_revenue_per_hour_usd`, `avg_final_surge`.
6. **구현 (2026-06-02 반영):** `ExperimentConfig.target_p` + `target_p_bucket`, 또는 `target_matching_rate_overrides`; `run_acceptance_experiment.py` CLI `--target-p`, `--target-p-bucket`, `--target-matching-rates`.

```powershell
# 고서지 P*만 0.80 → 0.95 (fair_ratio35, actual, 1h bench)
python sumo_service/scripts/run_acceptance_experiment.py --fast --sim-duration 3600 `
  --demand-source actual --passenger-lambda 88 --dispatch-max-candidates 10 `
  --target-p 0.85 --target-p-bucket raw_gte_3_5 --json-output .temp/pstar_sweep/p85.json

# 여러 구간 동시
python sumo_service/scripts/run_acceptance_experiment.py --fast --sim-duration 3600 `
  --target-matching-rates '{"raw_gte_3_5":0.90,"raw_lt_3_5":0.78}' --json-output .temp/pstar_multi.json
```

### 6.6 하지 않는 편이 나은 경우

- P\*만 바꾸고 **λ 과부하**(never_offered 40%+)를 그대로 두고 “matching이 왜 안 오르나”를 기대할 때.
- **6개 finalist × many P\* × actual+predicted** 풀팩토리얼 (비용·해석 모두 비효율).

### 6.7 AI(actual vs predicted)와의 관계

P\* 스윕은 **수요 소스와 독립**한 2차 축이다. 순서 권장:

1. 본 run 12/12 완료 → A/B로 “AI 수요가 정책에 쓸 만한가” 판단  
2. **유의미한 1개 finalist**에서 P\* 곡선 → “역산 목표를 어디에 두는가”  
3. (선택) 그 최적 P\* 근처에서 predicted만 43k M3 장run

**2차 P\* 2×2 그리드:** [`2026-06-02-pstar-grid-experiment.md`](./2026-06-02-pstar-grid-experiment.md) — **§1B 결과 표** (12/12 완료, 2026-06-03). 예약 스크립트(`continue`/`after_stress1`/phase)는 **중지됨** — 중복 run 방지.

---

## 7. 최종 알고리즘 선정 프로토콜

### 7.1 고정 스택 (선정 대상 아님)

6개 finalist 비교는 **서로 다른 ML 6종**이 아니라 **동일 Module 4** 위 레버(λ, K, rebalance, cap) 비교다.

| 항목 | 최종 정책 값 |
|------|----------------|
| 요금·수락 | PU 역산 (`README.experiment.v2-surge.md`) |
| 배차 | nearest + pool K (기본; K=10은 부록) |
| `policy_mode` | **matching** |
| α / elasticity | 1.5 / 0.6 |
| `demand_source` | **predicted** (Module3) |

**논문용 한 줄 정의:** Predicted-demand Module 4 — H3 예측 수요 → surge·역산 → matching 배차.

### 7.2 1차: 환경·레버 (6 finalist → 대표 3점)

| 단계 | 방법 | 산출 |
|------|------|------|
| Sweep 1000s | `score_scenario()` + case별 top-2 + `B_stress_55` 필수 | 6 finalist |
| 본 run 14k | actual vs predicted **짝** | A/B 표 |

**A/B 판정** (§4.4, stress 제외):

| 판정 | 조건 |
|------|------|
| **AI 채택** | matching ↑ **또는** never_offered ↓ 등 **핵심 2개+** |
| **중립** | matching 비슷 |
| **기각** | predicted 핵심 KPI 전부 악화 |

**우선 KPI:** `matching_success_rate`, `passengers_never_offered_rate` → 대기·`driver_revenue_per_hour_usd` → band P\* error.  
`module3_horizon_mae_avg`는 **설명용**, 단독 선정 기준 아님.

**14k (2026-06-03, 6/6):**

| scenario | AI A/B (Δmatch) | 역할 |
|----------|-----------------|------|
| **fair_ratio35** | +0.010 | **P\* 튜닝·역산 앵커** |
| **fair_dispatch10** | +0.008 | fair 대안 (부록) |
| **fair_ratio40** | −0.014 | **운영 4:1 일반화** (발표 1줄) |
| **B_stress_55** | −0.004 | **과부하 한계** |
| **imb_rebalance_40** | −0.026 | **메인 정책 제외** |
| **imb_combo** | **+0.022** | **부록** (combo 복합 레버) |

### 7.3 2차: P\* — **완료 (12/12)**

`target_p_bucket=raw_gte_3_5`, `{0.80, 0.85, 0.90}` × 2×2(λ × cap), sim=7200s.  
표·해석: **§1B**, [`2026-06-02-pstar-grid-experiment.md`](./2026-06-02-pstar-grid-experiment.md) §2.1.

**P\* 확정 규칙:**

1. `band_raw_gte_3_5_dispatch_n` 충분 (파일럿 3600s)  
2. `|band_raw_gte_3_5_p_error|` 최소  
3. 동점 → `driver_revenue_per_hour_usd` ↑, matching 하락 ≤1%p  
4. cap 4.9 clamp 크면 → 운영값 4.9 + cap 6.0 민감도(S3/S4) 병기

### 7.4 최종 박스 (발표·논문)

```text
demand_source = predicted
policy_mode   = matching
N_TAXIS       = 300, α = 1.5, surge_max = 4.9 (운영; cap 6.0 = 부록)
target P*     = raw_gte_3_5 → P* 그리드 12 run 후 확정

검증:
  튜닝/역산  — fair_ratio35
  일반화     — fair_ratio40 (4:1)
  한계       — B_stress_55 (5.5:1)
```

### 7.5 실행 (12 run, 6+6 배치)

```powershell
$env:PREDICTION_API_KEY = "<키>"
$env:EXPERIMENT_FAST = "1"
$env:DOCKER_MAX_JOBS = "3"

# 배치 1: fair 3.5:1 × cap 4.9/6.0 (6 run)
python -u sumo_service/scripts/run_pstar_grid_sweep.py --docker --batch 1 `
  --sim-duration 7200 --jobs 3 --out-dir .temp/pstar_grid --skip-cleanup

# 배치 2: stress 5.5:1 × cap (6 run)
python -u sumo_service/scripts/run_pstar_grid_sweep.py --docker --batch 2 `
  --sim-duration 7200 --jobs 3 --out-dir .temp/pstar_grid --skip-ok --skip-cleanup
```
