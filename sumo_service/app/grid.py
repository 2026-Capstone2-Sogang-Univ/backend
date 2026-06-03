import os
import h3

from .pricing import compute_limited_surge

H3_RESOLUTION = int(os.getenv("H3_RESOLUTION", "9"))

# 운영(experiment_config=None)과 실험 모드 fallback이 같은 값을 쓰도록 한 곳에서 관리.
DEFAULT_ELASTICITY = 0.6


def get_cell(lat: float, lng: float) -> str:
    return h3.latlng_to_cell(lat, lng, H3_RESOLUTION)


def cells_within_k_ring(cell: str, k: int = 1) -> set[str]:
    """Return H3 cell ids within k grid steps of center (k=0 → center only)."""
    return set(h3.grid_disk(cell, max(0, int(k))))


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
    max_surge: float = 4.9,
    increment: float = 0.1,
    elasticity: float = DEFAULT_ELASTICITY,
) -> float:
    return compute_limited_surge(
        supply,
        demand,
        elasticity=elasticity,
        min_active_surge=min_active_surge,
        max_surge=max_surge,
        increment=increment,
    )
