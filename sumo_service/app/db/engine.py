import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool() -> None:
    global _pool
    url = os.getenv("DATABASE_URL")
    if not url:
        return
    _pool = await asyncpg.create_pool(url, min_size=1, max_size=5)


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool | None:
    return _pool
