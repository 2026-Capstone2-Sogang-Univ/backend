import asyncio
import os
import signal
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .connection_manager import ConnectionManager
from .db.engine import close_pool, init_pool
from .simulation import SimulationManager
from .routers import simulation as simulation_router
from .routers import ws as ws_router

_CLI_KEYS: dict[str, str] = {
    "s": "start",
    "p": "pause",
    "u": "resume",
    "r": "restart",
    "e": "end",
    "q": "quit",
}


def _getch() -> str | None:
    """Read one character from stdin without requiring Enter.

    Returns the character, or None on EOF / error.
    Falls back to line-buffered readline when stdin is not a TTY
    (e.g. piped input or test environments).
    """
    try:
        if not sys.stdin.isatty():
            line = sys.stdin.readline()
            return line[0] if line else None

        if os.name == "nt":
            import msvcrt  # Windows

            raw = msvcrt.getch()
            return raw.decode("utf-8", errors="ignore") if raw else None
        else:
            import termios
            import tty

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                return sys.stdin.read(1) or None
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return None


def _cli_loop(loop: asyncio.AbstractEventLoop, sim: SimulationManager) -> None:
    """Read single-key commands from stdin and dispatch to SimulationManager."""
    keys_hint = "  ".join(f"{k}={v}" for k, v in _CLI_KEYS.items())
    print(f"Console ready. Keys: {keys_hint}", flush=True)

    while True:
        ch = _getch()
        if ch is None:
            break

        if ch == "s":
            asyncio.run_coroutine_threadsafe(sim.start(), loop)
        elif ch == "p":
            asyncio.run_coroutine_threadsafe(sim.pause(), loop)
        elif ch == "u":
            asyncio.run_coroutine_threadsafe(sim.resume(), loop)
        elif ch == "r":
            asyncio.run_coroutine_threadsafe(sim.restart(), loop)
        elif ch == "e":
            asyncio.run_coroutine_threadsafe(sim.stop(), loop)
        elif ch == "q":
            # Signal the main process to shut down gracefully
            os.kill(os.getpid(), signal.SIGINT)
            break
        else:
            continue

        print(f">> {_CLI_KEYS[ch]}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    loop = asyncio.get_event_loop()
    cli_thread = threading.Thread(
        target=_cli_loop,
        args=(loop, app.state.manager),
        daemon=True,
        name="cli-stdin",
    )
    cli_thread.start()
    yield
    await app.state.manager.stop()
    await close_pool()


app = FastAPI(title="SUMO Service", version="0.1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request, exc):
    return JSONResponse(
        status_code=422,
        content={
            "error": "invalid_request",
            "message": "Request validation failed",
        },
    )

connection_manager = ConnectionManager()
manager = SimulationManager()
manager.connection_manager = connection_manager

app.state.manager = manager
app.state.connection_manager = connection_manager

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(simulation_router.router, prefix="/simulation", tags=["simulation"])
app.include_router(ws_router.router, tags=["websocket"])
