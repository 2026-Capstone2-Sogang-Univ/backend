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
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime as _datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import traci
import traci.exceptions
from traci import constants as tc

from .coord import make_sumolib_converter, sumo_to_latlng
from .demand_history import DemandHistoryStore
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
from .grid import DEFAULT_ELASTICITY, H3_RESOLUTION, cell_center_latlng, compute_surge, get_cell
from .h3_cells import load_model_h3_cells
from .passenger import Passenger
from .pricing import RAW_SURGE_BUCKETS, apply_surge_limits, compute_raw_surge, raw_surge_bucket
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
SUMO_ROUTING_ALGORITHM = os.getenv("SUMO_ROUTING_ALGORITHM", "astar")
SUMO_REROUTING_THREADS = int(os.getenv("SUMO_REROUTING_THREADS", "0"))

SIM_DURATION = float(os.getenv("SIM_DURATION", "3600"))        # simulated seconds
FRAME_RATE = 60.0  # broadcast fps (WebSocket messages per real second)
SIMULATION_SPEED = float(os.getenv("SIMULATION_SPEED", "20"))  # simulated seconds per real second
SIM_PROFILE = os.getenv("SIM_PROFILE", "0").strip().lower() in {"1", "true", "yes", "on"}
_PROFILE_SLOW_SPEED_MPS = 1.0
_PROFILE_BOTTLENECK_EDGES = {
    edge.strip()
    for edge in os.getenv(
        "SIM_PROFILE_BOTTLENECK_EDGES",
        "5670996#0,-420895733#0,195743220#1",
    ).split(",")
    if edge.strip()
}
STEP_LENGTH = (
    SIMULATION_SPEED / FRAME_RATE
)  # simulated seconds per TraCI step (passed to SUMO)
REAL_STEP_SLEEP = 1.0 / FRAME_RATE  # real seconds between TraCI steps

N_TAXIS = int(os.getenv("N_TAXIS", "300"))
N_BACKGROUND_CARS = int(os.getenv("N_BACKGROUND_CARS", "800"))

PASSENGER_SPAWN_INTERVAL = 300.0
PASSENGERS_PER_5MIN = int(os.getenv("PASSENGERS_PER_5MIN", os.getenv("PASSENGER_LAMBDA", "5")))
# Deprecated alias kept for older scripts/tests. New code should use PASSENGERS_PER_5MIN.
PASSENGER_LAMBDA = PASSENGERS_PER_5MIN
PICKUP_THRESHOLD_M = 30.0
MAX_COMPLETED_PASSENGERS = 100
DISPATCH_TIMEOUT_S = 600.0   # 배차 후 이 시간 내 픽업 못하면 재배차
TRIP_TIMEOUT_S = 1800.0      # 탑승 후 이 시간 내 하차 못하면 강제 완료
MANUAL_COMMAND_TIMEOUT_S = float(os.getenv("MANUAL_COMMAND_TIMEOUT_S", "5.0"))
PASSENGER_WAIT_TIMEOUT_S = float(os.getenv("PASSENGER_WAIT_TIMEOUT_S", "900.0"))
TAXI_DISPATCH_COOLDOWN_S = float(os.getenv("TAXI_DISPATCH_COOLDOWN_S", "5.0"))
PAIR_DISPATCH_COOLDOWN_S = float(os.getenv("PAIR_DISPATCH_COOLDOWN_S", "60.0"))
DEFAULT_AVERAGE_TRIP_DISTANCE_M = 3000.0

# 기사 행동 모델: V/s 테이블은 2013 NYC 데이터 기반 → 동일 시간대 사용
SIM_BASE_DATETIME = _datetime(2013, 7, 8, 8, 0, 0)
# parquet 모드에서 승객 대량 스폰 시 findRoute RPC 폭증 방지용 튜닝값
DISPATCH_MAX_CANDIDATES = int(os.getenv("DISPATCH_MAX_CANDIDATES", "3"))

PASSENGER_SOURCE = os.getenv("PASSENGER_SOURCE", "parquet")
_DEFAULT_TRIPS_FILE = Path(__file__).parent.parent / "sumo_configs" / "NY" / "trips_processed.json"
TRIPS_FILE = Path(os.getenv("TRIPS_FILE", str(_DEFAULT_TRIPS_FILE)))
SCC_FILE = Path(__file__).parent.parent / "sumo_configs" / "NY" / "routable_scc.json"

DEFAULT_TARGET_MATCHING_RATES = {
    bucket: target for bucket, _, _, target in RAW_SURGE_BUCKETS
}
DEFAULT_PRICING_POLICY = {
    "epsilon": -0.6,
    "surge_min": 1.2,
    "surge_max": 4.9,
    "alpha_sensitivity": 1.0,
}
PARQUET_REPLAY_STAT_KEYS = (
    "original_count",
    "scheduled_count_per_loop",
    "source_duration_s",
    "loop_count",
    "scheduled_due_count",
    "spawned_count",
    "downsampled_count",
    "skipped_pickup",
    "skipped_dropoff",
    "route_failed",
    "missing_pickup_midpoint",
    "missing_dropoff_midpoint",
    "other_skipped",
)

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

_SUB_VARS = [tc.VAR_POSITION, tc.VAR_ANGLE, tc.VAR_SPEED, tc.VAR_DISTANCE, tc.VAR_ROAD_ID, tc.VAR_ROUTE_INDEX]
# bg 차량은 위치 스냅샷이 필요 없고 경로 연장 판정에 쓰는 값만 구독한다.
_BG_SUB_VARS = [tc.VAR_ROAD_ID, tc.VAR_ROUTE_INDEX, tc.VAR_SPEED]
# 경로 끝에서 남은 엣지 수가 이 값 이하이면 새 경로를 이어 붙인다.
# route_index는 단조 증가하므로 한 번 임계값을 넘으면 매 스텝 참 → 짧은 엣지를 한 스텝에
# 통과해도 연장이 누락되지 않는다(기존 단일 트리거 엣지 동등비교의 구조적 누락을 제거).
# 값이 2면 "마지막 세 엣지를 한 스텝에 모두 통과"해야만 누락되므로 짧은 커넥터 엣지로 인한
# 잔여 소실이 크게 줄어든다.
_ROUTE_EXTEND_REMAINING = 2
_BG_ROUTE_EXTEND_REMAINING = 5
_BG_ROUTE_EXTENSION_INTERVAL_S = 1.0
_BG_ROUTE_EXTENSION_MAX_PER_TICK = 4
# 경로 연장이 성공하면 차량은 새 경로의 index 0에서 시작하고 (경로길이 - _ROUTE_EXTEND_REMAINING - 1)
# 엣지를 주행한 뒤 다시 연장한다. 짧은 경로가 매 스텝 재연장(findRoute 폭증)되는 것을 막기 위해
# 라우팅이 돌려주는 경로 길이의 최소 목표치를 둔다 (재연장 주기 ≥ _MIN_ROUTE_EDGES - _ROUTE_EXTEND_REMAINING - 1).
_MIN_ROUTE_EDGES = 10
_BG_EXTENSION_MIN_ROUTE_EDGES = 25
_TAXI_EXTENSION_MIN_ROUTE_EDGES = 10
_EMPTY_TAXI_ROUTE_EXTEND_REMAINING = int(os.getenv("EMPTY_TAXI_ROUTE_EXTEND_REMAINING", "1"))
_DISPATCHED_TAXI_ROUTE_EXTEND_REMAINING = _ROUTE_EXTEND_REMAINING
# 차량별 경로 연장 cooldown: extension 성공 후 이 sim_seconds 동안 같은 차량은 재시도하지 않는다.
# 정체로 정지·저속 주행하는 차량이 매 bg-tick마다 trigger zone에 머물러 무의미한 findRoute를
# 반복하는 폭증을 차단. 차량이 충분히 진행해 다시 trigger zone에 진입하기 전까지 사실상
# extension은 일어나지 않으므로 안전.
_BG_EXTENSION_COOLDOWN_S = 30.0
_TAXI_EXTENSION_COOLDOWN_S = 15.0
_EMPTY_TAXI_EXTENSION_COOLDOWN_S = float(os.getenv("EMPTY_TAXI_EXTENSION_COOLDOWN_S", "30.0"))
_BG_ROUTE_EXTENSION_MIN_SPEED_MPS = 0.1
_TAXI_ROUTE_EXTENSION_MIN_SPEED_MPS = 0.1
_TAXI_CRITICAL_REMAINING_EDGES = 0
_DISPATCHED_TAXI_CRITICAL_REMAINING_EDGES = 1


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


def _valid_speed_mps(value: object) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


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
    target_p: float | None = None
    elasticity: float = DEFAULT_ELASTICITY
    beta_f: float | None = None
    seed: int = 42
    sim_duration: float = SIM_DURATION
    # 운영(start)과 같은 step rate를 기본값으로 둬야 sweep 결과를 운영에 옮길 수 있다.
    step_length: float = STEP_LENGTH
    real_sleep: float = 0.0
    broadcast: bool = False
    demand_source: str = "actual"
    prediction_mode: str = "none"
    prediction_url: str = "https://module3-ml.onrender.com/predict"
    prediction_horizon_min: int = 15
    prediction_fallback_policy: str = "error"
    passenger_elasticity: float = 0.0
    passengers_per_5min: int | None = None
    passenger_lambda: int | None = None
    alpha_sensitivity: float = 1.0
    weather_source: str = "static"


@dataclass(frozen=True)
class SimulationStartOptions:
    duration: float | None = None
    seed: int | None = None
    passenger_source: str | None = None
    target_matching_rates: dict[str, float] | None = None
    pricing_policy: dict[str, float] | None = None
    taxi_count: int | None = None
    background_vehicle_count: int | None = None
    passengers_per_5min: int | None = None
    simulation_speed: float | None = None
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
    incentive_limit: int = 0
    reply: Future | None = None


