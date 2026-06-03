"""2×2 P* grid base scenarios (predicted demand, inverse-pricing policy)."""
from __future__ import annotations

from dataclasses import dataclass

from scripts.screening_scenarios import lambda_for_ratio

# Default high-surge bucket for P* sweeps (raw_surge >= 3.5 → design target 85%).
DEFAULT_PSTAR_BUCKET = "raw_gte_3_5"
DEFAULT_PSTAR_LEVELS: tuple[float, ...] = (0.80, 0.85, 0.90)

N_TAXIS = 300


@dataclass(frozen=True)
class PstarGridCell:
    """One cell of the 2×2 matrix (demand environment × surge cap)."""

    cell_id: str
    label: str
    passenger_lambda: int
    ratio_label: str
    surge_max: float
    # Optional; fair_ratio35 has no K cap in 14k finalists
    dispatch_max_candidates: int | None = None

    @property
    def scenario_id_prefix(self) -> str:
        # 4.9 → cap49, 6.0 → cap60
        cap_tag = int(round(self.surge_max * 10))
        return f"pgrid_{self.cell_id}_cap{cap_tag}"


def build_pstar_grid_cells() -> tuple[PstarGridCell, ...]:
    """A=fair 3.5:1, B=stress 5.5:1 × cap 4.9 vs 6.0."""
    return (
        PstarGridCell(
            cell_id="fair35",
            label="[A] fair_ratio35 — AI가 소폭 유리했던 운영 수요",
            passenger_lambda=lambda_for_ratio(N_TAXIS, 3.5),
            ratio_label="3.5:1",
            surge_max=4.9,
        ),
        PstarGridCell(
            cell_id="stress55",
            label="[B] B_stress_55 — TLC 5.5:1 스트레스",
            passenger_lambda=lambda_for_ratio(N_TAXIS, 5.5),
            ratio_label="5.5:1",
            surge_max=4.9,
        ),
        PstarGridCell(
            cell_id="fair35",
            label="[A] fair_ratio35 — 규제 완화 (역산 추적)",
            passenger_lambda=lambda_for_ratio(N_TAXIS, 3.5),
            ratio_label="3.5:1",
            surge_max=6.0,
        ),
        PstarGridCell(
            cell_id="stress55",
            label="[B] B_stress_55 — 고수요 + 규제 완화",
            passenger_lambda=lambda_for_ratio(N_TAXIS, 5.5),
            ratio_label="5.5:1",
            surge_max=6.0,
        ),
    )


def cell_run_id(cell: PstarGridCell, target_p: float) -> str:
    ptag = int(round(target_p * 100))
    return f"{cell.scenario_id_prefix}_p{ptag}"
