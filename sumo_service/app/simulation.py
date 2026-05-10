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
import json
import math
import os
import random as _random
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import uuid

import traci
import traci.exceptions
from traci import constants as tc

from .coord import make_sumolib_converter, sumo_to_latlng
from .db.engine import get_pool
from .db.writer import db_writer_task as _db_writer_task
from .fare import SPEED_THRESHOLD_MPS, TripAccumulator, calculate_fare, estimate_fare
from .grid import H3_RESOLUTION, cell_center_latlng, compute_surge, get_cell
from .passenger import Passenger

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

SIM_DURATION = 3600.0  # simulated seconds (10 hours)
FRAME_RATE = 60.0  # broadcast fps (WebSocket messages per real second)
SIMULATION_SPEED = 20.0  # simulation speed (simulated seconds per real second)
STEP_LENGTH = (
    SIMULATION_SPEED / FRAME_RATE
)  # simulated seconds per TraCI step (passed to SUMO)
REAL_STEP_SLEEP = 1.0 / FRAME_RATE  # real seconds between TraCI steps

N_TAXIS = 300
N_BACKGROUND_CARS = 1200

PASSENGER_SPAWN_INTERVAL = 300.0
PASSENGER_LAMBDA = 5
PICKUP_THRESHOLD_M = 30.0
MAX_COMPLETED_PASSENGERS = 100
DISPATCH_TIMEOUT_S = 600.0   # 배차 후 이 시간 내 픽업 못하면 재배차
TRIP_TIMEOUT_S = 1800.0      # 탑승 후 이 시간 내 하차 못하면 강제 완료

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

_SUB_VARS = [tc.VAR_POSITION, tc.VAR_ANGLE, tc.VAR_SPEED, tc.VAR_DISTANCE, tc.VAR_ROAD_ID]


def _poisson_sample(lam: float) -> int:
    """Knuth 알고리즘 — numpy 불필요."""
    L = math.exp(-lam)
    k, p = 0, 1.0
    while p > L:
        k += 1
        p *= _random.random()
    return k - 1


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


