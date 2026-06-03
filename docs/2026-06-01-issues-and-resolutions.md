# Module 4 시뮬레이션 — 초기 문제 & 해결안 정리

**작성일:** 2026-06-01  
**용도:** 팀 교차검증 (실험 결과와 별도 — **무엇이 문제였고, 무엇을 했는지**)  
**연관:** [2026-06-01-experiment-results.md](./2026-06-01-experiment-results.md), [2026-06-01-simulation-tuning-plan.md](../2026-06-01-simulation-tuning-plan.md), [2026-06-02-algorithm-troubleshooting.md](./2026-06-02-algorithm-troubleshooting.md) (알고리즘 증상→조치·KPI·연쇄거절)

---

## 요약 표

| # | 문제 (증상) | 원인 | 해결 상태 | 조치 |
|---|-------------|------|-----------|------|
| 1 | α 올려도 `matching_success` ~0.64 | 과부하·offer 부족; KPI 혼동 | **원인 확인** | λ·택시 수 조정; 진단 KPI |
| 2 | `error`만 개선, matching 정체 | 제안 수준 P* vs 전역 매칭 분리 | **설계 이해** | 문서·KPI 정의 명시 |
| 3 | 5.5:1이 “말이 안 됨” | TLC 수요 vs 시뮬 300대 스케일 다운 | **서술 정리** | 스트레스 + Peak 대조군 |
| 4 | `rebalance`에서 역산 꺼짐 | `policy_mode` 분기 버그 | **해결** | 실험 모드 항상 역산 |
| 5 | 로컬 실험 실패 | Windows에 SUMO 없음 | **우회** | Docker 실행 |
| 6 | Docker 실험 import 오류 | `httpx` 미포함 | **우회** | run 시 `uv pip install httpx` |
| 7 | `surge_clamped_rate` = 100% | surge_max 4.9에 역산 상한 | **미해결** | surge_max sweep 예정 |
| 8 | 구간 P* (55/70/80/85) 미달 | cap + β_F 작음 + pool 평균 feature | **부분** | band KPI; §3.3 구조 수정 예정 |
| 9 | 계단식 구간 → fare 급변 | `get_target_matching_rate` step | **설계 검토** | 연속 P* (Phase F) |
| 10 | REST Lab ≠ 실험 정책 | inverse path 조건 | **미구현** | §3.2 |
| 11 | sweep 시 env만으로 λ 변경 어려움 | import 시점 상수 | **해결** | `ExperimentConfig` 필드 |
| 12 | plateau 원인 불명 | offer vs 거절 vs cap 미분리 | **해결** | never_offered 등 KPI |
| 13 | 14k actual 느림 (sim&lt;real) | bg 1200·step=1·배차 K×findRoute·`_event_log` | **완화 (2026-06-02)** | §13 bench 패키지 |

---

## 13. 14k 벤치 처리량 (2026-06-02)

### 문제

- `demand=actual` 14k run이 sim 2000s 전후 `waiting` 폭증 + **현실 1초보다 느린** TraCI step.
- Docker `N_BACKGROUND_CARS=200`이 코드에 반영되지 않던 시기 있음.

### 조치 (실험 취지 유지)

