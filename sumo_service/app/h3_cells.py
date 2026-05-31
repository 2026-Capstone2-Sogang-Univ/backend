from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_MODEL_H3_FILENAME = "supported_h3.json"


def default_model_h3_cells_path() -> Path:
    env_path = os.getenv("MODEL_H3_CELLS_PATH")
    if env_path:
        return Path(env_path)

    current = Path(__file__).resolve()
    config_candidate = current.parent.parent / "sumo_configs" / "NY" / DEFAULT_MODEL_H3_FILENAME
    if config_candidate.exists():
        return config_candidate

    for parent in current.parents:
        candidate = parent / DEFAULT_MODEL_H3_FILENAME
        if candidate.exists():
            return candidate
    return current.parents[2] / DEFAULT_MODEL_H3_FILENAME


def load_model_h3_cells(path: str | Path | None = None) -> list[str]:
    h3_path = Path(path) if path is not None else default_model_h3_cells_path()
    if not h3_path.exists():
        raise FileNotFoundError(f"model H3 cell file not found: {h3_path}")

    payload = json.loads(h3_path.read_text(encoding="utf-8"))
    cells = _extract_cells(payload)
    _validate_cells(cells, h3_path)
    return cells


def _extract_cells(payload: object) -> list[str]:
    if isinstance(payload, list):
        return [str(cell) for cell in payload]
    if not isinstance(payload, dict):
        raise ValueError("model H3 cell file must contain an array or object")

    for key in ("cells", "h3_cells", "model_h3_cells", "cells_cumulative", "cells_confirmed"):
        value = payload.get(key)
        if isinstance(value, list):
            return [str(cell) for cell in value]
    raise ValueError("model H3 cell file does not contain a supported cell list")


def _validate_cells(cells: list[str], h3_path: Path) -> None:
    if not cells:
        raise ValueError(f"model H3 cell file is empty: {h3_path}")
    duplicates = len(cells) - len(set(cells))
    if duplicates:
        raise ValueError(f"model H3 cell file contains {duplicates} duplicate cells: {h3_path}")
    if any(not cell for cell in cells):
        raise ValueError(f"model H3 cell file contains empty cell ids: {h3_path}")
