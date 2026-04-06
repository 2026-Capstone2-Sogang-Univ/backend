"""
SimulationManager: manages the SUMO/TraCI simulation loop in a dedicated thread.

TraCI is a synchronous (blocking) API. The loop runs in a ThreadPoolExecutor via
asyncio.run_in_executor() so it never blocks FastAPI's async event loop.

Speed: 1 real second = 60 simulated seconds (1 simulated minute).
Duration: simulation auto-terminates at simulated time 3600s (1 hour).
"""

import asyncio
import math
import random
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional

import traci
import traci.exceptions

SUMO_CONFIG = str(
    Path(__file__).parent.parent / "sumo_configs" / "gangnam" / "LargeGangNamSimulation.sumocfg"
)

SIM_DURATION = 3600.0           # simulated seconds (1 hour)
SPEED_FACTOR = 60.0             # 1 real second = 60 simulated seconds
STEP_LENGTH = 1.0               # simulated seconds per TraCI step
REAL_STEP_SLEEP = STEP_LENGTH / SPEED_FACTOR  # ~0.0167 real seconds between steps

PASSENGER_GEN_INTERVAL = 300.0  # simulated seconds between passenger generation cycles (5 min)
DEFAULT_DEMAND_LAMBDA = 5.0     # Poisson λ used when no prediction has arrived yet

N_TAXIS = 50
N_BACKGROUND_CARS = 200


class SimStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"


def _poisson_sample(lam: float) -> int:
    """Sample from Poisson(lam) using Knuth's algorithm (no numpy required)."""
    if lam <= 0:
        return 0
    L = math.exp(-lam)
    k, p = 0, 1.0
    while p > L:
        k += 1
        p *= random.random()
    return k - 1


