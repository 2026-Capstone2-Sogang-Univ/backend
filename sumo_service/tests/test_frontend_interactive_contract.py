from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.connection_manager import ConnectionManager
from app.main import app
from app.simulation import SimStatus
from app.ws_messages_pb2 import ServerMessage


def _manager_stub() -> MagicMock:
    manager = MagicMock()
    manager.status = SimStatus.RUNNING
    manager.stop = AsyncMock()
    manager.quote_manual_passenger = MagicMock(return_value={
        "ok": True,
        "expected_fare": 8200,
        "expected_distance_m": 3100,
        "estimated_wait_sec": 95,
        "surge_multiplier": 1.3658536585365855,
        "incentive_limit": 3000,
        "total_amount": 11200,
        "system_surge": 1.8,
        "uncapped_total_amount": 14760,
    })
    manager.create_manual_passenger = MagicMock(return_value={
        "ok": True,
        "passenger_id": "upax_1",
    })
    manager.create_manual_taxi = MagicMock(return_value={
        "ok": True,
        "taxi_id": "utaxi_1",
    })
    manager.get_taxi_standby_context = MagicMock(return_value={
        "taxi_id": "utaxi_1",
        "location": {"lat": 40.7484, "lng": -73.9857},
        "current_incentive": 3200,
        "current_surge": 1.5,
        "recommended_cells": [
            {
                "h3_index": "892a100d2abffff",
                "center": {"lat": 40.7584, "lng": -73.9757},
                "supply": 8,
                "demand": 21,
                "surge": 1.8,
                "expected_incentive": 4100,
            }
        ],
    })
    manager.get_taxi_call_detail = MagicMock(return_value={
        "taxi_id": "utaxi_1",
        "passenger_id": "upax_1",
        "pickup": {"lat": 40.7484, "lng": -73.9857},
        "dropoff": {"lat": 40.7584, "lng": -73.9757},
        "incentive": 8500,
        "destination_surge": 1.6,
        "incentive_breakdown": {
            "base_fare": 6200,
            "passenger_incentive": 2300,
            "surge_bonus": 0,
        },
    })
    return manager


def _ws_stub() -> MagicMock:
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.close = AsyncMock()
    return ws


def _sent_messages(ws: MagicMock) -> list[ServerMessage]:
    return [
        ServerMessage.FromString(call.args[0])
        for call in ws.send_bytes.call_args_list
    ]


def test_frontend_interactive_rest_success_contract_fields():
    app.state.manager = _manager_stub()
    request_body = {
        "pickup": {"lat": 40.7484, "lng": -73.9857},
        "dropoff": {"lat": 40.7584, "lng": -73.9757},
        "incentive_limit": 3000,
    }
    with TestClient(app) as client:
        quote = client.post("/simulation/passengers/quote", json=request_body)
        passenger = client.post("/simulation/passengers", json=request_body)
        taxi = client.post("/simulation/taxis", json={"lat": 40.7484, "lng": -73.9857})
        standby = client.get("/simulation/taxis/utaxi_1/standby")
        call = client.get("/simulation/taxis/utaxi_1/call")

    assert quote.status_code == 200
    assert set(quote.json()) == {
        "expected_fare",
        "expected_distance_m",
        "estimated_wait_sec",
        "surge_multiplier",
        "incentive_limit",
        "total_amount",
    }
    assert passenger.status_code == 200
    assert set(passenger.json()) == {"passenger_id"}
    assert taxi.status_code == 200
    assert set(taxi.json()) == {"taxi_id"}

    assert standby.status_code == 200
    standby_body = standby.json()
    assert set(standby_body) == {
        "taxi_id",
        "location",
        "current_incentive",
        "current_surge",
        "recommended_cells",
    }
    assert set(standby_body["location"]) == {"lat", "lng"}
    assert set(standby_body["recommended_cells"][0]) == {
        "h3_index",
        "center",
        "supply",
        "demand",
        "surge",
        "expected_incentive",
    }
    assert set(standby_body["recommended_cells"][0]["center"]) == {"lat", "lng"}

    assert call.status_code == 200
    call_body = call.json()
    assert set(call_body) == {
        "taxi_id",
        "passenger_id",
        "pickup",
        "dropoff",
        "incentive",
        "destination_surge",
        "incentive_breakdown",
    }
    assert set(call_body["pickup"]) == {"lat", "lng"}
    assert set(call_body["dropoff"]) == {"lat", "lng"}
    assert set(call_body["incentive_breakdown"]) == {
        "base_fare",
        "passenger_incentive",
        "surge_bonus",
    }


