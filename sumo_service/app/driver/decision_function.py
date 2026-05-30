"""
SmartTaxi Module 4 — 기사 콜 수락 확률 함수.

모듈1이 매 콜마다 호출:
    from decision_function import acceptance_probability
    p = acceptance_probability(last_dropoff_cell, dropoff_cell,
                               call_datetime, fare_amount, D_pu, trip_distance)
    if random.random() < p: 수락 else 거부

학술 근거:
- Buchholz (2022, RES) — binary logit 골격
- Buchholz, Shum, Xu (2023) — V(ℓ,t)를 historical 평균으로 외부 보정
- Elkan & Noto (2008, KDD) — PU Learning + SCAR 보정 상수 c
- Frechette, Lizzeri, Salz (2019, AER) — search time 정의

식:
    V_accept = F_c + V(ℓ_d, t + T_pu + T_c)
    V_reject = V(ℓ_0, t + s(ℓ_0, t))
    z       = β_0 + β_ΔV·ΔV + β_F·F_c + β_Dpu·D_pu + β_Tpu·T_pu
    P_겉보기 = σ(z)
    P       = clip(P_겉보기 / c, 0, 1)
"""
import json
import os
from dataclasses import dataclass
from datetime import datetime
from math import exp, isfinite, log

import pandas as pd

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_COEF_PATH = os.path.join(_BASE_DIR, 'model_coefficients.json')
_V_PATH = os.path.join(_BASE_DIR, 'V_table.parquet')
_S_PATH = os.path.join(_BASE_DIR, 's_table.parquet')

with open(_COEF_PATH, 'r') as _f:
    _COEF = json.load(_f)

_BETA_0 = _COEF['beta_0']
_BETA_DV = _COEF['beta_dV']
_BETA_F = _COEF['beta_F']
_BETA_DPU = _COEF['beta_Dpu']
_BETA_TPU = _COEF['beta_Tpu']
_C = _COEF['c']
_AVG_SPEED_MPH = _COEF.get('avg_speed_mph', 12)

_V_DF = pd.read_parquet(_V_PATH)
_S_DF = pd.read_parquet(_S_PATH)
_V_LOOKUP = {(r.h3_cell, int(r.dow), int(r.bin)): float(r.V_value) for r in _V_DF.itertuples()}
_S_LOOKUP = {(r.h3_cell, int(r.dow), int(r.bin)): float(r.s_value) for r in _S_DF.itertuples()}
_V_GLOBAL = float(_V_DF['V_value'].mean())
_S_GLOBAL = float(_S_DF['s_value'].mean())


# 역산 실험에서는 fare만 바꿔가며 같은 비가격 효용항을 재사용한다.
# 이 구조체는 정방향 확률 계산과 목표 확률 역산이 같은 feature 계산을 공유하게 한다.
@dataclass(frozen=True)
class AcceptanceFeatures:
    dV_without_fare: float
    t_pu: float
    t_c: float
    z_without_fare: float


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = exp(-x)
        return 1.0 / (1.0 + z)
    z = exp(x)
    return z / (1.0 + z)


def _get_V(cell: str, dow: int, bn: int) -> float:
    return _V_LOOKUP.get((cell, int(dow), int(bn)), _V_GLOBAL)


def _get_s(cell: str, dow: int, bn: int) -> float:
    return _S_LOOKUP.get((cell, int(dow), int(bn)), _S_GLOBAL)


# PU 보정은 sigmoid 결과를 관측 가능한 최종 수락확률로 변환한다.
# 역산 시에는 같은 보정상수를 반대로 적용해야 target_p와 정방향 계산이 어긋나지 않는다.
def probability_from_z(z: float, c: float = _C) -> float:
    p_apparent = _sigmoid(z)
    return min(1.0, max(0.0, p_apparent / c))


def pu_corrected_logit(target_p: float, c: float = _C) -> float:
    if not isfinite(target_p) or target_p <= 0 or target_p > 1.0:
        raise ValueError("target_p must be finite and in (0, 1]")
    apparent_p = target_p * c
    if apparent_p <= 0 or apparent_p >= 1:
        raise ValueError("target_p * c must be between 0 and 1")
    return log(apparent_p / (1.0 - apparent_p))


