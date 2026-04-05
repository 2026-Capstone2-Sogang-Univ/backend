---
name: sumo-docs
description: Reference skill for SUMO traffic simulation and TraCI API. Use when writing, debugging, or explaining code that uses SUMO, TraCI, sumolib, or related tools in the taxi dispatch simulation backend.
---

# SUMO Docs Reference

This skill provides quick access to SUMO documentation for the taxi simulation backend (`sumo-service/`).

**Official docs root:** https://sumo.dlr.de/docs/index.html

## When to use this skill

- Writing or debugging TraCI client code in `sumo-service/`
- Configuring SUMO network files, route files, or `.sumocfg`
- Handling vehicle state changes (rerouting taxis, dispatching, pickup)
- Tuning simulation parameters (step length, acceleration, speed)
- Interpreting SUMO output or errors

## TraCI Quick Reference

TraCI runs as a TCP client/server. SUMO is the server; Python code is the client.

```python
import traci

traci.start(["sumo", "-c", "map.sumocfg"])
while traci.simulation.getMinExpectedNumber() > 0:
    traci.simulationStep()
traci.close()
```

**IMPORTANT (from CLAUDE.md):** TraCI is synchronous/blocking. In `sumo-service/`, the TraCI loop runs in a separate thread via `asyncio.run_in_executor()`.

### Key TraCI modules for this project

| Module | Common use |
|--------|-----------|
| `traci.vehicle` | Get/set position, speed, route, state |
| `traci.simulation` | Step control, departed/arrived IDs, time |
| `traci.edge` | Travel time, vehicle count per edge |
| `traci.route` | Add/query routes |
| `traci.person` | Pedestrian/passenger entities |

### Vehicle commands used in taxi dispatch

```python
# Position & state
traci.vehicle.getPosition(vehID)       # (x, y)
traci.vehicle.getAngle(vehID)          # degrees
traci.vehicle.getSpeed(vehID)
traci.vehicle.getRoadID(vehID)         # current edge ID

# Rerouting (for dispatch incentive logic)
traci.vehicle.changeTarget(vehID, edgeID)
traci.vehicle.rerouteTraveltime(vehID)
traci.vehicle.setRoute(vehID, edgeList)

# Simulation events
traci.simulation.getDepartedIDList()   # new vehicles this step
traci.simulation.getArrivedIDList()    # vehicles that finished this step
traci.simulation.getTime()             # current sim time (seconds)
```

### Subscriptions (use for performance)

Subscriptions are ~2x faster than repeated polling (50k vs 25k vehicles/s):

```python
traci.vehicle.subscribe(vehID, [traci.constants.VAR_POSITION, traci.constants.VAR_ANGLE])
results = traci.vehicle.getSubscriptionResults(vehID)
```

## Key documentation pages

Load these reference files for deeper detail:

- `references/network.md` — Road network format, edge IDs, `.sumocfg`
- `references/vehicle-types.md` — Vehicle type params (taxi type definition)
- `references/demand.md` — Route files, flow definitions, Poisson demand

Or fetch directly:
- TraCI full API: https://sumo.dlr.de/docs/TraCI/index.html
- Vehicle value retrieval: https://sumo.dlr.de/docs/TraCI/Vehicle_Value_Retrieval.html
- Vehicle state change: https://sumo.dlr.de/docs/TraCI/Change_Vehicle_State.html
- Simulation control: https://sumo.dlr.de/docs/TraCI/Simulation_Value_Retrieval.html
- sumolib (network parsing): https://sumo.dlr.de/docs/Tools/Sumolib.html
