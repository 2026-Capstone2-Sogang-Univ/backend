# P* 2×2 그리드 2차 실험 — 실행 가이드

**작성일:** 2026-06-02  
**전제:** `demand_source=predicted` (Module3 + 역산 fare), `policy_mode=matching`, seed=42, bench fast  
**코드:** `sumo_service/scripts/run_pstar_grid_sweep.py`, `pstar_grid_matrix.py`  
**P* 주입:** `ExperimentConfig.target_p` + `target_p_bucket=raw_gte_3_5` (고서지 구간)

---

## 1. 실험 질문

고서지 목표 수락률 **P\*** 를 올릴 때:

1. **역산이 추적하는가?** → `band_raw_gte_3_5_p_error`, `avg_matching_rate_error`
2. **매칭·수익은 어떻게 되는가?** → `matching_success_rate`, `driver_revenue_per_hour_usd`
3. **규제(surge cap)와 수요(λ)가 곡선을 어떻게 바꾸는가?** → 2×2 셀 비교

---

## 2. 매트릭스 (4 셀 × 3 P* = 12 run)

|  | **[A] fair_ratio35** (λ=88, 3.5:1) | **[B] B_stress_55** (λ=138, 5.5:1) |
|--|-----------------------------------|-------------------------------------|
| **규제 현실** `surge_max=4.9` | **S1** `pgrid_fair35_cap49_p{80,85,90}` | **S2** `pgrid_stress55_cap49_p{…}` |
| **규제 완화** `surge_max=6.0` | **S3** `pgrid_fair35_cap60_p{…}` | **S4** `pgrid_stress55_cap60_p{…}` |

| 셀 | 의도 |
|----|------|
| **S1** | 14k에서 AI matching이 소폭 ↑였던 환경 + cap 왜곡 관측 |
| **S2** | 물리적 과부하 천장 — P*만으로 matching 정체 재현 |
| **S3** | cap 완화 시 역산·P* 추적 “순수” 잠재력 |
| **S4** | 고수요에서 cap 완화 시 수익·매칭 천장 돌파 여부 |

**P* 스윕 (고서지 구간만):** `target_p ∈ {0.80, 0.85, 0.90}` on bucket `raw_gte_3_5`  
(설계 기본 0.85 대비 −5%p / 0 / +5%p)

### 2.1 결과 (2026-06-03, 12/12 ok)

전체 표·14k A/B와 함께: [`2026-06-02-experiment-metrics-and-comparison.md`](./2026-06-02-experiment-metrics-and-comparison.md) **§1B**.

| run_id | match | never_off | band_err† | rev $/hr |
|--------|------:|----------:|----------:|---------:|
| pgrid_fair35_cap49_p80 | 0.712 | 0.286 | +0.026 | 16.0 |
| pgrid_fair35_cap49_p85 | 0.727 | 0.271 | +0.034 | 17.6 |
| pgrid_fair35_cap49_p90 | 0.710 | 0.289 | +0.049 | 16.8 |
| pgrid_fair35_cap60_p80 | 0.718 | 0.281 | +0.027 | 16.1 |
| pgrid_fair35_cap60_p85 | 0.736 | 0.263 | +0.044 | 14.7 |
| pgrid_fair35_cap60_p90 | 0.726 | 0.272 | +0.069 | 17.0 |
| pgrid_stress55_cap49_p80 | 0.559 | 0.440 | +0.063 | 19.8 |
| pgrid_stress55_cap49_p85 | 0.568 | 0.431 | +0.079 | 22.2 |
| pgrid_stress55_cap49_p90 | 0.570 | 0.429 | +0.089 | 21.1 |
| pgrid_stress55_cap60_p80 | 0.558 | 0.441 | +0.071 | 21.1 |
| pgrid_stress55_cap60_p85 | 0.571 | 0.428 | +0.089 | 22.8 |
| pgrid_stress55_cap60_p90 | 0.584 | 0.415 | +0.101 | 21.6 |

† `band_raw_gte_3_5_p_error`. 산출물: `.temp/pstar_grid/summary.json`.

---

## 3. sim 길이: 3600s vs 7200s?

| | **3600s (1 sim-h)** | **7200s (2 sim-h)** |
|--|---------------------|---------------------|
| Wall-clock (bench fast, 14k 대비 ~½) | **짧음** (~12 run / jobs=4 → 밤새 가능) | ~2× |
| `band_gte_3_5` 표본 수 | S2에서 적을 수 있음 | **여유** |
| 2×2 셀 간 비교 | 동일 duration이면 OK | **권장: 4셀 동일 조건** |
| 논문 곡선 | 형태 파악용 **파일럿**에 적합 | **본 12 run 권장** |

**권장 순서**

