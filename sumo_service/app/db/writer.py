import asyncio
import logging

from .engine import get_pool

_logger = logging.getLogger(__name__)


async def db_writer_task(queue: asyncio.Queue) -> None:
    while True:
        event = await queue.get()
        if event is None:
            break
        await _handle(event)


async def _handle(event: dict) -> None:
    pool = get_pool()
    if pool is None:
        return
    t = event["type"]
    try:
        async with pool.acquire() as conn:
            if t == "passenger":
                await conn.execute(
                    """
                    INSERT INTO passenger (
                        run_id, passenger_id, spawn_sim_time,
                        pickup_edge, dropoff_edge,
                        pickup_lat, pickup_lng, dropoff_lat, dropoff_lng,
                        expected_distance_m, expected_fare, h3_pickup, source
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                    ON CONFLICT DO NOTHING
                    """,
                    event["run_id"], event["passenger_id"], event["spawn_sim_time"],
                    event["pickup_edge"], event["dropoff_edge"],
                    event["pickup_lat"], event["pickup_lng"],
                    event["dropoff_lat"], event["dropoff_lng"],
                    event["expected_distance_m"], event["expected_fare"],
                    event.get("h3_pickup"), event["source"],
                )
            elif t == "dispatch":
                await conn.execute(
                    """
                    INSERT INTO dispatch (
                        id, run_id, passenger_id, taxi_id,
                        dispatch_sim_time, estimated_pickup_distance_m,
                        raw_surge, target_matching_rate, calculated_surge,
                        final_surge, final_fare_estimate_usd, p_actual, accepted
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                    ON CONFLICT DO NOTHING
                    """,
                    event["id"], event["run_id"], event["passenger_id"], event["taxi_id"],
                    event["dispatch_sim_time"], event.get("estimated_pickup_distance_m"),
                    event.get("raw_surge"), event.get("target_matching_rate"),
                    event.get("calculated_surge"), event.get("final_surge"),
                    event.get("final_fare_usd"), event.get("p_actual"),
                    event.get("accepted", True),
                )
            elif t == "dispatch_timeout":
                await conn.execute(
                    "UPDATE dispatch SET timed_out = TRUE WHERE id = $1",
                    event["id"],
                )
            elif t == "trip":
                await conn.execute(
                    """
                    INSERT INTO trip (
                        run_id, passenger_id, taxi_id, dispatch_id,
                        dispatch_sim_time, pickup_sim_time, dropoff_sim_time,
                        distance_m, low_speed_seconds, meter_fare, surge,
                        fare, expected_fare, completion
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                    """,
                    event["run_id"], event["passenger_id"], event["taxi_id"],
                    event["dispatch_id"],
                    event["dispatch_sim_time"], event["pickup_sim_time"],
                    event.get("dropoff_sim_time"),
                    event["distance_m"], event["low_speed_seconds"],
                    event.get("meter_fare", event["fare"]), event.get("surge", 1.0),
                    event["fare"], event["expected_fare"], event["completion"],
                )
            elif t == "run_end":
                await conn.execute(
                    "UPDATE simulation_run SET ended_at = now(), end_reason = $2 WHERE id = $1",
                    event["run_id"], event["end_reason"],
                )
    except Exception as exc:
        _logger.warning("DB write failed [%s]: %s", t, exc)
