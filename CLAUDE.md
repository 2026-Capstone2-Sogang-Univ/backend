# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

Three independent microservices:

- **`sumo-service/`** — SUMO/TraCI simulation loop + FastAPI (REST API + WebSocket server) + gRPC server/client
- **`prediction-service/`** — Deep learning model inference scheduler + gRPC server
- **`dispatch-service/`** — Supply-demand imbalance calculation, incentive algorithm, nearest-taxi assignment + gRPC client/server
- **`proto/`** — Shared gRPC `.proto` definitions used by all three services

Internal communication between services uses gRPC. External communication with the web frontend uses WebSocket. The SUMO Service is the single source of truth for simulation time.

### Key Design Constraints

- **TraCI is a synchronous (blocking) API.** Inside `sumo-service`, the TraCI loop must run in a separate thread via `asyncio.run_in_executor()` to avoid blocking FastAPI's async event loop.
- **Simulation speed**: real 1 second = simulated 1 minute (accelerated mode). WebSocket broadcasts at 60 fps (`FRAME_RATE = 60` in `simulation.py`).
- **Passenger generation**: dual-mode — `PASSENGER_SOURCE=random` uses Poisson(λ=5) every 5 simulated minutes; `PASSENGER_SOURCE=parquet` replays preprocessed NYC taxi trip data from `sumo_configs/NY/trips_processed.json`.
- **Dispatch algorithm**: empty taxis are matched to the nearest waiting passenger by Euclidean distance; dispatched taxi retargets to pickup edge via `traci.vehicle.changeTarget`.
- **Fare model**: NYC taxi meter (amounts in USD cents) — base $3.00, $0.70 per 1/5 mile (322 m), $0.70 per 60 s at low speed (<12 mph / 5.36 m/s), plus fixed surcharges $1.50 (improvement $1.00 + MTA $0.50). Conditional surcharges (NYS congestion, CBD toll) and time-based surcharges (night, rush hour) are documented but not applied.

### gRPC Communication Flow

```
[Prediction Service] --(predicted demand: t+1~t+6)--> [Dispatch Service]
[SUMO Service] --(simulation state: vehicle positions, empty taxi count, current time)--> [Dispatch Service]
[Dispatch Service] --(incentive levels, rerouting target taxi IDs)--> [SUMO Service]
[SUMO Service] --(current simulation time)--> [Prediction Service]
```

> gRPC is planned but not yet implemented. Proto contracts are pending ML model I/O spec confirmation.

### WebSocket Message Types (SUMO Service → Web Frontend)

| Type | Frequency | Description |
|------|-----------|-------------|
| `boundary` | Once on connect | Network bounding box — `sumo` (minX/Y/maxX/Y) and `geo` (minLat/Lng/maxLat/Lng) |
| `snapshot` | Every ~16.7ms (60 fps) | `vehicles` list (id, lat, lng, angle, speed, state) + `passengers` list (id, lat, lng, expected_fare, expected_distance_m) + `sim_time` |
| `surge` | Every 5 sim seconds | H3 grid cells with supply, demand, surge coefficient |
| `fare_update` | On trip completion | passenger_id, taxi_id, fare, expected_fare, distance_m, sim_time |
| `finished` | Once at sim end | Simulation complete notification |

Vehicle `state` values: `car` / `empty` / `dispatched` / `occupied`  
Passenger `state` values: `waiting` / `assigned`

## Development Commands

### Run the full system

```bash
cd back
docker compose up --build
```

### Local development (sumo-service)

```bash
cd back/sumo_service
uv sync --group dev

# Run tests (no SUMO/Docker required)
uv run pytest -v

# Run a specific test file
uv run pytest tests/test_fare.py -v
```

### Simulation control (REST API)

```bash
curl -X POST http://localhost:8080/simulation/start
curl -X POST http://localhost:8080/simulation/pause
curl -X POST http://localhost:8080/simulation/resume
curl -X POST http://localhost:8080/simulation/restart
curl      http://localhost:8080/simulation/status
curl      http://localhost:8080/simulation/surge
curl      http://localhost:8080/simulation/passengers
curl      http://localhost:8080/simulation/fare/{passenger_id}
```

### Preprocess NYC taxi data (parquet mode only)

```bash
python scripts/preprocess_trips.py \
  --input  real_taxi_data/od_month=07/consolidated.parquet \
  --net    back/sumo_service/sumo_configs/NY/manhattan_car_only.net.xml \
  --output back/sumo_service/sumo_configs/NY/trips_processed.json \
  --start  "2024-01-15 08-00-00" \
  --end    "2024-01-15 09-00-00" \
  --sample 5000
```

## Analysis & Scripting Principles

- Never approximate spatial data (coordinate bounds, cell lists, edge lists) with hardcoded guesses. Always derive them from actual project files (`routable_scc.json`, `.net.xml`, `.parquet`, etc.).
- Before writing any analysis script, ask: "Does a file already exist that provides the data I need?" If yes, read it first.

## Open Issues

- **gRPC proto contracts**: cannot be finalized until the ML model team confirms the Prediction Service input/output spec. This is the critical path for the full pipeline.
- **Prediction Service**: not yet implemented.
- **Dispatch Service**: not yet implemented (depends on Prediction Service gRPC contract).