1. **파일럿:** S1 한 셀 × 3 P* × **3600s** (~1–2h, jobs=2) → `band_raw_gte_3_5_dispatch_n` 확인  
2. **본 run:** 12 run 전부 **7200s** 통일 (셀 간 비교 공정성)

14k 본 run이 셀당 ~5h였으므로 7200s fast는 대략 **2–3h/ run**, `jobs=4`면 12 run ≈ **9–12h**.

---

## 4. 실행 방법

### 4.1 환경

```powershell
cd c:\Users\chobaa\Desktop\backend
$env:PREDICTION_API_KEY = "<키>"
$env:EXPERIMENT_FAST = "1"
$env:N_BACKGROUND_CARS = "200"
$env:BENCH_STEP_LENGTH = "2"
$env:DOCKER_MAX_JOBS = "4"
```

### 4.2 전체 12 run (Docker, 6+6 배치 권장)

호스트에 SUMO가 없으면 **`--docker`** 필수. 다른 실험 컨테이너가 돌 중이면 **`--skip-cleanup`**.

```powershell
# 배치 1 — fair_ratio35 × cap 4.9/6.0 (6 run)
python -u sumo_service/scripts/run_pstar_grid_sweep.py --docker --batch 1 `
  --sim-duration 7200 --jobs 3 --out-dir .temp/pstar_grid --skip-cleanup

# 배치 2 — B_stress_55 × cap (6 run)
python -u sumo_service/scripts/run_pstar_grid_sweep.py --docker --batch 2 `
  --sim-duration 7200 --jobs 5 --out-dir .temp/pstar_grid --skip-ok --skip-cleanup

### 4.4 수동 2단계 스케줄 (권장 — 지금 돌아가는 run 끝난 뒤 직접 실행)

| 단계 | 스크립트 | 내용 | 병렬 |
|------|----------|------|------|
| **Phase A** | `run_pstar_grid_phase_a.ps1` | batch1 fair **잔여 3** + stress **1 run** (`pgrid_stress55_cap49_p80` 기본) | fair `jobs=4`, stress `jobs=1` |
| **Phase B** | `run_pstar_grid_phase_b.ps1` | batch2 stress **잔여 5** (`--skip-ok`) | `jobs=5` |

```powershell
# 지금 컨테이너가 다 끝난 뒤 (또는 알려주신 타이밍에):
powershell -File sumo_service/scripts/run_pstar_grid_phase_a.ps1

# Phase A 끝난 뒤:
powershell -File sumo_service/scripts/run_pstar_grid_phase_b.ps1
```

이미 idle이면 `-NoWait` 로 대기 생략. stress 1 run을 바꾸려면 `-StressRunId pgrid_stress55_cap60_p85` 등.

로그: `.temp/pstar_grid/phase_a.log`, `phase_b.log`

> `run_pstar_grid_continue.ps1` 은 예전 자동 이어하기용 — **새 스케줄은 phase_a / phase_b 사용.**
```

한 번에 12 run: `--batch all` 또는 `--cells all` (동일).

### 4.3 파일럿 (S1만, 1h)

```powershell
python -u sumo_service/scripts/run_pstar_grid_sweep.py `
  --sim-duration 3600 `
  --jobs 2 `
  --cells pgrid_fair35_cap49 `
  --out-dir .temp/pstar_grid_pilot
```

### 4.4 재개

`--skip-ok`: 이미 `status=ok`인 `{run_id}.json`은 건너뜀.

---

## 5. 산출물

| 경로 | 내용 |
|------|------|
| `.temp/pstar_grid/pgrid_fair35_cap4_p80.json` | run별 metrics |
| `.temp/pstar_grid/summary.json` | 12 run 통합 |

---

## 6. 그래프 (권장)

**고정:** 셀당 작은 multiples (S1–S4), 각 패널에서

- **X:** `target_p` (0.80, 0.85, 0.90)
- **Y1:** `matching_success_rate`
- **Y2:** `driver_revenue_per_hour_usd`
- **Y3:** `band_raw_gte_3_5_p_error` (또는 `avg_abs_matching_rate_error`)

→ “P*를 올렸을 때 수익·매칭이 꺾이는 지점” = 한계효용 분기.

---

## 7. 해석 주의

- **S2/S4**에서 matching은 λ 천장 — P* 스윕은 **수익·band P* 추적** 중심.
- `surge_clamped_rate` — cap4.9 셀에서 1.0에 가까우면 S3/S4와 비교해 **역산 왜곡 vs 해소** 논증.
- 수요는 **predicted** 고정 — actual 대비는 14k A/B (`docs/2026-06-02-experiment-metrics-and-comparison.md`)와 분리.

---

## 8. 관련 문서

- [`2026-06-02-experiment-metrics-and-comparison.md`](./2026-06-02-experiment-metrics-and-comparison.md) §6  
- [`2026-06-01-experiment-matrix.md`](./2026-06-01-experiment-matrix.md)