class SimulationManager:
    def __init__(self) -> None:
        self.status = SimStatus.IDLE
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
        self._completed_passengers: list[dict] = []
        self._trip_queue: list[dict] = []
        self._latlng: Callable[[float, float], tuple[float, float]] | None = None
        self._bg_route_counter: int = 0
        self._edge_weights: list[float] = []
        self._routable_edges_set: set[str] = set()
        self._run_id: int | None = None
        self._db_queue: asyncio.Queue | None = None
        self._db_writer_task: asyncio.Task | None = None
        self._taxi_dispatch_ids: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public async API (called from FastAPI endpoints)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self.status == SimStatus.RUNNING:
            return
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
            self._completed_passengers = []
            self._trip_queue = []
            self._latlng = None
            self._bg_route_counter = 0
            self._edge_weights = []
            self._routable_edges_set = set()
            self._run_id = None
            self._db_queue = None
            self._taxi_dispatch_ids = {}

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
            cmd = [
                SUMO_BINARY,
                "-c",
                SUMO_CONFIG,
                "--no-step-log",
                "--no-warnings",
                "--step-length",
                str(STEP_LENGTH),
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
            self._edge_weights = self._compute_edge_weights()
            self._add_initial_vehicles()

            for veh_id in traci.vehicle.getIDList():
                if veh_id.startswith("taxi_"):
                    traci.vehicle.subscribe(veh_id, _SUB_VARS)

            if PASSENGER_SOURCE == "parquet":
                with open(TRIPS_FILE) as f:
                    self._trip_queue = sorted(json.load(f), key=lambda t: t["sim_time"])

            next_deadline = time.perf_counter()
            while not self._stop_event.is_set():
                if self._paused:
                    time.sleep(0.05)
                    next_deadline = time.perf_counter()  # pause 중 누적 방지
                    continue

                next_deadline += REAL_STEP_SLEEP
                traci.simulationStep()
                sim_time = traci.simulation.getTime()

                for veh_id in traci.simulation.getDepartedIDList():
                    if veh_id.startswith("taxi_"):
                        traci.vehicle.subscribe(veh_id, _SUB_VARS)

                for veh_id in traci.simulation.getArrivedIDList():
                    if veh_id.startswith("bg_"):
                        self._respawn_background_car(veh_id)

                sub_results = traci.vehicle.getAllSubscriptionResults()

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
                        self._build_surge_cells(grid_supply, grid_demand)
                        state["surge"] = self._surge_cells
                    else:
                        state["surge"] = None

                # Push state immediately after each step; broadcast loop sends it.
                self._loop.call_soon_threadsafe(self._state_queue.put_nowait, state)

                if sim_time >= SIM_DURATION:
                    # Force-complete all trips still in progress so their fares are recorded.
                    with self._lock:
                        for taxi_id, accum in list(self._active_trips.items()):
                            pid = self._taxi_targets.pop(taxi_id, None)
                            p = self._passengers.pop(pid, None) if pid else None
                            if p:
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
                            self._taxi_dispatch_ids.pop(taxi_id, None)
                        self._active_trips.clear()
                    self.status = SimStatus.FINISHED
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
        if self._run_id is None or self._db_queue is None or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._db_queue.put_nowait, event)

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
        try:
            shape = traci.lane.getShape(f"{edge_id}_0")
        except traci.exceptions.TraCIException:
            n = traci.edge.getLaneNumber(edge_id)
            shape = None
            for i in range(1, n):
                try:
                    shape = traci.lane.getShape(f"{edge_id}_{i}")
                    break
                except traci.exceptions.TraCIException:
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
        try:
            route = traci.simulation.findRoute(pickup_edge, dropoff_edge)
        except traci.exceptions.TraCIException:
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

    def _create_passenger_from_trip(self, trip: dict, sim_time: float) -> None:
        pickup_edge = trip["pickup_edge"]
        dropoff_edge = trip["dropoff_edge"]
        # SCC 외부 엣지면 스킵: 픽업이 외부면 배차 불가, 하차가 외부면 택시 갇힘
        if pickup_edge not in self._routable_edges_set or \
           dropoff_edge not in self._routable_edges_set:
            return
        try:
            route = traci.simulation.findRoute(pickup_edge, dropoff_edge)
        except traci.exceptions.TraCIException:
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

    def _update_taxi_states(self, sim_time: float, sub_results: dict) -> list[dict]:
        fare_updates: list[dict] = []
        waiting_passengers = [p for p in self._passengers.values() if p.state == "waiting"]

        for veh_id, vals in sub_results.items():
            if not veh_id.startswith("taxi_"):
                continue
            state = self._taxi_states.get(veh_id, "empty")
            tx, ty = vals[tc.VAR_POSITION]
            road_id = vals.get(tc.VAR_ROAD_ID, "")

            # 단계 1 — 배차: 가장 가까운 waiting 승객에게 배차
            if state == "empty" and waiting_passengers:
                nearest = min(waiting_passengers,
                              key=lambda p: (p.x - tx) ** 2 + (p.y - ty) ** 2)
                try:
                    route = traci.simulation.findRoute(road_id, nearest.pickup_edge)
                    if not route.edges:
                        continue
                    traci.vehicle.setRoute(veh_id, list(route.edges))
                except traci.exceptions.TraCIException:
                    continue
                nearest.state = "assigned"
                waiting_passengers.remove(nearest)
                self._taxi_targets[veh_id] = nearest.id
                self._taxi_states[veh_id] = "dispatched"
                self._taxi_dispatch_times[veh_id] = sim_time
                dispatch_id = str(uuid.uuid4())
                self._taxi_dispatch_ids[veh_id] = dispatch_id
                self._push_db_event({
                    "type": "dispatch",
                    "id": dispatch_id,
                    "run_id": self._run_id,
                    "passenger_id": nearest.id,
                    "taxi_id": veh_id,
                    "dispatch_sim_time": sim_time,
                    "estimated_pickup_distance_m": route.length,
                })

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
                        traci.vehicle.setRoute(veh_id, list(route.edges))
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
                    del self._passengers[passenger_id]
                    del self._active_trips[veh_id]
                    del self._taxi_targets[veh_id]
                    self._taxi_states[veh_id] = "empty"
                    self._taxi_dispatch_ids.pop(veh_id, None)
                    route = self._random_route_from(road_id)
                    if route:
                        traci.vehicle.setRoute(veh_id, route)
                    continue
                if road_id == passenger.dropoff_edge:
                    accum = self._active_trips[veh_id]
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
                    del self._passengers[passenger_id]
                    del self._active_trips[veh_id]
                    del self._taxi_targets[veh_id]
                    self._taxi_states[veh_id] = "empty"
                    self._taxi_dispatch_ids.pop(veh_id, None)
                    route = self._random_route_from(road_id)
                    if route:
                        traci.vehicle.setRoute(veh_id, route)

        return fare_updates

    def _accumulate_fares(self, sim_time: float, sub_results: dict) -> list[dict]:
        fare_updates: list[dict] = []
        for taxi_id, accum in list(self._active_trips.items()):
            vals = sub_results.get(taxi_id)
            if vals is None:
                # 택시가 목적지 엣지 끝에 도달해 SUMO가 제거한 경우 → fare 기록
                passenger_id = self._taxi_targets.pop(taxi_id, None)
                passenger = self._passengers.pop(passenger_id, None) if passenger_id else None
                if passenger:
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
                accum.low_speed_seconds += STEP_LENGTH
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

    def _respawn_background_car(self, veh_id: str) -> None:
        route_edges = self._random_route(self._routable_edges)
        route_id = f"bg_route_{self._bg_route_counter}"
        self._bg_route_counter += 1
        traci.route.add(route_id, route_edges)
        traci.vehicle.add(
            vehID=veh_id,
            routeID=route_id,
            typeID="DEFAULT_VEHTYPE",
            depart="now",
            departLane="best",
            departPos="random_free",
            departSpeed="max",
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

    def _random_route_from(self, current_edge: str, attempts: int = 10) -> list[str] | None:
        """Return a routable edge list starting from current_edge to a weighted-random destination."""
        edges = self._routable_edges
        weights = self._edge_weights if len(self._edge_weights) == len(edges) else None
        for _ in range(attempts):
            if weights:
                dst = _random.choices(edges, weights=weights, k=1)[0]
            else:
                dst = _random.choice(edges)
            if dst == current_edge:
                continue
            try:
                result = traci.simulation.findRoute(current_edge, dst)
                if result.edges:
                    return list(result.edges)
            except traci.exceptions.TraCIException:
                continue
        return None

    def _random_route(self, edges: list[str], attempts: int = 10) -> list[str]:
        """Return a routable edge list between two random edges, falling back to one edge."""
        for _ in range(attempts):
            src = _random.choice(edges)
            dst = _random.choice(edges)
            if src == dst:
                continue
            try:
                result = traci.simulation.findRoute(src, dst)
                if result.edges:
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
            x, y = vals[tc.VAR_POSITION]
            angle = vals[tc.VAR_ANGLE]
            speed = vals[tc.VAR_SPEED]
            lat, lng = self._latlng(x, y)
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
        self, grid_supply: dict[str, int], grid_demand: dict[str, int]
    ) -> None:
        surge_cells = []
        for cell in set(grid_supply) | set(grid_demand):
            lat_c, lng_c = cell_center_latlng(cell)
            surge_cells.append({
                "h3": cell,
                "supply": grid_supply.get(cell, 0),
                "demand": grid_demand.get(cell, 0),
                "surge": compute_surge(grid_supply.get(cell, 0), grid_demand.get(cell, 0)),
                "center": {"lat": lat_c, "lng": lng_c},
            })
        self._surge_cells = surge_cells