| 항목 | 유지 | 벤치 변경 |
|------|------|-----------|
| ED·PU 역산·수락 확률 | ✅ | — |
| `dispatch_max_candidates` (K=10 등) | ✅ | — |
| actual parquet 수요 | ✅ | — |
| TraCI step 수 | — | `BENCH_STEP_LENGTH=2` (같은 14400 sim초) |
| 배경차 | — | fast 기본 `N_BACKGROUND_CARS=200` |
| WebSocket state | — | fast bench에서 grid-only + state 생략 |
| surge 진단 | predicted M3 | actual fast는 per-cell diagnostic 생략 |
| CPU 상한 | — | `BENCH_MAX_FIND_ROUTE_PER_STEP`, empty-taxi cap |
| **`_event_log` dispatch_decision 누적** | — | fast: O(1) 카운터 + 종료 시 `dispatch_kpi_fast_summary` 1건 (2000s 급격 느려짐 주원인) |
| **거절 후 findRoute 반복** | ED 유지 | `TAXI_DISPATCH_COOLDOWN_S=5`, `PAIR_DISPATCH_COOLDOWN_S=60` |
| **O(T×W) 거리 스캔** | nearest-first ED | `DISPATCH_H3_K_RING=1` 사전 필터 (빈 결과 시 전체 fallback) |
| **findRoute 전 직선거리** | Q90 2.14mi cap | `PICKUP_MAX_EUCLIDEAN_MILES=2.14` — β_Dpu/Tpu와 정합 |
| **거절 쌍 재시도** | ED 다음 기사 | `_rejected_dispatch_pairs` 블랙리스트 |
| **장기 waiting** | 자연 이탈 | `PASSENGER_WAIT_ABANDON_S=900` (0=off), `passenger_abandoned` 이벤트 |

**env (Docker/overnight 기본):** `EXPERIMENT_FAST=1`, `N_BACKGROUND_CARS=200`, `BENCH_STEP_LENGTH=2`, `SURGE_RECOMPUTE_INTERVAL_S=15`, `BENCH_MAX_FIND_ROUTE_PER_STEP=600`.

**재현 fidelity run:** `BENCH_STEP_LENGTH=1`, `N_BACKGROUND_CARS=200`~1200, `--jobs 1` — 최종 KPI 확정용.

---

## 1. 매칭 성공률 plateau (α sweep)

### 문제

- 스크린샷 alpha sweep (0.75~2.0): `actual p`↑, `error`(p−P*) 개선, **`matching_success` ~0.64 정체**.
- “역산·α 튜닝이 실패한 것처럼” 보임.

### 원인

1. **KPI 정의가 다름**
   - `avg_matching_rate_error`: 배차 **제안이 있었던** 건의 p_actual − P*.
   - `matching_success_rate`: **스폰된 승객** 중 1회라도 수락된 비율.
2. **물리적 과부하**
   - 300대, PASSENGER_LAMBDA≈138 (5.5:1) → `passengers_never_offered` **~29%** (B_stress_55).
   - 택시·제안 부족 → α로 수락률만 올려도 **전체 매칭 수**는 한계.

### 해결안

| 조치 | 상태 |
|------|------|
| α는 **P* 추적·역산**용; matching은 **λ / N_TAXIS / DISPATCH_MAX_CANDIDATES** 우선 | ✅ 문서화 |
| Phase A: λ 100(4:1) → matching **~0.88–0.92** 확인 | ✅ 스크리닝 |
| 진단 KPI: `passengers_never_offered_rate`, `dispatch_acceptance_rate` | ✅ runner 반영 |
| Peak 대조: ratio 1.2~1.5 (λ≈30–38) 또는 N_TAXIS↑ | 📋 추후 |

---

## 2. TLC 5.5:1 vs 시뮬 300대 (스케일 다운)

### 문제

- “수락 콜만 있는 데이터인데 왜 5.5:1 과부하?”
- 심사·팀에서 **비현실적**으로 보일 수 있음.

### 원인

- `docs/택시당콜비율.txt`: **운행 중 택시 1대당** 시간당 콜 비율 ≈ **5.45** → 데이터는 **현실**.
- 시뮬: **N_TAXIS=300** (연산 제약) → 13k대 분의 수요 강도를 300대에 적용 → **의도치 않은 극한 스트레스**.

### 해결안

| 조치 | 상태 |
|------|------|
| 발표 서술: **scale-down 스트레스 시나리오** + **Peak 1.2~1.5:1 대조군** | ✅ 문서 |
| Set B: `B_stress_55` (5.5:1, 300대) | ✅ 실험 |
| Set A1: λ↓ (1.5:1 @300대 → λ≈38) | ✅ 실험 |
| Set A2: N_TAXIS=1100, λ=138 유지 (TLC 수요 밀도) | 📋 추후 (무거움) |

---

## 3. rebalance 모드에서 역산 미적용 (버그)

### 문제

