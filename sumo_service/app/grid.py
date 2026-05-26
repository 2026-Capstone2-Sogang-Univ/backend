import os
import math

import h3

H3_RESOLUTION = int(os.getenv("H3_RESOLUTION", "9"))

# 운영(experiment_config=None)과 실험 모드 fallback이 같은 값을 쓰도록 한 곳에서 관리.
DEFAULT_ELASTICITY = 0.6


def get_cell(lat: float, lng: float) -> str:
    return h3.latlng_to_cell(lat, lng, H3_RESOLUTION)


def cell_center_latlng(cell: str) -> tuple[float, float]:
    return h3.cell_to_latlng(cell)


def cell_boundary(cell: str) -> list[tuple[float, float]]:
    return list(h3.cell_to_boundary(cell))


def compute_surge(
    supply: int,
    demand: int,
    # min_active_surge가 1.2면 수요>공급이 발생하는 순간 1.0→1.2로 점프해 UI 상 작은 잡음을
    # 만들지 않는다. 0.1 단위 ceil도 가격 표시 안정성을 위한 정책 결정.
    min_active_surge: float = 1.2,
    max_surge: float = 4.0,
    increment: float = 0.1,
    elasticity: float = DEFAULT_ELASTICITY,
) -> float:
    if supply == 0 and demand == 0:
        return 1.0
    if supply == 0:
        return max_surge
    if demand == 0:
        return 1.0

    raw_surge = (demand / supply) ** (1 / elasticity)
    if raw_surge <= 1.0:
        return 1.0

    active_surge = max(raw_surge, min_active_surge)
    stepped_surge = math.ceil(active_surge / increment) * increment
    return min(round(stepped_surge, 10), max_surge)