class SimulationManager:
    def __init__(self) -> None:
        self.status = SimStatus.IDLE
        self._paused = False
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._executor_task: Optional[asyncio.Future] = None
        self._state: dict = {"vehicles": [], "passengers": [], "sim_time": 0.0}
        self._boundary: dict = {"minX": 0.0, "minY": 0.0, "maxX": 0.0, "maxY": 0.0}

        # Updated by gRPC handlers from outside the simulation thread (protected by _lock)
        self._predicted_demand: float = DEFAULT_DEMAND_LAMBDA
        self._pending_incentives: list[tuple[str, float]] = []

    # ------------------------------------------------------------------
    # Public async API (called from FastAPI endpoints)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self.status == SimStatus.RUNNING:
            return
        self._paused = False
        self._stop_event.clear()
        self.status = SimStatus.RUNNING
        loop = asyncio.get_event_loop()
        self._executor_task = loop.run_in_executor(None, self._run_loop)

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

    def update_predicted_demand(self, lam: float) -> None:
        """Called by gRPC handler (prediction service) to update Poisson λ."""
        with self._lock:
            self._predicted_demand = max(0.0, lam)

    def apply_incentives(self, incentives: list[tuple[str, float]]) -> None:
        """Called by gRPC handler (dispatch service) to queue incentive rerouting.

        Each entry is (taxi_id, incentive_level) where incentive_level ∈ [0.0, 1.0].
        """
        with self._lock:
            self._pending_incentives.extend(incentives)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        self._stop_event.set()
        if self._executor_task is not None:
            try:
                await self._executor_task
            except Exception:
                pass
            self._executor_task = None
        self.status = SimStatus.IDLE
        with self._lock:
            self._state = {"vehicles": [], "passengers": [], "sim_time": 0.0}

    # ------------------------------------------------------------------
    # Blocking loop — runs in ThreadPoolExecutor
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        # Simulation-thread-local state — only touched here, no lock needed.
        taxi_states: dict[str, str] = {}          # taxi_id → "empty"|"dispatched"|"occupied"
        taxi_to_passenger: dict[str, str] = {}    # taxi_id → passenger_id (while dispatched)
        waiting_passengers: dict[str, dict] = {}  # passenger_id → {id, x, y, edge}
        passenger_counter: int = 0
        next_gen_time: float = PASSENGER_GEN_INTERVAL  # first batch at t=300 s

        try:
            traci.start(
                ["sumo", "-c", SUMO_CONFIG, "--no-step-log", "--no-warnings"],
                label="main",
            )
            (min_x, min_y), (max_x, max_y) = traci.simulation.getNetBoundary()
            with self._lock:
                self._boundary = {"minX": min_x, "minY": min_y, "maxX": max_x, "maxY": max_y}

            edges = self._add_initial_vehicles()
            for i in range(N_TAXIS):
                taxi_states[f"taxi_{i}"] = "empty"

            while not self._stop_event.is_set():
                if self._paused:
                    time.sleep(0.05)
                    continue

                traci.simulationStep()
                sim_time = traci.simulation.getTime()
                active_ids = set(traci.vehicle.getIDList())

                # --- Clean up taxis that left the network ---
                for tid in list(taxi_states.keys()):
                    if tid not in active_ids:
                        pid = taxi_to_passenger.pop(tid, None)
                        if pid and pid in waiting_passengers:
                            # Re-queue the passenger so it can be re-dispatched
                            pass  # passenger already removed from waiting_passengers at dispatch
                        taxi_states.pop(tid)

                # --- Apply queued incentive rerouting commands ---
                with self._lock:
                    incentives = self._pending_incentives[:]
                    self._pending_incentives.clear()

                for taxi_id, level in incentives:
                    if taxi_states.get(taxi_id) == "empty":
                        if random.random() < level:
                            target_edge = random.choice(edges)
                            try:
                                traci.vehicle.changeTarget(taxi_id, target_edge)
                            except Exception:
                                pass

                # --- Passenger generation (every 5 simulated minutes) ---
                if sim_time >= next_gen_time:
                    with self._lock:
                        lam = self._predicted_demand
                    n = _poisson_sample(lam)
                    for _ in range(n):
                        edge = random.choice(edges)
                        x, y = _get_edge_position(edge)
                        pid = f"pax_{passenger_counter}"
                        passenger_counter += 1
                        waiting_passengers[pid] = {"id": pid, "x": x, "y": y, "edge": edge}
                    next_gen_time += PASSENGER_GEN_INTERVAL

                # --- Dispatch: match each waiting passenger to the nearest empty taxi ---
                empty_taxis = [
                    tid for tid, st in taxi_states.items()
                    if st == "empty" and tid in active_ids
                ]
                for pid in list(waiting_passengers.keys()):
                    if not empty_taxis:
                        break
                    pax = waiting_passengers[pid]
                    nearest = min(
                        empty_taxis,
                        key=lambda tid: _euclidean(
                            traci.vehicle.getPosition(tid), (pax["x"], pax["y"])
                        ),
                    )
                    try:
                        traci.vehicle.changeTarget(nearest, pax["edge"])
                    except Exception:
                        continue
                    empty_taxis.remove(nearest)
                    taxi_states[nearest] = "dispatched"
                    taxi_to_passenger[nearest] = pid
                    del waiting_passengers[pid]

                # --- State transitions: pickup & drop-off ---
                for taxi_id in list(taxi_states.keys()):
                    if taxi_id not in active_ids:
                        continue
                    state = taxi_states[taxi_id]

                    if state == "dispatched" and _on_last_route_edge(taxi_id):
                        # Taxi arrived at passenger edge — pick up
                        taxi_to_passenger.pop(taxi_id, None)
                        dropoff_edge = random.choice(edges)
                        try:
                            traci.vehicle.changeTarget(taxi_id, dropoff_edge)
                            taxi_states[taxi_id] = "occupied"
                        except Exception:
                            taxi_states[taxi_id] = "empty"

                    elif state == "occupied" and _on_last_route_edge(taxi_id):
                        # Taxi arrived at drop-off edge — become empty again
                        new_edge = random.choice(edges)
                        try:
                            traci.vehicle.changeTarget(taxi_id, new_edge)
                        except Exception:
                            pass
                        taxi_states[taxi_id] = "empty"

                # --- Capture state for WebSocket broadcast ---
                vehicles = []
                for veh_id in active_ids:
                    x, y = traci.vehicle.getPosition(veh_id)
                    angle = traci.vehicle.getAngle(veh_id)
                    state = taxi_states.get(veh_id, "car") if veh_id.startswith("taxi_") else "car"
                    vehicles.append({"id": veh_id, "x": x, "y": y, "angle": angle, "state": state})

                with self._lock:
                    self._state = {
                        "vehicles": vehicles,
                        "passengers": list(waiting_passengers.values()),
                        "sim_time": sim_time,
                    }

                if sim_time >= SIM_DURATION:
                    self.status = SimStatus.FINISHED
                    break

                time.sleep(REAL_STEP_SLEEP)

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

    def _add_initial_vehicles(self) -> list[str]:
        """Place 200 background cars and 50 taxis on the network at t=0.

        Returns the list of valid (non-internal) edge IDs for later use.
        """
        edges = [e for e in traci.edge.getIDList() if not e.startswith(":")]

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

        return edges

    def _random_route(self, edges: list[str], attempts: int = 5) -> list[str]:
        """Return a routable edge list between two random edges, falling back to one edge."""
        for _ in range(attempts):
            src = random.choice(edges)
            dst = random.choice(edges)
            if src == dst:
                continue
            result = traci.simulation.findRoute(src, dst)
            if result.edges:
                return list(result.edges)
        return [random.choice(edges)]


# ------------------------------------------------------------------
# Module-level helpers (pure logic, no self needed)
# ------------------------------------------------------------------

def _euclidean(pos1: tuple[float, float], pos2: tuple[float, float]) -> float:
    dx = pos1[0] - pos2[0]
    dy = pos1[1] - pos2[1]
    return math.sqrt(dx * dx + dy * dy)


def _on_last_route_edge(taxi_id: str) -> bool:
    """Return True when the taxi is traversing the final edge of its current route."""
    try:
        route = traci.vehicle.getRoute(taxi_id)
        idx = traci.vehicle.getRouteIndex(taxi_id)
        return idx >= len(route) - 1
    except Exception:
        return False


def _get_edge_position(edge: str) -> tuple[float, float]:
    """Return the approximate midpoint (x, y) of an edge."""
    try:
        length = traci.lane.getLength(f"{edge}_0")
        return traci.simulation.convert2D(edge, length / 2.0)
    except Exception:
        try:
            return traci.simulation.convert2D(edge, 0.0)
        except Exception:
            return 0.0, 0.0
