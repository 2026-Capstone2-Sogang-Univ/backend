# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

Three independent microservices:

- **`sumo-service/`** — SUMO/TraCI simulation loop + FastAPI (REST API + WebSocket server) + gRPC server/client
- **`prediction-service/`** — Deep learning model inference scheduler + gRPC server
- **`dispatch-service/`** — Supply-demand imbalance calculation, incentive algorithm, nearest-taxi assignment + gRPC client/server
- **`proto/`** — Shared gRPC `.proto` definitions used by all three services

Internal communication between services uses gRPC. External communication with the Unity frontend uses WebSocket. The SUMO Service is the single source of truth for simulation time.

### Key Design Constraints

- **TraCI is a synchronous (blocking) API.** Inside `sumo-service`, the TraCI loop must run in a separate thread via `asyncio.run_in_executor()` to avoid blocking FastAPI's async event loop.
- **Simulation speed**: real 1 second = simulated 1 minute (accelerated mode). WebSocket broadcasts at 60 fps (`BROADCAST_INTERVAL = 1/60` s in `simulation.py`).
- **Passenger generation**: sampled from a Poisson distribution using the predicted demand count as λ, once per 5-minute simulated interval.
- **Dispatch algorithm**: empty taxis are probabilistically rerouted based on incentive level (0.0–1.0); passengers are matched to the nearest available empty taxi by Euclidean distance.
- **Post-pickup behavior** *(subject to change)*: drop-off destination is a random edge within the road network; the taxi returns to `empty` state immediately upon arrival. Drop-off location is not included in WebSocket messages.

### gRPC Communication Flow

```
[Prediction Service] --(predicted demand: t+1~t+6)--> [Dispatch Service]
[SUMO Service] --(simulation state: vehicle positions, empty taxi count, current time)--> [Dispatch Service]
[Dispatch Service] --(incentive levels, rerouting target taxi IDs)--> [SUMO Service]
[SUMO Service] --(current simulation time)--> [Prediction Service]
```

### WebSocket Message Types (SUMO Service → Unity)

| Type | Frequency | Description |
|------|-----------|-------------|
| `boundary` | Once on connect | Network bounding box coordinates |
| `vehicles` | Every ~16.7ms (60 fps) | Snapshot of all vehicles (id, x, y, angle, state) |
| `passengers` | Every ~16.7ms (60 fps) | Full list of waiting passengers (id, x, y) |

Vehicle `state` values: `car` / `empty` / `dispatched` / `occupied`

## Development Commands

> The project is in its initial stage. Commands below will be refined as service directories are created.

### Run the full system

```bash
docker compose up
```

### Local development setup

Each service manages its own dependencies via `pyproject.toml` and `uv.lock`.

```bash
# Install dependencies for a service (e.g., dispatch-service)
cd dispatch-service
uv sync

# Install with dev dependencies
uv sync --dev
```

### Simulation control (REST API)

```bash
curl -X POST http://localhost:8000/simulation/start
curl -X POST http://localhost:8000/simulation/pause
curl -X POST http://localhost:8000/simulation/restart
```

### Run tests

Tests are focused on `dispatch-service` (no external dependencies required):

```bash
# from dispatch-service directory
uv run pytest
# run a single test
uv run pytest tests/test_dispatch.py::test_nearest_taxi_assignment
```

## Open Issues

- **Prediction Service model I/O spec**: gRPC proto interface cannot be finalized until the ML model team confirms the model's input/output contract. This is the critical path for the full pipeline.
