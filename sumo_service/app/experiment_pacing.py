"""Experiment pacing: Lab-aligned acceleration vs fast bench mode."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.simulation import ExperimentConfig

FRAME_RATE = 60.0


def default_simulation_speed() -> float:
    return float(os.getenv("SIMULATION_SPEED", "20"))


def default_policy_update_interval_real_s() -> float:
    return float(os.getenv("POLICY_UPDATE_INTERVAL_REAL_S", "900"))


def default_policy_update_interval_sim_s() -> float:
    """Sim-time seconds between Module 3 refresh in bench (--fast) mode (default 15 sim-min)."""
    return float(os.getenv("POLICY_UPDATE_INTERVAL_SIM_S", "900"))


def experiment_fast_enabled() -> bool:
    """Batch experiments: EXPERIMENT_FAST=1 (default) → TraCI bench, no wall sleep."""
    return os.getenv("EXPERIMENT_FAST", "1").strip().lower() not in ("0", "false", "no")


def bench_step_length() -> float:
    """Sim seconds per TraCI step in fast bench (default 2 → half the RPCs for same sim_duration)."""
    return float(os.getenv("BENCH_STEP_LENGTH", "2"))


def resolve_experiment_pacing(config: ExperimentConfig) -> tuple[float, float]:
    """Return (step_length, real_sleep) for TraCI loop."""
    if config.fast:
        step_length = (
            config.step_length if config.step_length is not None else bench_step_length()
        )
        return step_length, 0.0
    speed = config.simulation_speed
    step_length = (
        config.step_length if config.step_length is not None else speed / FRAME_RATE
    )
    real_sleep = config.real_sleep if config.real_sleep is not None else 1.0 / FRAME_RATE
    return step_length, real_sleep