def test_frontend_interactive_rest_error_contract_fields():
    manager = _manager_stub()
    manager.quote_manual_passenger = MagicMock(return_value={
        "ok": False,
        "error": "simulation_busy",
        "message": "Simulation is busy. Please retry.",
    })
    app.state.manager = manager

    with TestClient(app) as client:
        response = client.post("/simulation/passengers/quote", json={
            "pickup": {"lat": 40.7484, "lng": -73.9857},
            "dropoff": {"lat": 40.7584, "lng": -73.9757},
            "incentive_limit": 3000,
        })

    assert response.status_code == 400
    assert response.json() == {
        "error": "simulation_busy",
        "message": "Simulation is busy. Please retry.",
    }


async def test_frontend_interactive_websocket_contract_fields():
    manager = ConnectionManager()
    ws = _ws_stub()
    await manager.connect(ws)

    await manager.broadcast_surge(
        [
            {
                "h3": "892a100d2abffff",
                "supply": 8,
                "demand": 21,
                "surge": 1.8,
                "center": {"lat": 40.7584, "lng": -73.9757},
            }
        ],
        sim_time=10.0,
    )
    await manager.broadcast_state({
        "vehicles": [
            {
                "id": "utaxi_1",
                "lat": 40.7484,
                "lng": -73.9857,
                "angle": 90.0,
                "speed": 4.2,
                "state": "empty",
            }
        ],
        "passengers": [
            {
                "id": "upax_1",
                "lat": 40.7484,
                "lng": -73.9857,
                "expected_fare": 8200,
                "expected_distance_m": 3100,
            }
        ],
        "sim_time": 11.0,
    })
    await manager.broadcast_passenger_created("upax_1", 40.7484, -73.9857, 8200, 3100)
    await manager.broadcast_taxi_created("utaxi_1", 40.7484, -73.9857)
    await manager.broadcast_passenger_creation_failed("upax_2", "no_route_found")
    await manager.broadcast_dispatch_assigned("upax_1", "utaxi_1", 95)
    await manager.broadcast_passenger_boarded("upax_1", "utaxi_1", 12.0)
    await manager.broadcast_passenger_cancelled("upax_3", "timeout")
    await manager.broadcast_fare_update("upax_1", "utaxi_1", 11200, 8200, 3100.0, 18.0)

    messages = _sent_messages(ws)
    assert [m.WhichOneof("payload") for m in messages] == [
        "surge",
        "snapshot",
        "passenger_created",
        "taxi_created",
        "passenger_creation_failed",
        "dispatch_assigned",
        "passenger_boarded",
        "passenger_cancelled",
        "fare_update",
    ]

    surge = messages[0].surge
    assert surge.cells[0].h3_index == "892a100d2abffff"
    assert surge.cells[0].supply == 8
    assert surge.cells[0].demand == 21
    assert surge.cells[0].surge_coeff == pytest.approx(1.8)
    assert surge.cells[0].center_lat == 40.7584
    assert surge.cells[0].center_lng == -73.9757

    snapshot = messages[1].snapshot
    assert snapshot.vehicles[0].id == "utaxi_1"
    assert snapshot.vehicles[0].lat == 40.7484
    assert snapshot.vehicles[0].lng == -73.9857
    assert snapshot.vehicles[0].angle == pytest.approx(90.0)
    assert snapshot.vehicles[0].speed == pytest.approx(4.2)
    assert snapshot.passengers[0].id == "upax_1"
    assert snapshot.passengers[0].expected_fare == 8200
    assert snapshot.passengers[0].expected_distance_m == 3100

    assert messages[2].passenger_created.passenger_id == "upax_1"
    assert messages[2].passenger_created.pickup_lat == 40.7484
    assert messages[2].passenger_created.pickup_lng == -73.9857
    assert messages[2].passenger_created.expected_fare == 8200
    assert messages[2].passenger_created.expected_distance_m == 3100
    assert messages[3].taxi_created.taxi_id == "utaxi_1"
    assert messages[3].taxi_created.lat == 40.7484
    assert messages[3].taxi_created.lng == -73.9857
    assert messages[4].passenger_creation_failed.passenger_id == "upax_2"
    assert messages[4].passenger_creation_failed.reason == "no_route_found"
    assert messages[5].dispatch_assigned.passenger_id == "upax_1"
    assert messages[5].dispatch_assigned.taxi_id == "utaxi_1"
    assert messages[5].dispatch_assigned.eta == 95
    assert messages[6].passenger_boarded.passenger_id == "upax_1"
    assert messages[6].passenger_boarded.taxi_id == "utaxi_1"
    assert messages[6].passenger_boarded.sim_time == 12.0
    assert messages[7].passenger_cancelled.passenger_id == "upax_3"
    assert messages[7].passenger_cancelled.reason == "timeout"
    assert messages[8].fare_update.passenger_id == "upax_1"
    assert messages[8].fare_update.taxi_id == "utaxi_1"
    assert messages[8].fare_update.fare == 11200
    assert messages[8].fare_update.expected_fare == 8200
    assert messages[8].fare_update.distance_m == 3100.0
    assert messages[8].fare_update.sim_time == 18.0
