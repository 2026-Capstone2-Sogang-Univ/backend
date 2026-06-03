"""Env defaults for Module 3 demand prediction HTTP API."""
from __future__ import annotations

import os

DEFAULT_PREDICTION_URL = "https://module3-ml.onrender.com/predict"


def prediction_timeout_s() -> float:
    return float(os.getenv("PREDICTION_TIMEOUT_S", "30"))


def prediction_retry_max() -> int:
    return max(0, int(os.getenv("PREDICTION_RETRY_MAX", "4")))


def prediction_retry_backoff_s() -> float:
    return max(0.0, float(os.getenv("PREDICTION_RETRY_BACKOFF_S", "2")))


def prediction_warmup_enabled() -> bool:
    return os.getenv("PREDICTION_WARMUP", "1").strip().lower() not in ("0", "false", "no")


def resolve_prediction_url() -> str:
    url = os.getenv("PREDICTION_URL", DEFAULT_PREDICTION_URL).strip()
    return url or DEFAULT_PREDICTION_URL


def resolve_demand_source(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.getenv("DEMAND_SOURCE", "").strip().lower()
    if env == "actual":
        return "actual"
    return "predicted"


def require_prediction_api_key() -> str:
    key = os.getenv("PREDICTION_API_KEY", "").strip()
    if not key:
        raise ValueError(
            "PREDICTION_API_KEY is required (Module 3 surge policy always uses predicted demand)"
        )
    return key


def resolve_prediction_mode(
    demand_source: str,
    explicit: str | None = None,
) -> str:
    if explicit and explicit != "none":
        return explicit
    if demand_source == "predicted":
        return "sync"
    return "none"