@dataclass
class KpiBucketState:
    bucket: str
    target_rate: float
    request_count: int = 0
    matched_count: int = 0
    wait_seconds: list[float] | None = None
    unique_h3_cells: set[str] | None = None
    supply_sum: float = 0.0
    demand_sum: float = 0.0
    raw_surge_sum: float = 0.0
    cell_observation_count: int = 0

    def __post_init__(self) -> None:
        if self.wait_seconds is None:
            self.wait_seconds = []
        if self.unique_h3_cells is None:
            self.unique_h3_cells = set()


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
        self._prof_counters: defaultdict = defaultdict(int)
        self._prof_route_from_edges: defaultdict = defaultdict(int)
        self._prof_stopped_edges: defaultdict = defaultdict(int)
        self._prof_slow_edges: defaultdict = defaultdict(int)
        self._prof_stopped_h3: defaultdict = defaultdict(int)
        self._prof_slow_h3: defaultdict = defaultdict(int)
        self._prof_route_bottleneck_edges: defaultdict = defaultdict(int)
        self._prof_route_bottleneck_sources: defaultdict = defaultdict(int)
        self._prof_route_bottleneck_dest_h3: defaultdict = defaultdict(int)
        self._prof_route_dest_h3: defaultdict = defaultdict(int)
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
        self._routable_edges: list[str] = []
        self._edge_h3_cache: dict[str, str | None] = {}
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
        self._trip_template: list[dict] = []
        self._parquet_replay_stats: dict[str, float | int] = self._new_parquet_replay_stats()
        self._latlng: Callable[[float, float], tuple[float, float]] | None = None
        # veh_id → 현재 할당된 경로의 엣지 수. route_index와 비교해 끝에 근접했는지 판정.
        self._bg_route_len: dict[str, int] = {}
        self._taxi_route_len: dict[str, int] = {}
        # vehicle별 다음 extension 가능 sim_time (성공 시 +_EXTENSION_COOLDOWN_S로 세팅)
        self._bg_extend_cooldown: dict[str, float] = {}
        self._taxi_extend_cooldown: dict[str, float] = {}
        self._taxi_last_extend_route_index: dict[str, int] = {}
        self._last_bg_route_extension_time: float = float("-inf")
        self._edge_weights: list[float] = []
        self._routable_edges_set: set[str] = set()
        self._run_id: int | None = None
        self._db_queue: asyncio.Queue | None = None
        self._db_writer_task: asyncio.Task | None = None
        self._run_end_recorded: bool = False
        self._taxi_dispatch_ids: dict[str, str] = {}
        self._taxi_dispatch_surge: dict[str, float] = {}
        self._taxi_dispatch_fare_caps: dict[str, int] = {}
        self._taxi_dispatch_cooldown_until: dict[str, float] = {}
        self._pair_dispatch_cooldown_until: dict[tuple[str, str], float] = {}
        self._rejected_dispatch_pairs: set[tuple[str, str]] = set()
        self._taxi_active_call_details: dict[str, dict] = {}
        self._taxi_pickup_route_index: dict[str, int] = {}
        self._taxi_dropoff_route_index: dict[str, int] = {}
        self._passenger_dispatch_buckets: dict[str, str] = {}
        self._taxi_last_dropoff_cells: dict[str, str] = {}
        self._taxi_appeared: set[str] = set()   # taxis that have appeared in sub_results at least once
        self._taxi_missing_since: dict[str, float] = {}  # veh_id → sim_time when first detected missing
        # bg 차량은 트립 로직이 없어 arrival 시 영구 손실되므로 택시와 동일한 리스폰 폴백을 둔다.
        self._bg_appeared: set[str] = set()
        self._bg_missing_since: dict[str, float] = {}
        # 실험 모드는 DB 대신 메모리 이벤트 로그로 KPI를 집계한다.
        self._event_log: list[dict] = []
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
        self._runtime_background_vehicle_count: int = N_BACKGROUND_CARS
        self._runtime_passengers_per_5min: int = PASSENGERS_PER_5MIN
        self._runtime_simulation_speed: float = SIMULATION_SPEED
        self._runtime_initial_passenger_count: int = 0
        self._target_matching_rates: dict[str, float] = dict(DEFAULT_TARGET_MATCHING_RATES)
        self._pricing_policy: dict[str, float] = dict(DEFAULT_PRICING_POLICY)
        self._runtime_kpi: RuntimeKpiState = self._new_runtime_kpi()

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
                "n_background_cars": self._runtime_background_vehicle_count,
                "env_background_cars": N_BACKGROUND_CARS,
                "frame_rate": FRAME_RATE,
                "simulation_speed": self._runtime_simulation_speed,
                "env_simulation_speed": SIMULATION_SPEED,
                "passengers_per_5min": self._passengers_per_5min(),
                "passenger_lambda": self._passengers_per_5min(),
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
        self._reset_run_state()
        self._stop_event.clear()
        self.status = SimStatus.RUNNING
        try:
            self._run_loop()
            prediction_provider = self._close_prediction_demand_provider()
            self._emit_experiment_diagnostics(prediction_provider=prediction_provider)
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

    @staticmethod
    def _new_parquet_replay_stats() -> dict[str, float | int]:
        return {key: 0 for key in PARQUET_REPLAY_STAT_KEYS}

    def _passengers_per_5min(self) -> int:
        if self.experiment_config is not None:
            if self.experiment_config.passengers_per_5min is not None:
                return max(0, int(self.experiment_config.passengers_per_5min))
            if self.experiment_config.passenger_lambda is not None:
                return max(0, int(self.experiment_config.passenger_lambda))
        return max(0, int(self._runtime_passengers_per_5min))

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
        if options.background_vehicle_count is not None:
            self._runtime_background_vehicle_count = int(options.background_vehicle_count)
        if options.passengers_per_5min is not None:
            self._runtime_passengers_per_5min = int(options.passengers_per_5min)
        if options.simulation_speed is not None:
            self._runtime_simulation_speed = float(options.simulation_speed)
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
        self._runtime_background_vehicle_count = N_BACKGROUND_CARS
        self._runtime_passengers_per_5min = PASSENGERS_PER_5MIN
        self._runtime_simulation_speed = SIMULATION_SPEED
        self._runtime_initial_passenger_count = 0
        self._target_matching_rates = dict(DEFAULT_TARGET_MATCHING_RATES)
        self._pricing_policy = dict(DEFAULT_PRICING_POLICY)

    def get_status_summary(self) -> dict:
        with self._lock:
            state = dict(self._state)
            vehicles = list(state.get("vehicles", []))
            passengers = list(state.get("passengers", []))
            waiting_passenger_count = sum(
                1 for passenger in self._passengers.values() if passenger.state == "waiting"
            )
            assigned_passenger_count = sum(
                1 for passenger in self._passengers.values() if passenger.state == "assigned"
            )

            taxi_vehicles = [
                vehicle for vehicle in vehicles
                if self._is_taxi_id(str(vehicle.get("id", "")))
            ]
            taxi_count = len(taxi_vehicles)
            background_vehicle_count = sum(
                1 for vehicle in vehicles if str(vehicle.get("id", "")).startswith("bg_")
            )
            empty_taxi_count = sum(1 for vehicle in taxi_vehicles if vehicle.get("state") == "empty")
            dispatched_taxi_count = sum(1 for vehicle in taxi_vehicles if vehicle.get("state") == "dispatched")
            occupied_taxi_count = sum(1 for vehicle in taxi_vehicles if vehicle.get("state") == "occupied")

            return {
                "status": self.status,
                "sim_time": state.get("sim_time", 0.0),
                "vehicles": vehicles,
                "passengers": passengers,
                "frame_rate": FRAME_RATE,
                "simulation_speed": self._runtime_simulation_speed,
                "vehicle_count": len(vehicles),
                "taxi_count": taxi_count,
                "background_vehicle_count": background_vehicle_count,
                "configured_background_vehicle_count": self._runtime_background_vehicle_count,
                "empty_taxi_count": empty_taxi_count,
                "dispatched_taxi_count": dispatched_taxi_count,
                "occupied_taxi_count": occupied_taxi_count,
                "waiting_passenger_count": waiting_passenger_count,
                "assigned_passenger_count": assigned_passenger_count,
                "completed_trip_count": self._completed_trip_count,
                "h3_resolution": H3_RESOLUTION,
                "passenger_source": self._passenger_source(),
                "passengers_per_5min": self._passengers_per_5min(),
                "passenger_lambda": self._passengers_per_5min(),
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
            parquet_replay = dict(self._parquet_replay_stats)
            cell_bucket_payloads = self._cell_bucket_payloads()

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
                "cells": {
                    "by_raw_bucket": cell_bucket_payloads,
                },
                "parquet_replay": parquet_replay,
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

    def quote_manual_passenger(
        self,
        *,
        pickup_lat: float,
        pickup_lng: float,
        dropoff_lat: float,
        dropoff_lng: float,
        incentive_limit: int = 0,
        timeout_s: float = MANUAL_COMMAND_TIMEOUT_S,
    ) -> dict:
        return self._submit_manual_request(
            ManualCreationRequest(
                kind="passenger_quote",
                entity_id="quote",
                pickup_lat=pickup_lat,
                pickup_lng=pickup_lng,
                dropoff_lat=dropoff_lat,
                dropoff_lng=dropoff_lng,
                incentive_limit=max(0, int(incentive_limit)),
            ),
            timeout_s=timeout_s,
        )

    def create_manual_passenger(
        self,
        *,
        pickup_lat: float,
        pickup_lng: float,
        dropoff_lat: float,
        dropoff_lng: float,
        incentive_limit: int = 0,
        timeout_s: float = MANUAL_COMMAND_TIMEOUT_S,
    ) -> dict:
        with self._lock:
            passenger_id = f"upax_{self._manual_passenger_counter + 1}"
            self._manual_passenger_counter += 1
        return self._submit_manual_request(
            ManualCreationRequest(
                kind="passenger",
                entity_id=passenger_id,
                pickup_lat=pickup_lat,
                pickup_lng=pickup_lng,
                dropoff_lat=dropoff_lat,
                dropoff_lng=dropoff_lng,
                incentive_limit=max(0, int(incentive_limit)),
            ),
            timeout_s=timeout_s,
        )

    def create_manual_taxi(
        self,
        *,
        lat: float,
        lng: float,
        timeout_s: float = MANUAL_COMMAND_TIMEOUT_S,
    ) -> dict:
        with self._lock:
            taxi_id = f"utaxi_{self._manual_taxi_counter + 1}"
            self._manual_taxi_counter += 1
        return self._submit_manual_request(
            ManualCreationRequest(kind="taxi", entity_id=taxi_id, lat=lat, lng=lng),
            timeout_s=timeout_s,
        )

    def _submit_manual_request(
        self,
        request: ManualCreationRequest,
        *,
        timeout_s: float,
    ) -> dict:
        reply: Future = Future()
        queued_request = ManualCreationRequest(
            kind=request.kind,
            entity_id=request.entity_id,
            pickup_lat=request.pickup_lat,
            pickup_lng=request.pickup_lng,
            dropoff_lat=request.dropoff_lat,
            dropoff_lng=request.dropoff_lng,
            lat=request.lat,
            lng=request.lng,
            incentive_limit=request.incentive_limit,
            reply=reply,
        )
        with self._lock:
            self._manual_requests.append(queued_request)
        try:
            return reply.result(timeout=max(0.1, timeout_s))
        except FutureTimeoutError:
            reply.cancel()
            return {
                "ok": False,
                "error": "simulation_busy",
                "message": "Simulation is busy. Please retry.",
            }

    def get_taxi_call_detail(self, taxi_id: str) -> dict | None:
        with self._lock:
            detail = self._taxi_active_call_details.get(taxi_id)
            return dict(detail) if detail else None

    def get_taxi_standby_context(self, taxi_id: str) -> dict | None:
        with self._lock:
            vehicle = next(
                (v for v in self._state.get("vehicles", []) if v.get("id") == taxi_id),
                None,
            )
            if vehicle is None:
                return None
            lat = float(vehicle["lat"])
            lng = float(vehicle["lng"])
            current_h3 = get_cell(lat, lng)
            current_surge = float(self._surge_by_h3.get(current_h3, 1.0))
            average_fare = self._average_trip_fare_cents()
            cells = list(self._surge_cells)

        recommended = []
        for cell in cells:
            h3 = cell.get("h3")
            center = cell.get("center") or {}
            center_lat = center.get("lat")
            center_lng = center.get("lng")
            if not h3 or center_lat is None or center_lng is None or h3 == current_h3:
                continue
            surge = float(cell.get("surge", 1.0))
            supply = int(cell.get("supply", 0) or 0)
            demand = int(cell.get("demand", 0) or 0)
            if surge <= current_surge and demand <= supply:
                continue
            distance_km = self._haversine_km(lat, lng, float(center_lat), float(center_lng))
            imbalance = max(0, demand - supply)
            score = (2.0 * max(0.0, surge - 1.0)) + math.log1p(imbalance) - (0.15 * distance_km)
            recommended.append((
                score,
                {
                    "h3_index": h3,
                    "center": {"lat": float(center_lat), "lng": float(center_lng)},
                    "supply": supply,
                    "demand": demand,
                    "surge": surge,
                    "expected_incentive": self._surge_incentive_cents(average_fare, surge),
                },
            ))
        recommended.sort(key=lambda item: item[0], reverse=True)
        return {
            "taxi_id": taxi_id,
            "location": {"lat": lat, "lng": lng},
            "current_incentive": self._surge_incentive_cents(average_fare, current_surge),
            "current_surge": current_surge,
            "recommended_cells": [item[1] for item in recommended[:5]],
        }

    def _average_trip_fare_cents(self) -> int:
        if self._runtime_kpi.completed_trip_count > 0:
            return round(
                self._runtime_kpi.total_fare_cents / self._runtime_kpi.completed_trip_count
            )
        return estimate_fare(DEFAULT_AVERAGE_TRIP_DISTANCE_M)

    @staticmethod
    def _surge_incentive_cents(base_fare: int, surge: float) -> int:
        return max(0, round(base_fare * max(0.0, surge - 1.0)))

    @staticmethod
    def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        radius_km = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlng = math.radians(lng2 - lng1)
        a = (
            math.sin(dlat / 2.0) ** 2
            + math.cos(math.radians(lat1))
            * math.cos(math.radians(lat2))
            * math.sin(dlng / 2.0) ** 2
        )
        return 2.0 * radius_km * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

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
        end_reason = (
            "duration" if self.status == SimStatus.FINISHED
            else "error" if self.status == SimStatus.IDLE
            else "manual_stop"
        )
        await self._close_db_writer(end_reason)
        self.status = SimStatus.IDLE
        if self.connection_manager is not None:
            self.connection_manager.clear_boundary()
        self._reset_run_state()

    async def _close_db_writer(self, end_reason: str | None = None) -> None:
        queue = self._db_queue
        writer_task = self._db_writer_task
        if queue is None:
            return
        if end_reason is not None and self._run_id is not None and not self._run_end_recorded:
            self._run_end_recorded = True
            await queue.put({
                "type": "run_end",
                "run_id": self._run_id,
                "end_reason": end_reason,
            })
        await queue.put(None)
        if writer_task is not None:
            try:
                await writer_task
            except Exception:
                pass
        if self._db_writer_task is writer_task:
            self._db_writer_task = None
            self._db_queue = None

    def _close_db_writer_from_run_thread(self, end_reason: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        future = asyncio.run_coroutine_threadsafe(
            self._close_db_writer(end_reason),
            loop,
        )
        try:
            future.result(timeout=10.0)
        except Exception as exc:
            _logger.warning("DB writer close failed [%s]: %s", end_reason, exc)

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
            self._routable_edges = []
            self._edge_h3_cache = {}
            self._surge_cells = []
            self._surge_by_h3 = {}
            self._raw_surge_by_h3 = {}
            self._target_matching_rate_by_h3 = {}
            self._history_store = None
            self._surge_diagnostics = []
            self._completed_passengers = []
            self._completed_trip_count = 0
            self._trip_queue = []
            self._trip_template = []
            self._parquet_replay_stats = self._new_parquet_replay_stats()
            self._latlng = None
            self._bg_route_len = {}
            self._taxi_route_len = {}
            self._bg_extend_cooldown = {}
            self._taxi_extend_cooldown = {}
            self._taxi_last_extend_route_index = {}
            self._last_bg_route_extension_time = float("-inf")
            self._edge_weights = []
            self._routable_edges_set = set()
            self._run_id = None
            self._run_end_recorded = False
            self._loop = None
            self._state_queue = None
            self._executor_task = None
            self._broadcast_task = None
            self._db_queue = None
            self._db_writer_task = None
            self._taxi_dispatch_ids = {}
            self._taxi_dispatch_surge = {}
            self._taxi_dispatch_fare_caps = {}
            self._taxi_dispatch_cooldown_until = {}
            self._pair_dispatch_cooldown_until = {}
            self._rejected_dispatch_pairs = set()
            self._taxi_active_call_details = {}
            self._taxi_pickup_route_index = {}
            self._taxi_dropoff_route_index = {}
            self._passenger_dispatch_buckets = {}
            self._taxi_last_dropoff_cells = {}
            self._taxi_appeared.clear()
            self._taxi_missing_since.clear()
            self._bg_appeared.clear()
            self._bg_missing_since.clear()
            self._event_log = []
            self._prof_counters.clear()
            self._prof_route_from_edges.clear()
            self._prof_stopped_edges.clear()
            self._prof_slow_edges.clear()
            self._prof_stopped_h3.clear()
            self._prof_slow_h3.clear()
            self._prof_route_bottleneck_edges.clear()
            self._prof_route_bottleneck_sources.clear()
            self._prof_route_bottleneck_dest_h3.clear()
            self._prof_route_dest_h3.clear()
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
            runtime_step_length = self._runtime_simulation_speed / FRAME_RATE
            step_length = self.experiment_config.step_length if experiment else runtime_step_length
            sim_duration = self.experiment_config.sim_duration if experiment else self._runtime_duration
            self._runtime_duration = sim_duration
            real_step_sleep = self.experiment_config.real_sleep if experiment else REAL_STEP_SLEEP
            broadcast_enabled = (not experiment) or bool(self.experiment_config.broadcast)
            cmd = [
                SUMO_BINARY,
                "-c",
                SUMO_CONFIG,
                "--no-step-log",
                "--no-warnings",
                "--step-length",
                str(step_length),
                "--routing-algorithm",
                SUMO_ROUTING_ALGORITHM,
            ]
            if SUMO_REROUTING_THREADS > 0:
                cmd.extend(["--device.rerouting.threads", str(SUMO_REROUTING_THREADS)])
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
            self._add_initial_vehicles()
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
                    self._prepare_parquet_trip_queue(json.load(f))

            _PROF_INTERVAL_S = 10.0
            _prof = None
            if SIM_PROFILE:
                _prof = {
                    "totals": defaultdict(float),
                    "steps": 0,
                    "last_sim": 0.0,
                    "last_real": time.perf_counter(),
                }

            next_deadline = time.perf_counter()
            while not self._stop_event.is_set():
                if self._paused:
                    time.sleep(0.05)
                    next_deadline = time.perf_counter()  # pause 중 누적 방지
                    continue

                next_deadline += real_step_sleep
                _pt = time.perf_counter() if SIM_PROFILE else 0.0
                traci.simulationStep()
                sim_time = traci.simulation.getTime()

                for veh_id in traci.simulation.getDepartedIDList():
                    if self._is_taxi_id(veh_id):
                        traci.vehicle.subscribe(veh_id, _SUB_VARS)

                sub_results = traci.vehicle.getAllSubscriptionResults()
                if SIM_PROFILE:
                    _prof["totals"]["sumo_step"] += time.perf_counter() - _pt
                    _pt = time.perf_counter()
                    _vehicle_count = len(sub_results)
                    _taxi_count = 0
                    _bg_count = 0
                    _stopped_count = 0
                    _slow_count = 0
                    _invalid_speed_count = 0
                    _speed_sum = 0.0
                    _speed_samples = 0
                    for _veh_id, _vals in sub_results.items():
                        if self._is_taxi_id(_veh_id):
                            _taxi_count += 1
                        elif _veh_id.startswith("bg_"):
                            _bg_count += 1
                        _speed = _valid_speed_mps(_vals.get(tc.VAR_SPEED))
                        if _speed is None:
                            _invalid_speed_count += 1
                            continue
                        _speed_samples += 1
                        _speed_sum += _speed
                        if _speed <= _BG_ROUTE_EXTENSION_MIN_SPEED_MPS:
                            _stopped_count += 1
                            _road_id = _vals.get(tc.VAR_ROAD_ID, "")
                            if _road_id and not _road_id.startswith(":"):
                                self._prof_stopped_edges[_road_id] += 1
                                _cell = self._h3_for_edge(_road_id)
                                if _cell:
                                    self._prof_stopped_h3[_cell] += 1
                        if _speed <= _PROFILE_SLOW_SPEED_MPS:
                            _slow_count += 1
                            _road_id = _vals.get(tc.VAR_ROAD_ID, "")
                            if _road_id and not _road_id.startswith(":"):
                                self._prof_slow_edges[_road_id] += 1
                                _cell = self._h3_for_edge(_road_id)
                                if _cell:
                                    self._prof_slow_h3[_cell] += 1
                    self._prof_counters["traffic_steps"] += 1
                    self._prof_counters["traffic_vehicle_count_sum"] += _vehicle_count
                    self._prof_counters["traffic_taxi_count_sum"] += _taxi_count
                    self._prof_counters["traffic_bg_count_sum"] += _bg_count
                    self._prof_counters["traffic_stopped_sum"] += _stopped_count
                    self._prof_counters["traffic_slow_sum"] += _slow_count
                    self._prof_counters["traffic_invalid_speed_sum"] += _invalid_speed_count
                    self._prof_counters["traffic_speed_sum"] += _speed_sum
                    self._prof_counters["traffic_speed_samples"] += _speed_samples
                    _prof["totals"]["traffic_profile"] += time.perf_counter() - _pt

                _pt = time.perf_counter() if SIM_PROFILE else 0.0
                # Track which vehicles have appeared at least once, and clear stale missing records
                for veh_id in sub_results:
                    if self._is_taxi_id(veh_id):
                        self._taxi_appeared.add(veh_id)
                        self._taxi_missing_since.pop(veh_id, None)
                    elif veh_id.startswith("bg_"):
                        self._bg_appeared.add(veh_id)
                        self._bg_missing_since.pop(veh_id, None)
                if SIM_PROFILE:
                    _prof["totals"]["appeared_track"] += time.perf_counter() - _pt

                _pt = time.perf_counter() if SIM_PROFILE else 0.0
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
                        route_edges = self._random_route(
                            self._routable_edges,
                            profile_source="respawn_taxi",
                        )
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
                                self._taxi_last_extend_route_index.pop(veh_id, None)
                                self._taxi_missing_since.pop(veh_id, None)
                                print(f"[respawn] {veh_id} at sim_time={sim_time:.0f}", flush=True)
                            except traci.exceptions.TraCIException as e:
                                print(f"[respawn] FAILED {veh_id}: {e}", flush=True)

                if SIM_PROFILE:
                    _prof["totals"]["respawn_taxi"] += time.perf_counter() - _pt

                _pt = time.perf_counter() if SIM_PROFILE else 0.0
                # Respawn bg cars that left the network. bg는 트립 로직이 없어 한 번 arrival되면
                # 영구 손실되므로(running 단조 감소의 주원인), 누락분을 재투입해 차량 수를 유지한다.
                for veh_id in list(self._bg_route_len.keys()):
                    if veh_id in sub_results:
                        continue
                    if veh_id not in self._bg_appeared:
                        continue  # 아직 최초 출발 전 (insertion 대기)
                    missing_since = self._bg_missing_since.get(veh_id)
                    if missing_since is None:
                        self._bg_missing_since[veh_id] = sim_time
                    elif sim_time - missing_since >= _RESPAWN_THRESHOLD:
                        route_edges = self._random_route(
                            self._routable_edges,
                            profile_source="respawn_bg",
                        )
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

                if SIM_PROFILE:
                    _prof["totals"]["respawn_bg"] += time.perf_counter() - _pt

                _pt = time.perf_counter() if SIM_PROFILE else 0.0
                extend_bg_routes = (
                    sim_time - self._last_bg_route_extension_time
                    >= _BG_ROUTE_EXTENSION_INTERVAL_S
                )
                if extend_bg_routes:
                    self._last_bg_route_extension_time = sim_time
                self._extend_vehicle_routes(sub_results, sim_time, extend_bg_routes=extend_bg_routes)
                if SIM_PROFILE:
                    _prof["totals"]["extend_routes"] += time.perf_counter() - _pt

                _pt = time.perf_counter() if SIM_PROFILE else 0.0
                self._process_manual_requests(sim_time)
                self._spawn_passengers(sim_time)
                self._cancel_timed_out_passengers(sim_time, sub_results)
                if SIM_PROFILE:
                    _prof["totals"]["spawn"] += time.perf_counter() - _pt

                _pt = time.perf_counter() if SIM_PROFILE else 0.0
                surge_payload = None
                with self._lock:
                    grid_supply, grid_demand = self._capture_grid_counts(sub_results)
                    surge_interval = int(sim_time / 5.0)
                    if surge_interval > self._last_surge_interval:
                        self._last_surge_interval = surge_interval
                        self._build_surge_cells(grid_supply, grid_demand, sim_time)
                        surge_payload = self._surge_cells
                if SIM_PROFILE:
                    _prof["totals"]["surge"] += time.perf_counter() - _pt

                _pt = time.perf_counter() if SIM_PROFILE else 0.0
                fare_updates = self._update_taxi_states(sim_time, sub_results)
                if SIM_PROFILE:
                    _prof["totals"]["taxi_states"] += time.perf_counter() - _pt
                _pt = time.perf_counter() if SIM_PROFILE else 0.0
                fare_updates += self._accumulate_fares(sim_time, sub_results)
                if SIM_PROFILE:
                    _prof["totals"]["fares"] += time.perf_counter() - _pt

                _pt = time.perf_counter() if SIM_PROFILE else 0.0
                with self._lock:
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
                if SIM_PROFILE:
                    _prof["totals"]["broadcast_build"] += time.perf_counter() - _pt

                if SIM_PROFILE:
                    _prof["steps"] += 1
                if SIM_PROFILE and sim_time - _prof["last_sim"] >= _PROF_INTERVAL_S:
                    _real_dt = time.perf_counter() - _prof["last_real"]
                    _sim_dt = sim_time - _prof["last_sim"]
                    _speedup = (_sim_dt / _real_dt) if _real_dt > 0 else 0
                    _lines = [
                        f"[prof] sim={sim_time:.0f}s steps={_prof['steps']} "
                        f"real={_real_dt:.2f}s speedup={_speedup:.2f}x"
                    ]
                    _accounted = sum(_prof["totals"].values())
                    for _sec, _t in sorted(_prof["totals"].items(), key=lambda kv: -kv[1]):
                        _pct = 100 * _t / _real_dt if _real_dt else 0
                        _avg_ms = 1000 * _t / _prof["steps"] if _prof["steps"] else 0
                        _lines.append(
                            f"[prof]   {_sec:18s} {_t:6.2f}s ({_pct:5.1f}%) "
                            f"avg {_avg_ms:6.2f}ms/step"
                        )
                    _unacc = _real_dt - _accounted
                    _lines.append(
                        f"[prof]   {'(unaccounted)':18s} {_unacc:6.2f}s "
                        f"({100 * _unacc / _real_dt if _real_dt else 0:5.1f}%)"
                    )
                    # Call counters collected only when SIM_PROFILE is enabled.
                    _rrf = self._prof_counters.get("rrf_calls", 0)
                    _fr = self._prof_counters.get("findRoute_calls", 0)
                    _none = self._prof_counters.get("rrf_none", 0)
                    _invalid_current_edge = self._prof_counters.get("rrf_invalid_current_edge", 0)
                    _tbg = self._prof_counters.get("trigger_bg", 0)
                    _ttx = self._prof_counters.get("trigger_taxi", 0)
                    _ttx_empty = self._prof_counters.get("trigger_taxi_empty", 0)
                    _ttx_dispatched = self._prof_counters.get("trigger_taxi_dispatched", 0)
                    _pickup_index_reached = self._prof_counters.get("pickup_index_reached", 0)
                    _dropoff_index_reached = self._prof_counters.get("dropoff_index_reached", 0)
                    _skip_bg_cd = self._prof_counters.get("skip_bg_cooldown", 0)
                    _skip_bg_stopped = self._prof_counters.get("skip_bg_stopped", 0)
                    _skip_bg_budget = self._prof_counters.get("skip_bg_budget", 0)
                    _skip_taxi_stopped = self._prof_counters.get("skip_taxi_stopped", 0)
                    _skip_taxi_static_index = self._prof_counters.get("skip_taxi_static_index", 0)
                    _skip_taxi_static_index_empty = self._prof_counters.get(
                        "skip_taxi_static_index_empty", 0
                    )
                    _skip_taxi_static_index_dispatched = self._prof_counters.get(
                        "skip_taxi_static_index_dispatched", 0
                    )
                    _traffic_steps = self._prof_counters.get("traffic_steps", 0)
                    _traffic_div = _traffic_steps if _traffic_steps else 1
                    _traffic_speed_samples = self._prof_counters.get("traffic_speed_samples", 0)
                    _traffic_speed_div = _traffic_speed_samples if _traffic_speed_samples else 1
                    _traffic_avg_vehicles = self._prof_counters.get("traffic_vehicle_count_sum", 0) / _traffic_div
                    _traffic_avg_taxis = self._prof_counters.get("traffic_taxi_count_sum", 0) / _traffic_div
                    _traffic_avg_bg = self._prof_counters.get("traffic_bg_count_sum", 0) / _traffic_div
                    _traffic_avg_stopped = self._prof_counters.get("traffic_stopped_sum", 0) / _traffic_div
                    _traffic_avg_slow = self._prof_counters.get("traffic_slow_sum", 0) / _traffic_div
                    _traffic_avg_invalid_speed = self._prof_counters.get("traffic_invalid_speed_sum", 0) / _traffic_div
                    _traffic_avg_speed = self._prof_counters.get("traffic_speed_sum", 0.0) / _traffic_speed_div
                    _route_from_total = sum(self._prof_route_from_edges.values())
                    _route_from_unique = len(self._prof_route_from_edges)
                    _route_from_repeat = max(0, _route_from_total - _route_from_unique)
                    _route_from_repeat_ratio = (
                        _route_from_repeat / _route_from_total if _route_from_total else 0
                    )
                    _route_from_top = sorted(
                        self._prof_route_from_edges.items(), key=lambda kv: -kv[1]
                    )[:3]
                    _route_from_top_text = ",".join(
                        f"{_edge}:{_count}" for _edge, _count in _route_from_top
                    ) or "-"
                    _top_stopped_edges = self._format_prof_top(self._prof_stopped_edges)
                    _top_slow_edges = self._format_prof_top(self._prof_slow_edges)
                    _top_stopped_h3 = self._format_prof_top(self._prof_stopped_h3)
                    _top_slow_h3 = self._format_prof_top(self._prof_slow_h3)
                    _route_generated = self._prof_counters.get("route_generated_total", 0)
                    _route_bottleneck_hits = self._prof_counters.get("route_bottleneck_hit", 0)
                    _route_bottleneck_ratio = (
                        _route_bottleneck_hits / _route_generated if _route_generated else 0
                    )
                    _top_route_bottleneck_edges = self._format_prof_top(
                        self._prof_route_bottleneck_edges
                    )
                    _top_route_bottleneck_sources = self._format_prof_top(
                        self._prof_route_bottleneck_sources
                    )
                    _top_route_bottleneck_dest_h3 = self._format_prof_top(
                        self._prof_route_bottleneck_dest_h3
                    )
                    _top_route_dest_h3 = self._format_prof_top(self._prof_route_dest_h3)
                    _avg_fr_per_rrf = (_fr / _rrf) if _rrf else 0
                    _avg_rrf_time_ms = (1000 * _prof["totals"].get("extend_routes", 0) / _rrf) if _rrf else 0
                    _lines.append(
                        f"[prof]   traffic: vehicles={_traffic_avg_vehicles:.1f} "
                        f"taxis={_traffic_avg_taxis:.1f} bg={_traffic_avg_bg:.1f} "
                        f"stopped={_traffic_avg_stopped:.1f} slow={_traffic_avg_slow:.1f} "
                        f"invalid_speed={_traffic_avg_invalid_speed:.1f} "
                        f"avg_speed={_traffic_avg_speed:.2f}mps"
                    )
                    _lines.append(
                        f"[prof]   route_from: total={_route_from_total} "
                        f"unique={_route_from_unique} repeat={_route_from_repeat} "
                        f"repeat_ratio={_route_from_repeat_ratio:.2f} "
                        f"top={_route_from_top_text}"
                    )
                    _lines.append(
                        f"[prof]   congestion: stopped_edges={_top_stopped_edges} "
                        f"slow_edges={_top_slow_edges} "
                        f"stopped_h3={_top_stopped_h3} slow_h3={_top_slow_h3}"
                    )
                    _lines.append(
                        f"[prof]   route_bottleneck: generated={_route_generated} "
                        f"hits={_route_bottleneck_hits} "
                        f"hit_ratio={_route_bottleneck_ratio:.2f} "
                        f"edges={_top_route_bottleneck_edges} "
                        f"sources={_top_route_bottleneck_sources} "
                        f"dest_h3={_top_route_bottleneck_dest_h3} "
                        f"all_dest_h3={_top_route_dest_h3}"
                    )
                    _lines.append(
                        f"[prof]   counts: trigger_bg={_tbg} trigger_taxi={_ttx} "
                        f"trigger_taxi_empty={_ttx_empty} "
                        f"trigger_taxi_dispatched={_ttx_dispatched} "
                        f"pickup_index_reached={_pickup_index_reached} "
                        f"dropoff_index_reached={_dropoff_index_reached} "
                        f"rrf_calls={_rrf} rrf_none={_none} "
                        f"rrf_invalid_current_edge={_invalid_current_edge} "
                        f"findRoute_calls={_fr} "
                        f"findRoute/rrf={_avg_fr_per_rrf:.2f} extend/rrf={_avg_rrf_time_ms:.2f}ms"
                    )
                    _lines.append(
                        f"[prof]   skips: bg_cooldown={_skip_bg_cd} "
                        f"bg_stopped={_skip_bg_stopped} bg_budget={_skip_bg_budget} "
                        f"taxi_stopped={_skip_taxi_stopped} "
                        f"taxi_static_index={_skip_taxi_static_index} "
                        f"taxi_static_empty={_skip_taxi_static_index_empty} "
                        f"taxi_static_dispatched={_skip_taxi_static_index_dispatched}"
                    )
                    print("\n".join(_lines), flush=True)
                    _prof["totals"].clear()
                    _prof["steps"] = 0
                    _prof["last_sim"] = sim_time
                    _prof["last_real"] = time.perf_counter()
                    self._prof_counters.clear()
                    self._prof_route_from_edges.clear()
                    self._prof_stopped_edges.clear()
                    self._prof_slow_edges.clear()
                    self._prof_stopped_h3.clear()
                    self._prof_slow_h3.clear()
                    self._prof_route_bottleneck_edges.clear()
                    self._prof_route_bottleneck_sources.clear()
                    self._prof_route_bottleneck_dest_h3.clear()
                    self._prof_route_dest_h3.clear()

                if sim_time >= sim_duration:
                    # Force-complete all trips still in progress so their fares are recorded.
                    with self._lock:
                        for taxi_id, accum in list(self._active_trips.items()):
                            pid = self._taxi_targets.pop(taxi_id, None)
                            p = self._passengers.pop(pid, None) if pid else None
                            if pid:
                                self._clear_rejection_history_for_passenger(pid)
                            if p:
                                dropoff_h3 = p.h3_dropoff or get_cell(p.dropoff_lat, p.dropoff_lng)
                                fare_result = self._fare_result_for_accumulator(p, accum)
                                meter_fare = fare_result["meter_fare"]
                                fare = fare_result["fare"]
                                self._completed_passengers.append({
                                    "passenger_id": pid,
                                    "taxi_id": taxi_id,
                                    "meter_fare": meter_fare,
                                    "uncapped_fare": fare_result["uncapped_fare"],
                                    "fare": fare,
                                    "incentive_cap_applied": fare_result["incentive_cap_applied"],
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
                                    "uncapped_fare": fare_result["uncapped_fare"],
                                    "fare": fare,
                                    "incentive_cap_applied": fare_result["incentive_cap_applied"],
                                    "surge": accum.surge,
                                    "expected_fare": p.expected_fare,
                                    "completion": "forced_at_end",
                                })
                                self._record_history_dropoff(sim_time, dropoff_h3)
                            self._taxi_dispatch_ids.pop(taxi_id, None)
                            self._taxi_dispatch_surge.pop(taxi_id, None)
                            self._taxi_dispatch_fare_caps.pop(taxi_id, None)
                            self._taxi_active_call_details.pop(taxi_id, None)
                            self._taxi_pickup_route_index.pop(taxi_id, None)
                            self._taxi_dropoff_route_index.pop(taxi_id, None)
                        self._active_trips.clear()
                    self.status = SimStatus.FINISHED
                    if broadcast_enabled and self._loop is not None and self._state_queue is not None:
                        self._loop.call_soon_threadsafe(self._state_queue.put_nowait, None)
                    self._close_db_writer_from_run_thread("duration")
                    break

                remaining = next_deadline - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)

        except traci.exceptions.FatalTraCIError:
            # SUMO closed the connection (e.g. reached configured end time)
            self.status = SimStatus.FINISHED
            self._close_db_writer_from_run_thread("sumo_closed")
        except Exception:
            self.status = SimStatus.IDLE
            self._close_db_writer_from_run_thread("error")
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
        self._event_log.append({"type": event_type, **payload})

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
        if self._passenger_source() == "parquet":
            self._emit_event("parquet_replay", dict(self._parquet_replay_stats))
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
        return raw_surge_bucket(raw_surge)

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
        raw_surge = float(decision_payload.get("raw_surge", 1.0) or 1.0)
        bucket_key = self._raw_surge_bucket(raw_surge)
        bucket = self._runtime_kpi.buckets[bucket_key]
        bucket.request_count += 1
        if accepted:
            bucket.matched_count += 1
            passenger_id = decision_payload.get("passenger_id")
            if passenger_id:
                self._passenger_dispatch_buckets[str(passenger_id)] = bucket_key
        empty_wait = decision_payload.get("empty_wait_time_s")
        if empty_wait is not None:
            self._runtime_kpi.empty_wait_seconds.append(float(empty_wait))

    def _record_cell_bucket_kpi(
        self,
        *,
        bucket_key: str,
        h3_cell: str,
        supply: float | int,
        demand: float | int,
        raw_surge: float,
    ) -> None:
        bucket = self._runtime_kpi.buckets[bucket_key]
        bucket.unique_h3_cells.add(h3_cell)
        bucket.supply_sum += float(supply)
        bucket.demand_sum += float(demand)
        bucket.raw_surge_sum += float(raw_surge)
        bucket.cell_observation_count += 1

    def _cell_bucket_payloads(self) -> list[dict]:
        payloads = []
        for bucket_key, _, _, _ in RAW_SURGE_BUCKETS:
            bucket = self._runtime_kpi.buckets[bucket_key]
            observations = bucket.cell_observation_count
            cells = sorted(bucket.unique_h3_cells or set())
            payloads.append({
                "bucket": bucket_key,
                "unique_cell_count": len(cells),
                "avg_raw_surge": bucket.raw_surge_sum / observations if observations else 0.0,
                "avg_supply": bucket.supply_sum / observations if observations else 0.0,
                "avg_demand": bucket.demand_sum / observations if observations else 0.0,
                "dispatch_request_count": bucket.request_count,
                "sample_h3_cells": cells[:10],
            })
        return payloads

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

    def _prepare_parquet_trip_queue(self, trips: list[dict]) -> None:
        sampled = self._sample_parquet_trips_per_5min(trips)
        source_duration_s = self._parquet_source_duration_s(sampled or trips)
        loop_count = (
            max(1, math.ceil(self._runtime_duration / source_duration_s))
            if source_duration_s > 0 else 1
        )

        queue: list[dict] = []
        for loop_index in range(loop_count):
            offset = loop_index * source_duration_s
            for trip in sampled:
                scheduled = dict(trip)
                scheduled["sim_time"] = float(trip["sim_time"]) + offset
                scheduled["_parquet_loop_index"] = loop_index
                if scheduled["sim_time"] <= self._runtime_duration:
                    queue.append(scheduled)

        queue.sort(key=lambda t: float(t["sim_time"]))
        self._trip_template = sampled
        self._trip_queue = queue
        self._parquet_replay_stats["original_count"] = len(trips)
        self._parquet_replay_stats["scheduled_count_per_loop"] = len(sampled)
        self._parquet_replay_stats["source_duration_s"] = source_duration_s
        self._parquet_replay_stats["loop_count"] = loop_count
        self._parquet_replay_stats["downsampled_count"] = max(0, len(trips) - len(sampled))

    def _sample_parquet_trips_per_5min(self, trips: list[dict]) -> list[dict]:
        target_count = self._passengers_per_5min()
        if target_count <= 0:
            return []
        buckets: dict[int, list[dict]] = defaultdict(list)
        for trip in trips:
            sim_time = float(trip.get("sim_time", 0.0))
            buckets[int(sim_time // PASSENGER_SPAWN_INTERVAL)].append(trip)

        seed = self._runtime_seed
        if seed is None and self.experiment_config is not None:
            seed = self.experiment_config.seed
        if seed is None:
            seed = 0

        sampled: list[dict] = []
        for bucket in sorted(buckets):
            bucket_trips = sorted(buckets[bucket], key=lambda t: float(t.get("sim_time", 0.0)))
            if len(bucket_trips) <= target_count:
                selected = bucket_trips
            else:
                rng = _random.Random(f"{seed}:{bucket}:{target_count}:{len(bucket_trips)}")
                selected = rng.sample(bucket_trips, target_count)
                selected.sort(key=lambda t: float(t.get("sim_time", 0.0)))
            sampled.extend(dict(trip) for trip in selected)
        return sampled

    @staticmethod
    def _parquet_source_duration_s(trips: list[dict]) -> float:
        if not trips:
            return PASSENGER_SPAWN_INTERVAL
        max_sim_time = max(float(trip.get("sim_time", 0.0)) for trip in trips)
        return max(
            PASSENGER_SPAWN_INTERVAL,
            (math.floor(max_sim_time / PASSENGER_SPAWN_INTERVAL) + 1)
            * PASSENGER_SPAWN_INTERVAL,
        )

    def _spawn_passengers(self, sim_time: float) -> None:
        if self._passenger_source() == "parquet":
            while self._trip_queue and self._trip_queue[0]["sim_time"] <= sim_time:
                trip = self._trip_queue.pop(0)
                self._parquet_replay_stats["scheduled_due_count"] += 1
                self._create_passenger_from_trip(trip, sim_time)
        else:
            interval = int(sim_time / PASSENGER_SPAWN_INTERVAL)
            if interval <= self._last_spawn_interval:
                return
            self._last_spawn_interval = interval
            n = _poisson_sample(self._passengers_per_5min())
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

    def _h3_for_edge(self, edge_id: str) -> str | None:
        if edge_id in self._edge_h3_cache:
            return self._edge_h3_cache[edge_id]
        cell = None
        pt = self._get_edge_midpoint(edge_id)
        if pt is not None and self._latlng is not None:
            lat, lng = self._latlng(*pt)
            if math.isfinite(lat) and math.isfinite(lng):
                cell = get_cell(lat, lng)
        self._edge_h3_cache[edge_id] = cell
        return cell

    @staticmethod
    def _format_prof_top(counter: dict[str, int], limit: int = 3) -> str:
        top = sorted(counter.items(), key=lambda kv: -kv[1])[:limit]
        return ",".join(f"{key}:{value}" for key, value in top) or "-"

    def _profile_generated_route(
        self,
        source: str,
        dst_edge: str | None,
        route_edges: list[str] | tuple[str, ...],
    ) -> None:
        if not SIM_PROFILE:
            return
        self._prof_counters["route_generated_total"] += 1
        self._prof_counters[f"route_generated_{source}"] += 1
        try:
            dst_h3 = self._h3_for_edge(dst_edge) if dst_edge else None
        except Exception:
            dst_h3 = None
        if dst_h3:
            self._prof_route_dest_h3[dst_h3] += 1
        hits = sorted(_PROFILE_BOTTLENECK_EDGES.intersection(route_edges))
        if not hits:
            return
        self._prof_counters["route_bottleneck_hit"] += 1
        self._prof_counters[f"route_bottleneck_hit_{source}"] += 1
        self._prof_route_bottleneck_sources[source] += 1
        if dst_h3:
            self._prof_route_bottleneck_dest_h3[dst_h3] += 1
        for edge_id in hits:
            self._prof_route_bottleneck_edges[edge_id] += 1

    def _random_route_from_profiled(
        self,
        source: str,
        current_edge: str,
        attempts: int = 10,
        *,
        min_edges: int = _MIN_ROUTE_EDGES,
        pass_min_edges: bool = False,
    ) -> list[str] | None:
        if attempts == 10 and min_edges == _MIN_ROUTE_EDGES and not pass_min_edges:
            route_edges = self._random_route_from(current_edge)
        elif attempts == 10:
            route_edges = self._random_route_from(current_edge, min_edges=min_edges)
        else:
            route_edges = self._random_route_from(
                current_edge,
                attempts,
                min_edges=min_edges,
            )
        if route_edges:
            self._profile_generated_route(source, route_edges[-1], route_edges)
        return route_edges

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
        if pickup_edge not in self._routable_edges_set:
            self._parquet_replay_stats["skipped_pickup"] += 1
            return
        if dropoff_edge not in self._routable_edges_set:
            self._parquet_replay_stats["skipped_dropoff"] += 1
            return
        traci_mod = _traci_module()
        try:
            route = traci_mod.simulation.findRoute(pickup_edge, dropoff_edge)
        except traci_mod.exceptions.TraCIException:
            self._parquet_replay_stats["route_failed"] += 1
            return
        if not route.edges:
            self._parquet_replay_stats["route_failed"] += 1
            return
        pt = self._get_edge_midpoint(pickup_edge)
        if pt is None:
            self._parquet_replay_stats["missing_pickup_midpoint"] += 1
            return
        x, y = pt
        dt = self._get_edge_midpoint(dropoff_edge)
        if dt is None:
            self._parquet_replay_stats["missing_dropoff_midpoint"] += 1
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
        self._parquet_replay_stats["spawned_count"] += 1

    def _process_manual_requests(self, sim_time: float) -> None:
        with self._lock:
            requests = list(self._manual_requests)
            self._manual_requests.clear()

        for request in requests:
            if request.reply is not None and request.reply.cancelled():
                continue
            if request.kind == "passenger_quote":
                self._complete_manual_request(
                    request,
                    self._build_manual_passenger_quote(request),
                )
            elif request.kind == "passenger":
                result = self._process_manual_passenger_request(request, sim_time)
                self._complete_manual_request(request, result)
            elif request.kind == "taxi":
                result = self._process_manual_taxi_request(request, sim_time)
                self._complete_manual_request(request, result)

    @staticmethod
    def _complete_manual_request(request: ManualCreationRequest, result: dict | None) -> None:
        if request.reply is not None and not request.reply.done():
            request.reply.set_result(result or {"ok": True})

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

    def _manual_request_error(self, code: str, message: str | None = None) -> dict:
        messages = {
            "invalid_request": "Request validation failed",
            "out_of_network": "Selected location is outside the routable network.",
            "no_route_found": "No route found between pickup and dropoff.",
        }
        return {
            "ok": False,
            "error": code,
            "message": message or messages.get(code, "Request failed"),
        }

    def _quote_fare_payload(
        self,
        *,
        expected_fare: int,
        expected_distance_m: float,
        h3_pickup: str | None,
        incentive_limit: int,
        estimated_wait_sec: int,
    ) -> dict:
        system_surge = self._surge_by_h3.get(h3_pickup or "", 1.0)
        uncapped_total = round(expected_fare * system_surge)
        cap_total = expected_fare + max(0, int(incentive_limit))
        total_amount = min(uncapped_total, cap_total)
        effective_surge = total_amount / expected_fare if expected_fare > 0 else 1.0
        return {
            "ok": True,
            "expected_fare": expected_fare,
            "expected_distance_m": int(round(expected_distance_m)),
            "estimated_wait_sec": estimated_wait_sec,
            "surge_multiplier": effective_surge,
            "incentive_limit": max(0, int(incentive_limit)),
            "total_amount": total_amount,
            "system_surge": system_surge,
            "uncapped_total_amount": uncapped_total,
            "incentive_cap_applied": total_amount < uncapped_total,
        }

    def _build_manual_passenger_quote(self, request: ManualCreationRequest) -> dict:
        if (
            request.pickup_lat is None or request.pickup_lng is None
            or request.dropoff_lat is None or request.dropoff_lng is None
        ):
            return self._manual_request_error("invalid_request")

        pickup = self._snap_latlng_to_routable_edge(request.pickup_lat, request.pickup_lng)
        dropoff = self._snap_latlng_to_routable_edge(request.dropoff_lat, request.dropoff_lng)
        if pickup is None or dropoff is None:
            return self._manual_request_error("out_of_network")

        pickup_edge, x, y = pickup
        dropoff_edge, _, _ = dropoff
        traci_mod = _traci_module()
        try:
            route = traci_mod.simulation.findRoute(pickup_edge, dropoff_edge)
        except traci_mod.exceptions.TraCIException:
            return self._manual_request_error("no_route_found")
        if not route.edges:
            return self._manual_request_error("no_route_found")

        lat, lng = self._latlng(x, y)
        h3_pickup = get_cell(lat, lng)
        expected_fare = estimate_fare(route.length)
        return self._quote_fare_payload(
            expected_fare=expected_fare,
            expected_distance_m=route.length,
            h3_pickup=h3_pickup,
            incentive_limit=request.incentive_limit,
            estimated_wait_sec=self._estimate_wait_seconds(lat, lng),
        )

    def _estimate_wait_seconds(self, lat: float, lng: float) -> int:
        with self._lock:
            empty_taxis = [
                v for v in self._state.get("vehicles", [])
                if v.get("state") == "empty" and self._is_taxi_id(str(v.get("id", "")))
            ]
        if not empty_taxis:
            return 0
        nearest_km = min(
            self._haversine_km(lat, lng, float(v["lat"]), float(v["lng"]))
            for v in empty_taxis
        )
        return int(round((nearest_km * 1000.0) / 8.0))

    def _process_manual_passenger_request(
        self,
        request: ManualCreationRequest,
        sim_time: float,
    ) -> dict:
        if (
            request.pickup_lat is None or request.pickup_lng is None
            or request.dropoff_lat is None or request.dropoff_lng is None
        ):
            if request.reply is None:
                self._schedule_ws_event(
                    "broadcast_passenger_creation_failed",
                    request.entity_id,
                    "invalid_request",
                )
            return self._manual_request_error("invalid_request")

        pickup = self._snap_latlng_to_routable_edge(request.pickup_lat, request.pickup_lng)
        dropoff = self._snap_latlng_to_routable_edge(request.dropoff_lat, request.dropoff_lng)
        if pickup is None or dropoff is None:
            if request.reply is None:
                self._schedule_ws_event(
                    "broadcast_passenger_creation_failed",
                    request.entity_id,
                    "out_of_network",
                )
            return self._manual_request_error("out_of_network")

        pickup_edge, x, y = pickup
        dropoff_edge, dx, dy = dropoff
        traci_mod = _traci_module()
        try:
            route = traci_mod.simulation.findRoute(pickup_edge, dropoff_edge)
        except traci_mod.exceptions.TraCIException:
            if request.reply is None:
                self._schedule_ws_event(
                    "broadcast_passenger_creation_failed",
                    request.entity_id,
                    "no_route_found",
                )
            return self._manual_request_error("no_route_found")
        if not route.edges:
            if request.reply is None:
                self._schedule_ws_event(
                    "broadcast_passenger_creation_failed",
                    request.entity_id,
                    "no_route_found",
                )
            return self._manual_request_error("no_route_found")

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
            incentive_limit=max(0, int(request.incentive_limit)),
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
        return {"ok": True, "passenger_id": request.entity_id}

    def _process_manual_taxi_request(
        self,
        request: ManualCreationRequest,
        sim_time: float,
    ) -> dict:
        if request.lat is None or request.lng is None:
            return self._manual_request_error("invalid_request")
        snapped = self._snap_latlng_to_routable_edge(request.lat, request.lng)
        if snapped is None:
            return self._manual_request_error("out_of_network")
        edge_id, x, y = snapped
        route_edges = self._random_route_from_profiled("manual_taxi", edge_id)
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
            return self._manual_request_error("out_of_network")
        self._taxi_states[request.entity_id] = "empty"
        self._taxi_route_len[request.entity_id] = len(route_edges)
        self._taxi_pickup_route_index.pop(request.entity_id, None)
        self._taxi_dropoff_route_index.pop(request.entity_id, None)
        lat, lng = self._latlng(x, y)
        self._taxi_last_dropoff_cells[request.entity_id] = get_cell(lat, lng)
        self._schedule_ws_event("broadcast_taxi_created", request.entity_id, lat, lng)
        return {"ok": True, "taxi_id": request.entity_id}

    def _cancel_timed_out_passengers(self, sim_time: float, sub_results: dict) -> None:
        if PASSENGER_WAIT_TIMEOUT_S <= 0:
            return
        timed_out = [
            passenger
            for passenger in self._passengers.values()
            if passenger.state in ("waiting", "assigned")
            and sim_time - passenger.spawn_time > PASSENGER_WAIT_TIMEOUT_S
        ]
        for passenger in timed_out:
            taxi_id = passenger.taxi_id
            if not taxi_id:
                taxi_id = next(
                    (
                        candidate_taxi
                        for candidate_taxi, passenger_id in self._taxi_targets.items()
                        if passenger_id == passenger.id
                    ),
                    None,
                )
            if taxi_id:
                self._taxi_targets.pop(taxi_id, None)
                self._taxi_states[taxi_id] = "empty"
                self._taxi_dispatch_times.pop(taxi_id, None)
                old_dispatch_id = self._taxi_dispatch_ids.pop(taxi_id, None)
                self._taxi_dispatch_surge.pop(taxi_id, None)
                self._taxi_dispatch_fare_caps.pop(taxi_id, None)
                self._taxi_active_call_details.pop(taxi_id, None)
                self._taxi_pickup_route_index.pop(taxi_id, None)
                self._taxi_dropoff_route_index.pop(taxi_id, None)
                self._taxi_last_extend_route_index.pop(taxi_id, None)
                vals = sub_results.get(taxi_id)
                road_id = vals.get(tc.VAR_ROAD_ID, "") if vals else ""
                if road_id and not road_id.startswith(":") and road_id in self._routable_edges_set:
                    route = self._random_route_from_profiled("post_cancel_idle", road_id)
                    if route:
                        try:
                            traci.vehicle.setRoute(taxi_id, route)
                            self._taxi_route_len[taxi_id] = len(route)
                        except traci.exceptions.TraCIException:
                            self._taxi_route_len[taxi_id] = 0
                    else:
                        self._taxi_route_len[taxi_id] = 0
                if old_dispatch_id:
                    self._push_db_event({"type": "dispatch_timeout", "id": old_dispatch_id})
            self._passengers.pop(passenger.id, None)
            self._clear_rejection_history_for_passenger(passenger.id)
            self._passenger_dispatch_buckets.pop(passenger.id, None)
            self._schedule_ws_event("broadcast_passenger_cancelled", passenger.id, "timeout")
            self._emit_event("passenger_cancelled", {
                "sim_time": sim_time,
                "passenger_id": passenger.id,
                "reason": "timeout",
            })

    def _current_driver_features(
        self,
        *,
        candidate: Passenger,
        sim_time: float,
        sub_results: dict,
        current_veh_id: str,
        current_route,
    ) -> tuple[float, float, float] | None:
        if not candidate.h3_dropoff:
            return None
        vals = sub_results.get(current_veh_id)
        if vals is None or not getattr(current_route, "edges", None):
            return None
        call_dt = SIM_BASE_DATETIME + timedelta(seconds=sim_time)
        trip_miles = candidate.expected_distance_m / 1609.344
        D_pu_miles = current_route.length / 1609.344
        x, y = vals[tc.VAR_POSITION]
        lat, lng = self._latlng(x, y)
        last_cell = self._taxi_last_dropoff_cells.get(current_veh_id) or get_cell(lat, lng)
        features = _acceptance_features(
            last_dropoff_cell=last_cell,
            dropoff_cell=candidate.h3_dropoff,
            call_datetime=call_dt,
            D_pu=D_pu_miles,
            trip_distance=trip_miles,
            pickup_cell=candidate.h3_pickup,
        )
        return features.dV_without_fare, D_pu_miles, features.t_pu

    @staticmethod
    def _fare_cap_for_passenger(passenger: Passenger) -> int | None:
        if passenger.incentive_limit is None:
            return None
        return passenger.expected_fare + max(0, int(passenger.incentive_limit))

    @staticmethod
    def _apply_fare_cap(
        *,
        meter_fare: int,
        surge: float,
        fare_cap: int | None,
    ) -> dict:
        uncapped_fare = round(meter_fare * surge)
        if fare_cap is None:
            final_fare = uncapped_fare
        else:
            final_fare = min(uncapped_fare, fare_cap)
        effective_surge = final_fare / meter_fare if meter_fare > 0 else 1.0
        return {
            "meter_fare": meter_fare,
            "uncapped_fare": uncapped_fare,
            "fare": final_fare,
            "effective_surge": effective_surge,
            "incentive_cap_applied": final_fare < uncapped_fare,
        }

    def _fare_result_for_accumulator(
        self,
        passenger: Passenger,
        accum: TripAccumulator,
    ) -> dict:
        return self._apply_fare_cap(
            meter_fare=calculate_meter_fare(accum),
            surge=accum.surge,
            fare_cap=accum.fare_cap,
        )

    def _store_call_detail(
        self,
        *,
        taxi_id: str,
        passenger: Passenger,
        final_fare: int,
    ) -> None:
        base_fare = passenger.expected_fare
        destination_surge = self._surge_by_h3.get(passenger.h3_dropoff or "", 1.0)
        self._taxi_active_call_details[taxi_id] = {
            "taxi_id": taxi_id,
            "passenger_id": passenger.id,
            "pickup": {"lat": passenger.lat, "lng": passenger.lng},
            "dropoff": {"lat": passenger.dropoff_lat, "lng": passenger.dropoff_lng},
            "incentive": final_fare,
            "destination_surge": destination_surge,
            "incentive_breakdown": {
                "base_fare": base_fare,
                "passenger_incentive": max(0, final_fare - base_fare),
                "surge_bonus": 0,
            },
        }

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
        bucket = self._raw_surge_bucket(raw_surge)
        target_matching_rate = self._target_matching_rates[bucket]
        required_fare_usd = None
        calculated_surge = raw_surge
        pricing_driver_count = None

        if self.experiment_config is not None and base_fare_usd > 0:
            current_features = self._current_driver_features(
                candidate=candidate,
                sim_time=sim_time,
                sub_results=sub_results,
                current_veh_id=current_veh_id,
                current_route=current_route,
            )
            if current_features is not None:
                dV_without_fare, D_pu_miles, T_pu = current_features
                pricing_driver_count = 1
                required_fare_usd = _required_fare_for_target_features(
                    target_p=target_matching_rate,
                    dV_without_fare=dV_without_fare,
                    D_pu=D_pu_miles,
                    T_pu=T_pu,
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

        uncapped_fare_cents = round(candidate.expected_fare * final_surge)
        fare_cap_cents = self._fare_cap_for_passenger(candidate)
        final_fare_cents = (
            min(uncapped_fare_cents, fare_cap_cents)
            if fare_cap_cents is not None else uncapped_fare_cents
        )
        effective_surge = (
            final_fare_cents / candidate.expected_fare
            if candidate.expected_fare > 0 else final_surge
        )
        final_fare_usd = final_fare_cents / 100.0
        return {
            "base_fare_usd": base_fare_usd,
            "raw_surge": raw_surge,
            "bucket": bucket,
            "target_matching_rate": target_matching_rate,
            "required_fare_usd": required_fare_usd,
            "calculated_surge": calculated_surge,
            "system_surge": final_surge,
            "final_surge": effective_surge,
            "final_fare_usd": final_fare_usd,
            "final_fare_cents": final_fare_cents,
            "uncapped_fare_cents": uncapped_fare_cents,
            "quote_cap_fare_cents": fare_cap_cents,
            "incentive_cap_applied": (
                fare_cap_cents is not None and final_fare_cents < uncapped_fare_cents
            ),
            "surge_clamped": abs(calculated_surge - final_surge) > 1e-6,
            "pricing_driver_count": pricing_driver_count,
        }

    def _prune_dispatch_cooldowns(self, sim_time: float) -> None:
        self._taxi_dispatch_cooldown_until = {
            taxi_id: until
            for taxi_id, until in self._taxi_dispatch_cooldown_until.items()
            if until > sim_time
        }
        self._pair_dispatch_cooldown_until = {
            pair: until
            for pair, until in self._pair_dispatch_cooldown_until.items()
            if until > sim_time
        }
        self._rejected_dispatch_pairs = {
            pair for pair in self._rejected_dispatch_pairs
            if pair in self._pair_dispatch_cooldown_until
        }

    def _taxi_on_dispatch_cooldown(self, taxi_id: str, sim_time: float) -> bool:
        return self._taxi_dispatch_cooldown_until.get(taxi_id, 0.0) > sim_time

    def _pair_on_dispatch_cooldown(self, taxi_id: str, passenger_id: str, sim_time: float) -> bool:
        return self._pair_dispatch_cooldown_until.get((taxi_id, passenger_id), 0.0) > sim_time

    def _set_dispatch_cooldowns(self, taxi_id: str, passenger_id: str, sim_time: float) -> None:
        if TAXI_DISPATCH_COOLDOWN_S > 0:
            self._taxi_dispatch_cooldown_until[taxi_id] = sim_time + TAXI_DISPATCH_COOLDOWN_S
        if PAIR_DISPATCH_COOLDOWN_S > 0:
            self._pair_dispatch_cooldown_until[(taxi_id, passenger_id)] = (
                sim_time + PAIR_DISPATCH_COOLDOWN_S
            )

    def _record_dispatch_rejection(self, taxi_id: str, passenger_id: str) -> None:
        self._rejected_dispatch_pairs.add((taxi_id, passenger_id))

    def _is_rejected_dispatch_pair(self, taxi_id: str, passenger_id: str) -> bool:
        return (taxi_id, passenger_id) in self._rejected_dispatch_pairs

    def _clear_rejection_history_for_passenger(self, passenger_id: str) -> None:
        self._rejected_dispatch_pairs = {
            pair for pair in self._rejected_dispatch_pairs if pair[1] != passenger_id
        }
        self._pair_dispatch_cooldown_until = {
            pair: until
            for pair, until in self._pair_dispatch_cooldown_until.items()
            if pair[1] != passenger_id
        }

    def _update_taxi_states(self, sim_time: float, sub_results: dict) -> list[dict]:
        fare_updates: list[dict] = []
        self._prune_dispatch_cooldowns(sim_time)
        waiting_passengers = [p for p in self._passengers.values() if p.state == "waiting"]

        for veh_id, vals in sub_results.items():
            if not self._is_taxi_id(veh_id):
                continue
            state = self._taxi_states.get(veh_id, "empty")
            tx, ty = vals[tc.VAR_POSITION]
            road_id = vals.get(tc.VAR_ROAD_ID, "")

            # 단계 1 — 배차: 거리 기준 상위 K명 검토 후 수락 확률로 배차
            if state == "empty" and waiting_passengers:
                if self._taxi_on_dispatch_cooldown(veh_id, sim_time):
                    continue
                if (
                    not road_id
                    or road_id.startswith(":")
                    or road_id not in self._routable_edges_set
                ):
                    continue
                candidates = heapq.nsmallest(
                    DISPATCH_MAX_CANDIDATES, waiting_passengers,
                    key=lambda p: (p.x - tx) ** 2 + (p.y - ty) ** 2,
                )
                for candidate in candidates:
                    if (
                        self._is_rejected_dispatch_pair(veh_id, candidate.id)
                        or self._pair_on_dispatch_cooldown(veh_id, candidate.id, sim_time)
                    ):
                        continue
                    try:
                        route = traci.simulation.findRoute(road_id, candidate.pickup_edge)
                        if not route.edges:
                            continue
                    except traci.exceptions.TraCIException:
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
                        "pickup_h3": candidate.h3_pickup,
                        "bucket": self._raw_surge_bucket(1.0),
                        "target_p": None,
                        "target_matching_rate": None,
                        "p_actual": 1.0,
                        "base_fare_usd": candidate.expected_fare / 100.0,
                        "surge": 1.0,
                        "raw_surge": 1.0,
                        "calculated_surge": 1.0,
                        "final_surge": 1.0,
                        "system_surge": 1.0,
                        "final_fare_usd": candidate.expected_fare / 100.0,
                        "final_fare_cents": candidate.expected_fare,
                        "uncapped_fare_cents": candidate.expected_fare,
                        "quote_cap_fare_cents": self._fare_cap_for_passenger(candidate),
                        "incentive_cap_applied": False,
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
                        fare_usd = pricing["final_fare_usd"]
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
                        accepted = _random.random() < p
                        target_matching_rate = pricing["target_matching_rate"]
                        decision_payload.update({
                            "p_actual": p,
                            "target_p": target_matching_rate,
                            "target_matching_rate": target_matching_rate,
                            "base_fare_usd": pricing["base_fare_usd"],
                            "surge": pricing["final_surge"],
                            "raw_surge": pricing["raw_surge"],
                            "bucket": pricing["bucket"],
                            "calculated_surge": pricing["calculated_surge"],
                            "final_surge": pricing["final_surge"],
                            "system_surge": pricing["system_surge"],
                            "final_fare_usd": pricing["final_fare_usd"],
                            "final_fare_cents": pricing["final_fare_cents"],
                            "uncapped_fare_cents": pricing["uncapped_fare_cents"],
                            "quote_cap_fare_cents": pricing["quote_cap_fare_cents"],
                            "incentive_cap_applied": pricing["incentive_cap_applied"],
                            "required_fare_usd": pricing["required_fare_usd"],
                            "surge_clamped": pricing["surge_clamped"],
                            "pricing_driver_count": pricing["pricing_driver_count"],
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
                        self._emit_event("dispatch_decision", {**decision_payload, "accepted": False})
                        self._push_db_event({**dispatch_payload, **decision_payload, "accepted": False})
                        continue

                    dispatch_edges = list(route.edges)
                    pickup_route_index = len(dispatch_edges) - 1
                    # Buffer past pickup edge: prevents network exit if taxi traverses
                    # pickup edge in one step before our pickup detection fires.
                    buf = self._random_route_from_profiled("pickup_buffer", candidate.pickup_edge)
                    if buf and len(buf) > 1:
                        dispatch_edges = dispatch_edges + buf[1:]
                    traci.vehicle.setRoute(veh_id, dispatch_edges)
                    self._taxi_route_len[veh_id] = len(dispatch_edges)
                    self._taxi_last_extend_route_index.pop(veh_id, None)
                    candidate.state = "assigned"
                    candidate.taxi_id = veh_id
                    self._clear_rejection_history_for_passenger(candidate.id)
                    waiting_passengers.remove(candidate)
                    self._taxi_targets[veh_id] = candidate.id
                    self._taxi_states[veh_id] = "dispatched"
                    self._taxi_dispatch_times[veh_id] = sim_time
                    self._taxi_dispatch_ids[veh_id] = dispatch_id
                    self._taxi_dispatch_surge[veh_id] = decision_payload["final_surge"]
                    if decision_payload["quote_cap_fare_cents"] is not None:
                        self._taxi_dispatch_fare_caps[veh_id] = decision_payload["quote_cap_fare_cents"]
                    self._store_call_detail(
                        taxi_id=veh_id,
                        passenger=candidate,
                        final_fare=decision_payload["final_fare_cents"],
                    )
                    self._taxi_pickup_route_index[veh_id] = pickup_route_index
                    self._taxi_dropoff_route_index.pop(veh_id, None)
                    previous_dropoff_time = self._taxi_previous_dropoff_times.get(veh_id)
                    if previous_dropoff_time is not None:
                        # 첫 승객 전 대기시간은 정의상 제외하고, 하차 이후 다음 수락까지의 search time만 기록한다.
                        decision_payload["empty_wait_time_s"] = sim_time - previous_dropoff_time
                    self._record_dispatch_kpi(decision_payload, accepted=True)
                    self._emit_event("dispatch_decision", {**decision_payload, "accepted": True})
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
                    self._set_dispatch_cooldowns(veh_id, passenger.id, sim_time)
                    self._record_dispatch_rejection(veh_id, passenger.id)
                    passenger.state = "waiting"
                    passenger.taxi_id = None
                    del self._taxi_targets[veh_id]
                    self._taxi_states[veh_id] = "empty"
                    self._taxi_last_extend_route_index.pop(veh_id, None)
                    self._taxi_pickup_route_index.pop(veh_id, None)
                    self._taxi_dropoff_route_index.pop(veh_id, None)
                    self._taxi_dispatch_times.pop(veh_id, None)
                    self._taxi_dispatch_surge.pop(veh_id, None)
                    self._taxi_dispatch_fare_caps.pop(veh_id, None)
                    self._taxi_active_call_details.pop(veh_id, None)
                    waiting_passengers.append(passenger)
                    old_dispatch_id = self._taxi_dispatch_ids.pop(veh_id, None)
                    if old_dispatch_id:
                        self._push_db_event({"type": "dispatch_timeout", "id": old_dispatch_id})
                    continue
                route_index = vals.get(tc.VAR_ROUTE_INDEX)
                pickup_index = self._taxi_pickup_route_index.get(veh_id)
                # 픽업 엣지를 한 스텝에 통째로 지나치는 overshoot 안전망.
                # strict '>' — 픽업 엣지에 막 진입한 순간(==)이 아니라 지나친 경우만.
                overshot = (
                    pickup_index is not None
                    and route_index is not None
                    and route_index > pickup_index
                )
                # 같은 엣지라도 엣지 길이만큼 떨어져 있을 수 있으므로, 픽업 엣지 위에서는
                # 실제 근거리일 때만 픽업한다. (overshoot일 때만 거리 조건을 우회)
                near_pickup = (
                    (tx - passenger.x) ** 2 + (ty - passenger.y) ** 2
                    <= PICKUP_THRESHOLD_M ** 2
                )
                # 픽업은 반드시 실제 근거리(near_pickup)에서만. overshoot는 픽업 엣지를 한
                # 스텝에 지나친 경우의 안전망이지만, 그 역시 근거리일 때만 유효하다.
                # (overshoot 단독 허용 시, 먼 택시가 route_index>pickup_index만으로 km 밖에서
                #  즉시 픽업되어 승객이 생성 직후 사라지는 버그가 있었다.)
                if near_pickup and (road_id == passenger.pickup_edge or overshot):
                    if not road_id or road_id.startswith(":") or road_id not in self._routable_edges_set:
                        continue
                    try:
                        route = traci.simulation.findRoute(road_id, passenger.dropoff_edge)
                        if not route.edges:
                            continue
                        trip_edges = list(route.edges)
                        dropoff_route_index = len(trip_edges) - 1
                        # Buffer past dropoff edge: prevents network exit if taxi traverses
                        # dropoff edge in one step before our dropoff detection fires.
                        buf = self._random_route_from_profiled("dropoff_buffer", passenger.dropoff_edge)
                        if buf and len(buf) > 1:
                            trip_edges = trip_edges + buf[1:]
                        traci.vehicle.setRoute(veh_id, trip_edges)
                    except traci.exceptions.TraCIException:
                        continue
                    passenger.state = "picked_up"
                    passenger.taxi_id = veh_id
                    self._record_passenger_boarded_kpi(passenger, sim_time)
                    self._taxi_states[veh_id] = "occupied"
                    self._taxi_last_extend_route_index.pop(veh_id, None)
                    self._taxi_pickup_route_index.pop(veh_id, None)
                    self._taxi_dropoff_route_index[veh_id] = dropoff_route_index
                    if overshot and road_id != passenger.pickup_edge and SIM_PROFILE:
                        self._prof_counters["pickup_index_reached"] += 1
                    dispatch_time = self._taxi_dispatch_times.pop(veh_id, sim_time)
                    dispatch_surge = self._taxi_dispatch_surge.pop(veh_id, 1.0)
                    dispatch_fare_cap = self._taxi_dispatch_fare_caps.pop(veh_id, None)
                    self._active_trips[veh_id] = TripAccumulator(
                        passenger_id=passenger_id,
                        pickup_sim_time=sim_time,
                        dispatch_id=self._taxi_dispatch_ids.get(veh_id),
                        dispatch_sim_time=dispatch_time,
                        last_distance_snapshot=traci.vehicle.getDistance(veh_id),
                        surge=dispatch_surge,
                        fare_cap=dispatch_fare_cap,
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
                    fare_result = self._fare_result_for_accumulator(passenger, accum)
                    meter_fare = fare_result["meter_fare"]
                    fare = fare_result["fare"]
                    fare_updates.append({
                        "passenger_id": passenger_id,
                        "taxi_id": veh_id,
                        "meter_fare": meter_fare,
                        "uncapped_fare": fare_result["uncapped_fare"],
                        "fare": fare,
                        "incentive_cap_applied": fare_result["incentive_cap_applied"],
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
                        "uncapped_fare": fare_result["uncapped_fare"],
                        "fare": fare,
                        "incentive_cap_applied": fare_result["incentive_cap_applied"],
                        "surge": accum.surge,
                        "expected_fare": passenger.expected_fare,
                        "completion": "trip_timeout",
                    })
                    self._record_history_dropoff(sim_time, dropoff_h3)
                    self._taxi_last_dropoff_cells[veh_id] = dropoff_h3
                    del self._passengers[passenger_id]
                    self._clear_rejection_history_for_passenger(passenger_id)
                    del self._active_trips[veh_id]
                    del self._taxi_targets[veh_id]
                    self._taxi_states[veh_id] = "empty"
                    self._taxi_last_extend_route_index.pop(veh_id, None)
                    self._taxi_pickup_route_index.pop(veh_id, None)
                    self._taxi_dropoff_route_index.pop(veh_id, None)
                    self._taxi_dispatch_ids.pop(veh_id, None)
                    self._taxi_dispatch_surge.pop(veh_id, None)
                    self._taxi_dispatch_fare_caps.pop(veh_id, None)
                    self._taxi_active_call_details.pop(veh_id, None)
                    route = self._random_route_from_profiled("post_timeout_idle", road_id)
                    if route:
                        traci.vehicle.setRoute(veh_id, route)
                        self._taxi_route_len[veh_id] = len(route)
                    else:
                        # 픽업 시 붙인 버퍼가 하차 지점 너머까지 이어져 있다. route_len=0으로 두어
                        # 다음 스텝에 _extend_vehicle_routes가 즉시 연장을 시도하게 한다.
                        self._taxi_route_len[veh_id] = 0
                    continue
                route_index = vals.get(tc.VAR_ROUTE_INDEX)
                dropoff_index = self._taxi_dropoff_route_index.get(veh_id)
                dropoff_reached_by_index = (
                    dropoff_index is not None
                    and route_index is not None
                    and route_index >= dropoff_index
                )
                if road_id == passenger.dropoff_edge or dropoff_reached_by_index:
                    accum = self._active_trips[veh_id]
                    dropoff_h3 = passenger.h3_dropoff or get_cell(passenger.dropoff_lat, passenger.dropoff_lng)
                    fare_result = self._fare_result_for_accumulator(passenger, accum)
                    meter_fare = fare_result["meter_fare"]
                    fare = fare_result["fare"]
                    fare_updates.append({
                        "passenger_id": passenger_id,
                        "taxi_id": veh_id,
                        "meter_fare": meter_fare,
                        "uncapped_fare": fare_result["uncapped_fare"],
                        "fare": fare,
                        "incentive_cap_applied": fare_result["incentive_cap_applied"],
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
                        "uncapped_fare": fare_result["uncapped_fare"],
                        "fare": fare,
                        "incentive_cap_applied": fare_result["incentive_cap_applied"],
                        "surge": accum.surge,
                        "expected_fare": passenger.expected_fare,
                        "completion": "normal",
                    })
                    self._record_history_dropoff(sim_time, dropoff_h3)
                    self._taxi_last_dropoff_cells[veh_id] = dropoff_h3
                    del self._passengers[passenger_id]
                    self._clear_rejection_history_for_passenger(passenger_id)
                    del self._active_trips[veh_id]
                    del self._taxi_targets[veh_id]
                    self._taxi_states[veh_id] = "empty"
                    self._taxi_last_extend_route_index.pop(veh_id, None)
                    self._taxi_pickup_route_index.pop(veh_id, None)
                    self._taxi_dropoff_route_index.pop(veh_id, None)
                    if dropoff_reached_by_index and road_id != passenger.dropoff_edge and SIM_PROFILE:
                        self._prof_counters["dropoff_index_reached"] += 1
                    self._taxi_dispatch_ids.pop(veh_id, None)
                    self._taxi_dispatch_surge.pop(veh_id, None)
                    self._taxi_dispatch_fare_caps.pop(veh_id, None)
                    self._taxi_active_call_details.pop(veh_id, None)
                    route = self._random_route_from_profiled("post_dropoff_idle", road_id)
                    if route:
                        traci.vehicle.setRoute(veh_id, route)
                        self._taxi_route_len[veh_id] = len(route)
                    else:
                        # 하차 후 경로를 못 찾으면 route_len=0으로 두어 다음 스텝에 즉시 재연장.
                        self._taxi_route_len[veh_id] = 0

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
                    self._set_dispatch_cooldowns(veh_id, passenger.id, sim_time)
                    self._record_dispatch_rejection(veh_id, passenger.id)
                    passenger.state = "waiting"
                    passenger.taxi_id = None
                    waiting_passengers.append(passenger)
            self._taxi_states[veh_id] = "empty"
            self._taxi_last_extend_route_index.pop(veh_id, None)
            self._taxi_pickup_route_index.pop(veh_id, None)
            self._taxi_dropoff_route_index.pop(veh_id, None)
            self._taxi_dispatch_times.pop(veh_id, None)
            self._taxi_dispatch_ids.pop(veh_id, None)
            self._taxi_dispatch_surge.pop(veh_id, None)
            self._taxi_dispatch_fare_caps.pop(veh_id, None)
            self._taxi_active_call_details.pop(veh_id, None)

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
                if passenger_id:
                    self._clear_rejection_history_for_passenger(passenger_id)
                if passenger:
                    dropoff_h3 = passenger.h3_dropoff or get_cell(passenger.dropoff_lat, passenger.dropoff_lng)
                    fare_result = self._fare_result_for_accumulator(passenger, accum)
                    meter_fare = fare_result["meter_fare"]
                    fare = fare_result["fare"]
                    fare_updates.append({
                        "passenger_id": passenger_id,
                        "taxi_id": taxi_id,
                        "meter_fare": meter_fare,
                        "uncapped_fare": fare_result["uncapped_fare"],
                        "fare": fare,
                        "incentive_cap_applied": fare_result["incentive_cap_applied"],
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
                        "uncapped_fare": fare_result["uncapped_fare"],
                        "fare": fare,
                        "incentive_cap_applied": fare_result["incentive_cap_applied"],
                        "surge": accum.surge,
                        "expected_fare": passenger.expected_fare,
                        "completion": "sumo_removed",
                    })
                    self._record_history_dropoff(sim_time, dropoff_h3)
                    self._taxi_last_dropoff_cells[taxi_id] = dropoff_h3
                self._active_trips.pop(taxi_id, None)
                self._taxi_states.pop(taxi_id, None)
                self._taxi_pickup_route_index.pop(taxi_id, None)
                self._taxi_dropoff_route_index.pop(taxi_id, None)
                self._taxi_dispatch_times.pop(taxi_id, None)
                self._taxi_dispatch_ids.pop(taxi_id, None)
                self._taxi_dispatch_surge.pop(taxi_id, None)
                self._taxi_dispatch_fare_caps.pop(taxi_id, None)
                self._taxi_active_call_details.pop(taxi_id, None)
                continue
            dist = vals[tc.VAR_DISTANCE]
            delta = dist - accum.last_distance_snapshot
            if delta > 0:
                accum.distance_m += delta
            accum.last_distance_snapshot = dist
            speed = _valid_speed_mps(vals.get(tc.VAR_SPEED))
            if speed is not None and speed < SPEED_THRESHOLD_MPS:
                step_length = (
                    self.experiment_config.step_length
                    if self.experiment_config
                    else self._runtime_simulation_speed / FRAME_RATE
                )
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
        """Place configured background cars and taxis on the network at t=0."""
        edges = self._routable_edges

        # Define a yellow taxi vehicle type based on the default
        traci.vehicletype.copy("DEFAULT_VEHTYPE", "taxi")
        traci.vehicletype.setColor("taxi", (255, 200, 0, 255))

        route_index = 0
        for i in range(self._runtime_background_vehicle_count):
            route_edges = self._random_route(edges, profile_source="init_bg")
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
            route_edges = self._random_route(edges, profile_source="init_taxi")
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

    def _extend_vehicle_routes(self, sub_results: dict, sim_time: float, *, extend_bg_routes: bool = True) -> None:
        """경로 끝에 근접한 bg 차량·empty/dispatched 택시에 새 경로를 이어 붙여 arrival 제거를 막는다.

        구독값 route_index와 저장해 둔 경로 길이로 '남은 엣지 수'를 계산한다. 이 값은 단조
        증가하므로 한 번 임계값(_ROUTE_EXTEND_REMAINING)을 넘으면 매 스텝 참이 되어, 짧은
        엣지를 한 스텝에 통과해도 연장이 누락되지 않는다. 연장에 실패하면 route_len을 그대로
        두므로 다음 스텝에 자동으로 재시도된다(임계값이 계속 참이기 때문)."""
        bg_extension_attempts = 0
        for veh_id, vals in sub_results.items():
            road_id = vals.get(tc.VAR_ROAD_ID, "")
            if not road_id or road_id.startswith(":"):
                continue  # 내부 정션 엣지에서는 경로 산출 불가 — 다음 스텝에 재시도
            route_index = vals.get(tc.VAR_ROUTE_INDEX)
            if route_index is None or route_index < 0:
                continue  # 미구독이거나 아직 미출발(-1)한 차량은 건너뜀

            if veh_id.startswith("bg_"):
                if not extend_bg_routes:
                    continue
                if sim_time < self._bg_extend_cooldown.get(veh_id, 0.0):
                    if SIM_PROFILE:
                        self._prof_counters["skip_bg_cooldown"] += 1
                    continue  # cooldown 중 — 정체로 정지한 차량의 무의미한 재시도 차단
                route_len = self._bg_route_len.get(veh_id)
                if route_len is None or route_len - 1 - route_index > _BG_ROUTE_EXTEND_REMAINING:
                    continue
                speed = _valid_speed_mps(vals.get(tc.VAR_SPEED))
                if speed is None:
                    speed = _BG_ROUTE_EXTENSION_MIN_SPEED_MPS + 1.0
                if speed <= _BG_ROUTE_EXTENSION_MIN_SPEED_MPS:
                    if SIM_PROFILE:
                        self._prof_counters["skip_bg_stopped"] += 1
                    continue
                if bg_extension_attempts >= _BG_ROUTE_EXTENSION_MAX_PER_TICK:
                    if SIM_PROFILE:
                        self._prof_counters["skip_bg_budget"] += 1
                    continue
                bg_extension_attempts += 1
                if SIM_PROFILE:
                    self._prof_counters["trigger_bg"] += 1
                new_route = self._random_route_from(
                    road_id,
                    min_edges=_BG_EXTENSION_MIN_ROUTE_EDGES,
                )
                if new_route:
                    self._profile_generated_route("bg_extension", new_route[-1], new_route)
                if new_route:
                    try:
                        traci.vehicle.setRoute(veh_id, new_route)
                        self._bg_route_len[veh_id] = len(new_route)
                        self._bg_extend_cooldown[veh_id] = sim_time + _BG_EXTENSION_COOLDOWN_S
                    except traci.exceptions.TraCIException:
                        pass  # route_len 유지 → 다음 스텝 재시도

            elif self._is_taxi_id(veh_id):
                # empty·dispatched 택시만 연장 (occupied 경로는 트립 로직이 관리)
                taxi_state = self._taxi_states.get(veh_id, "empty")
                if taxi_state not in ("empty", "dispatched"):
                    self._taxi_last_extend_route_index.pop(veh_id, None)
                    continue
                if taxi_state != "dispatched":
                    self._taxi_last_extend_route_index.pop(veh_id, None)
                if sim_time < self._taxi_extend_cooldown.get(veh_id, 0.0):
                    continue
                route_len = self._taxi_route_len.get(veh_id)
                remaining_edges = route_len - 1 - route_index if route_len is not None else None
                extend_remaining = (
                    _DISPATCHED_TAXI_ROUTE_EXTEND_REMAINING
                    if taxi_state == "dispatched"
                    else _EMPTY_TAXI_ROUTE_EXTEND_REMAINING
                )
                if remaining_edges is None or remaining_edges > extend_remaining:
                    continue
                critical_remaining_edges = (
                    _DISPATCHED_TAXI_CRITICAL_REMAINING_EDGES
                    if taxi_state == "dispatched"
                    else _TAXI_CRITICAL_REMAINING_EDGES
                )
                speed = _valid_speed_mps(vals.get(tc.VAR_SPEED))
                if speed is None:
                    speed = _TAXI_ROUTE_EXTENSION_MIN_SPEED_MPS + 1.0
                if (
                    speed <= _TAXI_ROUTE_EXTENSION_MIN_SPEED_MPS
                    and remaining_edges > critical_remaining_edges
                ):
                    if SIM_PROFILE:
                        self._prof_counters["skip_taxi_stopped"] += 1
                    continue
                if taxi_state == "dispatched" and remaining_edges > critical_remaining_edges:
                    previous_route_index = self._taxi_last_extend_route_index.get(veh_id)
                    self._taxi_last_extend_route_index[veh_id] = route_index
                    if previous_route_index == route_index:
                        if SIM_PROFILE:
                            self._prof_counters["skip_taxi_static_index"] += 1
                            self._prof_counters[f"skip_taxi_static_index_{taxi_state}"] += 1
                        continue
                if SIM_PROFILE:
                    self._prof_counters["trigger_taxi"] += 1
                    self._prof_counters[f"trigger_taxi_{taxi_state}"] += 1
                if taxi_state == "empty":
                    new_route = self._random_route_from_profiled(
                        "taxi_empty_extension",
                        road_id,
                        min_edges=_TAXI_EXTENSION_MIN_ROUTE_EDGES,
                        pass_min_edges=True,
                    )
                else:
                    new_route = self._random_route_from(
                        road_id,
                        min_edges=_TAXI_EXTENSION_MIN_ROUTE_EDGES,
                    )
                    if new_route:
                        self._profile_generated_route(
                            f"taxi_{taxi_state}_extension",
                            new_route[-1],
                            new_route,
                        )
                if new_route:
                    try:
                        traci.vehicle.setRoute(veh_id, new_route)
                        self._taxi_route_len[veh_id] = len(new_route)
                        cooldown_s = (
                            _TAXI_EXTENSION_COOLDOWN_S
                            if taxi_state == "dispatched"
                            else _EMPTY_TAXI_EXTENSION_COOLDOWN_S
                        )
                        self._taxi_extend_cooldown[veh_id] = sim_time + cooldown_s
                        self._taxi_last_extend_route_index.pop(veh_id, None)
                    except traci.exceptions.TraCIException:
                        pass  # route_len 유지 → 다음 스텝 재시도

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

    def _random_route_from(
        self,
        current_edge: str,
        attempts: int = 10,
        *,
        min_edges: int = _MIN_ROUTE_EDGES,
    ) -> list[str] | None:
        """current_edge에서 시작하는 경로를 반환. _MIN_ROUTE_EDGES개 이상을 우선 시도하고
        점차 길이를 낮춰 2개까지 폴백, 모두 실패 시 None."""
        if SIM_PROFILE:
            self._prof_counters["rrf_calls"] += 1
        if (
            not current_edge
            or current_edge.startswith(":")
            or current_edge not in self._routable_edges_set
        ):
            if SIM_PROFILE:
                self._prof_counters["rrf_invalid_current_edge"] += 1
            return None
        if SIM_PROFILE:
            self._prof_route_from_edges[current_edge] += 1
        edges = self._routable_edges
        weights = self._edge_weights if len(self._edge_weights) == len(edges) else None

        def _pick() -> str:
            return _random.choices(edges, weights=weights, k=1)[0] if weights else _random.choice(edges)

        for min_len in range(min_edges, 1, -1):
            for _ in range(attempts):
                dst = _pick()
                if dst == current_edge:
                    continue
                try:
                    if SIM_PROFILE:
                        self._prof_counters["findRoute_calls"] += 1
                    result = traci.simulation.findRoute(current_edge, dst)
                    if len(result.edges) >= min_len:
                        return list(result.edges)
                except traci.exceptions.TraCIException:
                    continue
        if SIM_PROFILE:
            self._prof_counters["rrf_none"] += 1
        return None

    def _random_route(
        self,
        edges: list[str],
        attempts: int = 10,
        *,
        profile_source: str = "unknown",
    ) -> list[str]:
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
                        route_edges = list(result.edges)
                        self._profile_generated_route(profile_source, dst, route_edges)
                        return route_edges
                except traci.exceptions.TraCIException:
                    continue
        fallback_edge = _random.choice(edges)
        route_edges = [fallback_edge]
        self._profile_generated_route(profile_source, fallback_edge, route_edges)
        return route_edges

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
            speed = _valid_speed_mps(vals.get(tc.VAR_SPEED))
            if speed is None:
                speed = 0.0
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

    def _capture_grid_counts(self, sub_results: dict) -> tuple[dict[str, int], dict[str, int]]:
        """Return lightweight (grid_supply, grid_demand) for surge calculation."""
        grid_supply: dict[str, int] = defaultdict(int)
        grid_demand: dict[str, int] = defaultdict(int)

        for veh_id, vals in sub_results.items():
            if veh_id.startswith("bg_"):
                continue
            if self._taxi_states.get(veh_id, "empty") != "empty":
                continue
            x, y = vals[tc.VAR_POSITION]
            lat, lng = self._latlng(x, y)
            if math.isfinite(lat) and math.isfinite(lng):
                grid_supply[get_cell(lat, lng)] += 1

        for p in self._passengers.values():
            if p.state in ("waiting", "assigned") and p.h3_pickup:
                grid_demand[p.h3_pickup] += 1

        return grid_supply, grid_demand

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
            if self._prediction_demand_provider is None:
                raise RuntimeError("predicted demand source requires a prediction demand provider")
            demand_for_surge = self._prediction_demand_provider.demand_by_h3(
                SIM_BASE_DATETIME + timedelta(seconds=sim_time),
                mode=self._provider_prediction_mode(config.prediction_mode),
                actual_demand=grid_demand,
            )

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
            bucket = self._raw_surge_bucket(raw_surge)
            target_matching_rate = self._target_matching_rates[bucket]
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
            self._record_cell_bucket_kpi(
                bucket_key=bucket,
                h3_cell=cell,
                supply=grid_supply.get(cell, 0),
                demand=selected_demand,
                raw_surge=raw_surge,
            )
            surge_cells.append({
                "h3": cell,
                "bucket": bucket,
                "supply": grid_supply.get(cell, 0),
                "demand": selected_demand,
                "actual_demand": actual_demand,
                "surge": surge,
                "raw_surge": raw_surge,
                "target_matching_rate": target_matching_rate,
                "center": {"lat": lat_c, "lng": lng_c},
            })
            if self.experiment_config is not None:
                self._surge_diagnostics.append({
                    "sim_time": sim_time,
                    "h3": cell,
                    "bucket": bucket,
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
