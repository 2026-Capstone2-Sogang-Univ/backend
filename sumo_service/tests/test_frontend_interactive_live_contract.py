import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import websockets

from app.ws_messages_pb2 import ServerMessage


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_CONTRACT") != "1",
    reason="set RUN_LIVE_CONTRACT=1 to run the SUMO live frontend contract test",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_for_server(client: httpx.AsyncClient, base_url: str) -> None:
    for _ in range(60):
        try:
            response = await client.get(f"{base_url}/simulation/status", timeout=2.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    raise AssertionError("server did not become ready")


async def _wait_for_vehicles(client: httpx.AsyncClient, base_url: str) -> dict:
    last_status = {}
    for _ in range(60):
        response = await client.get(f"{base_url}/simulation/status", timeout=5.0)
        response.raise_for_status()
        last_status = response.json()
        vehicles = last_status.get("vehicles") or []
        if len(vehicles) >= 3:
            return last_status
        await asyncio.sleep(1.0)
    raise AssertionError(f"vehicles did not appear: {last_status}")


async def _find_valid_trip_request(
    client: httpx.AsyncClient,
    base_url: str,
    vehicles: list[dict],
) -> tuple[dict, dict]:
    candidates = []
    if len(vehicles) >= 3:
        a, b, c = vehicles[0], vehicles[1], vehicles[2]
        candidates.append({
            "pickup": {
                "lat": (float(a["lat"]) + float(b["lat"])) / 2.0,
                "lng": (float(a["lng"]) + float(b["lng"])) / 2.0,
            },
            "dropoff": {"lat": float(c["lat"]), "lng": float(c["lng"])},
            "incentive_limit": 3000,
        })
    for i, pickup in enumerate(vehicles):
        for dropoff in vehicles[i + 1:]:
            candidates.append({
                "pickup": {"lat": float(pickup["lat"]), "lng": float(pickup["lng"])},
                "dropoff": {"lat": float(dropoff["lat"]), "lng": float(dropoff["lng"])},
                "incentive_limit": 3000,
            })

    valid_quotes = []
    last_error = None
    for body in candidates:
        response = await client.post(
            f"{base_url}/simulation/passengers/quote",
            json=body,
            timeout=10.0,
        )
        if response.status_code == 200:
            quote = response.json()
            valid_quotes.append((quote["expected_distance_m"], body, quote))
        last_error = response.text
    if valid_quotes:
        _, body, quote = min(valid_quotes, key=lambda item: item[0])
        return body, quote
    raise AssertionError(f"no valid live trip request found: {last_error}")


async def _recv_payload(ws, *, timeout_s: float = 30.0) -> tuple[str, ServerMessage]:
    data = await asyncio.wait_for(ws.recv(), timeout=timeout_s)
    if isinstance(data, str):
        raise AssertionError(f"expected binary websocket message, got text: {data!r}")
    message = ServerMessage.FromString(data)
    payload = message.WhichOneof("payload")
    if payload is None:
        raise AssertionError("websocket message has no payload")
    return payload, message


async def _recv_until(ws, predicate, *, timeout_s: float = 60.0) -> ServerMessage:
    deadline = asyncio.get_running_loop().time() + timeout_s
    seen = []
    while asyncio.get_running_loop().time() < deadline:
        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
        payload, message = await _recv_payload(ws, timeout_s=remaining)
        seen.append(payload)
        if predicate(payload, message):
            return message
    raise AssertionError(f"target websocket message not received; seen={seen}")


async def test_live_dispatch_call_boarding_and_fare_contract():
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/ws"
    service_dir = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update({
        "N_BACKGROUND_CARS": "0",
        "SIMULATION_SPEED": "30",
        "SIM_PROFILE": "0",
    })
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=service_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        async with httpx.AsyncClient() as client:
            await _wait_for_server(client, base_url)
            start_response = await client.post(
                f"{base_url}/simulation/start",
                json={
                    "duration": 1800,
                    "seed": 7,
                    "taxi_count": 8,
                    "initial_passenger_count": 0,
                    "passenger_source": "random",
                },
                timeout=20.0,
            )
            start_response.raise_for_status()
            status = await _wait_for_vehicles(client, base_url)
            vehicles = [
                vehicle
                for vehicle in status["vehicles"]
                if vehicle.get("state") == "empty"
            ]
            assert len(vehicles) >= 3
            trip_request, quote = await _find_valid_trip_request(client, base_url, vehicles)
            assert set(quote) == {
                "expected_fare",
                "expected_distance_m",
                "estimated_wait_sec",
                "surge_multiplier",
                "incentive_limit",
                "total_amount",
            }

            async with websockets.connect(ws_url) as ws:
                taxi_response = await client.post(
                    f"{base_url}/simulation/taxis",
                    json=trip_request["pickup"],
                    timeout=20.0,
                )
                taxi_response.raise_for_status()
                taxi_id = taxi_response.json()["taxi_id"]
                taxi_created = await _recv_until(
                    ws,
                    lambda payload, message: (
                        payload == "taxi_created"
                        and message.taxi_created.taxi_id == taxi_id
                    ),
                    timeout_s=30.0,
                )
                assert taxi_created.taxi_created.lat == pytest.approx(trip_request["pickup"]["lat"])
                assert taxi_created.taxi_created.lng == pytest.approx(trip_request["pickup"]["lng"])

                passenger_response = await client.post(
                    f"{base_url}/simulation/passengers",
                    json=trip_request,
                    timeout=20.0,
                )
                passenger_response.raise_for_status()
                passenger_id = passenger_response.json()["passenger_id"]
                passenger_created = await _recv_until(
                    ws,
                    lambda payload, message: (
                        payload == "passenger_created"
                        and message.passenger_created.passenger_id == passenger_id
                    ),
                    timeout_s=30.0,
                )
                assert passenger_created.passenger_created.expected_fare == quote["expected_fare"]
                assert (
                    passenger_created.passenger_created.expected_distance_m
                    == quote["expected_distance_m"]
                )

                dispatch_assigned = await _recv_until(
                    ws,
                    lambda payload, message: (
                        payload == "dispatch_assigned"
                        and message.dispatch_assigned.passenger_id == passenger_id
                    ),
                    timeout_s=90.0,
                )
                assigned_taxi_id = dispatch_assigned.dispatch_assigned.taxi_id
                assert dispatch_assigned.dispatch_assigned.eta >= 0

                call_response = await client.get(
                    f"{base_url}/simulation/taxis/{assigned_taxi_id}/call",
                    timeout=10.0,
                )
                call_response.raise_for_status()
                call_detail = call_response.json()
                assert call_detail["taxi_id"] == assigned_taxi_id
                assert call_detail["passenger_id"] == passenger_id
                assert set(call_detail["pickup"]) == {"lat", "lng"}
                assert set(call_detail["dropoff"]) == {"lat", "lng"}
                assert call_detail["incentive"] <= (
                    quote["expected_fare"] + trip_request["incentive_limit"]
                )
                assert call_detail["destination_surge"] >= 1.0
                assert set(call_detail["incentive_breakdown"]) == {
                    "base_fare",
                    "passenger_incentive",
                    "surge_bonus",
                }

                boarded = await _recv_until(
                    ws,
                    lambda payload, message: (
                        payload == "passenger_boarded"
                        and message.passenger_boarded.passenger_id == passenger_id
                    ),
                    timeout_s=90.0,
                )
                assert boarded.passenger_boarded.taxi_id == assigned_taxi_id

                fare_update = await _recv_until(
                    ws,
                    lambda payload, message: (
                        payload == "fare_update"
                        and message.fare_update.passenger_id == passenger_id
                    ),
                    timeout_s=120.0,
                )
                assert fare_update.fare_update.taxi_id == assigned_taxi_id
                assert fare_update.fare_update.fare <= (
                    quote["expected_fare"] + trip_request["incentive_limit"]
                )
                assert fare_update.fare_update.expected_fare == quote["expected_fare"]
                assert fare_update.fare_update.distance_m >= 0
    finally:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(f"{base_url}/simulation/stop", timeout=5.0)
        except Exception:
            pass
        process.terminate()
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10.0)
        if process.returncode not in (0, -15, 1):
            stdout, stderr = process.communicate(timeout=1.0)
            raise AssertionError(
                f"uvicorn exited with {process.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )
