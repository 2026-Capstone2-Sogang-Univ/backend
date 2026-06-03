"""
SimulationManager: manages the SUMO/TraCI simulation loop in a dedicated thread.

TraCI is a synchronous (blocking) API. The loop runs in a ThreadPoolExecutor via
asyncio.run_in_executor() so it never blocks FastAPI's async event loop.

Speed: SIMULATION_SPEED controls how many simulated seconds pass per real second.
  SIMULATION_SPEED = 60 → 1 real second = 1 simulated minute
  SIMULATION_SPEED =  2 → 1 real second = 2 simulated seconds (slow/debug)
Broadcast: driven by simulation steps (no separate timer). One WebSocket message
  is sent immediately after each traci.simulationStep().
  Broadcast fps = FRAME_RATE. SUMO step-length = SIMULATION_SPEED / FRAME_RATE.
Duration: simulation auto-terminates at simulated time 3600s (1 hour).
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import math
import os
import random as _random
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime as _datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import traci
import traci.exceptions
from traci import constants as tc

from .coord import make_sumolib_converter, sumo_to_latlng
from .demand_history import DemandHistoryStore, floor_to_15min
from .module3_validation import horizon_eval_snapshot
from .driver.decision_function import (
    acceptance_features as _acceptance_features,
    acceptance_probability as _acceptance_probability,
    required_fare_for_target_features as _required_fare_for_target_features,
)

_logger = logging.getLogger(__name__)
from .db.engine import get_pool
from .db.writer import db_writer_task as _db_writer_task
from .fare import (
    SPEED_THRESHOLD_MPS,
    TripAccumulator,
    calculate_fare,
    calculate_meter_fare,
    estimate_fare,
)
from .grid import (
    DEFAULT_ELASTICITY,
    H3_RESOLUTION,
    cell_center_latlng,
    cells_within_k_ring,
    compute_surge,
    get_cell,
)
from .h3_cells import load_model_h3_cells
from .passenger import Passenger
from .pricing import apply_surge_limits, compute_raw_surge
from .rebalance import (
    acceptance_bonus_from_raw_surge,
    gaussian_cell_weight,
    select_top_surge_deficit_cells,
)
from .experiment_pacing import resolve_experiment_pacing
from .prediction import PredictionDemandProvider
from .weather import StaticWeatherProvider

if TYPE_CHECKING:
    from .connection_manager import ConnectionManager

SUMO_CONFIG = str(
    Path(__file__).parent.parent
    / "sumo_configs"
    / "NY"
    / "manhattan.sumocfg"
)
SUMO_NET = str(
    Path(__file__).parent.parent
    / "sumo_configs"
    / "NY"
    / "manhattan_car_only.net.xml"
)
# Set SUMO_GUI=1 to open the SUMO GUI window (useful for local debugging).
SUMO_BINARY = "sumo-gui" if os.getenv("SUMO_GUI") == "1" else "sumo"

SIM_DURATION = float(os.getenv("SIM_DURATION", "3600"))        # simulated seconds
# Docker / batch progress: log every N sim-seconds (0 = disabled).
SIM_PROGRESS_LOG_INTERVAL_S = float(os.getenv("SIM_PROGRESS_LOG_INTERVAL_S", "500"))
FRAME_RATE = 60.0  # broadcast fps (WebSocket messages per real second)
SIMULATION_SPEED = float(os.getenv("SIMULATION_SPEED", "20"))  # simulated seconds per real second
STEP_LENGTH = (
    SIMULATION_SPEED / FRAME_RATE
)  # simulated seconds per TraCI step (passed to SUMO)
REAL_STEP_SLEEP = 1.0 / FRAME_RATE  # real seconds between TraCI steps

N_TAXIS = int(os.getenv("N_TAXIS", "300"))


def _resolve_n_background_cars() -> int:
    raw = os.getenv("N_BACKGROUND_CARS", "").strip()
    if raw:
        return max(0, int(raw))
    if os.getenv("EXPERIMENT_FAST", "1").strip().lower() not in ("0", "false", "no"):
        return 200
    return 1200


N_BACKGROUND_CARS = _resolve_n_background_cars()
# Fast bench: cap empty-taxi dispatch tries per step when waiting backlog is large.
_DISPATCH_BACKLOG_WAIT_THRESHOLD = int(os.getenv("DISPATCH_BACKLOG_WAIT_THRESHOLD", "60"))
_DISPATCH_MAX_EMPTY_PER_STEP_FAST = int(os.getenv("DISPATCH_MAX_EMPTY_PER_STEP_FAST", "80"))
BENCH_MAX_FIND_ROUTE_PER_STEP = int(os.getenv("BENCH_MAX_FIND_ROUTE_PER_STEP", "600"))
# After reject / failed offer: skip repeat findRoute for same taxi (and optionally taxi×passenger).
TAXI_DISPATCH_COOLDOWN_S = float(os.getenv("TAXI_DISPATCH_COOLDOWN_S", "5"))
PAIR_DISPATCH_COOLDOWN_S = float(os.getenv("PAIR_DISPATCH_COOLDOWN_S", "60"))
DISPATCH_H3_K_RING = int(os.getenv("DISPATCH_H3_K_RING", "1"))
DISPATCH_H3_FILTER = os.getenv("DISPATCH_H3_FILTER", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
# NYC Q90 accepted pickup ~2.14 mi — skip findRoute beyond this straight-line cap.
PICKUP_MAX_EUCLIDEAN_MILES = float(os.getenv("PICKUP_MAX_EUCLIDEAN_MILES", "2.14"))
PICKUP_MAX_EUCLIDEAN_M_SQ = (PICKUP_MAX_EUCLIDEAN_MILES * 1609.344) ** 2
# Waiting passenger natural churn (0 = disabled). Default 15 sim-min.
PASSENGER_WAIT_ABANDON_S = float(os.getenv("PASSENGER_WAIT_ABANDON_S", "900"))

PASSENGER_SPAWN_INTERVAL = 300.0
PASSENGER_LAMBDA = int(os.getenv("PASSENGER_LAMBDA", "5"))
PICKUP_THRESHOLD_M = 30.0
MAX_COMPLETED_PASSENGERS = 100
DISPATCH_TIMEOUT_S = 600.0   # 배차 후 이 시간 내 픽업 못하면 재배차
TRIP_TIMEOUT_S = 1800.0      # 탑승 후 이 시간 내 하차 못하면 강제 완료

# 기사 행동 모델: V/s 테이블은 2013 NYC 데이터 기반 → 동일 시간대 사용
SIM_BASE_DATETIME = _datetime(2013, 7, 8, 8, 0, 0)
# parquet 모드에서 승객 대량 스폰 시 findRoute RPC 폭증 방지용 튜닝값
DISPATCH_MAX_CANDIDATES = int(os.getenv("DISPATCH_MAX_CANDIDATES", "3"))

PASSENGER_SOURCE = os.getenv("PASSENGER_SOURCE", "random")
TRIPS_FILE = Path(__file__).parent.parent / "sumo_configs" / "NY" / "trips_processed.json"
SCC_FILE = Path(__file__).parent.parent / "sumo_configs" / "NY" / "routable_scc.json"

RAW_SURGE_BUCKETS: tuple[tuple[str, float, float | None, float], ...] = (
    ("raw_lt_1_5", float("-inf"), 1.5, 0.55),
    ("raw_lt_2_5", 1.5, 2.5, 0.70),
    ("raw_lt_3_5", 2.5, 3.5, 0.80),
    ("raw_gte_3_5", 3.5, None, 0.85),
)
DEFAULT_TARGET_MATCHING_RATES = {
    bucket: target for bucket, _, _, target in RAW_SURGE_BUCKETS
}
DEFAULT_PRICING_POLICY = {
    "epsilon": -0.6,
    "surge_min": 1.2,
    "surge_max": 4.9,
    "alpha_sensitivity": 1.0,
}

# Manhattan 핫스팟: (lat, lng, importance). 하차 후 택시 목적지 가중치에 사용.
HOTSPOTS: list[tuple[float, float, float]] = [
    (40.7580, -73.9855, 1.0),  # Times Square
    (40.7506, -73.9935, 1.0),  # Penn Station
    (40.7527, -73.9772, 1.0),  # Grand Central
    (40.7484, -73.9857, 0.7),  # Empire State Building
    (40.7359, -73.9911, 0.7),  # Union Square
    (40.7681, -73.9819, 0.7),  # Columbus Circle
    (40.7587, -73.9787, 0.7),  # Rockefeller Center
    (40.7074, -74.0113, 1.0),  # Wall Street / Financial District
    (40.7127, -74.0134, 0.7),  # World Trade Center
]
HOTSPOT_SIGMA_M = 300.0     # 가우시안 감쇠 스케일 (m)
HOTSPOT_BASE_WEIGHT = 1.0   # 모든 엣지 최소 가중치
REBALANCE_SIGMA_M = 400.0   # 고서지 H3 셀 중심으로 빈차 유도 시 감쇠 스케일 (m)

_SUB_VARS = [tc.VAR_POSITION, tc.VAR_ANGLE, tc.VAR_SPEED, tc.VAR_DISTANCE, tc.VAR_ROAD_ID, tc.VAR_ROUTE_INDEX]
# bg 차량은 위치 스냅샷이 필요 없고 경로 연장 판정에 쓰는 값만 구독한다.
_BG_SUB_VARS = [tc.VAR_ROAD_ID, tc.VAR_ROUTE_INDEX]
# 경로 끝에서 남은 엣지 수가 이 값 이하이면 새 경로를 이어 붙인다.
# route_index는 단조 증가하므로 한 번 임계값을 넘으면 매 스텝 참 → 짧은 엣지를 한 스텝에
# 통과해도 연장이 누락되지 않는다(기존 단일 트리거 엣지 동등비교의 구조적 누락을 제거).
# 값이 2면 "마지막 세 엣지를 한 스텝에 모두 통과"해야만 누락되므로 짧은 커넥터 엣지로 인한
# 잔여 소실이 크게 줄어든다.
_ROUTE_EXTEND_REMAINING = 2
# route_len이 없을 때 매 스텝 findRoute를 돌리지 않도록 최소 간격(시뮬 초).
_ROUTE_EXTEND_RETRY_SIM_S = 15.0
# 경로 연장이 성공하면 차량은 새 경로의 index 0에서 시작하고 (경로길이 - _ROUTE_EXTEND_REMAINING - 1)
# 엣지를 주행한 뒤 다시 연장한다. 짧은 경로가 매 스텝 재연장(findRoute 폭증)되는 것을 막기 위해
# 라우팅이 돌려주는 경로 길이의 최소 목표치를 둔다 (재연장 주기 ≥ _MIN_ROUTE_EDGES - _ROUTE_EXTEND_REMAINING - 1).
_MIN_ROUTE_EDGES = 5


def _poisson_sample(lam: float) -> int:
    """Knuth 알고리즘 — numpy 불필요."""
    L = math.exp(-lam)
    k, p = 0, 1.0
    while p > L:
        k += 1
        p *= _random.random()
    return k - 1


def _traci_module():
    return sys.modules.get("traci", traci)


def _kosaraju_scc(graph: dict[str, set[str]]) -> list[set[str]]:
    """Iterative Kosaraju's algorithm — returns list of SCCs.

    Uses iterative DFS to avoid Python recursion limits on large graphs.
    """
    # Phase 1: forward DFS, record finish order
    visited: set[str] = set()
    finish_order: list[str] = []
    for start in graph:
        if start in visited:
            continue
        visited.add(start)
        stack: list[tuple[str, "object"]] = [(start, iter(graph[start]))]
        while stack:
            node, neighbors = stack[-1]
            advanced = False
            for w in neighbors:
                if w not in visited:
                    visited.add(w)
                    stack.append((w, iter(graph.get(w, set()))))
                    advanced = True
                    break
            if not advanced:
                finish_order.append(node)
                stack.pop()

    # 역방향 그래프 구축
    rgraph: dict[str, set[str]] = {v: set() for v in graph}
    for v, outs in graph.items():
        for w in outs:
            rgraph.setdefault(w, set()).add(v)

    # Phase 2: 종료 순서 역순으로 역방향 DFS — 각 트리가 하나의 SCC
    visited = set()
    sccs: list[set[str]] = []
    for start in reversed(finish_order):
        if start in visited:
            continue
        scc: set[str] = set()
        rstack = [start]
        visited.add(start)
        while rstack:
            node = rstack.pop()
            scc.add(node)
            for w in rgraph.get(node, set()):
                if w not in visited:
                    visited.add(w)
                    rstack.append(w)
        sccs.append(scc)
    return sccs


class SimStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"


# FastAPI 실행과 분리된 실험 전용 설정.
# None이면 기존 WebSocket/DB/실시간 pacing 경로를 그대로 사용하고, 값이 있으면 빠른 동기 실행 경로를 탄다.
@dataclass(frozen=True)
class ExperimentConfig:
    # Single-bucket P* override (default: high-surge band). Use target_matching_rate_overrides for multi-band sweeps.
    target_p: float | None = None
    target_p_bucket: str = "raw_gte_3_5"
    # e.g. (("raw_gte_3_5", 0.90), ("raw_lt_3_5", 0.82))
    target_matching_rate_overrides: tuple[tuple[str, float], ...] | None = None
    elasticity: float = DEFAULT_ELASTICITY
    beta_f: float | None = None
    seed: int = 42
    sim_duration: float = SIM_DURATION
    # 운영(start)과 같은 step rate를 기본값으로 둬야 sweep 결과를 운영에 옮길 수 있다.
    # fast=True: bench (step≈1s, no wall-clock sleep). fast=False: Lab pacing (SIMULATION_SPEED).
    fast: bool = False
    simulation_speed: float = field(
        default_factory=lambda: float(os.getenv("SIMULATION_SPEED", "20"))
    )
    step_length: float | None = None
    real_sleep: float | None = None
    # Predicted-demand policy: surge + Module 3 API refresh cadence (wall clock).
    policy_update_interval_real_s: float = field(
        default_factory=lambda: float(os.getenv("POLICY_UPDATE_INTERVAL_REAL_S", "900"))
    )
    # Bench (--fast): refresh prediction every N sim-seconds (default 900 = 15 sim-min).
    policy_update_interval_sim_s: float = field(
        default_factory=lambda: float(os.getenv("POLICY_UPDATE_INTERVAL_SIM_S", "900"))
    )
    broadcast: bool = False
    demand_source: str = "predicted"
    prediction_mode: str = "sync"
    prediction_url: str = "https://module3-ml.onrender.com/predict"
    prediction_horizon_min: int = 15
    prediction_fallback_policy: str = "error"
    passenger_elasticity: float = 0.0
    alpha_sensitivity: float = 1.0
    weather_source: str = "static"
    # matching: 역산 fare만. rebalance: 역산 fare + 빈차 고서지 재배치(공급 보조).
    policy_mode: str = "matching"
    rebalance_interval_s: float = 60.0
    rebalance_top_k: int = 8
    rebalance_min_raw_surge: float = 1.5
    rebalance_acceptance_coef: float = 0.08
    n_taxis: int | None = None
    passenger_lambda: int | None = None
    dispatch_max_candidates: int | None = None
    surge_max: float | None = None
    # 구간별 플랫폼 인센티브(USD): (<1.5, <2.5, <3.5, >=3.5 raw_surge)
    band_incentive_usd: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class SimulationStartOptions:
    duration: float | None = None
    seed: int | None = None
    passenger_source: str | None = None
    target_matching_rates: dict[str, float] | None = None
    pricing_policy: dict[str, float] | None = None
    taxi_count: int | None = None
    initial_passenger_count: int | None = None


@dataclass(frozen=True)
class ManualCreationRequest:
    kind: str
    entity_id: str
    pickup_lat: float | None = None
    pickup_lng: float | None = None
    dropoff_lat: float | None = None
    dropoff_lng: float | None = None
    lat: float | None = None
    lng: float | None = None


@dataclass
class KpiBucketState:
    bucket: str
    target_rate: float
    request_count: int = 0
    matched_count: int = 0
    wait_seconds: list[float] | None = None
    p_actual_sum: float = 0.0
    p_actual_count: int = 0

    def __post_init__(self) -> None:
        if self.wait_seconds is None:
            self.wait_seconds = []


@dataclass
class FastBenchDispatchStats:
    """Running dispatch KPIs when fast bench skips per-decision _event_log rows."""

    decision_count: int = 0
    accept_count: int = 0
    offered_passenger_ids: set[str] = field(default_factory=set)
    matched_passenger_ids: set[str] = field(default_factory=set)
    decisions_per_passenger: dict[str, int] = field(default_factory=dict)
    matching_rate_error_sum: float = 0.0
    matching_rate_error_abs_sum: float = 0.0
    matching_rate_error_count: int = 0
    surge_clamped_count: int = 0
    final_surge_sum: float = 0.0
    final_surge_count: int = 0
    final_fare_sum: float = 0.0
    final_fare_count: int = 0
    required_fare_sum: float = 0.0
    required_fare_count: int = 0
    p_actual_sum: float = 0.0
    p_actual_count: int = 0


@dataclass
class RuntimeKpiState:
    buckets: dict[str, KpiBucketState]
    completed_trip_count: int = 0
    total_fare_cents: int = 0
    total_meter_fare_cents: int = 0
    empty_wait_seconds: list[float] | None = None

    def __post_init__(self) -> None:
        if self.empty_wait_seconds is None:
            self.empty_wait_seconds = []


class SimulationManager:
    def __init__(self, experiment_config: ExperimentConfig | None = None) -> None:
        self.status = SimStatus.IDLE
        self.experiment_config = experiment_config
        self.connection_manager: Optional[ConnectionManager] = None
        self._paused = False
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._state_queue: Optional[asyncio.Queue] = None
        self._executor_task: Optional[asyncio.Future] = None
        self._broadcast_task: Optional[asyncio.Task] = None
        self._state: dict = {"vehicles": [], "passengers": [], "sim_time": 0.0}
        self._boundary: dict = {"minX": 0.0, "minY": 0.0, "maxX": 0.0, "maxY": 0.0}
        self._passengers: dict[str, Passenger] = {}
        self._passenger_counter: int = 0
        self._active_trips: dict[str, TripAccumulator] = {}
        self._taxi_targets: dict[str, str] = {}
        self._taxi_states: dict[str, str] = {}
        self._taxi_dispatch_times: dict[str, float] = {}
        self._last_spawn_interval: int = -1
        self._last_surge_interval: int = -1
        self._last_progress_log_bucket: int = -1
        self._last_policy_wall_time: float | None = None
        self._last_policy_sim_time: float | None = None
        self._cached_predicted_demand: dict[str, float] | None = None
        self._forecasts_by_target_sim_time: dict[float, dict[str, float]] = {}
        self._forecast_issued_sim_time: dict[float, float] = {}
        self._evaluated_forecast_targets: set[float] = set()
        self._last_rebalance_interval: int = -1
        self._routable_edges: list[str] = []
        self._h3_to_edges: dict[str, list[str]] = {}
        self._last_grid_supply: dict[str, int] = {}
        self._last_grid_demand: dict[str, int] = {}
        self._rebalance_edge_weights: list[float] | None = None
        self._surge_cells: list[dict] = []
        # WebSocket은 list 형태가 필요하지만 배차 판단은 pickup H3로 즉시 조회해야 하므로 dict 캐시를 함께 둔다.
        self._surge_by_h3: dict[str, float] = {}
        self._raw_surge_by_h3: dict[str, float] = {}
        self._target_matching_rate_by_h3: dict[str, float] = {}
        self._history_store: DemandHistoryStore | None = None
        self._prediction_demand_provider: PredictionDemandProvider | None = None
        self._surge_diagnostics: list[dict] = []
        self._completed_passengers: list[dict] = []
        self._completed_trip_count: int = 0
        self._trip_queue: list[dict] = []
        self._latlng: Callable[[float, float], tuple[float, float]] | None = None
        # veh_id → 현재 할당된 경로의 엣지 수. route_index와 비교해 끝에 근접했는지 판정.
        self._bg_route_len: dict[str, int] = {}
        self._taxi_route_len: dict[str, int] = {}
        self._route_extend_retry_after: dict[str, float] = {}
        self._edge_weights: list[float] = []
        self._routable_edges_set: set[str] = set()
        self._run_id: int | None = None
        self._db_queue: asyncio.Queue | None = None
        self._db_writer_task: asyncio.Task | None = None
        self._taxi_dispatch_ids: dict[str, str] = {}
        self._taxi_dispatch_surge: dict[str, float] = {}
        self._passenger_dispatch_buckets: dict[str, str] = {}
        self._taxi_last_dropoff_cells: dict[str, str] = {}
        self._taxi_appeared: set[str] = set()   # taxis that have appeared in sub_results at least once
        self._taxi_missing_since: dict[str, float] = {}  # veh_id → sim_time when first detected missing
        # bg 차량은 트립 로직이 없어 arrival 시 영구 손실되므로 택시와 동일한 리스폰 폴백을 둔다.
        self._bg_appeared: set[str] = set()
        self._bg_missing_since: dict[str, float] = {}
        # 실험 모드는 DB 대신 메모리 이벤트 로그로 KPI를 집계한다.
        self._event_log: list[dict] = []
        self._dispatch_pricing_cache: dict[str, dict] = {}
        self._dispatch_taxi_round: int = 0
        self._find_route_calls_this_step: int = 0
        self._fast_dispatch_stats: FastBenchDispatchStats | None = None
        self._taxi_dispatch_cooldown_until: dict[str, float] = {}
        self._pair_dispatch_cooldown_until: dict[tuple[str, str], float] = {}
        self._rejected_dispatch_pairs: set[tuple[str, str]] = set()
        # 기사 빈차 대기시간은 이전 하차 시각과 다음 수락 시각의 차이로 계산한다.
        self._taxi_previous_dropoff_times: dict[str, float] = {}
        self._manual_requests: deque[ManualCreationRequest] = deque()
        self._manual_passenger_counter: int = 0
        self._manual_taxi_counter: int = 0
        self._runtime_duration: float = SIM_DURATION
        self._runtime_seed: int | None = None
        self._runtime_passenger_source: str = PASSENGER_SOURCE
        self._runtime_passenger_source_overridden: bool = False
        self._runtime_taxi_count: int = N_TAXIS
        self._runtime_initial_passenger_count: int = 0
        self._target_matching_rates: dict[str, float] = dict(DEFAULT_TARGET_MATCHING_RATES)
        self._pricing_policy: dict[str, float] = dict(DEFAULT_PRICING_POLICY)
        self._apply_experiment_overrides()
        self._runtime_kpi: RuntimeKpiState = self._new_runtime_kpi(self._target_matching_rates)

    @staticmethod
    def _resolve_target_matching_rates_from_config(
        config: ExperimentConfig | None,
    ) -> dict[str, float]:
        rates = dict(DEFAULT_TARGET_MATCHING_RATES)
        if config is None:
            return rates
        if config.target_matching_rate_overrides:
            for bucket, value in config.target_matching_rate_overrides:
                if bucket not in rates:
                    raise ValueError(
                        f"unknown target_matching_rate bucket {bucket!r}; "
                        f"expected one of {sorted(rates)}"
                    )
                p = float(value)
                if not 0.0 < p <= 1.0:
                    raise ValueError(f"target P* for {bucket!r} must be in (0, 1], got {p}")
                rates[bucket] = p
        if config.target_p is not None:
            bucket = config.target_p_bucket
            if bucket not in rates:
                raise ValueError(
                    f"unknown target_p_bucket {bucket!r}; expected one of {sorted(rates)}"
                )
            p = float(config.target_p)
            if not 0.0 < p <= 1.0:
                raise ValueError(f"target_p must be in (0, 1], got {p}")
            rates[bucket] = p
        return rates

    def _sync_runtime_kpi_bucket_targets(self) -> None:
        """Keep band KPI target_rate aligned with _target_matching_rates after overrides."""
        for bucket, state in self._runtime_kpi.buckets.items():
            if bucket in self._target_matching_rates:
                state.target_rate = self._target_matching_rates[bucket]

    def _apply_experiment_overrides(self) -> None:
        config = self.experiment_config
        if config is None:
            return
        if config.n_taxis is not None:
            self._runtime_taxi_count = int(config.n_taxis)
        if config.surge_max is not None:
            self._pricing_policy["surge_max"] = float(config.surge_max)
        if config.alpha_sensitivity is not None:
            self._pricing_policy["alpha_sensitivity"] = float(config.alpha_sensitivity)
        self._target_matching_rates = self._resolve_target_matching_rates_from_config(config)
        if hasattr(self, "_runtime_kpi"):
            self._sync_runtime_kpi_bucket_targets()

    def _passenger_lambda(self) -> int:
        if self.experiment_config is not None and self.experiment_config.passenger_lambda is not None:
            return int(self.experiment_config.passenger_lambda)
        return PASSENGER_LAMBDA

    def _dispatch_max_candidates(self) -> int:
        if (
            self.experiment_config is not None
            and self.experiment_config.dispatch_max_candidates is not None
        ):
            return int(self.experiment_config.dispatch_max_candidates)
        return DISPATCH_MAX_CANDIDATES

    def _uses_realtime_policy_cadence(self) -> bool:
        config = self.experiment_config
        return config is not None and config.demand_source == "predicted"

    def _policy_update_interval_real_s(self) -> float:
        config = self.experiment_config
        if config is not None:
            return float(config.policy_update_interval_real_s)
        return float(os.getenv("POLICY_UPDATE_INTERVAL_REAL_S", "900"))

    def _policy_update_interval_sim_s(self) -> float:
        config = self.experiment_config
        if config is not None:
            return float(config.policy_update_interval_sim_s)
        return float(os.getenv("POLICY_UPDATE_INTERVAL_SIM_S", "900"))

    def _maybe_log_sim_progress(self, sim_time: float, sim_duration: float) -> None:
        """Emit periodic progress to stdout (visible in docker logs)."""
        if SIM_PROGRESS_LOG_INTERVAL_S <= 0:
            return
        bucket = int(sim_time // SIM_PROGRESS_LOG_INTERVAL_S)
        if bucket <= 0 or bucket <= self._last_progress_log_bucket:
            return
        self._last_progress_log_bucket = bucket

        waiting = sum(1 for p in self._passengers.values() if p.state == "waiting")
        assigned = sum(1 for p in self._passengers.values() if p.state == "assigned")
        active_trips = len(self._active_trips)
        pct = (100.0 * sim_time / sim_duration) if sim_duration > 0 else 0.0

        label = ""
        config = self.experiment_config
        if config is not None:
            label = (
                f" demand={config.demand_source} policy={config.policy_mode}"
                f" taxis={config.n_taxis or self._runtime_taxi_count}"
            )

        print(
            f"[progress] sim_time={sim_time:.0f}/{sim_duration:.0f} ({pct:.1f}%)"
            f"{label}"
            f" waiting={waiting} assigned={assigned}"
            f" active_trips={active_trips} completed_trips={self._completed_trip_count}",
            flush=True,
        )
        if self.experiment_config is not None and self.experiment_config.fast:
            fds = self._fast_dispatch_stats
            print(
                f"[progress] event_log_rows={len(self._event_log)} "
                f"dispatch_stats={fds.decision_count if fds else 0}",
                flush=True,
            )

    def _should_fetch_prediction(self, sim_time: float) -> bool:
        if not self._uses_realtime_policy_cadence():
            return False
        config = self.experiment_config
        if config is not None and config.fast:
            if self._last_policy_sim_time is None:
                self._last_policy_sim_time = sim_time
                return True
            if sim_time - self._last_policy_sim_time >= self._policy_update_interval_sim_s():
                self._last_policy_sim_time = sim_time
                return True
            return False
        now = time.perf_counter()
        if self._last_policy_wall_time is None:
            self._last_policy_wall_time = now
            return True
        if now - self._last_policy_wall_time >= self._policy_update_interval_real_s():
            self._last_policy_wall_time = now
            return True
        return False

    def _surge_recompute_interval_sim_s(self) -> float:
        if self.experiment_config is not None and self.experiment_config.fast:
            return float(os.getenv("SURGE_RECOMPUTE_INTERVAL_S", "15"))
        return 5.0

    def _should_recompute_surge_grid(self, sim_time: float) -> bool:
        interval_s = self._surge_recompute_interval_sim_s()
        surge_interval = int(sim_time / interval_s)
        if surge_interval > self._last_surge_interval:
            self._last_surge_interval = surge_interval
            return True
        return False

    def _bench_skip_surge_cell_diagnostics(self) -> bool:
        """Actual fast bench: skip per-H3 diagnostic rows (M3 predicted runs still record)."""
        config = self.experiment_config
        return (
            config is not None
            and config.fast
            and config.demand_source == "actual"
        )

    def _find_route_budget_ok(self) -> bool:
        config = self.experiment_config
        if config is None or not config.fast:
            return True
        return self._find_route_calls_this_step < BENCH_MAX_FIND_ROUTE_PER_STEP

    def _sim_find_route(self, from_edge: str, to_edge: str):
        if not self._find_route_budget_ok():
            raise traci.exceptions.TraCIException("bench findRoute budget exhausted")
        self._find_route_calls_this_step += 1
        return traci.simulation.findRoute(from_edge, to_edge)

    def _prediction_horizon_sim_seconds(self) -> float:
        config = self.experiment_config
        minutes = config.prediction_horizon_min if config else 15
        return float(minutes) * 60.0

    def _target_sim_time(self, sim_time: float) -> float:
        sim_dt = SIM_BASE_DATETIME + timedelta(seconds=sim_time)
        target_dt = floor_to_15min(
            sim_dt + timedelta(minutes=self.experiment_config.prediction_horizon_min)
        )
        return (target_dt - SIM_BASE_DATETIME).total_seconds()

    def _register_prediction_forecast(self, sim_time: float, demand: dict[str, float]) -> None:
        target_sim_time = self._target_sim_time(sim_time)
        self._forecasts_by_target_sim_time[target_sim_time] = dict(demand)
        self._forecast_issued_sim_time[target_sim_time] = sim_time

    def _evaluate_forecasts_at_horizon(
        self,
        sim_time: float,
        grid_demand: dict[str, int],
    ) -> None:
        if not self._uses_realtime_policy_cadence():
            return
        for target_sim_time, predicted in list(self._forecasts_by_target_sim_time.items()):
            if target_sim_time in self._evaluated_forecast_targets:
                continue
            if sim_time < target_sim_time:
                continue
            issued_sim_time = self._forecast_issued_sim_time.get(target_sim_time, target_sim_time)
            self._evaluated_forecast_targets.add(target_sim_time)
            snap = horizon_eval_snapshot(
                issued_sim_time=issued_sim_time,
                target_sim_time=target_sim_time,
                predicted=predicted,
                actual={k: float(v) for k, v in grid_demand.items()},
            )
            self._emit_event(
                snap["type"],
                {k: v for k, v in snap.items() if k != "type"},
            )

    def _resolve_predicted_demand(
        self,
        sim_time: float,
        grid_demand: dict[str, int],
    ) -> dict[str, float]:
        if self._prediction_demand_provider is None:
            raise RuntimeError("predicted demand source requires a prediction demand provider")
        if self._should_fetch_prediction(sim_time) or self._cached_predicted_demand is None:
            demand = self._prediction_demand_provider.demand_by_h3(
                SIM_BASE_DATETIME + timedelta(seconds=sim_time),
                mode=self._provider_prediction_mode(self.experiment_config.prediction_mode),
                actual_demand=grid_demand,
            )
            self._cached_predicted_demand = dict(demand)
            self._register_prediction_forecast(sim_time, self._cached_predicted_demand)
        return self._cached_predicted_demand

    def _band_incentive_usd(self, raw_surge: float) -> float:
        config = self.experiment_config
        if config is None or not config.band_incentive_usd:
            return 0.0
        bucket = self._raw_surge_bucket(float(raw_surge))
        order = ("raw_lt_1_5", "raw_lt_2_5", "raw_lt_3_5", "raw_gte_3_5")
        idx = order.index(bucket)
        return float(config.band_incentive_usd[idx])

    # ------------------------------------------------------------------
    # Public async API (called from FastAPI endpoints)
    # ------------------------------------------------------------------

    async def start(self, options: SimulationStartOptions | None = None) -> None:
        if self.status == SimStatus.RUNNING:
            return
        self._reset_runtime_options()
        self._apply_start_options(options)
        self._reset_run_state()
        if self._runtime_seed is not None:
            _random.seed(self._runtime_seed)
        self._paused = False
        self._stop_event.clear()
        self._loop = asyncio.get_event_loop()
        self._state_queue = asyncio.Queue()

        pool = get_pool()
        if pool is not None:
            params = json.dumps({
                "n_taxis": N_TAXIS,
                "runtime_taxi_count": self._runtime_taxi_count,
                "n_background_cars": N_BACKGROUND_CARS,
                "frame_rate": FRAME_RATE,
                "simulation_speed": SIMULATION_SPEED,
                "passenger_lambda": PASSENGER_LAMBDA,
                "dispatch_timeout_s": DISPATCH_TIMEOUT_S,
                "trip_timeout_s": TRIP_TIMEOUT_S,
                "seed": self._runtime_seed,
                "target_matching_rates": self._target_matching_rates,
                "pricing_policy": self._pricing_policy,
            })
            async with pool.acquire() as conn:
                self._run_id = await conn.fetchval(
                    "INSERT INTO simulation_run (sim_duration_s, passenger_source, params) "
                    "VALUES ($1, $2, $3) RETURNING id",
                    self._runtime_duration, self._passenger_source(), params,
                )
                await conn.executemany(
                    "INSERT INTO taxi (run_id, taxi_id) VALUES ($1, $2)",
                    [(self._run_id, f"taxi_{i}") for i in range(self._runtime_taxi_count)],
                )
        self._db_queue = asyncio.Queue()
        self._db_writer_task = asyncio.create_task(_db_writer_task(self._db_queue))

        self.status = SimStatus.RUNNING
        self._executor_task = self._loop.run_in_executor(None, self._run_loop)
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())

    async def pause(self) -> None:
        if self.status == SimStatus.RUNNING:
            self._paused = True
            self.status = SimStatus.PAUSED

    async def resume(self) -> None:
        if self.status == SimStatus.PAUSED:
            self._paused = False
            self.status = SimStatus.RUNNING

    async def restart(self, options: SimulationStartOptions | None = None) -> None:
        await self._shutdown()
        await self.start(options)

    async def stop(self) -> None:
        await self._shutdown()

    @classmethod
    def fresh_experiment(
        cls,
        config: ExperimentConfig,
        connection_manager: Optional[ConnectionManager] = None,
    ) -> "SimulationManager":
        """실험 sweep용 인스턴스 팩토리. 매 조합마다 새 매니저를 강제해 누적 상태 오염을 막는다."""
        inst = cls(experiment_config=config)
        inst.connection_manager = connection_manager
        return inst

    def run_experiment(self) -> list[dict]:
        """Run a synchronous fast-path simulation and return in-memory events."""
        if self.experiment_config is None:
            raise RuntimeError("run_experiment requires experiment_config")
        _random.seed(self.experiment_config.seed)
        self._apply_experiment_overrides()
        self._reset_run_state()
        self._stop_event.clear()
        self.status = SimStatus.RUNNING
        try:
            self._run_loop()
            prediction_provider = self._close_prediction_demand_provider()
            self._emit_experiment_diagnostics(prediction_provider=prediction_provider)
            self._emit_fast_dispatch_summary()
            return list(self._event_log)
        finally:
            self._close_prediction_demand_provider()

    def get_state(self) -> dict:
        with self._lock:
            return dict(self._state)

    @staticmethod
    def _new_runtime_kpi(
        target_matching_rates: dict[str, float] | None = None,
    ) -> RuntimeKpiState:
        targets = target_matching_rates or DEFAULT_TARGET_MATCHING_RATES
        return RuntimeKpiState(
            buckets={
                bucket: KpiBucketState(bucket=bucket, target_rate=targets[bucket])
                for bucket, _, _, _ in RAW_SURGE_BUCKETS
            }
        )

    def _apply_start_options(self, options: SimulationStartOptions | None) -> None:
        if options is None:
            return
        if options.duration is not None:
            self._runtime_duration = float(options.duration)
        if options.seed is not None:
            self._runtime_seed = int(options.seed)
        if options.passenger_source is not None:
            self._runtime_passenger_source = options.passenger_source
            self._runtime_passenger_source_overridden = True
        if options.taxi_count is not None:
            self._runtime_taxi_count = int(options.taxi_count)
        if options.initial_passenger_count is not None:
            self._runtime_initial_passenger_count = int(options.initial_passenger_count)
        if options.target_matching_rates:
            rates = dict(DEFAULT_TARGET_MATCHING_RATES)
            for key, value in options.target_matching_rates.items():
                if key in rates:
                    rates[key] = float(value)
            self._target_matching_rates = rates
        if options.pricing_policy:
            policy = dict(DEFAULT_PRICING_POLICY)
            for key, value in options.pricing_policy.items():
                if key in policy:
                    policy[key] = float(value)
            self._pricing_policy = policy

    def _reset_runtime_options(self) -> None:
        self._runtime_duration = SIM_DURATION
        self._runtime_seed = None
        self._runtime_passenger_source = PASSENGER_SOURCE
        self._runtime_passenger_source_overridden = False
        self._runtime_taxi_count = N_TAXIS
        self._runtime_initial_passenger_count = 0
        self._target_matching_rates = dict(DEFAULT_TARGET_MATCHING_RATES)
        self._pricing_policy = dict(DEFAULT_PRICING_POLICY)

    def get_status_summary(self) -> dict:
        with self._lock:
            state = dict(self._state)
            vehicles = list(state.get("vehicles", []))
            passengers = list(state.get("passengers", []))
            taxi_states = dict(self._taxi_states)
            waiting_passenger_count = sum(
                1 for passenger in self._passengers.values() if passenger.state == "waiting"
            )
            assigned_passenger_count = sum(
                1 for passenger in self._passengers.values() if passenger.state == "assigned"
            )

            if taxi_states:
                taxi_count = len(taxi_states)
                empty_taxi_count = sum(1 for state_value in taxi_states.values() if state_value == "empty")
                dispatched_taxi_count = sum(
                    1 for state_value in taxi_states.values() if state_value == "dispatched"
                )
                occupied_taxi_count = sum(1 for state_value in taxi_states.values() if state_value == "occupied")
            else:
                taxi_vehicles = [
                    vehicle for vehicle in vehicles
                    if self._is_taxi_id(str(vehicle.get("id", "")))
                ]
                taxi_count = len(taxi_vehicles)
                empty_taxi_count = sum(1 for vehicle in taxi_vehicles if vehicle.get("state") == "empty")
                dispatched_taxi_count = sum(1 for vehicle in taxi_vehicles if vehicle.get("state") == "dispatched")
                occupied_taxi_count = sum(1 for vehicle in taxi_vehicles if vehicle.get("state") == "occupied")

            return {
                "status": self.status,
                "sim_time": state.get("sim_time", 0.0),
                "vehicles": vehicles,
                "passengers": passengers,
                "frame_rate": FRAME_RATE,
                "simulation_speed": SIMULATION_SPEED,
                "vehicle_count": len(vehicles),
                "taxi_count": taxi_count,
                "empty_taxi_count": empty_taxi_count,
                "dispatched_taxi_count": dispatched_taxi_count,
                "occupied_taxi_count": occupied_taxi_count,
                "waiting_passenger_count": waiting_passenger_count,
                "assigned_passenger_count": assigned_passenger_count,
                "completed_trip_count": self._completed_trip_count,
                "h3_resolution": H3_RESOLUTION,
                "passenger_source": self._passenger_source(),
                "target_matching_rates": dict(self._target_matching_rates),
                "pricing_policy": dict(self._pricing_policy),
                "duration": self._runtime_duration,
                "seed": self._runtime_seed,
                "initial_passenger_count": self._runtime_initial_passenger_count,
            }

    def get_kpi_summary(self) -> dict:
        with self._lock:
            sim_time = self._state.get("sim_time", 0.0)
            bucket_payloads = []
            total_requests = 0
            total_matched = 0
            weighted_target_sum = 0.0
            all_wait_seconds: list[float] = []

            for bucket_key, _, _, _ in RAW_SURGE_BUCKETS:
                bucket = self._runtime_kpi.buckets[bucket_key]
                request_count = bucket.request_count
                matched_count = bucket.matched_count
                actual_rate = matched_count / request_count if request_count else 0.0
                matching_rate_error = actual_rate - bucket.target_rate
                waits = list(bucket.wait_seconds or [])
                all_wait_seconds.extend(waits)
                average_wait = sum(waits) / len(waits) if waits else 0.0
                p95_wait = self._percentile(waits, 95.0) if waits else 0.0
                bucket_payloads.append({
                    "bucket": bucket.bucket,
                    "target_rate": bucket.target_rate,
                    "actual_rate": actual_rate,
                    "matching_rate_error": matching_rate_error,
                    "request_count": request_count,
                    "matched_count": matched_count,
                    "average_wait_seconds": average_wait,
                    "p95_wait_seconds": p95_wait,
                    "marginal_utility_points": [],
                })
                total_requests += request_count
                total_matched += matched_count
                weighted_target_sum += bucket.target_rate * request_count

            summary_target = (
                weighted_target_sum / total_requests
                if total_requests else sum(self._target_matching_rates.values()) / len(self._target_matching_rates)
            )
            summary_actual = total_matched / total_requests if total_requests else 0.0
            average_wait = sum(all_wait_seconds) / len(all_wait_seconds) if all_wait_seconds else 0.0
            p95_wait = self._percentile(all_wait_seconds, 95.0) if all_wait_seconds else 0.0
            empty_waits = list(self._runtime_kpi.empty_wait_seconds or [])
            average_empty_wait = sum(empty_waits) / len(empty_waits) if empty_waits else 0.0
            completed_count = self._runtime_kpi.completed_trip_count
            total_fare = self._runtime_kpi.total_fare_cents

            return {
                "sim_time": sim_time,
                "h3_resolution": H3_RESOLUTION,
                "matching": {
                    "target_rate": summary_target,
                    "actual_rate": summary_actual,
                    "matching_rate_error": summary_actual - summary_target,
                    "request_count": total_requests,
                    "matched_count": total_matched,
                    "by_raw_bucket": bucket_payloads,
                },
                "idle_time": {
                    "baseline_seconds": None,
                    "with_incentive_seconds": sum(empty_waits),
                    "with_surge_seconds": sum(empty_waits),
                    "average_empty_taxi_seconds": average_empty_wait,
                },
                "driver_revenue": {
                    "average_cents": int(round(total_fare / completed_count)) if completed_count else 0,
                    "total_cents": total_fare,
                    "completed_trip_count": completed_count,
                    "by_alpha_bucket": [],
                },
                "passenger_waiting_incentive": {
                    "average_wait_seconds": average_wait,
                    "p95_wait_seconds": p95_wait,
                    "marginal_utility_points": [],
                },
                "passenger_waiting": {
                    "average_wait_seconds": average_wait,
                    "p95_wait_seconds": p95_wait,
                    "marginal_utility_points": [],
                },
            }

    def get_boundary(self) -> dict:
        with self._lock:
            return dict(self._boundary)

    def get_passengers(self) -> list[dict]:
        with self._lock:
            return list(self._state.get("passengers", []))

    def get_surge(self) -> list[dict]:
        with self._lock:
            return list(self._surge_cells)

    def get_fare(self, passenger_id: str) -> dict | None:
        with self._lock:
            return next(
                (t for t in self._completed_passengers if t["passenger_id"] == passenger_id),
                None,
            )

    def enqueue_manual_passenger(
        self,
        *,
        pickup_lat: float,
        pickup_lng: float,
        dropoff_lat: float,
        dropoff_lng: float,
    ) -> str:
        with self._lock:
            passenger_id = f"upax_{self._manual_passenger_counter + 1}"
            self._manual_passenger_counter += 1
            self._manual_requests.append(ManualCreationRequest(
                kind="passenger",
                entity_id=passenger_id,
                pickup_lat=pickup_lat,
                pickup_lng=pickup_lng,
                dropoff_lat=dropoff_lat,
                dropoff_lng=dropoff_lng,
            ))
            return passenger_id

    def enqueue_manual_taxi(self, *, lat: float, lng: float) -> str:
        with self._lock:
            taxi_id = f"utaxi_{self._manual_taxi_counter + 1}"
            self._manual_taxi_counter += 1
            self._manual_requests.append(ManualCreationRequest(
                kind="taxi",
                entity_id=taxi_id,
                lat=lat,
                lng=lng,
            ))
            return taxi_id

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        sorted_values = sorted(values)
        if len(sorted_values) == 1:
            return sorted_values[0]
        rank = (len(sorted_values) - 1) * percentile / 100.0
        low = math.floor(rank)
        high = math.ceil(rank)
        if low == high:
            return sorted_values[int(rank)]
        weight = rank - low
        return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight

    @staticmethod
    def _is_taxi_id(vehicle_id: str) -> bool:
        return vehicle_id.startswith(("taxi_", "utaxi_"))

    @staticmethod
    def _is_manual_entity(*entity_ids: str | None) -> bool:
        return any(
            entity_id is not None and entity_id.startswith(("upax_", "utaxi_"))
            for entity_id in entity_ids
        )

    @staticmethod
    def _estimate_pickup_eta_seconds(route) -> int:
        travel_time = getattr(route, "travelTime", None)
        if travel_time is None:
            travel_time = getattr(route, "travel_time", None)
        if (
            travel_time is not None
            and math.isfinite(float(travel_time))
            and float(travel_time) > 0.0
        ):
            return int(round(float(travel_time)))
        length = float(getattr(route, "length", 0.0) or 0.0)
        return max(0, int(round(length / 8.0)))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        self._stop_event.set()
        # Unblock the broadcast loop if it's waiting on the queue
        if self._state_queue is not None:
            await self._state_queue.put(None)
        if self._broadcast_task is not None:
            try:
                await self._broadcast_task
            except (asyncio.CancelledError, Exception):
                pass
            self._broadcast_task = None
        if self._executor_task is not None:
            try:
                await self._executor_task
            except Exception:
                pass
            self._executor_task = None
        # Flush any pending call_soon_threadsafe callbacks from the run thread
        await asyncio.sleep(0)
        # Push run_end and drain DB writer before resetting state
        if self._run_id is not None and self._db_queue is not None:
            end_reason = (
                "duration" if self.status == SimStatus.FINISHED
                else "error" if self.status == SimStatus.IDLE
                else "manual_stop"
            )
            await self._db_queue.put({"type": "run_end", "run_id": self._run_id, "end_reason": end_reason})
        if self._db_queue is not None:
            await self._db_queue.put(None)
        if self._db_writer_task is not None:
            try:
                await self._db_writer_task
            except Exception:
                pass
            self._db_writer_task = None
        self.status = SimStatus.IDLE
        if self.connection_manager is not None:
            self.connection_manager.clear_boundary()
        self._reset_run_state()

    def _reset_run_state(self) -> None:
        """Clear per-run mutable state while preserving manager configuration."""
        self._close_prediction_demand_provider()
        with self._lock:
            self._state = {"vehicles": [], "passengers": [], "sim_time": 0.0}
            self._passengers = {}
            self._passenger_counter = 0
            self._active_trips = {}
            self._taxi_targets = {}
            self._taxi_states = {}
            self._taxi_dispatch_times = {}
            self._last_spawn_interval = -1
            self._last_surge_interval = -1
            self._last_progress_log_bucket = -1
            self._last_policy_wall_time = None
            self._last_policy_sim_time = None
            self._cached_predicted_demand = None
            self._forecasts_by_target_sim_time = {}
            self._forecast_issued_sim_time = {}
            self._evaluated_forecast_targets = set()
            self._last_rebalance_interval = -1
            self._routable_edges = []
            self._h3_to_edges = {}
            self._last_grid_supply = {}
            self._last_grid_demand = {}
            self._rebalance_edge_weights = None
            self._surge_cells = []
            self._surge_by_h3 = {}
            self._raw_surge_by_h3 = {}
            self._target_matching_rate_by_h3 = {}
            self._history_store = None
            self._surge_diagnostics = []
            self._completed_passengers = []
            self._completed_trip_count = 0
            self._trip_queue = []
            self._latlng = None
            self._bg_route_len = {}
            self._route_extend_retry_after = {}
            self._taxi_route_len = {}
            self._edge_weights = []
            self._routable_edges_set = set()
            self._run_id = None
            self._loop = None
            self._state_queue = None
            self._executor_task = None
            self._broadcast_task = None
            self._db_queue = None
            self._db_writer_task = None
            self._taxi_dispatch_ids = {}
            self._taxi_dispatch_surge = {}
            self._passenger_dispatch_buckets = {}
            self._taxi_last_dropoff_cells = {}
            self._taxi_appeared.clear()
            self._taxi_missing_since.clear()
            self._bg_appeared.clear()
            self._bg_missing_since.clear()
            self._event_log = []
            self._dispatch_pricing_cache = {}
            self._dispatch_taxi_round = 0
            self._find_route_calls_this_step = 0
            self._fast_dispatch_stats = (
                FastBenchDispatchStats()
                if self.experiment_config is not None and self.experiment_config.fast
                else None
            )
            self._taxi_dispatch_cooldown_until = {}
            self._pair_dispatch_cooldown_until = {}
            self._rejected_dispatch_pairs = set()
            self._taxi_previous_dropoff_times = {}
            self._manual_requests.clear()
            self._manual_passenger_counter = 0
            self._manual_taxi_counter = 0
            self._runtime_kpi = self._new_runtime_kpi(self._target_matching_rates)

    # ------------------------------------------------------------------
    # Async broadcast loop — runs in the event loop
    # ------------------------------------------------------------------

    async def _broadcast_loop(self) -> None:
        """Broadcast loop driven by simulation steps via _state_queue.

        _run_loop pushes a state dict after each traci.simulationStep(), or None
        as a sentinel to signal shutdown / finished.
        """
        while True:
            state = await self._state_queue.get()
            if state is None:
                # Sentinel: stop signal or simulation finished
                if (
                    self.status == SimStatus.FINISHED
                    and self.connection_manager is not None
                ):
                    await self.connection_manager.notify_finished()
                break
            if self.connection_manager is not None:
                await self.connection_manager.broadcast_state(state)
                for payload in state.get("fare_updates", []):
                    await self.connection_manager.broadcast_fare_update(
                        payload["passenger_id"],
                        payload["taxi_id"],
                        payload["fare"],
                        payload["expected_fare"],
                        payload["distance_m"],
                        payload["sim_time"],
                    )
                if state.get("surge") is not None:
                    await self.connection_manager.broadcast_surge(
                        state["surge"], state["sim_time"]
                    )

    # ------------------------------------------------------------------
    # Blocking loop — runs in ThreadPoolExecutor
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        try:
            # 일반 모드는 기존 상수와 broadcast queue를 사용하고,
            # 실험 모드는 SUMO step만 최대한 빠르게 진행한 뒤 메모리 이벤트만 남긴다.
            experiment = self.experiment_config is not None
            if experiment:
                step_length, real_step_sleep = resolve_experiment_pacing(self.experiment_config)
            else:
                step_length = STEP_LENGTH
                real_step_sleep = REAL_STEP_SLEEP
            sim_duration = self.experiment_config.sim_duration if experiment else self._runtime_duration
            broadcast_enabled = (not experiment) or bool(self.experiment_config.broadcast)
            cmd = [
                SUMO_BINARY,
                "-c",
                SUMO_CONFIG,
                "--no-step-log",
                "--no-warnings",
                "--step-length",
                str(step_length),
            ]
            traci.start(cmd, label="main")
            (min_x, min_y), (max_x, max_y) = traci.simulation.getNetBoundary()
            corners = [(min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y)]
            lats, lngs = zip(*[sumo_to_latlng(x, y) for x, y in corners])
            min_lat, max_lat = min(lats), max(lats)
            min_lng, max_lng = min(lngs), max(lngs)
            if self.connection_manager is not None:
                self.connection_manager.set_boundary(
                    min_x, min_y, max_x, max_y,
                    min_lat, min_lng, max_lat, max_lng,
                )
            self._latlng = make_sumolib_converter(SUMO_NET)
            all_routable = self._get_routable_edges()
            if SCC_FILE.exists():
                with open(SCC_FILE) as f:
                    scc_set = set(json.load(f))
                # 사전 계산된 SCC와 현재 routable의 교집합 사용 (안전망)
                self._routable_edges = [e for e in all_routable if e in scc_set]
                print(
                    f"[init] loaded pre-computed SCC: "
                    f"{len(self._routable_edges)} edges (of {len(all_routable)} routable)",
                    flush=True,
                )
            else:
                self._routable_edges = self._filter_to_largest_scc(all_routable)
                print(
                    f"[init] computed SCC at runtime: {len(all_routable)} → "
                    f"{len(self._routable_edges)} (run scripts/compute_scc.py to cache)",
                    flush=True,
                )
            self._routable_edges_set = set(self._routable_edges)
            self._initialize_prediction_components()
            self._edge_weights = self._compute_edge_weights()
            self._h3_to_edges = self._build_h3_edge_index()
            self._add_initial_vehicles()
            if experiment and self.experiment_config is not None and self.experiment_config.fast:
                print(
                    f"[bench] step_length={step_length}s n_bg={N_BACKGROUND_CARS} "
                    f"findRoute_cap/step={BENCH_MAX_FIND_ROUTE_PER_STEP} "
                    f"surge_recompute={self._surge_recompute_interval_sim_s()}s "
                    f"dispatch_empty_cap={_DISPATCH_MAX_EMPTY_PER_STEP_FAST}"
                    f"@{_DISPATCH_BACKLOG_WAIT_THRESHOLD}+waiting",
                    flush=True,
                )
            if (
                self._runtime_initial_passenger_count > 0
                and not experiment
                and self._passenger_source() == "random"
            ):
                for _ in range(self._runtime_initial_passenger_count):
                    self._create_passenger_random(0.0)

            for veh_id in traci.vehicle.getIDList():
                if self._is_taxi_id(veh_id):
                    traci.vehicle.subscribe(veh_id, _SUB_VARS)
                elif veh_id.startswith("bg_"):
                    traci.vehicle.subscribe(veh_id, _BG_SUB_VARS)

            if self._passenger_source() == "parquet":
                with open(TRIPS_FILE) as f:
                    self._trip_queue = sorted(json.load(f), key=lambda t: t["sim_time"])

            next_deadline = time.perf_counter()
            while not self._stop_event.is_set():
                if self._paused:
                    time.sleep(0.05)
                    next_deadline = time.perf_counter()  # pause 중 누적 방지
                    continue

                next_deadline += real_step_sleep
                traci.simulationStep()
                sim_time = traci.simulation.getTime()
                self._maybe_log_sim_progress(sim_time, sim_duration)

                for veh_id in traci.simulation.getDepartedIDList():
                    if self._is_taxi_id(veh_id):
                        traci.vehicle.subscribe(veh_id, _SUB_VARS)

                sub_results = traci.vehicle.getAllSubscriptionResults()

                # Track which vehicles have appeared at least once, and clear stale missing records
                for veh_id in sub_results:
                    if self._is_taxi_id(veh_id):
                        self._taxi_appeared.add(veh_id)
                        self._taxi_missing_since.pop(veh_id, None)
                    elif veh_id.startswith("bg_"):
                        self._bg_appeared.add(veh_id)
                        self._bg_missing_since.pop(veh_id, None)

                # Respawn empty taxis that have definitively left the network.
                # Only fires for taxis that (a) previously appeared, (b) are now absent,
                # (c) have been missing for > _RESPAWN_THRESHOLD sim seconds.
                _RESPAWN_THRESHOLD = 60.0
                for veh_id in list(self._taxi_route_len.keys()):
                    if veh_id in sub_results:
                        continue
                    if veh_id not in self._taxi_appeared:
                        continue  # never appeared yet (pending initial departure)
                    if self._taxi_states.get(veh_id, "empty") != "empty":
                        continue  # non-empty handled elsewhere
                    missing_since = self._taxi_missing_since.get(veh_id)
                    if missing_since is None:
                        self._taxi_missing_since[veh_id] = sim_time
                    elif sim_time - missing_since >= _RESPAWN_THRESHOLD:
                        route_edges = self._random_route(self._routable_edges)
                        if route_edges:
                            route_id = f"respawn_{veh_id}_{int(sim_time)}"
                            try:
                                traci.route.add(route_id, route_edges)
                                traci.vehicle.add(
                                    vehID=veh_id, routeID=route_id, typeID="taxi",
                                    depart=sim_time, departLane="best",
                                    departPos="random_free", departSpeed="max",
                                )
                                traci.vehicle.subscribe(veh_id, _SUB_VARS)
                                self._taxi_route_len[veh_id] = len(route_edges)
                                self._taxi_missing_since.pop(veh_id, None)
                                print(f"[respawn] {veh_id} at sim_time={sim_time:.0f}", flush=True)
                            except traci.exceptions.TraCIException as e:
                                print(f"[respawn] FAILED {veh_id}: {e}", flush=True)

                # Respawn bg cars that left the network. bg는 트립 로직이 없어 한 번 arrival되면
                # 영구 손실되므로(running 단조 감소의 주원인), 누락분을 재투입해 차량 수를 유지한다.
                if N_BACKGROUND_CARS > 0:
                    for veh_id in list(self._bg_route_len.keys()):
                        if veh_id in sub_results:
                            continue
                        if veh_id not in self._bg_appeared:
                            continue  # 아직 최초 출발 전 (insertion 대기)
                        missing_since = self._bg_missing_since.get(veh_id)
                        if missing_since is None:
                            self._bg_missing_since[veh_id] = sim_time
                        elif sim_time - missing_since >= _RESPAWN_THRESHOLD:
                            route_edges = self._random_route(self._routable_edges)
                            if route_edges:
                                route_id = f"respawn_{veh_id}_{int(sim_time)}"
                                try:
                                    traci.route.add(route_id, route_edges)
                                    traci.vehicle.add(
                                        vehID=veh_id, routeID=route_id, typeID="DEFAULT_VEHTYPE",
                                        depart=sim_time, departLane="best",
                                        departPos="random_free", departSpeed="max",
                                    )
                                    traci.vehicle.subscribe(veh_id, _BG_SUB_VARS)
                                    self._bg_route_len[veh_id] = len(route_edges)
                                    self._bg_missing_since.pop(veh_id, None)
                                except traci.exceptions.TraCIException:
                                    pass

                self._extend_vehicle_routes(sub_results)
                self._find_route_calls_this_step = 0

                self._process_manual_requests(sim_time)
                self._spawn_passengers(sim_time)
                surge_payload = None
                bench_no_broadcast = (
                    experiment
                    and self.experiment_config is not None
                    and self.experiment_config.fast
                    and not broadcast_enabled
                )
                with self._lock:
                    if bench_no_broadcast:
                        grid_supply, grid_demand = self._capture_grid_only(sub_results)
                    else:
                        _, grid_supply, grid_demand = self._capture_state(sim_time, sub_results)
                    self._evaluate_forecasts_at_horizon(sim_time, grid_demand)
                    if self._should_recompute_surge_grid(sim_time):
                        self._build_surge_cells(grid_supply, grid_demand, sim_time)
                    if self._surge_cells:
                        surge_payload = self._surge_cells
                    if self._is_rebalance_policy():
                        self._maybe_rebalance_empty_taxis(sim_time, sub_results)

                fare_updates = self._update_taxi_states(sim_time, sub_results)
                fare_updates += self._accumulate_fares(sim_time, sub_results)

                with self._lock:
                    if bench_no_broadcast:
                        state = {"vehicles": [], "passengers": [], "sim_time": sim_time}
                    else:
                        state, _, _ = self._capture_state(sim_time, sub_results)
                    self._state = state
                    state["fare_updates"] = fare_updates
                    for fu in fare_updates:
                        self._completed_passengers.append(fu)
                        self._completed_trip_count += 1
                        self._record_trip_kpi(
                            fare=fu.get("fare", 0),
                            meter_fare=fu.get("meter_fare", fu.get("fare", 0)),
                        )
                        if len(self._completed_passengers) > MAX_COMPLETED_PASSENGERS:
                            self._completed_passengers.pop(0)
                    state["surge"] = surge_payload

                # Push state immediately after each step; broadcast loop sends it.
                if broadcast_enabled and self._loop is not None and self._state_queue is not None:
                    self._loop.call_soon_threadsafe(self._state_queue.put_nowait, state)

                if sim_time >= sim_duration:
                    # Force-complete all trips still in progress so their fares are recorded.
                    with self._lock:
                        for taxi_id, accum in list(self._active_trips.items()):
                            pid = self._taxi_targets.pop(taxi_id, None)
                            p = self._passengers.pop(pid, None) if pid else None
                            if p:
                                dropoff_h3 = p.h3_dropoff or get_cell(p.dropoff_lat, p.dropoff_lng)
                                meter_fare = calculate_meter_fare(accum)
                                fare = calculate_fare(accum)
                                self._completed_passengers.append({
                                    "passenger_id": pid,
                                    "taxi_id": taxi_id,
                                    "meter_fare": meter_fare,
                                    "fare": fare,
                                    "surge": accum.surge,
                                    "expected_fare": p.expected_fare,
                                    "distance_m": accum.distance_m,
                                    "sim_time": sim_time,
                                })
                                self._completed_trip_count += 1
                                self._record_trip_kpi(fare=fare, meter_fare=meter_fare)
                                if len(self._completed_passengers) > MAX_COMPLETED_PASSENGERS:
                                    self._completed_passengers.pop(0)
                                self._push_db_event({
                                    "type": "trip",
                                    "run_id": self._run_id,
                                    "passenger_id": pid,
                                    "taxi_id": taxi_id,
                                    "dispatch_id": accum.dispatch_id,
                                    "dispatch_sim_time": accum.dispatch_sim_time,
                                    "pickup_sim_time": accum.pickup_sim_time,
                                    "dropoff_sim_time": sim_time,
                                    "distance_m": accum.distance_m,
                                    "low_speed_seconds": accum.low_speed_seconds,
                                    "meter_fare": meter_fare,
                                    "fare": fare,
                                    "surge": accum.surge,
                                    "expected_fare": p.expected_fare,
                                    "completion": "forced_at_end",
                                })
                                self._record_history_dropoff(sim_time, dropoff_h3)
                            self._taxi_dispatch_ids.pop(taxi_id, None)
                            self._taxi_dispatch_surge.pop(taxi_id, None)
                        self._active_trips.clear()
                    self.status = SimStatus.FINISHED
                    if broadcast_enabled and self._loop is not None and self._state_queue is not None:
                        self._loop.call_soon_threadsafe(self._state_queue.put_nowait, None)
                    break

                remaining = next_deadline - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)

        except traci.exceptions.FatalTraCIError:
            # SUMO closed the connection (e.g. reached configured end time)
            self.status = SimStatus.FINISHED
        except Exception:
            self.status = SimStatus.IDLE
            raise
        finally:
            try:
                traci.close()
            except Exception:
                pass

    def _push_db_event(self, event: dict) -> None:
        # 실험 모드는 DB writer를 띄우지 않으므로 기존 DB 이벤트 중 KPI에 필요한 완료 이벤트만 메모리에 투영한다.
        if self.experiment_config is not None:
            if event.get("type") == "trip":
                self._emit_event("trip_completed", {
                    "sim_time": event.get("dropoff_sim_time"),
                    "passenger_id": event.get("passenger_id"),
                    "taxi_id": event.get("taxi_id"),
                    "dispatch_id": event.get("dispatch_id"),
                    "dispatch_sim_time": event.get("dispatch_sim_time"),
                    "pickup_sim_time": event.get("pickup_sim_time"),
                    "dropoff_sim_time": event.get("dropoff_sim_time"),
                    "meter_fare_usd": (event.get("meter_fare") or 0) / 100.0,
                    "fare_usd": (event.get("fare") or 0) / 100.0,
                    "surge": event.get("surge", 1.0),
                    "completion": event.get("completion"),
                })
                if event.get("completion") != "forced_at_end":
                    taxi_id = event.get("taxi_id")
                    dropoff_time = event.get("dropoff_sim_time")
                    if taxi_id is not None and dropoff_time is not None:
                        self._taxi_previous_dropoff_times[taxi_id] = float(dropoff_time)
        if self._run_id is None or self._db_queue is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._db_queue.put_nowait, event)

    def _emit_event(self, event_type: str, payload: dict) -> None:
        if self.experiment_config is None:
            return
        # Fast bench: O(1) counters instead of millions of dispatch_decision dicts.
        if self.experiment_config.fast and event_type in (
            "dispatch_attempted",
            "dispatch_decision",
        ):
            return
        self._event_log.append({"type": event_type, **payload})

    def _record_fast_dispatch_stats(self, decision_payload: dict, *, accepted: bool) -> None:
        stats = self._fast_dispatch_stats
        if stats is None:
            return
        stats.decision_count += 1
        pid = decision_payload.get("passenger_id")
        if pid:
            pid_s = str(pid)
            stats.offered_passenger_ids.add(pid_s)
            stats.decisions_per_passenger[pid_s] = (
                stats.decisions_per_passenger.get(pid_s, 0) + 1
            )
        if accepted:
            stats.accept_count += 1
            if pid:
                stats.matched_passenger_ids.add(str(pid))
        mre = decision_payload.get("matching_rate_error")
        if mre is not None:
            mre_f = float(mre)
            stats.matching_rate_error_sum += mre_f
            stats.matching_rate_error_abs_sum += abs(mre_f)
            stats.matching_rate_error_count += 1
        if decision_payload.get("surge_clamped"):
            stats.surge_clamped_count += 1
        fs = decision_payload.get("final_surge")
        if fs is not None:
            stats.final_surge_sum += float(fs)
            stats.final_surge_count += 1
        ff = decision_payload.get("final_fare_usd")
        if ff is not None:
            stats.final_fare_sum += float(ff)
            stats.final_fare_count += 1
        rf = decision_payload.get("required_fare_usd")
        if rf is not None:
            stats.required_fare_sum += float(rf)
            stats.required_fare_count += 1
        pa = decision_payload.get("p_actual")
        if pa is not None:
            stats.p_actual_sum += float(pa)
            stats.p_actual_count += 1

    def _emit_fast_dispatch_summary(self) -> None:
        stats = self._fast_dispatch_stats
        if stats is None or self.experiment_config is None:
            return
        bucket_payload = {
            bucket.bucket: {
                "request_count": bucket.request_count,
                "matched_count": bucket.matched_count,
                "p_actual_sum": bucket.p_actual_sum,
                "p_actual_count": bucket.p_actual_count,
                "target_rate": bucket.target_rate,
            }
            for bucket in self._runtime_kpi.buckets.values()
        }
        empty_waits = list(self._runtime_kpi.empty_wait_seconds or [])
        self._event_log.append({
            "type": "dispatch_kpi_fast_summary",
            "decision_count": stats.decision_count,
            "accept_count": stats.accept_count,
            "offered_passenger_count": len(stats.offered_passenger_ids),
            "matched_passenger_count": len(stats.matched_passenger_ids),
            "empty_wait_seconds": empty_waits,
            "decisions_per_passenger": dict(stats.decisions_per_passenger),
            "matching_rate_error_sum": stats.matching_rate_error_sum,
            "matching_rate_error_abs_sum": stats.matching_rate_error_abs_sum,
            "matching_rate_error_count": stats.matching_rate_error_count,
            "surge_clamped_count": stats.surge_clamped_count,
            "final_surge_sum": stats.final_surge_sum,
            "final_surge_count": stats.final_surge_count,
            "final_fare_sum": stats.final_fare_sum,
            "final_fare_count": stats.final_fare_count,
            "required_fare_sum": stats.required_fare_sum,
            "required_fare_count": stats.required_fare_count,
            "p_actual_sum": stats.p_actual_sum,
            "p_actual_count": stats.p_actual_count,
            "buckets": bucket_payload,
        })

    def _fast_dispatch_empty_cap(self, waiting_count: int) -> int | None:
        """Limit empty taxis that run dispatch per step when backlog grows (fast bench only)."""
        if (
            self.experiment_config is None
            or not self.experiment_config.fast
            or waiting_count < _DISPATCH_BACKLOG_WAIT_THRESHOLD
        ):
            return None
        return _DISPATCH_MAX_EMPTY_PER_STEP_FAST

    def _emit_experiment_diagnostics(self, prediction_provider=None) -> None:
        if self.experiment_config is None:
            return
        diagnostics: dict[str, float | int | str] = {}
        provider = prediction_provider or self._prediction_demand_provider
        if provider is not None:
            diagnostics.update(provider.diagnostics())
        if self._history_store is not None:
            diagnostics.update(self._history_store.diagnostics())
        if diagnostics:
            self._emit_event("diagnostics", diagnostics)
        for row in self._surge_diagnostics:
            self._emit_event("surge_diagnostic", row)

    def _close_prediction_demand_provider(self):
        provider = self._prediction_demand_provider
        if provider is None:
            return None
        self._prediction_demand_provider = None
        provider.close()
        return provider

    def _record_history_spawn(self, sim_time: float, h3_cell: str | None) -> None:
        if self._history_store is None:
            return
        self._history_store.record_spawn(
            SIM_BASE_DATETIME + timedelta(seconds=sim_time),
            h3_cell,
        )

    def _record_history_dropoff(self, sim_time: float, h3_cell: str | None) -> None:
        if self._history_store is None:
            return
        self._history_store.record_dropoff(
            SIM_BASE_DATETIME + timedelta(seconds=sim_time),
            h3_cell,
        )

    @staticmethod
    def _provider_prediction_mode(mode: str) -> str:
        return "sync" if mode == "none" else mode

    @staticmethod
    def _raw_surge_bucket(raw_surge: float) -> str:
        for bucket, lower, upper, _ in RAW_SURGE_BUCKETS:
            if raw_surge >= lower and (upper is None or raw_surge < upper):
                return bucket
        return RAW_SURGE_BUCKETS[-1][0]

    def _target_matching_rate(self, raw_surge: float) -> float:
        return self._target_matching_rates[self._raw_surge_bucket(raw_surge)]

    def _runtime_elasticity(self) -> float:
        return abs(float(self._pricing_policy.get("epsilon", DEFAULT_PRICING_POLICY["epsilon"])))

    def _runtime_alpha_sensitivity(self) -> float:
        return float(self._pricing_policy.get(
            "alpha_sensitivity",
            DEFAULT_PRICING_POLICY["alpha_sensitivity"],
        ))

    def _passenger_source(self) -> str:
        if self._runtime_passenger_source_overridden:
            return self._runtime_passenger_source
        return PASSENGER_SOURCE

    def _record_dispatch_kpi(self, decision_payload: dict, *, accepted: bool) -> None:
        self._record_fast_dispatch_stats(decision_payload, accepted=accepted)
        raw_surge = float(decision_payload.get("raw_surge", 1.0) or 1.0)
        bucket_key = self._raw_surge_bucket(raw_surge)
        bucket = self._runtime_kpi.buckets[bucket_key]
        bucket.request_count += 1
        p_actual = decision_payload.get("p_actual")
        if p_actual is not None:
            bucket.p_actual_sum += float(p_actual)
            bucket.p_actual_count += 1
        if accepted:
            bucket.matched_count += 1
            passenger_id = decision_payload.get("passenger_id")
            if passenger_id:
                self._passenger_dispatch_buckets[str(passenger_id)] = bucket_key
        empty_wait = decision_payload.get("empty_wait_time_s")
        if empty_wait is not None:
            self._runtime_kpi.empty_wait_seconds.append(float(empty_wait))

    def _record_passenger_boarded_kpi(self, passenger: Passenger, sim_time: float) -> None:
        bucket_key = self._passenger_dispatch_buckets.get(passenger.id)
        if not bucket_key:
            return
        self._runtime_kpi.buckets[bucket_key].wait_seconds.append(
            max(0.0, sim_time - passenger.spawn_time)
        )

    def _record_trip_kpi(self, *, fare: int, meter_fare: int = 0) -> None:
        self._runtime_kpi.completed_trip_count += 1
        self._runtime_kpi.total_fare_cents += int(fare)
        self._runtime_kpi.total_meter_fare_cents += int(meter_fare)

    def _schedule_ws_event(self, method_name: str, *args) -> None:
        if self.connection_manager is None or self._loop is None:
            return
        method = getattr(self.connection_manager, method_name, None)
        if method is None:
            return
        self._loop.call_soon_threadsafe(asyncio.create_task, method(*args))

    def _initialize_prediction_components(self) -> None:
        self._close_prediction_demand_provider()
        config = self.experiment_config
        if config is None:
            self._history_store = None
            self._surge_diagnostics = []
            return

        model_h3_cells = load_model_h3_cells()

        self._history_store = DemandHistoryStore(model_h3_cells=model_h3_cells)
        self._surge_diagnostics = []

        if config.demand_source != "predicted":
            return
        if config.weather_source != "static":
            raise ValueError(f"unsupported weather source: {config.weather_source}")

        self._prediction_demand_provider = PredictionDemandProvider(
            prediction_url=config.prediction_url,
            model_h3_cells=model_h3_cells,
            history_store=self._history_store,
            weather_provider=StaticWeatherProvider(),
            prediction_horizon_min=config.prediction_horizon_min,
            fallback_policy=config.prediction_fallback_policy,
        )

    def _adjust_spawn_count_for_elasticity(self, raw_count: int, h3_cell: str | None, sim_time: float) -> int:
        passenger_elasticity = (
            self.experiment_config.passenger_elasticity
            if self.experiment_config is not None
            else 0.0
        )
        if passenger_elasticity == 0.0 or raw_count <= 0:
            adjusted = raw_count
        else:
            surge = self._surge_by_h3.get(h3_cell or "", 1.0)
            adjusted = int(round(raw_count * (surge ** passenger_elasticity)))
            adjusted = max(0, min(raw_count, adjusted))
        self._emit_event("passenger_elasticity", {
            "sim_time": sim_time,
            "raw_spawn_candidate_count": raw_count,
            "elasticity_removed_count": raw_count - adjusted,
            "actual_spawned_passengers": adjusted,
        })
        return adjusted

    def _spawn_passengers(self, sim_time: float) -> None:
        if self._passenger_source() == "parquet":
            while self._trip_queue and self._trip_queue[0]["sim_time"] <= sim_time:
                trip = self._trip_queue.pop(0)
                self._create_passenger_from_trip(trip, sim_time)
        else:
            interval = int(sim_time / PASSENGER_SPAWN_INTERVAL)
            if interval <= self._last_spawn_interval:
                return
            self._last_spawn_interval = interval
            n = _poisson_sample(self._passenger_lambda())
            n = self._adjust_spawn_count_for_elasticity(n, None, sim_time)
            for _ in range(n):
                self._create_passenger_random(sim_time)

    def _get_edge_midpoint(self, edge_id: str) -> tuple[float, float] | None:
        traci_mod = _traci_module()
        try:
            shape = traci_mod.lane.getShape(f"{edge_id}_0")
        except traci_mod.exceptions.TraCIException:
            n = traci_mod.edge.getLaneNumber(edge_id)
            shape = None
            for i in range(1, n):
                try:
                    shape = traci_mod.lane.getShape(f"{edge_id}_{i}")
                    break
                except traci_mod.exceptions.TraCIException:
                    continue
            if not shape:
                return None
        mid = len(shape) // 2
        return shape[mid]

    def _create_passenger_random(self, sim_time: float) -> None:
        pickup_edge = _random.choice(self._routable_edges)
        dropoff_edge = _random.choice(self._routable_edges)
        if pickup_edge == dropoff_edge:
            return
        traci_mod = _traci_module()
        try:
            route = traci_mod.simulation.findRoute(pickup_edge, dropoff_edge)
        except traci_mod.exceptions.TraCIException:
            return
        if not route.edges:
            return
        pt = self._get_edge_midpoint(pickup_edge)
        if pt is None:
            return
        x, y = pt
        dt = self._get_edge_midpoint(dropoff_edge)
        if dt is None:
            return
        dx, dy = dt
        lat, lng = self._latlng(x, y)
        dlat, dlng = self._latlng(dx, dy)
        pid = f"p_{self._passenger_counter}"
        self._passenger_counter += 1
        h3 = get_cell(lat, lng)
        h3_dropoff = get_cell(dlat, dlng)
        expected_fare = estimate_fare(route.length)
        self._passengers[pid] = Passenger(
            id=pid, x=x, y=y, lat=lat, lng=lng,
            pickup_edge=pickup_edge, dropoff_edge=dropoff_edge,
            dropoff_x=dx, dropoff_y=dy, dropoff_lat=dlat, dropoff_lng=dlng,
            expected_distance_m=route.length,
            expected_fare=expected_fare,
            spawn_time=sim_time,
            state="waiting",
            h3_pickup=h3,
            h3_dropoff=h3_dropoff,
        )
        self._push_db_event({
            "type": "passenger",
            "run_id": self._run_id,
            "passenger_id": pid,
            "spawn_sim_time": sim_time,
            "pickup_edge": pickup_edge,
            "dropoff_edge": dropoff_edge,
            "pickup_lat": lat,
            "pickup_lng": lng,
            "dropoff_lat": dlat,
            "dropoff_lng": dlng,
            "expected_distance_m": route.length,
            "expected_fare": expected_fare,
            "h3_pickup": h3,
            "source": "random",
        })
        self._record_history_spawn(sim_time, h3)
        # 승객 생성 수와 unique matched passenger denominator를 계산하기 위한 실험 이벤트.
        self._emit_event("passenger_spawned", {
            "sim_time": sim_time,
            "passenger_id": pid,
            "pickup_h3": h3,
            "dropoff_h3": h3_dropoff,
            "expected_fare_usd": expected_fare / 100.0,
            "expected_distance_m": route.length,
        })

    def _create_passenger_from_trip(self, trip: dict, sim_time: float) -> None:
        pickup_edge = trip["pickup_edge"]
        dropoff_edge = trip["dropoff_edge"]
        # SCC 외부 엣지면 스킵: 픽업이 외부면 배차 불가, 하차가 외부면 택시 갇힘
        if pickup_edge not in self._routable_edges_set or \
           dropoff_edge not in self._routable_edges_set:
            return
        traci_mod = _traci_module()
        try:
            route = traci_mod.simulation.findRoute(pickup_edge, dropoff_edge)
        except traci_mod.exceptions.TraCIException:
            return
        if not route.edges:
            return
        pt = self._get_edge_midpoint(pickup_edge)
        if pt is None:
            return
        x, y = pt
        dt = self._get_edge_midpoint(dropoff_edge)
        if dt is None:
            return
        dx, dy = dt
        lat, lng = self._latlng(x, y)
        dlat, dlng = self._latlng(dx, dy)
        pid = f"p_{self._passenger_counter}"
        self._passenger_counter += 1
        h3 = trip["h3_pickup"]
        h3_dropoff = get_cell(dlat, dlng)
        expected_fare = estimate_fare(route.length)
        self._passengers[pid] = Passenger(
            id=pid, x=x, y=y, lat=lat, lng=lng,
            pickup_edge=pickup_edge, dropoff_edge=dropoff_edge,
            dropoff_x=dx, dropoff_y=dy, dropoff_lat=dlat, dropoff_lng=dlng,
            expected_distance_m=route.length,
            expected_fare=expected_fare,
            spawn_time=sim_time,
            state="waiting",
            h3_pickup=h3,
            h3_dropoff=h3_dropoff,
        )
        self._push_db_event({
            "type": "passenger",
            "run_id": self._run_id,
            "passenger_id": pid,
            "spawn_sim_time": sim_time,
            "pickup_edge": pickup_edge,
            "dropoff_edge": dropoff_edge,
            "pickup_lat": lat,
            "pickup_lng": lng,
            "dropoff_lat": dlat,
            "dropoff_lng": dlng,
            "expected_distance_m": route.length,
            "expected_fare": expected_fare,
            "h3_pickup": h3,
            "source": "parquet",
        })
        self._record_history_spawn(sim_time, h3)
        # parquet replay도 random spawn과 동일한 이벤트 스키마로 남겨 집계 코드를 공유한다.
        self._emit_event("passenger_spawned", {
            "sim_time": sim_time,
            "passenger_id": pid,
            "pickup_h3": h3,
            "dropoff_h3": h3_dropoff,
            "expected_fare_usd": expected_fare / 100.0,
            "expected_distance_m": route.length,
        })

    def _process_manual_requests(self, sim_time: float) -> None:
        with self._lock:
            requests = list(self._manual_requests)
            self._manual_requests.clear()

        for request in requests:
            if request.kind == "passenger":
                self._process_manual_passenger_request(request, sim_time)
            elif request.kind == "taxi":
                self._process_manual_taxi_request(request, sim_time)

    def _snap_latlng_to_routable_edge(
        self,
        lat: float,
        lng: float,
    ) -> tuple[str, float, float] | None:
        traci_mod = _traci_module()
        try:
            x, y = traci_mod.simulation.convertGeo(lng, lat, fromGeo=True)
            road = traci_mod.simulation.convertRoad(x, y, isGeo=False)
        except traci_mod.exceptions.TraCIException:
            return None
        edge_id = road[0] if isinstance(road, tuple) else str(road)
        if not edge_id or edge_id.startswith(":") or edge_id not in self._routable_edges_set:
            return None
        return edge_id, x, y

    def _process_manual_passenger_request(
        self,
        request: ManualCreationRequest,
        sim_time: float,
    ) -> None:
        if (
            request.pickup_lat is None or request.pickup_lng is None
            or request.dropoff_lat is None or request.dropoff_lng is None
        ):
            self._schedule_ws_event(
                "broadcast_passenger_creation_failed",
                request.entity_id,
                "invalid_request",
            )
            return

        pickup = self._snap_latlng_to_routable_edge(request.pickup_lat, request.pickup_lng)
        dropoff = self._snap_latlng_to_routable_edge(request.dropoff_lat, request.dropoff_lng)
        if pickup is None or dropoff is None:
            self._schedule_ws_event(
                "broadcast_passenger_creation_failed",
                request.entity_id,
                "out_of_network",
            )
            return

        pickup_edge, x, y = pickup
        dropoff_edge, dx, dy = dropoff
        traci_mod = _traci_module()
        try:
            route = traci_mod.simulation.findRoute(pickup_edge, dropoff_edge)
        except traci_mod.exceptions.TraCIException:
            self._schedule_ws_event(
                "broadcast_passenger_creation_failed",
                request.entity_id,
                "no_route_found",
            )
            return
        if not route.edges:
            self._schedule_ws_event(
                "broadcast_passenger_creation_failed",
                request.entity_id,
                "no_route_found",
            )
            return

        lat, lng = self._latlng(x, y)
        dlat, dlng = self._latlng(dx, dy)
        h3 = get_cell(lat, lng)
        h3_dropoff = get_cell(dlat, dlng)
        expected_fare = estimate_fare(route.length)
        self._passengers[request.entity_id] = Passenger(
            id=request.entity_id, x=x, y=y, lat=lat, lng=lng,
            pickup_edge=pickup_edge, dropoff_edge=dropoff_edge,
            dropoff_x=dx, dropoff_y=dy, dropoff_lat=dlat, dropoff_lng=dlng,
            expected_distance_m=route.length,
            expected_fare=expected_fare,
            spawn_time=sim_time,
            state="waiting",
            h3_pickup=h3,
            h3_dropoff=h3_dropoff,
        )
        self._push_db_event({
            "type": "passenger",
            "run_id": self._run_id,
            "passenger_id": request.entity_id,
            "spawn_sim_time": sim_time,
            "pickup_edge": pickup_edge,
            "dropoff_edge": dropoff_edge,
            "pickup_lat": lat,
            "pickup_lng": lng,
            "dropoff_lat": dlat,
            "dropoff_lng": dlng,
            "expected_distance_m": route.length,
            "expected_fare": expected_fare,
            "h3_pickup": h3,
            "source": "manual",
        })
        self._record_history_spawn(sim_time, h3)
        self._schedule_ws_event(
            "broadcast_passenger_created",
            request.entity_id,
            lat,
            lng,
            expected_fare,
            int(round(route.length)),
        )

    def _process_manual_taxi_request(
        self,
        request: ManualCreationRequest,
        sim_time: float,
    ) -> None:
        if request.lat is None or request.lng is None:
            return
        snapped = self._snap_latlng_to_routable_edge(request.lat, request.lng)
        if snapped is None:
            return
        edge_id, x, y = snapped
        route_edges = self._random_route_from(edge_id)
        if not route_edges:
            route_edges = [edge_id]
        route_id = f"manual_taxi_route_{request.entity_id}_{int(sim_time * 1000)}"
        traci_mod = _traci_module()
        try:
            traci_mod.route.add(route_id, route_edges)
            traci_mod.vehicle.add(
                vehID=request.entity_id,
                routeID=route_id,
                typeID="taxi",
                depart=sim_time,
                departLane="best",
                departPos="random_free",
                departSpeed="max",
            )
            traci_mod.vehicle.subscribe(request.entity_id, _SUB_VARS)
        except traci_mod.exceptions.TraCIException:
            return
        self._taxi_states[request.entity_id] = "empty"
        self._taxi_route_len[request.entity_id] = len(route_edges)
        lat, lng = self._latlng(x, y)
        self._taxi_last_dropoff_cells[request.entity_id] = get_cell(lat, lng)
        self._schedule_ws_event("broadcast_taxi_created", request.entity_id, lat, lng)

    def _candidate_driver_average_features(
        self,
        *,
        candidate: Passenger,
        sim_time: float,
        sub_results: dict,
        current_veh_id: str,
        current_route,
    ) -> tuple[float, float, float, int] | None:
        if not candidate.h3_dropoff:
            return None
        call_dt = SIM_BASE_DATETIME + timedelta(seconds=sim_time)
        trip_miles = candidate.expected_distance_m / 1609.344
        empty_taxis = []
        for taxi_id, vals in sub_results.items():
            if not self._is_taxi_id(taxi_id):
                continue
            if self._taxi_states.get(taxi_id, "empty") != "empty":
                continue
            x, y = vals[tc.VAR_POSITION]
            empty_taxis.append(((candidate.x - x) ** 2 + (candidate.y - y) ** 2, taxi_id, vals))

        driver_features = []
        for _, taxi_id, vals in sorted(empty_taxis)[: self._dispatch_max_candidates()]:
            x, y = vals[tc.VAR_POSITION]
            road_id = vals.get(tc.VAR_ROAD_ID, "")
            route = current_route if taxi_id == current_veh_id else None
            if route is None:
                if not self._is_valid_road_edge(road_id):
                    continue
                try:
                    route = self._sim_find_route(road_id, candidate.pickup_edge)
                except traci.exceptions.TraCIException:
                    continue
            if not route.edges:
                continue
            D_pu_miles = route.length / 1609.344
            lat, lng = self._latlng(x, y)
            last_cell = self._taxi_last_dropoff_cells.get(taxi_id) or get_cell(lat, lng)
            features = _acceptance_features(
                last_dropoff_cell=last_cell,
                dropoff_cell=candidate.h3_dropoff,
                call_datetime=call_dt,
                D_pu=D_pu_miles,
                trip_distance=trip_miles,
                pickup_cell=candidate.h3_pickup,
            )
            driver_features.append((features.dV_without_fare, D_pu_miles, features.t_pu))

        if not driver_features:
            return None
        count = len(driver_features)
        return (
            sum(v[0] for v in driver_features) / count,
            sum(v[1] for v in driver_features) / count,
            sum(v[2] for v in driver_features) / count,
            count,
        )

    def _dispatch_pricing(
        self,
        *,
        candidate: Passenger,
        sim_time: float,
        sub_results: dict,
        current_veh_id: str,
        current_route,
    ) -> dict:
        base_fare_usd = candidate.expected_fare / 100.0
        raw_surge = self._raw_surge_by_h3.get(
            candidate.h3_pickup or "",
            self._surge_by_h3.get(candidate.h3_pickup or "", 1.0),
        )
        target_matching_rate = self._target_matching_rate(raw_surge)
        required_fare_usd = None
        calculated_surge = raw_surge
        pricing_driver_count = None

        # 실험 모드는 PU 학습 역산(required_fare) 경로를 항상 사용한다. rebalance는 공급 이동만 추가.
        use_inverse = self.experiment_config is not None and base_fare_usd > 0
        if use_inverse:
            cached = self._dispatch_pricing_cache.get(candidate.id)
            if cached is not None:
                return dict(cached)
            averages = self._candidate_driver_average_features(
                candidate=candidate,
                sim_time=sim_time,
                sub_results=sub_results,
                current_veh_id=current_veh_id,
                current_route=current_route,
            )
            if averages is not None:
                avg_dv, avg_dpu, avg_tpu, pricing_driver_count = averages
                required_fare_usd = _required_fare_for_target_features(
                    target_p=target_matching_rate,
                    dV_without_fare=avg_dv,
                    D_pu=avg_dpu,
                    T_pu=avg_tpu,
                    beta_f=self.experiment_config.beta_f,
                    alpha_sensitivity=self.experiment_config.alpha_sensitivity,
                )
                calculated_surge = required_fare_usd / base_fare_usd
            final_surge = apply_surge_limits(
                calculated_surge,
                min_active_surge=float(self._pricing_policy.get("surge_min", 1.2)),
                max_surge=float(self._pricing_policy.get("surge_max", 4.9)),
            )
        else:
            final_surge = self._surge_by_h3.get(candidate.h3_pickup or "", 1.0)
            calculated_surge = final_surge

        final_fare_usd = base_fare_usd * final_surge
        result = {
            "base_fare_usd": base_fare_usd,
            "raw_surge": raw_surge,
            "target_matching_rate": target_matching_rate,
            "required_fare_usd": required_fare_usd,
            "calculated_surge": calculated_surge,
            "final_surge": final_surge,
            "final_fare_usd": final_fare_usd,
            "surge_clamped": abs(calculated_surge - final_surge) > 1e-6,
            "pricing_driver_count": pricing_driver_count,
        }
        if use_inverse:
            self._dispatch_pricing_cache[candidate.id] = result
        return result

    @staticmethod
    def _is_valid_road_edge(edge_id: str) -> bool:
        """TraCI findRoute/setRoute require a non-empty, non-internal edge id."""
        return bool(edge_id) and not edge_id.startswith(":")

    def _prune_dispatch_cooldowns(self, sim_time: float) -> None:
        self._taxi_dispatch_cooldown_until = {
            vid: until
            for vid, until in self._taxi_dispatch_cooldown_until.items()
            if until > sim_time
        }
        self._pair_dispatch_cooldown_until = {
            key: until
            for key, until in self._pair_dispatch_cooldown_until.items()
            if until > sim_time
        }

    def _taxi_on_dispatch_cooldown(self, veh_id: str, sim_time: float) -> bool:
        return sim_time < self._taxi_dispatch_cooldown_until.get(veh_id, 0.0)

    def _pair_on_dispatch_cooldown(
        self, veh_id: str, passenger_id: str, sim_time: float
    ) -> bool:
        return sim_time < self._pair_dispatch_cooldown_until.get((veh_id, passenger_id), 0.0)

    def _set_dispatch_cooldowns(
        self, veh_id: str, passenger_id: str, sim_time: float
    ) -> None:
        self._taxi_dispatch_cooldown_until[veh_id] = sim_time + TAXI_DISPATCH_COOLDOWN_S
        self._pair_dispatch_cooldown_until[(veh_id, passenger_id)] = (
            sim_time + PAIR_DISPATCH_COOLDOWN_S
        )

    def _record_dispatch_rejection(self, veh_id: str, passenger_id: str) -> None:
        """ED: once driver rejects, do not re-offer same pair while passenger waits."""
        self._rejected_dispatch_pairs.add((veh_id, passenger_id))

    def _is_rejected_dispatch_pair(self, veh_id: str, passenger_id: str) -> bool:
        return (veh_id, passenger_id) in self._rejected_dispatch_pairs

    def _clear_rejection_history_for_passenger(self, passenger_id: str) -> None:
        if not self._rejected_dispatch_pairs:
            return
        self._rejected_dispatch_pairs = {
            pair
            for pair in self._rejected_dispatch_pairs
            if pair[1] != passenger_id
        }

    @staticmethod
    def _within_pickup_euclidean_cap(tx: float, ty: float, passenger: Passenger) -> bool:
        dx = passenger.x - tx
        dy = passenger.y - ty
        return dx * dx + dy * dy <= PICKUP_MAX_EUCLIDEAN_M_SQ

    def _filter_by_pickup_euclidean(
        self,
        tx: float,
        ty: float,
        passengers: list[Passenger],
    ) -> list[Passenger]:
        if not passengers:
            return passengers
        return [
            p for p in passengers
            if self._within_pickup_euclidean_cap(tx, ty, p)
        ]

    def _expire_abandoned_waiting_passengers(self, sim_time: float) -> None:
        if PASSENGER_WAIT_ABANDON_S <= 0:
            return
        for pid, passenger in list(self._passengers.items()):
            if passenger.state != "waiting":
                continue
            if sim_time - passenger.spawn_time <= PASSENGER_WAIT_ABANDON_S:
                continue
            del self._passengers[pid]
            self._clear_rejection_history_for_passenger(pid)
            self._emit_event("passenger_abandoned", {
                "sim_time": sim_time,
                "passenger_id": pid,
                "wait_seconds": sim_time - passenger.spawn_time,
            })

    def _waiting_passengers_near_taxi(
        self,
        tx: float,
        ty: float,
        waiting_passengers: list[Passenger],
    ) -> list[Passenger]:
        if not DISPATCH_H3_FILTER or not waiting_passengers:
            return waiting_passengers
        lat, lng = self._latlng(tx, ty)
        if not (math.isfinite(lat) and math.isfinite(lng)):
            return waiting_passengers
        near_cells = cells_within_k_ring(get_cell(lat, lng), DISPATCH_H3_K_RING)
        filtered = [
            p for p in waiting_passengers
            if p.h3_pickup and p.h3_pickup in near_cells
        ]
        return filtered if filtered else waiting_passengers

    def _update_taxi_states(self, sim_time: float, sub_results: dict) -> list[dict]:
        fare_updates: list[dict] = []
        self._dispatch_pricing_cache.clear()
        self._prune_dispatch_cooldowns(sim_time)
        self._expire_abandoned_waiting_passengers(sim_time)
        waiting_passengers = [p for p in self._passengers.values() if p.state == "waiting"]
        waiting_count = len(waiting_passengers)
        empty_dispatch_cap = self._fast_dispatch_empty_cap(waiting_count)
        eligible_empty_taxis: set[str] | None = None
        if empty_dispatch_cap is not None:
            empty_ids = sorted(
                veh_id for veh_id, vals in sub_results.items()
                if self._is_taxi_id(veh_id)
                and self._taxi_states.get(veh_id, "empty") == "empty"
                and self._is_valid_road_edge(vals.get(tc.VAR_ROAD_ID, ""))
            )
            if empty_ids:
                n = len(empty_ids)
                start = self._dispatch_taxi_round % n
                eligible_empty_taxis = {
                    empty_ids[(start + i) % n] for i in range(min(empty_dispatch_cap, n))
                }
                self._dispatch_taxi_round = (start + empty_dispatch_cap) % n

        for veh_id, vals in sub_results.items():
            if not self._is_taxi_id(veh_id):
                continue
            state = self._taxi_states.get(veh_id, "empty")
            tx, ty = vals[tc.VAR_POSITION]
            road_id = vals.get(tc.VAR_ROAD_ID, "")

            # 단계 1 — 배차: 거리 기준 상위 K명 검토 후 수락 확률로 배차 (findRoute needs valid edge)
            if state == "empty" and waiting_passengers and self._is_valid_road_edge(road_id):
                if self._taxi_on_dispatch_cooldown(veh_id, sim_time):
                    continue
                if not self._find_route_budget_ok():
                    continue
                if eligible_empty_taxis is not None and veh_id not in eligible_empty_taxis:
                    continue
                local_waiting = self._waiting_passengers_near_taxi(
                    tx, ty, waiting_passengers,
                )
                local_waiting = self._filter_by_pickup_euclidean(tx, ty, local_waiting)
                candidates = heapq.nsmallest(
                    self._dispatch_max_candidates(), local_waiting,
                    key=lambda p: (p.x - tx) ** 2 + (p.y - ty) ** 2,
                )
                for candidate in candidates:
                    if self._is_rejected_dispatch_pair(veh_id, candidate.id):
                        continue
                    if self._pair_on_dispatch_cooldown(veh_id, candidate.id, sim_time):
                        continue
                    if not self._find_route_budget_ok():
                        break
                    try:
                        route = self._sim_find_route(road_id, candidate.pickup_edge)
                        if not route.edges:
                            continue
                    except traci.exceptions.TraCIException:
                        self._set_dispatch_cooldowns(veh_id, candidate.id, sim_time)
                        continue

                    dispatch_id = str(uuid.uuid4())
                    dispatch_payload = {
                        "type": "dispatch",
                        "id": dispatch_id,
                        "run_id": self._run_id,
                        "passenger_id": candidate.id,
                        "taxi_id": veh_id,
                        "dispatch_sim_time": sim_time,
                        "estimated_pickup_distance_m": route.length,
                    }
                    self._emit_event("dispatch_attempted", {
                        "sim_time": sim_time,
                        "dispatch_id": dispatch_id,
                        "passenger_id": candidate.id,
                        "taxi_id": veh_id,
                        "estimated_pickup_distance_m": route.length,
                    })

                    # decision_payload는 수락/거절 모두 같은 스키마로 기록되어 cap, gap, 확률 진단 지표를 집계한다.
                    accepted = True
                    decision_payload = {
                        "sim_time": sim_time,
                        "dispatch_id": dispatch_id,
                        "passenger_id": candidate.id,
                        "taxi_id": veh_id,
                        "target_p": None,
                        "target_matching_rate": None,
                        "p_actual": 1.0,
                        "base_fare_usd": candidate.expected_fare / 100.0,
                        "surge": 1.0,
                        "raw_surge": 1.0,
                        "calculated_surge": 1.0,
                        "final_surge": 1.0,
                        "final_fare_usd": candidate.expected_fare / 100.0,
                        "required_fare_usd": None,
                        "surge_clamped": False,
                        "pricing_driver_count": None,
                        "target_gap": 0.0,
                        "matching_rate_error": 0.0,
                        "estimated_pickup_distance_m": route.length,
                    }
                    if candidate.h3_pickup and candidate.h3_dropoff:
                        D_pu_miles = route.length / 1609.344
                        pricing = self._dispatch_pricing(
                            candidate=candidate,
                            sim_time=sim_time,
                            sub_results=sub_results,
                            current_veh_id=veh_id,
                            current_route=route,
                        )
                        band_incentive_usd = self._band_incentive_usd(pricing["raw_surge"])
                        fare_usd = pricing["final_fare_usd"] + band_incentive_usd
                        trip_miles = candidate.expected_distance_m / 1609.344
                        call_dt    = SIM_BASE_DATETIME + timedelta(seconds=sim_time)
                        last_cell  = (self._taxi_last_dropoff_cells.get(veh_id)
                                      or get_cell(*self._latlng(tx, ty)))
                        try:
                            p = _acceptance_probability(
                                last_dropoff_cell=last_cell,
                                dropoff_cell=candidate.h3_dropoff,
                                call_datetime=call_dt,
                                fare_amount=fare_usd,
                                D_pu=D_pu_miles,
                                trip_distance=trip_miles,
                                beta_f=self.experiment_config.beta_f if self.experiment_config else None,
                                alpha_sensitivity=(
                                    self.experiment_config.alpha_sensitivity
                                    if self.experiment_config else self._runtime_alpha_sensitivity()
                                ),
                                pickup_cell=candidate.h3_pickup,
                            )
                        except Exception as e:
                            _logger.warning(
                                "acceptance_probability failed (pickup=%s dropoff=%s): %s"
                                " - defaulting to accept",
                                candidate.h3_pickup, candidate.h3_dropoff, e,
                            )
                            p = 1.0
                        if (
                            self.experiment_config is not None
                            and self.experiment_config.policy_mode == "rebalance"
                        ):
                            bonus = acceptance_bonus_from_raw_surge(
                                pricing["raw_surge"],
                                coef=self.experiment_config.rebalance_acceptance_coef,
                                activation=float(
                                    self._pricing_policy.get("surge_min", 1.2)
                                ),
                            )
                            p = min(1.0, p + bonus)
                        accepted = _random.random() < p
                        target_matching_rate = pricing["target_matching_rate"]
                        decision_payload.update({
                            "p_actual": p,
                            "target_p": target_matching_rate,
                            "target_matching_rate": target_matching_rate,
                            "base_fare_usd": pricing["base_fare_usd"],
                            "surge": pricing["final_surge"],
                            "raw_surge": pricing["raw_surge"],
                            "calculated_surge": pricing["calculated_surge"],
                            "final_surge": pricing["final_surge"],
                            "final_fare_usd": pricing["final_fare_usd"],
                            "required_fare_usd": pricing["required_fare_usd"],
                            "surge_clamped": pricing["surge_clamped"],
                            "pricing_driver_count": pricing["pricing_driver_count"],
                            "band_incentive_usd": band_incentive_usd,
                            "target_gap": (
                                target_matching_rate - p
                                if target_matching_rate is not None else 0.0
                            ),
                            "matching_rate_error": (
                                p - target_matching_rate
                                if target_matching_rate is not None else 0.0
                            ),
                        })
                    if not accepted:
                        self._set_dispatch_cooldowns(veh_id, candidate.id, sim_time)
                        self._record_dispatch_rejection(veh_id, candidate.id)
                        self._record_dispatch_kpi(decision_payload, accepted=False)
                        if not (
                            self.experiment_config is not None
                            and self.experiment_config.fast
                        ):
                            self._emit_event(
                                "dispatch_decision",
                                {**decision_payload, "accepted": False},
                            )
                        self._push_db_event({**dispatch_payload, **decision_payload, "accepted": False})
                        continue

                    dispatch_edges = list(route.edges)
                    # Buffer past pickup edge: prevents network exit if taxi traverses
                    # pickup edge in one step before our pickup detection fires.
                    buf = self._random_route_from(candidate.pickup_edge)
                    if buf and len(buf) > 1:
                        dispatch_edges = dispatch_edges + buf[1:]
                    traci.vehicle.setRoute(veh_id, dispatch_edges)
                    self._taxi_route_len[veh_id] = len(dispatch_edges)
                    candidate.state = "assigned"
                    self._clear_rejection_history_for_passenger(candidate.id)
                    waiting_passengers.remove(candidate)
                    self._taxi_targets[veh_id] = candidate.id
                    self._taxi_states[veh_id] = "dispatched"
                    self._taxi_dispatch_times[veh_id] = sim_time
                    self._taxi_dispatch_ids[veh_id] = dispatch_id
                    self._taxi_dispatch_surge[veh_id] = decision_payload["final_surge"]
                    previous_dropoff_time = self._taxi_previous_dropoff_times.get(veh_id)
                    if previous_dropoff_time is not None:
                        # 첫 승객 전 대기시간은 정의상 제외하고, 하차 이후 다음 수락까지의 search time만 기록한다.
                        decision_payload["empty_wait_time_s"] = sim_time - previous_dropoff_time
                    self._record_dispatch_kpi(decision_payload, accepted=True)
                    if not (
                        self.experiment_config is not None
                        and self.experiment_config.fast
                    ):
                        self._emit_event(
                            "dispatch_decision",
                            {**decision_payload, "accepted": True},
                        )
                    self._push_db_event({**dispatch_payload, **decision_payload, "accepted": True})
                    if self._is_manual_entity(candidate.id, veh_id):
                        self._schedule_ws_event(
                            "broadcast_dispatch_assigned",
                            candidate.id,
                            veh_id,
                            self._estimate_pickup_eta_seconds(route),
                        )
                    break

            # 단계 2 — 픽업: 택시가 픽업 엣지 위에 도달했을 때 (또는 배차 타임아웃)
            elif state == "dispatched":
                passenger_id = self._taxi_targets.get(veh_id)
                if passenger_id is None:
                    continue
                passenger = self._passengers.get(passenger_id)
                if passenger is None:
                    continue
                # 배차 타임아웃: 승객 waiting으로 되돌리고 택시 empty로 리셋
                dispatch_time = self._taxi_dispatch_times.get(veh_id, sim_time)
                if sim_time - dispatch_time > DISPATCH_TIMEOUT_S:
                    passenger.state = "waiting"
                    del self._taxi_targets[veh_id]
                    self._taxi_states[veh_id] = "empty"
                    self._taxi_dispatch_times.pop(veh_id, None)
                    self._taxi_dispatch_surge.pop(veh_id, None)
                    waiting_passengers.append(passenger)
                    old_dispatch_id = self._taxi_dispatch_ids.pop(veh_id, None)
                    if old_dispatch_id:
                        self._push_db_event({"type": "dispatch_timeout", "id": old_dispatch_id})
                    continue
                if road_id == passenger.pickup_edge and self._is_valid_road_edge(road_id):
                    try:
                        route = traci.simulation.findRoute(road_id, passenger.dropoff_edge)
                        if not route.edges:
                            continue
                        trip_edges = list(route.edges)
                        # Buffer past dropoff edge: prevents network exit if taxi traverses
                        # dropoff edge in one step before our dropoff detection fires.
                        buf = self._random_route_from(passenger.dropoff_edge)
                        if buf and len(buf) > 1:
                            trip_edges = trip_edges + buf[1:]
                        traci.vehicle.setRoute(veh_id, trip_edges)
                    except traci.exceptions.TraCIException:
                        continue
                    passenger.state = "picked_up"
                    self._record_passenger_boarded_kpi(passenger, sim_time)
                    self._taxi_states[veh_id] = "occupied"
                    dispatch_time = self._taxi_dispatch_times.pop(veh_id, sim_time)
                    dispatch_surge = self._taxi_dispatch_surge.pop(veh_id, 1.0)
                    self._active_trips[veh_id] = TripAccumulator(
                        passenger_id=passenger_id,
                        pickup_sim_time=sim_time,
                        dispatch_id=self._taxi_dispatch_ids.get(veh_id),
                        dispatch_sim_time=dispatch_time,
                        last_distance_snapshot=traci.vehicle.getDistance(veh_id),
                        surge=dispatch_surge,
                    )
                    if self._is_manual_entity(passenger_id, veh_id):
                        self._schedule_ws_event(
                            "broadcast_passenger_boarded",
                            passenger_id,
                            veh_id,
                            sim_time,
                        )

            # 단계 3 — 하차: 택시가 하차 엣지 위에 도달했을 때 (또는 트립 타임아웃)
            elif state == "occupied":
                passenger_id = self._taxi_targets.get(veh_id)
                if passenger_id is None:
                    continue
                passenger = self._passengers.get(passenger_id)
                if passenger is None:
                    continue
                accum = self._active_trips.get(veh_id)
                # 트립 타임아웃: 누적된 요금으로 강제 완료
                if accum and sim_time - accum.pickup_sim_time > TRIP_TIMEOUT_S:
                    dropoff_h3 = passenger.h3_dropoff or get_cell(passenger.dropoff_lat, passenger.dropoff_lng)
                    meter_fare = calculate_meter_fare(accum)
                    fare = calculate_fare(accum)
                    fare_updates.append({
                        "passenger_id": passenger_id,
                        "taxi_id": veh_id,
                        "meter_fare": meter_fare,
                        "fare": fare,
                        "surge": accum.surge,
                        "expected_fare": passenger.expected_fare,
                        "distance_m": accum.distance_m,
                        "sim_time": sim_time,
                    })
                    self._push_db_event({
                        "type": "trip",
                        "run_id": self._run_id,
                        "passenger_id": passenger_id,
                        "taxi_id": veh_id,
                        "dispatch_id": accum.dispatch_id,
                        "dispatch_sim_time": accum.dispatch_sim_time,
                        "pickup_sim_time": accum.pickup_sim_time,
                        "dropoff_sim_time": sim_time,
                        "distance_m": accum.distance_m,
                        "low_speed_seconds": accum.low_speed_seconds,
                        "meter_fare": meter_fare,
                        "fare": fare,
                        "surge": accum.surge,
                        "expected_fare": passenger.expected_fare,
                        "completion": "trip_timeout",
                    })
                    self._record_history_dropoff(sim_time, dropoff_h3)
                    self._taxi_last_dropoff_cells[veh_id] = dropoff_h3
                    del self._passengers[passenger_id]
                    del self._active_trips[veh_id]
                    del self._taxi_targets[veh_id]
                    self._taxi_states[veh_id] = "empty"
                    self._taxi_dispatch_ids.pop(veh_id, None)
                    self._taxi_dispatch_surge.pop(veh_id, None)
                    route = self._random_route_from(road_id)
                    if route:
                        traci.vehicle.setRoute(veh_id, route)
                        self._taxi_route_len[veh_id] = len(route)
                    else:
                        # 경로 연장 필요 — None이면 _extend_vehicle_routes가 재시도 (0은 매 스텝 무한 재시도 버그)
                        self._taxi_route_len.pop(veh_id, None)
                    continue
                if road_id == passenger.dropoff_edge:
                    accum = self._active_trips[veh_id]
                    dropoff_h3 = passenger.h3_dropoff or get_cell(passenger.dropoff_lat, passenger.dropoff_lng)
                    meter_fare = calculate_meter_fare(accum)
                    fare = calculate_fare(accum)
                    fare_updates.append({
                        "passenger_id": passenger_id,
                        "taxi_id": veh_id,
                        "meter_fare": meter_fare,
                        "fare": fare,
                        "surge": accum.surge,
                        "expected_fare": passenger.expected_fare,
                        "distance_m": accum.distance_m,
                        "sim_time": sim_time,
                    })
                    self._push_db_event({
                        "type": "trip",
                        "run_id": self._run_id,
                        "passenger_id": passenger_id,
                        "taxi_id": veh_id,
                        "dispatch_id": accum.dispatch_id,
                        "dispatch_sim_time": accum.dispatch_sim_time,
                        "pickup_sim_time": accum.pickup_sim_time,
                        "dropoff_sim_time": sim_time,
                        "distance_m": accum.distance_m,
                        "low_speed_seconds": accum.low_speed_seconds,
                        "meter_fare": meter_fare,
                        "fare": fare,
                        "surge": accum.surge,
                        "expected_fare": passenger.expected_fare,
                        "completion": "normal",
                    })
                    self._record_history_dropoff(sim_time, dropoff_h3)
                    self._taxi_last_dropoff_cells[veh_id] = dropoff_h3
                    del self._passengers[passenger_id]
                    del self._active_trips[veh_id]
                    del self._taxi_targets[veh_id]
                    self._taxi_states[veh_id] = "empty"
                    self._taxi_dispatch_ids.pop(veh_id, None)
                    self._taxi_dispatch_surge.pop(veh_id, None)
                    route = self._random_route_from(road_id)
                    if route:
                        traci.vehicle.setRoute(veh_id, route)
                        self._taxi_route_len[veh_id] = len(route)
                    else:
                        self._taxi_route_len.pop(veh_id, None)

        # Detect dispatched taxis that have exited the network (not in sub_results)
        for veh_id, state in list(self._taxi_states.items()):
            if state != "dispatched":
                continue
            if veh_id in sub_results:
                continue
            _logger.warning("dispatched taxi %s not found in sub_results - vehicle lost", veh_id)
            passenger_id = self._taxi_targets.pop(veh_id, None)
            if passenger_id:
                passenger = self._passengers.get(passenger_id)
                if passenger and passenger.state == "assigned":
                    passenger.state = "waiting"
                    waiting_passengers.append(passenger)
            self._taxi_states[veh_id] = "empty"
            self._taxi_dispatch_times.pop(veh_id, None)
            self._taxi_dispatch_ids.pop(veh_id, None)
            self._taxi_dispatch_surge.pop(veh_id, None)

        return fare_updates

    def _accumulate_fares(self, sim_time: float, sub_results: dict) -> list[dict]:
        fare_updates: list[dict] = []
        for taxi_id, accum in list(self._active_trips.items()):
            vals = sub_results.get(taxi_id)
            if vals is None:
                # 택시가 네트워크에서 제거됨 (경로 끝 도달) - 버퍼 경로가 누락된 경우
                _logger.warning("taxi %s removed from network while occupied - vehicle lost", taxi_id)
                passenger_id = self._taxi_targets.pop(taxi_id, None)
                passenger = self._passengers.pop(passenger_id, None) if passenger_id else None
                if passenger:
                    dropoff_h3 = passenger.h3_dropoff or get_cell(passenger.dropoff_lat, passenger.dropoff_lng)
                    meter_fare = calculate_meter_fare(accum)
                    fare = calculate_fare(accum)
                    fare_updates.append({
                        "passenger_id": passenger_id,
                        "taxi_id": taxi_id,
                        "meter_fare": meter_fare,
                        "fare": fare,
                        "surge": accum.surge,
                        "expected_fare": passenger.expected_fare,
                        "distance_m": accum.distance_m,
                        "sim_time": sim_time,
                    })
                    self._push_db_event({
                        "type": "trip",
                        "run_id": self._run_id,
                        "passenger_id": passenger_id,
                        "taxi_id": taxi_id,
                        "dispatch_id": accum.dispatch_id,
                        "dispatch_sim_time": accum.dispatch_sim_time,
                        "pickup_sim_time": accum.pickup_sim_time,
                        "dropoff_sim_time": sim_time,
                        "distance_m": accum.distance_m,
                        "low_speed_seconds": accum.low_speed_seconds,
                        "meter_fare": meter_fare,
                        "fare": fare,
                        "surge": accum.surge,
                        "expected_fare": passenger.expected_fare,
                        "completion": "sumo_removed",
                    })
                    self._record_history_dropoff(sim_time, dropoff_h3)
                    self._taxi_last_dropoff_cells[taxi_id] = dropoff_h3
                self._active_trips.pop(taxi_id, None)
                self._taxi_states.pop(taxi_id, None)
                self._taxi_dispatch_times.pop(taxi_id, None)
                self._taxi_dispatch_ids.pop(taxi_id, None)
                self._taxi_dispatch_surge.pop(taxi_id, None)
                continue
            dist = vals[tc.VAR_DISTANCE]
            delta = dist - accum.last_distance_snapshot
            if delta > 0:
                accum.distance_m += delta
            accum.last_distance_snapshot = dist
            if vals[tc.VAR_SPEED] < SPEED_THRESHOLD_MPS:
                if self.experiment_config:
                    step_length, _ = resolve_experiment_pacing(self.experiment_config)
                else:
                    step_length = STEP_LENGTH
                accum.low_speed_seconds += step_length
        return fare_updates

    def _get_routable_edges(self) -> list[str]:
        """Return edges where passenger vehicles are permitted to depart."""
        routable = []
        for edge_id in traci.edge.getIDList():
            if edge_id.startswith(":"):
                continue
            num_lanes = traci.edge.getLaneNumber(edge_id)
            for lane_idx in range(num_lanes):
                lane_id = f"{edge_id}_{lane_idx}"
                allowed = traci.lane.getAllowed(lane_id)
                # Empty allowed list means all vehicle classes are permitted
                if not allowed or "passenger" in allowed:
                    routable.append(edge_id)
                    break
        return routable

    def _filter_to_largest_scc(self, edges: list[str]) -> list[str]:
        """Filter edges to those in the largest strongly connected component.

        Within the returned set, findRoute is guaranteed to succeed for any
        (src, dst) pair — eliminating unreachable-destination failures.
        """
        if not edges:
            return edges

        edge_set = set(edges)

        # 그래프 구축: edge → 도달 가능한 outgoing edges (edge_set 내)
        graph: dict[str, set[str]] = {e: set() for e in edges}
        for edge_id in edges:
            num_lanes = traci.edge.getLaneNumber(edge_id)
            for lane_idx in range(num_lanes):
                lane_id = f"{edge_id}_{lane_idx}"
                try:
                    links = traci.lane.getLinks(lane_id)
                except traci.exceptions.TraCIException:
                    continue
                for link in links:
                    next_lane = link[0] if link else ""
                    if not next_lane:
                        continue
                    next_edge = next_lane.rsplit("_", 1)[0]
                    if next_edge in edge_set and next_edge != edge_id:
                        graph[edge_id].add(next_edge)

        sccs = _kosaraju_scc(graph)
        if not sccs:
            return edges
        largest = max(sccs, key=len)
        return list(largest)

    def _add_initial_vehicles(self) -> None:
        """Place N_BACKGROUND_CARS bg vehicles and n_taxis on the network at t=0."""
        edges = self._routable_edges

        # Define a yellow taxi vehicle type based on the default
        traci.vehicletype.copy("DEFAULT_VEHTYPE", "taxi")
        traci.vehicletype.setColor("taxi", (255, 200, 0, 255))

        route_index = 0
        for i in range(N_BACKGROUND_CARS):
            route_edges = self._random_route(edges)
            route_id = f"init_route_{route_index}"
            route_index += 1
            traci.route.add(route_id, route_edges)
            traci.vehicle.add(
                vehID=f"bg_{i}",
                routeID=route_id,
                typeID="DEFAULT_VEHTYPE",
                depart=0,
                departLane="best",
                departPos="random_free",
                departSpeed="max",
            )
            traci.vehicle.subscribe(f"bg_{i}", _BG_SUB_VARS)
            self._bg_route_len[f"bg_{i}"] = len(route_edges)

        for i in range(self._runtime_taxi_count):
            route_edges = self._random_route(edges)
            route_id = f"init_route_{route_index}"
            route_index += 1
            traci.route.add(route_id, route_edges)
            traci.vehicle.add(
                vehID=f"taxi_{i}",
                routeID=route_id,
                typeID="taxi",
                depart=0,
                departLane="best",
                departPos="random_free",
                departSpeed="max",
            )
            self._taxi_route_len[f"taxi_{i}"] = len(route_edges)
            pt = self._get_edge_midpoint(route_edges[0])
            if pt:
                lat, lng = self._latlng(*pt)
                self._taxi_last_dropoff_cells[f"taxi_{i}"] = get_cell(lat, lng)

    def _should_attempt_route_extend(self, veh_id: str, route_len: int | None, sim_time: float) -> bool:
        if route_len is not None:
            return True
        retry_after = self._route_extend_retry_after.get(veh_id, -1.0)
        if sim_time < retry_after:
            return False
        self._route_extend_retry_after[veh_id] = sim_time + _ROUTE_EXTEND_RETRY_SIM_S
        return True

    def _extend_vehicle_routes(self, sub_results: dict) -> None:
        """경로 끝에 근접한 bg 차량·empty/dispatched 택시에 새 경로를 이어 붙여 arrival 제거를 막는다.

        구독값 route_index와 저장해 둔 경로 길이로 '남은 엣지 수'를 계산한다. 이 값은 단조
        증가하므로 한 번 임계값(_ROUTE_EXTEND_REMAINING)을 넘으면 매 스텝 참이 되어, 짧은
        엣지를 한 스텝에 통과해도 연장이 누락되지 않는다. 연장에 실패하면 route_len을 그대로
        두므로 다음 스텝에 자동으로 재시도된다(임계값이 계속 참이기 때문)."""
        sim_time = traci.simulation.getTime()
        for veh_id, vals in sub_results.items():
            road_id = vals.get(tc.VAR_ROAD_ID, "")
            if not self._is_valid_road_edge(road_id):
                continue
            route_index = vals.get(tc.VAR_ROUTE_INDEX)
            if route_index is None or route_index < 0:
                continue  # 미구독이거나 아직 미출발(-1)한 차량은 건너뜀

            if veh_id.startswith("bg_"):
                if N_BACKGROUND_CARS <= 0:
                    continue
                route_len = self._bg_route_len.get(veh_id)
                if route_len is not None and route_len - 1 - route_index > _ROUTE_EXTEND_REMAINING:
                    continue
                if not self._should_attempt_route_extend(veh_id, route_len, sim_time):
                    continue
                new_route = self._random_route_from(road_id)
                if new_route:
                    try:
                        traci.vehicle.setRoute(veh_id, new_route)
                        self._bg_route_len[veh_id] = len(new_route)
                    except traci.exceptions.TraCIException:
                        pass  # route_len 유지 → 다음 스텝 재시도

            elif self._is_taxi_id(veh_id):
                # empty·dispatched 택시만 연장 (occupied 경로는 트립 로직이 관리)
                if self._taxi_states.get(veh_id, "empty") not in ("empty", "dispatched"):
                    continue
                route_len = self._taxi_route_len.get(veh_id)
                if route_len is not None and route_len - 1 - route_index > _ROUTE_EXTEND_REMAINING:
                    continue
                if not self._should_attempt_route_extend(veh_id, route_len, sim_time):
                    continue
                new_route = self._random_route_from(road_id, weights=self._cruise_route_weights())
                if new_route:
                    try:
                        traci.vehicle.setRoute(veh_id, new_route)
                        self._taxi_route_len[veh_id] = len(new_route)
                    except traci.exceptions.TraCIException:
                        pass  # route_len 유지 → 다음 스텝 재시도

    def _is_rebalance_policy(self) -> bool:
        return (
            self.experiment_config is not None
            and self.experiment_config.policy_mode == "rebalance"
        )

    def _cruise_route_weights(self) -> list[float] | None:
        if (
            self._rebalance_edge_weights is not None
            and len(self._rebalance_edge_weights) == len(self._routable_edges)
            and self._is_rebalance_policy()
        ):
            return self._rebalance_edge_weights
        if len(self._edge_weights) == len(self._routable_edges):
            return self._edge_weights
        return None

    def _build_h3_edge_index(self) -> dict[str, list[str]]:
        if self._latlng is None:
            return {}
        h3_edges: dict[str, list[str]] = {}
        for edge_id in self._routable_edges:
            pt = self._get_edge_midpoint(edge_id)
            if pt is None:
                continue
            lat, lng = self._latlng(*pt)
            cell = get_cell(lat, lng)
            h3_edges.setdefault(cell, []).append(edge_id)
        return h3_edges

    def _update_rebalance_edge_weights(self) -> None:
        config = self.experiment_config
        if config is None or config.policy_mode != "rebalance" or self._latlng is None:
            self._rebalance_edge_weights = None
            return
        targets = select_top_surge_deficit_cells(
            self._raw_surge_by_h3,
            self._last_grid_supply,
            self._last_grid_demand,
            top_k=config.rebalance_top_k,
            min_raw_surge=config.rebalance_min_raw_surge,
        )
        if not targets:
            self._rebalance_edge_weights = None
            return
        target_meta: list[tuple[float, float, float]] = []
        for cell in targets:
            raw = float(self._raw_surge_by_h3.get(cell, 1.0))
            importance = max(0.0, raw - 1.0)
            if importance <= 0:
                continue
            lat, lng = cell_center_latlng(cell)
            try:
                x, y = traci.simulation.convertGeo(lng, lat, fromGeo=True)
            except Exception:
                continue
            target_meta.append((x, y, importance))
        if not target_meta:
            self._rebalance_edge_weights = None
            return
        weights: list[float] = []
        for edge_id in self._routable_edges:
            pt = self._get_edge_midpoint(edge_id)
            if pt is None:
                weights.append(HOTSPOT_BASE_WEIGHT)
                continue
            w = HOTSPOT_BASE_WEIGHT
            for cx, cy, importance in target_meta:
                w += gaussian_cell_weight(
                    pt,
                    (cx, cy),
                    importance,
                    sigma_m=REBALANCE_SIGMA_M,
                )
            weights.append(w)
        self._rebalance_edge_weights = weights

    def _maybe_rebalance_empty_taxis(self, sim_time: float, sub_results: dict) -> None:
        config = self.experiment_config
        if config is None or config.policy_mode != "rebalance":
            return
        interval = int(sim_time / config.rebalance_interval_s)
        if interval <= self._last_rebalance_interval:
            return
        self._last_rebalance_interval = interval

        targets = select_top_surge_deficit_cells(
            self._raw_surge_by_h3,
            self._last_grid_supply,
            self._last_grid_demand,
            top_k=config.rebalance_top_k,
            min_raw_surge=config.rebalance_min_raw_surge,
        )
        if not targets:
            return

        empty_taxis = [
            veh_id
            for veh_id, state in self._taxi_states.items()
            if state == "empty" and veh_id in sub_results
        ]
        for index, veh_id in enumerate(empty_taxis):
            cell = targets[index % len(targets)]
            edges = self._h3_to_edges.get(cell)
            if not edges:
                continue
            road_id = sub_results[veh_id].get(tc.VAR_ROAD_ID, "")
            if not self._is_valid_road_edge(road_id):
                continue
            dst = _random.choice(edges)
            new_route = self._route_to_edge(road_id, dst)
            if not new_route:
                continue
            try:
                traci.vehicle.setRoute(veh_id, new_route)
            except traci.exceptions.TraCIException:
                continue
            self._taxi_route_len[veh_id] = len(new_route)
            self._emit_event(
                "rebalance_redirect",
                {
                    "sim_time": sim_time,
                    "taxi_id": veh_id,
                    "target_h3": cell,
                    "raw_surge": self._raw_surge_by_h3.get(cell),
                    "destination_edge": dst,
                },
            )

    def _compute_edge_weights(self) -> list[float]:
        """Compute weighted destination probability for each routable edge.

        Each edge's weight = base + sum_i(importance_i * exp(-d_i^2 / 2σ²))
        where d_i is distance (m) from edge midpoint to hotspot i.
        """
        # 핫스팟 lat/lng → SUMO 좌표 (한 번만 변환)
        hotspots_xy: list[tuple[float, float, float]] = []
        for lat, lng, importance in HOTSPOTS:
            try:
                x, y = traci.simulation.convertGeo(lng, lat, fromGeo=True)
                hotspots_xy.append((x, y, importance))
            except Exception:
                continue

        two_sigma_sq = 2.0 * HOTSPOT_SIGMA_M * HOTSPOT_SIGMA_M
        weights = []
        for edge_id in self._routable_edges:
            pt = self._get_edge_midpoint(edge_id)
            if pt is None:
                weights.append(HOTSPOT_BASE_WEIGHT)
                continue
            ex, ey = pt
            w = HOTSPOT_BASE_WEIGHT
            for hx, hy, imp in hotspots_xy:
                dx, dy = ex - hx, ey - hy
                w += imp * math.exp(-(dx * dx + dy * dy) / two_sigma_sq)
            weights.append(w)
        return weights

    def _route_to_edge(self, current_edge: str, dst_edge: str, attempts: int = 10) -> list[str] | None:
        if not self._is_valid_road_edge(current_edge) or not dst_edge:
            return None
        if current_edge == dst_edge:
            return [current_edge]
        for _ in range(attempts):
            try:
                result = traci.simulation.findRoute(current_edge, dst_edge)
                if result.edges:
                    return list(result.edges)
            except traci.exceptions.TraCIException:
                continue
        return None

    def _random_route_from(
        self,
        current_edge: str,
        attempts: int = 10,
        *,
        weights: list[float] | None = None,
    ) -> list[str] | None:
        """current_edge에서 시작하는 경로를 반환. _MIN_ROUTE_EDGES개 이상을 우선 시도하고
        점차 길이를 낮춰 2개까지 폴백, 모두 실패 시 None."""
        if not self._is_valid_road_edge(current_edge):
            return None
        edges = self._routable_edges
        if weights is None:
            weights = self._cruise_route_weights()
        if weights is not None and len(weights) != len(edges):
            weights = None

        def _pick() -> str:
            return _random.choices(edges, weights=weights, k=1)[0] if weights else _random.choice(edges)

        for min_len in range(_MIN_ROUTE_EDGES, 1, -1):
            for _ in range(attempts):
                dst = _pick()
                if dst == current_edge:
                    continue
                try:
                    result = traci.simulation.findRoute(current_edge, dst)
                    if len(result.edges) >= min_len:
                        return list(result.edges)
                except traci.exceptions.TraCIException:
                    continue
        return None

    def _random_route(self, edges: list[str], attempts: int = 10) -> list[str]:
        """_MIN_ROUTE_EDGES개 이상의 엣지로 구성된 경로를 우선 시도하고 점차 낮춰 폴백, 최후엔 1개."""
        for min_len in range(_MIN_ROUTE_EDGES, 0, -1):
            for _ in range(attempts):
                src = _random.choice(edges)
                dst = _random.choice(edges)
                if src == dst:
                    continue
                try:
                    result = traci.simulation.findRoute(src, dst)
                    if len(result.edges) >= min_len:
                        return list(result.edges)
                except traci.exceptions.TraCIException:
                    continue
        return [_random.choice(edges)]

    def _capture_grid_only(
        self, sub_results: dict
    ) -> tuple[dict[str, int], dict[str, int]]:
        """H3 supply/demand only — fast bench (no WebSocket vehicle/passenger payloads)."""
        grid_supply: dict[str, int] = defaultdict(int)
        grid_demand: dict[str, int] = defaultdict(int)
        for veh_id, vals in sub_results.items():
            if not self._is_taxi_id(veh_id):
                continue
            if self._taxi_states.get(veh_id, "empty") != "empty":
                continue
            x, y = vals[tc.VAR_POSITION]
            lat, lng = self._latlng(x, y)
            if math.isfinite(lat) and math.isfinite(lng):
                grid_supply[get_cell(lat, lng)] += 1
        for passenger in self._passengers.values():
            if passenger.state in ("waiting", "assigned") and passenger.h3_pickup:
                grid_demand[passenger.h3_pickup] += 1
        return grid_supply, grid_demand

    def _capture_state(
        self, sim_time: float, sub_results: dict
    ) -> tuple[dict, dict[str, int], dict[str, int]]:
        """Return (state, grid_supply, grid_demand).

        Surge computation is intentionally excluded — caller decides when to run it.
        """
        grid_supply: dict[str, int] = defaultdict(int)
        grid_demand: dict[str, int] = defaultdict(int)

        vehicles = []
        for veh_id, vals in sub_results.items():
            if veh_id.startswith("bg_"):
                continue
            x, y = vals[tc.VAR_POSITION]
            angle = vals[tc.VAR_ANGLE]
            speed = vals[tc.VAR_SPEED]
            lat, lng = self._latlng(x, y)
            if not (math.isfinite(lat) and math.isfinite(lng)):
                continue
            state = self._taxi_states.get(veh_id, "empty")
            if state == "empty":
                grid_supply[get_cell(lat, lng)] += 1
            vehicles.append({"id": veh_id, "lat": lat, "lng": lng,
                             "angle": angle, "speed": speed, "state": state})

        passengers_list = []
        for p in self._passengers.values():
            if p.state in ("waiting", "assigned"):
                if p.h3_pickup:
                    grid_demand[p.h3_pickup] += 1
                passengers_list.append({"id": p.id, "lat": p.lat, "lng": p.lng,
                                         "expected_fare": p.expected_fare,
                                         "expected_distance_m": p.expected_distance_m})

        state_dict = {"vehicles": vehicles, "passengers": passengers_list, "sim_time": sim_time}
        return state_dict, grid_supply, grid_demand

    def _build_surge_cells(
        self,
        grid_supply: dict[str, int],
        grid_demand: dict[str, int],
        sim_time: float,
    ) -> None:
        surge_cells = []
        surge_by_h3 = {}
        raw_surge_by_h3 = {}
        target_matching_rate_by_h3 = {}
        demand_for_surge: dict[str, float | int] = grid_demand
        config = self.experiment_config
        if config is not None and config.demand_source == "predicted":
            demand_for_surge = self._resolve_predicted_demand(sim_time, grid_demand)

        # 일반 실행은 기본 탄력성을 유지하고, 실험 실행만 sweep 입력값으로 override한다.
        elasticity = (
            self.experiment_config.elasticity if self.experiment_config else self._runtime_elasticity()
        )
        for cell in set(grid_supply) | set(grid_demand) | set(demand_for_surge):
            lat_c, lng_c = cell_center_latlng(cell)
            raw_surge = compute_raw_surge(
                grid_supply.get(cell, 0),
                demand_for_surge.get(cell, 0),
                elasticity=elasticity,
                max_surge=float(self._pricing_policy.get("surge_max", 4.9)),
            )
            target_matching_rate = self._target_matching_rate(raw_surge)
            surge = compute_surge(
                grid_supply.get(cell, 0),
                demand_for_surge.get(cell, 0),
                elasticity=elasticity,
                min_active_surge=float(self._pricing_policy.get("surge_min", 1.2)),
                max_surge=float(self._pricing_policy.get("surge_max", 4.9)),
            )
            surge_by_h3[cell] = surge
            raw_surge_by_h3[cell] = raw_surge
            target_matching_rate_by_h3[cell] = target_matching_rate
            actual_demand = grid_demand.get(cell, 0)
            selected_demand = demand_for_surge.get(cell, 0)
            surge_cells.append({
                "h3": cell,
                "supply": grid_supply.get(cell, 0),
                "demand": selected_demand,
                "actual_demand": actual_demand,
                "surge": surge,
                "raw_surge": raw_surge,
                "target_matching_rate": target_matching_rate,
                "center": {"lat": lat_c, "lng": lng_c},
            })
            if self.experiment_config is not None and not self._bench_skip_surge_cell_diagnostics():
                self._surge_diagnostics.append({
                    "sim_time": sim_time,
                    "h3": cell,
                    "supply": grid_supply.get(cell, 0),
                    "actual_demand": actual_demand,
                    "demand_for_surge": selected_demand,
                    "raw_surge": raw_surge,
                    "target_matching_rate": target_matching_rate,
                    "surge": surge,
                })
        self._surge_cells = surge_cells
        self._surge_by_h3 = surge_by_h3
        self._raw_surge_by_h3 = raw_surge_by_h3
        self._target_matching_rate_by_h3 = target_matching_rate_by_h3
        self._last_grid_supply = dict(grid_supply)
        self._last_grid_demand = dict(grid_demand)
        self._update_rebalance_edge_weights()
