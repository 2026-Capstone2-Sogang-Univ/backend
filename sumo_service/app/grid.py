import os
import math

import h3

H3_RESOLUTION = int(os.getenv("H3_RESOLUTION", "9"))


def get_cell(lat: float, lng: float) -> str:
    return h3.latlng_to_cell(lat, lng, H3_RESOLUTION)


def cell_center_latlng(cell: str) -> tuple[float, float]:
    return h3.cell_to_latlng(cell)


def cell_boundary(cell: str) -> list[tuple[float, float]]:
    return list(h3.cell_to_boundary(cell))


def compute_surge(
    supply: int,
    demand: int,
    min_active_surge: float = 1.2,
    max_surge: float = 4.9,
    increment: float = 0.1,
    elasticity: float = 0.6,
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
