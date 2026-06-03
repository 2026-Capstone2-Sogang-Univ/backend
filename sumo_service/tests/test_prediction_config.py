import os

from app.prediction_config import (
    resolve_demand_source,
    resolve_prediction_mode,
    resolve_prediction_url,
)


def test_resolve_demand_source_prefers_explicit():
    assert resolve_demand_source("actual") == "actual"


def test_resolve_demand_source_uses_env(monkeypatch):
    monkeypatch.delenv("PREDICTION_API_KEY", raising=False)
    monkeypatch.setenv("DEMAND_SOURCE", "predicted")
    assert resolve_demand_source() == "predicted"


def test_resolve_demand_source_defaults_to_predicted(monkeypatch):
    monkeypatch.delenv("DEMAND_SOURCE", raising=False)
    assert resolve_demand_source() == "predicted"


def test_resolve_demand_source_actual_only_when_env(monkeypatch):
    monkeypatch.setenv("DEMAND_SOURCE", "actual")
    assert resolve_demand_source() == "actual"


def test_resolve_prediction_mode_sync_for_predicted():
    assert resolve_prediction_mode("predicted", None) == "sync"
    assert resolve_prediction_mode("actual", None) == "none"


def test_resolve_prediction_url_from_env(monkeypatch):
    monkeypatch.setenv("PREDICTION_URL", "https://custom.test/predict")
    assert resolve_prediction_url() == "https://custom.test/predict"