def acceptance_features(
    last_dropoff_cell: str,
    dropoff_cell: str,
    call_datetime: datetime,
    D_pu: float,
    trip_distance: float,
    *,
    pickup_cell: str | None = None,  # 예약 인자: 추후 pickup-cell 기반 feature 도입 시 사용.
) -> AcceptanceFeatures:
    """Return fare-independent feature terms used by the acceptance model."""
    del pickup_cell  # 현재 계산엔 미사용. 시그니처는 미래 사용을 위해 유지.
    dow = call_datetime.weekday()
    pickup_min = call_datetime.hour * 60 + call_datetime.minute
    pickup_bin = (pickup_min // 15) % 96

    T_pu = D_pu / _AVG_SPEED_MPH * 60.0
    T_c = trip_distance / _AVG_SPEED_MPH * 60.0

    arrival_bin = int((pickup_min + T_pu + T_c) // 15) % 96
    V_acc_without_fare = _get_V(dropoff_cell, dow, arrival_bin)

    s_val = _get_s(last_dropoff_cell, dow, pickup_bin)
    reject_bin = int((pickup_min + s_val) // 15) % 96
    V_rej = _get_V(last_dropoff_cell, dow, reject_bin)

    dV_without_fare = V_acc_without_fare - V_rej
    z_without_fare = (
        _BETA_0
        + _BETA_DV * dV_without_fare
        + _BETA_DPU * D_pu
        + _BETA_TPU * T_pu
    )
    return AcceptanceFeatures(
        dV_without_fare=dV_without_fare,
        t_pu=T_pu,
        t_c=T_c,
        z_without_fare=z_without_fare,
    )


def acceptance_probability(
    last_dropoff_cell: str,
    dropoff_cell: str,
    call_datetime: datetime,
    fare_amount: float,
    D_pu: float,
    trip_distance: float,
    beta_f: float | None = None,
    *,
    alpha_sensitivity: float = 1.0,
    pickup_cell: str | None = None,  # 예약 인자: 추후 pickup-cell 기반 feature 도입 시 사용.
) -> float:
    """0~1 범위 P(수락) 반환.

    Args:
        last_dropoff_cell: 후보 기사의 직전 dropoff H3 셀 (lvl 9). vacant 시작 위치.
        dropoff_cell: 콜 도착 H3 셀.
        call_datetime: 콜 발생 시각 (datetime, 분 단위).
        fare_amount: 서지 반영된 최종 요금. 모듈1이 이미 계산.
        D_pu: 빈차로 픽업까지 가는 거리 (마일).
        trip_distance: 콜 운행 거리 (마일).
        pickup_cell: (예약) 콜 출발 H3 셀. 현재 계산엔 미사용.
    """
    features = acceptance_features(
        last_dropoff_cell=last_dropoff_cell,
        dropoff_cell=dropoff_cell,
        call_datetime=call_datetime,
        D_pu=D_pu,
        trip_distance=trip_distance,
        pickup_cell=pickup_cell,
    )
    beta = beta_f if beta_f is not None else _BETA_F
    z = _BETA_0 + alpha_sensitivity * (
        (features.z_without_fare - _BETA_0) + beta * fare_amount
    )
    return probability_from_z(z, _C)


# target_p를 만족하는 최종 fare_amount를 푸는 역함수.
# 호출부는 이 값을 최종 운임으로 쓰지 않고, 현재 surged fare 대비 추가 인센티브 산정 기준으로 사용한다.
def required_fare_for_target_p(
    last_dropoff_cell: str,
    dropoff_cell: str,
    call_datetime: datetime,
    target_p: float,
    D_pu: float,
    trip_distance: float,
    beta_f: float | None = None,
    *,
    alpha_sensitivity: float = 1.0,
    pickup_cell: str | None = None,  # 예약 인자: 추후 pickup-cell 기반 feature 도입 시 사용.
) -> float:
    beta = beta_f if beta_f is not None else _BETA_F
    if not isfinite(beta) or abs(beta) < 1e-9:
        raise ValueError("beta_f must be finite and non-zero for inverse fare calculation")
    if not isfinite(alpha_sensitivity) or alpha_sensitivity <= 1e-9:
        raise ValueError("alpha_sensitivity must be positive and finite for inverse fare calculation")
    features = acceptance_features(
        last_dropoff_cell=last_dropoff_cell,
        dropoff_cell=dropoff_cell,
        call_datetime=call_datetime,
        D_pu=D_pu,
        trip_distance=trip_distance,
        pickup_cell=pickup_cell,
    )
    z_target = pu_corrected_logit(target_p, _C)
    return ((z_target - _BETA_0) / alpha_sensitivity - (features.z_without_fare - _BETA_0)) / beta


def pu_correction_constant() -> float:
    return _C


if __name__ == '__main__':
    # smoke test
    import h3
    cell_test = h3.latlng_to_cell(40.7580, -73.9855, 9)  # Times Square
    p = acceptance_probability(
        last_dropoff_cell=cell_test,
        dropoff_cell=cell_test,
        call_datetime=datetime(2013, 7, 1, 18, 30),
        fare_amount=12.5,
        D_pu=0.3,
        trip_distance=1.8,
    )
    print(f'sample P = {p:.4f}')
