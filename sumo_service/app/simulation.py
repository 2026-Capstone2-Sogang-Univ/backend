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
from collections import defaultdict
from collections.abc import Callable
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
    acceptance_probability as _acceptance_probability,
    required_fare_for_target_p as _required_fare_for_target_p,
)

_logger = logging.getLogger(__name__)
from .db.engine import get_pool
from .db.writer import db_writer_task as _db_writer_task
from .fare import SPEED_THRESHOLD_MPS, TripAccumulator, calculate_fare, estimate_fare
from .grid import DEFAULT_ELASTICITY, H3_RESOLUTION, cell_center_latlng, compute_surge, get_cell
from .passenger import Passenger
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
FRAME_RATE = 60.0  # broadcast fps (WebSocket messages per real second)
SIMULATION_SPEED = float(os.getenv("SIMULATION_SPEED", "20"))  # simulated seconds per real second
STEP_LENGTH = (
    SIMULATION_SPEED / FRAME_RATE
)  # simulated seconds per TraCI step (passed to SUMO)
REAL_STEP_SLEEP = 1.0 / FRAME_RATE  # real seconds between TraCI steps

N_TAXIS = int(os.getenv("N_TAXIS", "300"))
N_BACKGROUND_CARS = 1200

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
_BG_SUB_VARS = [tc.VAR_ROAD_ID, tc.VAR_ROUTE_INDEX]
# 경로 끝에서 남은 엣지 수가 이 값 이하이면 새 경로를 이어 붙인다.
# route_index는 단조 증가하므로 한 번 임계값을 넘으면 매 스텝 참 → 짧은 엣지를 한 스텝에
# 통과해도 연장이 누락되지 않는다(기존 단일 트리거 엣지 동등비교의 구조적 누락을 제거).
# 값이 2면 "마지막 세 엣지를 한 스텝에 모두 통과"해야만 누락되므로 짧은 커넥터 엣지로 인한
# 잔여 소실이 크게 줄어든다.
_ROUTE_EXTEND_REMAINING = 2
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
    target_p: float = 0.8
    elasticity: float = DEFAULT_ELASTICITY
    beta_f: float = 0.006
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
    alpha_sensitivity: float = 1.0
    weather_source: str = "static"


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
        self._routable_edges: list[str] = []
        self._surge_cells: list[dict] = []
        # WebSocket은 list 형태가 필요하지만 배차 판단은 pickup H3로 즉시 조회해야 하므로 dict 캐시를 함께 둔다.
        self._surge_by_h3: dict[str, float] = {}
        self._history_store: DemandHistoryStore | None = None
        self._prediction_demand_provider: PredictionDemandProvider | None = None
        self._surge_diagnostics: list[dict] = []
        self._completed_passengers: list[dict] = []
        self._trip_queue: list[dict] = []
        self._latlng: Callable[[float, float], tuple[float, float]] | None = None
        # veh_id → 현재 할당된 경로의 엣지 수. route_index와 비교해 끝에 근접했는지 판정.
        self._bg_route_len: dict[str, int] = {}
        self._taxi_route_len: dict[str, int] = {}
        self._edge_weights: list[float] = []
        self._routable_edges_set: set[str] = set()
        self._run_id: int | None = None
        self._db_queue: asyncio.Queue | None = None
        self._db_writer_task: asyncio.Task | None = None
        self._taxi_dispatch_ids: dict[str, str] = {}
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

    # ------------------------------------------------------------------
    # Public async API (called from FastAPI endpoints)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self.status == SimStatus.RUNNING:
            return
        self._reset_run_state()
        self._paused = False
        self._stop_event.clear()
        self._loop = asyncio.get_event_loop()
        self._state_queue = asyncio.Queue()

        pool = get_pool()
        if pool is not None:
            params = json.dumps({
                "n_taxis": N_TAXIS,
                "n_background_cars": N_BACKGROUND_CARS,
                "frame_rate": FRAME_RATE,
                "simulation_speed": SIMULATION_SPEED,
                "passenger_lambda": PASSENGER_LAMBDA,
                "dispatch_timeout_s": DISPATCH_TIMEOUT_S,
                "trip_timeout_s": TRIP_TIMEOUT_S,
            })
            async with pool.acquire() as conn:
                self._run_id = await conn.fetchval(
                    "INSERT INTO simulation_run (sim_duration_s, passenger_source, params) "
                    "VALUES ($1, $2, $3) RETURNING id",
                    SIM_DURATION, PASSENGER_SOURCE, params,
                )
                await conn.executemany(
                    "INSERT INTO taxi (run_id, taxi_id) VALUES ($1, $2)",
                    [(self._run_id, f"taxi_{i}") for i in range(N_TAXIS)],
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

    async def restart(self) -> None:
        await self._shutdown()
        await self.start()

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
            self._routable_edges = []
            self._surge_cells = []
            self._surge_by_h3 = {}
            self._history_store = None
            self._surge_diagnostics = []
            self._completed_passengers = []
            self._trip_queue = []
            self._latlng = None
            self._bg_route_len = {}
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
            self._taxi_last_dropoff_cells = {}
            self._taxi_appeared.clear()
            self._taxi_missing_since.clear()
            self._bg_appeared.clear()
            self._bg_missing_since.clear()
            self._event_log = []
            self._taxi_previous_dropoff_times = {}

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
                    await self.connection_manager.broadcast_fare_update(**payload)
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
            step_length = self.experiment_config.step_length if experiment else STEP_LENGTH
            sim_duration = self.experiment_config.sim_duration if experiment else SIM_DURATION
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
            self._add_initial_vehicles()

            for veh_id in traci.vehicle.getIDList():
                if veh_id.startswith("taxi_"):
                    traci.vehicle.subscribe(veh_id, _SUB_VARS)
                elif veh_id.startswith("bg_"):
                    traci.vehicle.subscribe(veh_id, _BG_SUB_VARS)

            if PASSENGER_SOURCE == "parquet":
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

                for veh_id in traci.simulation.getDepartedIDList():
                    if veh_id.startswith("taxi_"):
                        traci.vehicle.subscribe(veh_id, _SUB_VARS)

                sub_results = traci.vehicle.getAllSubscriptionResults()

                # Track which vehicles have appeared at least once, and clear stale missing records
                for veh_id in sub_results:
                    if veh_id.startswith("taxi_"):
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

                self._spawn_passengers(sim_time)
                fare_updates = self._update_taxi_states(sim_time, sub_results)
                fare_updates += self._accumulate_fares(sim_time, sub_results)

                with self._lock:
                    state, grid_supply, grid_demand = self._capture_state(sim_time, sub_results)
                    self._state = state
                    state["fare_updates"] = fare_updates
                    for fu in fare_updates:
                        self._completed_passengers.append(fu)
                        if len(self._completed_passengers) > MAX_COMPLETED_PASSENGERS:
                            self._completed_passengers.pop(0)
                    surge_interval = int(sim_time / 5.0)
                    if surge_interval > self._last_surge_interval:
                        self._last_surge_interval = surge_interval
                        self._build_surge_cells(grid_supply, grid_demand, sim_time)
                        state["surge"] = self._surge_cells
                    else:
                        state["surge"] = None

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
                                fare = calculate_fare(accum)
                                self._completed_passengers.append({
                                    "passenger_id": pid,
                                    "taxi_id": taxi_id,
                                    "fare": fare,
                                    "expected_fare": p.expected_fare,
                                    "distance_m": accum.distance_m,
                                    "sim_time": sim_time,
                                })
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
                                    "fare": fare,
                                    "expected_fare": p.expected_fare,
                                    "completion": "forced_at_end",
                                })
                                self._record_history_dropoff(sim_time, dropoff_h3)
                            self._taxi_dispatch_ids.pop(taxi_id, None)
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
                    "fare_usd": (event.get("fare") or 0) / 100.0,
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

    def _initialize_prediction_components(self) -> None:
        self._close_prediction_demand_provider()
        config = self.experiment_config
        if config is None:
            self._history_store = None
            self._surge_diagnostics = []
            return

        if self._latlng is None:
            raise RuntimeError("lat/lng converter must be initialized before prediction components")

        model_h3_cells: list[str] = []
        seen_cells: set[str] = set()
        for edge_id in self._routable_edges:
            midpoint = self._get_edge_midpoint(edge_id)
            if midpoint is None:
                continue
            lat, lng = self._latlng(*midpoint)
            if not (math.isfinite(lat) and math.isfinite(lng)):
                continue
            h3_cell = get_cell(lat, lng)
            if h3_cell not in seen_cells:
                seen_cells.add(h3_cell)
                model_h3_cells.append(h3_cell)

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

    def _spawn_passengers(self, sim_time: float) -> None:
        if PASSENGER_SOURCE == "parquet":
            while self._trip_queue and self._trip_queue[0]["sim_time"] <= sim_time:
                trip = self._trip_queue.pop(0)
                self._create_passenger_from_trip(trip, sim_time)
        else:
            interval = int(sim_time / PASSENGER_SPAWN_INTERVAL)
            if interval <= self._last_spawn_interval:
                return
            self._last_spawn_interval = interval
            n = _poisson_sample(PASSENGER_LAMBDA)
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

    def _update_taxi_states(self, sim_time: float, sub_results: dict) -> list[dict]:
        fare_updates: list[dict] = []
        waiting_passengers = [p for p in self._passengers.values() if p.state == "waiting"]

        for veh_id, vals in sub_results.items():
            if not veh_id.startswith("taxi_"):
                continue
            state = self._taxi_states.get(veh_id, "empty")
            tx, ty = vals[tc.VAR_POSITION]
            road_id = vals.get(tc.VAR_ROAD_ID, "")

            # 단계 1 — 배차: 거리 기준 상위 K명 검토 후 수락 확률로 배차
            if state == "empty" and waiting_passengers:
                candidates = heapq.nsmallest(
                    DISPATCH_MAX_CANDIDATES, waiting_passengers,
                    key=lambda p: (p.x - tx) ** 2 + (p.y - ty) ** 2,
                )
                for candidate in candidates:
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
                        "target_p": self.experiment_config.target_p if self.experiment_config else None,
                        "p_actual": 1.0,
                        "base_fare_usd": candidate.expected_fare / 100.0,
                        "surge": 1.0,
                        "surged_fare_usd": candidate.expected_fare / 100.0,
                        "required_fare_usd": None,
                        "raw_incentive_usd": 0.0,
                        "incentive_usd": 0.0,
                        "capped": False,
                        "target_gap": 0.0,
                        "estimated_pickup_distance_m": route.length,
                    }
                    if candidate.h3_pickup and candidate.h3_dropoff:
                        D_pu_miles = route.length / 1609.344
                        base_fare_usd = candidate.expected_fare / 100.0
                        surge = self._surge_by_h3.get(candidate.h3_pickup, 1.0)
                        fare_usd = base_fare_usd
                        required_fare_usd = None
                        raw_incentive_usd = 0.0
                        incentive_usd = 0.0
                        capped = False
                        if self.experiment_config is not None:
                            # 실험 모드에서는 target_p를 만족하는 운임을 역산한 뒤,
                            # 현재 pickup cell surge 운임에 필요한 추가분만 인센티브로 지급한다.
                            fare_usd = base_fare_usd * surge
                            required_fare_usd = _required_fare_for_target_p(
                                last_dropoff_cell=(
                                    self._taxi_last_dropoff_cells.get(veh_id)
                                    or get_cell(*self._latlng(tx, ty))
                                ),
                                dropoff_cell=candidate.h3_dropoff,
                                call_datetime=SIM_BASE_DATETIME + timedelta(seconds=sim_time),
                                target_p=self.experiment_config.target_p,
                                D_pu=D_pu_miles,
                                trip_distance=candidate.expected_distance_m / 1609.344,
                                beta_f=self.experiment_config.beta_f,
                                alpha_sensitivity=self.experiment_config.alpha_sensitivity,
                                pickup_cell=candidate.h3_pickup,
                            )
                            raw_incentive_usd = required_fare_usd - fare_usd
                            incentive_cap_usd = min(10.0, base_fare_usd)
                            incentive_usd = min(max(raw_incentive_usd, 0.0), incentive_cap_usd)
                            # raw_incentive_usd가 음수(이미 target_p 초과 달성)일 때는 capped로 간주하지 않음.
                            capped = raw_incentive_usd > incentive_cap_usd
                            fare_usd += incentive_usd
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
                                    if self.experiment_config else 1.0
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
                        decision_payload.update({
                            "p_actual": p,
                            "base_fare_usd": base_fare_usd,
                            "surge": surge,
                            "surged_fare_usd": base_fare_usd * surge,
                            "required_fare_usd": required_fare_usd,
                            "raw_incentive_usd": raw_incentive_usd,
                            "incentive_usd": incentive_usd,
                            "capped": capped,
                            "target_gap": (
                                self.experiment_config.target_p - p
                                if self.experiment_config else 0.0
                            ),
                        })
                    if not accepted:
                        self._emit_event("dispatch_decision", {**decision_payload, "accepted": False})
                        self._push_db_event({**dispatch_payload, "accepted": False})
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
                    waiting_passengers.remove(candidate)
                    self._taxi_targets[veh_id] = candidate.id
                    self._taxi_states[veh_id] = "dispatched"
                    self._taxi_dispatch_times[veh_id] = sim_time
                    self._taxi_dispatch_ids[veh_id] = dispatch_id
                    previous_dropoff_time = self._taxi_previous_dropoff_times.get(veh_id)
                    if previous_dropoff_time is not None:
                        # 첫 승객 전 대기시간은 정의상 제외하고, 하차 이후 다음 수락까지의 search time만 기록한다.
                        decision_payload["empty_wait_time_s"] = sim_time - previous_dropoff_time
                    self._emit_event("dispatch_decision", {**decision_payload, "accepted": True})
                    self._push_db_event({**dispatch_payload, "accepted": True})
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
                    waiting_passengers.append(passenger)
                    old_dispatch_id = self._taxi_dispatch_ids.pop(veh_id, None)
                    if old_dispatch_id:
                        self._push_db_event({"type": "dispatch_timeout", "id": old_dispatch_id})
                    continue
                if road_id == passenger.pickup_edge:
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
                    self._taxi_states[veh_id] = "occupied"
                    dispatch_time = self._taxi_dispatch_times.pop(veh_id, sim_time)
                    self._active_trips[veh_id] = TripAccumulator(
                        passenger_id=passenger_id,
                        pickup_sim_time=sim_time,
                        dispatch_id=self._taxi_dispatch_ids.get(veh_id),
                        dispatch_sim_time=dispatch_time,
                        last_distance_snapshot=traci.vehicle.getDistance(veh_id),
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
                    fare = calculate_fare(accum)
                    fare_updates.append({
                        "passenger_id": passenger_id,
                        "taxi_id": veh_id,
                        "fare": fare,
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
                        "fare": fare,
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
                    route = self._random_route_from(road_id)
                    if route:
                        traci.vehicle.setRoute(veh_id, route)
                        self._taxi_route_len[veh_id] = len(route)
                    else:
                        # 픽업 시 붙인 버퍼가 하차 지점 너머까지 이어져 있다. route_len=0으로 두어
                        # 다음 스텝에 _extend_vehicle_routes가 즉시 연장을 시도하게 한다.
                        self._taxi_route_len[veh_id] = 0
                    continue
                if road_id == passenger.dropoff_edge:
                    accum = self._active_trips[veh_id]
                    dropoff_h3 = passenger.h3_dropoff or get_cell(passenger.dropoff_lat, passenger.dropoff_lng)
                    fare = calculate_fare(accum)
                    fare_updates.append({
                        "passenger_id": passenger_id,
                        "taxi_id": veh_id,
                        "fare": fare,
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
                        "fare": fare,
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
                    route = self._random_route_from(road_id)
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
                    passenger.state = "waiting"
                    waiting_passengers.append(passenger)
            self._taxi_states[veh_id] = "empty"
            self._taxi_dispatch_times.pop(veh_id, None)
            self._taxi_dispatch_ids.pop(veh_id, None)

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
                    fare = calculate_fare(accum)
                    fare_updates.append({
                        "passenger_id": passenger_id,
                        "taxi_id": taxi_id,
                        "fare": fare,
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
                        "fare": fare,
                        "expected_fare": passenger.expected_fare,
                        "completion": "sumo_removed",
                    })
                    self._record_history_dropoff(sim_time, dropoff_h3)
                    self._taxi_last_dropoff_cells[taxi_id] = dropoff_h3
                self._active_trips.pop(taxi_id, None)
                self._taxi_states.pop(taxi_id, None)
                self._taxi_dispatch_times.pop(taxi_id, None)
                self._taxi_dispatch_ids.pop(taxi_id, None)
                continue
            dist = vals[tc.VAR_DISTANCE]
            delta = dist - accum.last_distance_snapshot
            if delta > 0:
                accum.distance_m += delta
            accum.last_distance_snapshot = dist
            if vals[tc.VAR_SPEED] < SPEED_THRESHOLD_MPS:
                step_length = self.experiment_config.step_length if self.experiment_config else STEP_LENGTH
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
        """Place 200 background cars and 50 taxis on the network at t=0."""
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

        for i in range(N_TAXIS):
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

    def _extend_vehicle_routes(self, sub_results: dict) -> None:
        """경로 끝에 근접한 bg 차량·empty/dispatched 택시에 새 경로를 이어 붙여 arrival 제거를 막는다.

        구독값 route_index와 저장해 둔 경로 길이로 '남은 엣지 수'를 계산한다. 이 값은 단조
        증가하므로 한 번 임계값(_ROUTE_EXTEND_REMAINING)을 넘으면 매 스텝 참이 되어, 짧은
        엣지를 한 스텝에 통과해도 연장이 누락되지 않는다. 연장에 실패하면 route_len을 그대로
        두므로 다음 스텝에 자동으로 재시도된다(임계값이 계속 참이기 때문)."""
        for veh_id, vals in sub_results.items():
            road_id = vals.get(tc.VAR_ROAD_ID, "")
            if not road_id or road_id.startswith(":"):
                continue  # 내부 정션 엣지에서는 경로 산출 불가 — 다음 스텝에 재시도
            route_index = vals.get(tc.VAR_ROUTE_INDEX)
            if route_index is None or route_index < 0:
                continue  # 미구독이거나 아직 미출발(-1)한 차량은 건너뜀

            if veh_id.startswith("bg_"):
                route_len = self._bg_route_len.get(veh_id)
                if route_len is None or route_len - 1 - route_index > _ROUTE_EXTEND_REMAINING:
                    continue
                new_route = self._random_route_from(road_id)
                if new_route:
                    try:
                        traci.vehicle.setRoute(veh_id, new_route)
                        self._bg_route_len[veh_id] = len(new_route)
                    except traci.exceptions.TraCIException:
                        pass  # route_len 유지 → 다음 스텝 재시도

            elif veh_id.startswith("taxi_"):
                # empty·dispatched 택시만 연장 (occupied 경로는 트립 로직이 관리)
                if self._taxi_states.get(veh_id, "empty") not in ("empty", "dispatched"):
                    continue
                route_len = self._taxi_route_len.get(veh_id)
                if route_len is None or route_len - 1 - route_index > _ROUTE_EXTEND_REMAINING:
                    continue
                new_route = self._random_route_from(road_id)
                if new_route:
                    try:
                        traci.vehicle.setRoute(veh_id, new_route)
                        self._taxi_route_len[veh_id] = len(new_route)
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

    def _random_route_from(self, current_edge: str, attempts: int = 10) -> list[str] | None:
        """current_edge에서 시작하는 경로를 반환. _MIN_ROUTE_EDGES개 이상을 우선 시도하고
        점차 길이를 낮춰 2개까지 폴백, 모두 실패 시 None."""
        edges = self._routable_edges
        weights = self._edge_weights if len(self._edge_weights) == len(edges) else None

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
            self.experiment_config.elasticity if self.experiment_config else DEFAULT_ELASTICITY
        )
        for cell in set(grid_supply) | set(grid_demand) | set(demand_for_surge):
            lat_c, lng_c = cell_center_latlng(cell)
            surge = compute_surge(
                grid_supply.get(cell, 0),
                demand_for_surge.get(cell, 0),
                elasticity=elasticity,
            )
            surge_by_h3[cell] = surge
            actual_demand = grid_demand.get(cell, 0)
            selected_demand = demand_for_surge.get(cell, 0)
            surge_cells.append({
                "h3": cell,
                "supply": grid_supply.get(cell, 0),
                "demand": selected_demand,
                "actual_demand": actual_demand,
                "surge": surge,
                "center": {"lat": lat_c, "lng": lng_c},
            })
            if self.experiment_config is not None:
                self._surge_diagnostics.append({
                    "sim_time": sim_time,
                    "h3": cell,
                    "supply": grid_supply.get(cell, 0),
                    "actual_demand": actual_demand,
                    "demand_for_surge": selected_demand,
                    "surge": surge,
                })
        self._surge_cells = surge_cells
        self._surge_by_h3 = surge_by_h3
