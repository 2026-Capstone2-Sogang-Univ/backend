import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    url = os.getenv("DATABASE_URL")
    if not url:
        return
    _pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
    await _ensure_schema()


async def _ensure_schema() -> None:
    if _pool is None:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            ALTER TABLE dispatch
                ADD COLUMN IF NOT EXISTS raw_surge DOUBLE PRECISION,
                ADD COLUMN IF NOT EXISTS target_matching_rate DOUBLE PRECISION,
                ADD COLUMN IF NOT EXISTS calculated_surge DOUBLE PRECISION,
                ADD COLUMN IF NOT EXISTS final_surge DOUBLE PRECISION,
                ADD COLUMN IF NOT EXISTS final_fare_estimate_usd DOUBLE PRECISION,
                ADD COLUMN IF NOT EXISTS p_actual DOUBLE PRECISION;

            ALTER TABLE trip
                ADD COLUMN IF NOT EXISTS meter_fare INTEGER,
                ADD COLUMN IF NOT EXISTS surge DOUBLE PRECISION NOT NULL DEFAULT 1.0;

            UPDATE trip SET meter_fare = fare WHERE meter_fare IS NULL;

            ALTER TABLE trip
                ALTER COLUMN meter_fare SET NOT NULL;
            """
        )


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool | None:
    return _pool