- Case 1(공간 재배치) 구현 시 `rebalance`만 그리드 surge 사용 → **PU 역산 실험 의미 퇴색**.

### 원인

```text
use_inverse = (policy_mode == "matching")  # rebalance 시 False
```

### 해결안

| 조치 | 상태 |
|------|------|
| `use_inverse = (experiment_config is not None)` — **모든 실험 모드 역산** | ✅ `simulation.py` |
| rebalance = 역산 + 빈차 재배치 + (선택) 구간 인센티브 | ✅ |

---

## 4. 실행 환경 (로컬 / Docker)

### 문제

| 증상 | 원인 |
|------|------|
| `uv` 없음 | PATH 미설치 |
| `FileNotFoundError: sumo` | Windows pip에 SUMO 바이너리 없음 |
| Docker `No module named httpx` | 이미지 dev 의존성 미포함 |

### 해결안

| 조치 | 상태 |
|------|------|
| 로컬: `.venv` + pip 또는 **Docker 권장** | ✅ |
| Docker: `uv pip install httpx` 후 runner 실행 | ✅ |
| 병렬: `run_screening_parallel.py --jobs 3` | ✅ |
| (선택) `pyproject.toml`에 httpx 추가 | 📋 |

---

## 5. surge 상한(surge_max) — 전 run clamp

### 문제

- 스크리닝 **14/14 run** `surge_clamped_rate = 1.0`.
- `surge_max=6` 시나리오도 matching·error가 ratio40과 거의 동일 → **cap 완화 효과 불명확**.

### 원인 (가설)

- 역산 `calculated_surge`가 **4.9 상한**에 걸림 → `final_surge`·수락 분포 왜곡.
- 구간 **≥3.5 (P*=85%)** 에서 `p_error` **음수** 반복 (고서지에서 P* 미달).

### 해결안

| 조치 | 상태 |
|------|------|
| Phase B2: `surge_max` 4.9 / 6 / 8 sweep (ratio 4:1 고정) | 📋 |
| `calculated_surge` vs `final_surge` 로깅·집계 (`surge_clamped` 이벤트) | 📋 |
| cap 완화 + α 조합 1h run | 📋 |

---

## 6. 구간별 목표 P* (55/70/80/85%) 미달성

### 문제

- 설계: raw_surge 구간마다 P* 다르게 → 역산 fare.
- 관측: **<1.5 구간 p > P***, **≥3.5 구간 p < P*** 패턴; 중간 구간(70%, 80%) **표본 거의 없음** (배차가 고서지에 몰림).

### 원인

