"""
SimulationManager: manages the SUMO/TraCI simulation loop in a dedicated thread.

TraCI is a synchronous (blocking) API. The loop runs in a ThreadPoolExecutor via
asyncio.run_in_executor() so it never blocks FastAPI's async event loop.

Speed: 1 real second = 60 simulated seconds (1 simulated minute).
Duration: simulation auto-terminates at simulated time 3600s (1 hour).
"""

import asyncio
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

SIM_DURATION = 3600.0       # simulated seconds (1 hour)
SPEED_FACTOR = 60.0         # 1 real second = 60 simulated seconds
STEP_LENGTH = 1.0           # simulated seconds per TraCI step
REAL_STEP_SLEEP = STEP_LENGTH / SPEED_FACTOR  # ~0.0167 real seconds between steps

N_TAXIS = 50
N_BACKGROUND_CARS = 200


class SimStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"


class SimulationManager:
    def __init__(self) -> None:
        self.status = SimStatus.IDLE
        self._paused = False
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._executor_task: Optional[asyncio.Future] = None
        self._state: dict = {"vehicles": [], "passengers": [], "sim_time": 0.0}
        self._boundary: dict = {"minX": 0.0, "minY": 0.0, "maxX": 0.0, "maxY": 0.0}

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
        try:
            traci.start(
                ["sumo", "-c", SUMO_CONFIG, "--no-step-log", "--no-warnings"],
                label="main",
            )
            (min_x, min_y), (max_x, max_y) = traci.simulation.getNetBoundary()
            with self._lock:
                self._boundary = {"minX": min_x, "minY": min_y, "maxX": max_x, "maxY": max_y}
            self._add_initial_vehicles()

            while not self._stop_event.is_set():
                if self._paused:
                    time.sleep(0.05)
                    continue

                traci.simulationStep()
                sim_time = traci.simulation.getTime()

                with self._lock:
                    self._state = self._capture_state(sim_time)

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

    def _add_initial_vehicles(self) -> None:
        """Place 200 background cars and 50 taxis on the network at t=0."""
        edges = [e for e in traci.edge.getIDList() if not e.startswith(":")]

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

    def _capture_state(self, sim_time: float) -> dict:
        vehicles = []
        for veh_id in traci.vehicle.getIDList():
            x, y = traci.vehicle.getPosition(veh_id)
            angle = traci.vehicle.getAngle(veh_id)
            state = "empty" if veh_id.startswith("taxi_") else "car"
            vehicles.append({"id": veh_id, "x": x, "y": y, "angle": angle, "state": state})

        return {"vehicles": vehicles, "passengers": [], "sim_time": sim_time}
