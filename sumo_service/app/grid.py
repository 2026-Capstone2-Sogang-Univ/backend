import os

import h3

H3_RESOLUTION = int(os.getenv("H3_RESOLUTION", "9"))


def get_cell(lat: float, lng: float) -> str:
    return h3.latlng_to_cell(lat, lng, H3_RESOLUTION)


def cell_center_latlng(cell: str) -> tuple[float, float]:
    return h3.cell_to_latlng(cell)


def cell_boundary(cell: str) -> list[tuple[float, float]]:
    return list(h3.cell_to_boundary(cell))


def compute_surge(supply: int, demand: int, max_surge: float = 5.0) -> float:
    if supply == 0 and demand == 0:
        return 1.0
    if supply == 0:
        return max_surge
    if demand == 0:
        return 0.0
    return min((demand / supply) ** (1 / 0.6), max_surge)
