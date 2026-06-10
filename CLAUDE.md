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
- **Fare model**: 2013 NYC TLC rates (amounts in USD cents) — base $2.50, $0.50 per 1/5 mile (322 m), $0.50 per 60 s at low speed (<12 mph / 5.36 m/s), plus MTA surcharge $0.50. Peak/night time surcharges are documented in `fare.py` as comments but not applied (simulation is fixed to 2013-07-08 08:00 time window).

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
curl      http://localhost:8080/simulation/demand-forecast
```

### Live demand forecast (external AI prediction in the live server)

The live server can expose multi-horizon demand predictions from the external
Module3 server (`https://module3-ml.onrender.com/predict`). This is OFF by
default and gated by an env toggle (the experiment-only `demand_source=predicted`
path is unchanged).

- Enable with `DEMAND_FORECAST_ENABLED=1` (requires `PREDICTION_API_KEY`).
- `DEMAND_FORECAST_STEPS` (default 4) and `PREDICTION_URL` are configurable.
- These (and `PREDICTION_TIMEOUT_S` / `PREDICTION_RETRY_MAX` / `PREDICTION_RETRY_BACKOFF_S`)
  are managed via `sumo_service/.env` — copy `sumo_service/.env.example` to
  `sumo_service/.env`. The file is loaded at startup by `app/__init__.py`
  (`python-dotenv`), and real env vars / docker-compose `environment:` override it.
  docker-compose loads the same file via `env_file` (optional).
- Module3 only predicts a single horizon (t+15) per request, so the provider
  produces N buckets by **roll-forward**: each prediction is fed back as a
  synthetic history record (`records_for_prediction(..., predicted_overrides=...)`)
  for the next request. For the current 15-min bucket `n`, the forecast covers
  buckets `n+1 … n+STEPS` (t+15, t+30, t+45, t+60).
- The forecast refreshes in a background thread once per 15 simulated minutes
  (`_maybe_refresh_demand_forecast`) so the TraCI loop is never blocked, and the
  latest snapshot is served by `GET /simulation/demand-forecast`.

> Note: `GET /simulation/demand-forecast` is **display-only** — it does not feed
> the incentive/pricing calculation. To drive pricing with prediction, use the
> live predicted-demand toggle below.

### Live predicted demand in surge/incentive (experiment-parity)

Independently of the display forecast, the live server can feed **predicted
demand into the surge/incentive calculation**, reusing the exact experiment path
(`PredictionDemandProvider.demand_by_h3`, single horizon t+15). OFF by default.

- Enable with `LIVE_DEMAND_SOURCE=predicted` (requires `PREDICTION_API_KEY`).
- `LIVE_PREDICTION_MODE` (default `async`), `LIVE_PREDICTION_HORIZON_MIN`
  (default 15), `LIVE_PREDICTION_FALLBACK` (default `last_prediction`).
- `async` is the live default because `_build_surge_cells` runs under the
  manager lock; `sync` would block REST/WS during the Module3 HTTP call. The
  prediction is cached per 15-min bucket, so at most one fetch per bucket.
- If provider creation fails (e.g. missing API key) the live server logs a
  warning and falls back to actual demand instead of crashing.

### Preprocess NYC taxi data (parquet mode only)
Sample = 24h × 7days × N_TAXIS × 5.5 passengers/taxi/h = 277,200 for a 1-week run.

```bash
python scripts/preprocess_trips.py \
  --input  real_taxi_data/od_month=07/consolidated.parquet \
  --net    back/sumo_service/sumo_configs/NY/manhattan_car_only.net.xml \
  --output back/sumo_service/sumo_configs/NY/trips_processed.json \
  --start  "2013-07-08 08-00-00" \
  --end    "2013-07-15 08-00-00" \
  --sample 277200
```

## Analysis & Scripting Principles

- Never approximate spatial data (coordinate bounds, cell lists, edge lists) with hardcoded guesses. Always derive them from actual project files (`routable_scc.json`, `.net.xml`, `.parquet`, etc.).
- Before writing any analysis script, ask: "Does a file already exist that provides the data I need?" If yes, read it first.

## Open Issues

- **gRPC proto contracts**: cannot be finalized until the ML model team confirms the Prediction Service input/output spec. This is the critical path for the full pipeline.
- **Prediction Service**: not yet implemented.
- **Dispatch Service**: not yet implemented (depends on Prediction Service gRPC contract).