1. **surge cap** (위 #5).
2. **β_F ≈ 0** (`docs/기사의사결정 진행방식 설명.txt`) — fare 레버 약함.
3. **fare 역산 = pool 평균 feature**, 수락 = **개별 택시** (`plan` §3.3 1층).
4. **계단식 구간** — 1.49 vs 1.51에서 P*·fare 급변.

### 해결안

| 조치 | 상태 |
|------|------|
| `experiment_metrics.py` — `band_*_p_error`, `band_*_accept_rate` | ✅ |
| 구간 **연속 P*** (logistic 등) — Phase F | 📋 |
| dispatch **옵션 A**: pool 대표 택시 = 최근접 empty | 📋 |
| 구간 **플랫폼 인센티브** `band_incentive_usd` | ✅ 실험 (효과 제한적) |

---

## 7. 정책 설계: 계단 vs 연속 vs 분위수

### 문제 (검토 요청)

- 4구간 고정 threshold (1.5, 2.5, 3.5) → 문턱 효과.
- 대안: 연속 incentive, 분위수 동적 구간.

### 해결안 (방향)

| 대안 | 용도 | 상태 |
|------|------|------|
| **연속 P*(s)** | 구간 P*·역산 매끄게 | 📋 Phase F |
| **분위수 구간** | 논문용 상대 평가 | 📋 필요 시 |
| **구간 인센티브** | 고서지 공급 보조 | ✅ `band_incentive_usd` |

**권장:** PU·역산 스토리는 **연속 P*** 유지; 4구간 표는 **목표값 앵커**로 설명.

---

## 8. REST / Lab vs 실험 러너 불일치

### 문제

- `POST /simulation/start` body의 `pricing_policy`가 실험과 다르게 동작할 수 있음 (`plan` §3.2).
- 실험: `experiment_config` 있을 때만 inverse path.

### 해결안

| 조치 | 상태 |
|------|------|
| `use_inverse_pricing` 또는 `pricing_policy`에 alpha 있으면 inverse | 📋 코드 |
| Lab은 스크리닝 **확정 파라미터** 반영 후 연동 | 📋 |

---

## 9. 실험 sweep 인프라

### 문제

- `PASSENGER_LAMBDA`, `N_TAXIS`가 모듈 import 시 env 고정 → run마다 바꾸기 불편.
- 시나리오 많을 때 순차 실행만으로 시간 과다.

### 해결안

| 조치 | 상태 |
|------|------|
| `ExperimentConfig`: `passenger_lambda`, `n_taxis`, `surge_max`, `dispatch_max_candidates`, `band_incentive_usd` | ✅ |
| `screening_scenarios.py` + `run_screening_parallel.py` | ✅ |
| `run_screening_one.py` — 단일 시나리오 JSON 출력 | ✅ |
| CLI `--passenger-lambda` (plan §3.1) | 📋 runner 확장 |

---

## 10. 두 가지 실험 Case (의도 정렬)

### 문제

- Case 1(불균형 해소) vs Case 2(매칭·가격 균형)를 한 KPI로 비교하면 혼란.
- 초기 rebalance가 역산 없이 설계됨.

### 해결안

| Case | policy | 1순위 KPI | 스크리닝 후보 |
|------|--------|-----------|---------------|
| **2 — fair** | `matching` + 역산 | matching, \|error\|, never_offered | `fair_dispatch10`, `fair_ratio35` |
| **1 — imbalance** | `rebalance` + 역산 | deficit, rebalance_redirect, matching | `imb_rebalance_40`, `imb_combo` |
| **Stress** | `matching` + 역산, 5.5:1 | matching ceiling, 스트레스 서사 | `B_stress_55` |

---

## 11. 아직 열린 이슈 (해결 예정)

우선순위 순.

1. **surge_max / clamp** — 구간 P*·역산 실효성 회복.
2. **1h sim 본 run** — 후보 5종 × 3600s.
3. **fare pool vs 개별 택시** — `error` 음수·required fare 왜곡.
4. **연속 P*** — 계단 artifact.
5. **REST inverse** — Lab·실험 일치.
6. **N_TAXIS=1100** Peak — TLC 수요 밀도 대조 1회.
7. **surge ON/OFF** — 6주차 시뮬 증명 (선택).
8. **CSV band 컬럼** — 분석 편의.

---

## 12. 팀 검증 시 “해결됐다”고 말할 수 있는 것

- [x] α sweep plateau → **과부하·KPI 혼동**으로 설명 가능.
- [x] λ 100(4:1) → matching **~0.92** (스크리닝).
- [x] 역산 + PU는 실험 모드에서 **항상 동작** (rebalance 포함).
- [x] 구간 P* 정책 **코드 반영** + band KPI 출력.
- [ ] surge cap 해소 후 구간 P* **수치적** 달성 — **아직**.

---

## 13. 관련 커밋·파일 (구현 추적)

| 영역 | 파일 |
|------|------|
| 역산·rebalance | `sumo_service/app/simulation.py` |
| 구간 KPI | `sumo_service/app/experiment_metrics.py` |
| rebalance 로직 | `sumo_service/app/rebalance.py` |
| runner | `sumo_service/scripts/run_acceptance_experiment.py` |
| 병렬 스크리닝 | `sumo_service/scripts/run_screening_parallel.py` |
| 시나리오 목록 | `sumo_service/scripts/screening_scenarios.py` |

---

*실험 수치 상세는 [2026-06-01-experiment-results.md](./2026-06-01-experiment-results.md) 참고.*
