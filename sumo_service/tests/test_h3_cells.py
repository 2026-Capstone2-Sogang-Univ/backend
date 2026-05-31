import json
from pathlib import Path

import pytest

from app.h3_cells import load_model_h3_cells


def test_load_model_h3_cells_uses_supported_h3_list():
    path = Path(__file__).resolve().parents[1] / "sumo_configs" / "NY" / "supported_h3.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    cells = load_model_h3_cells(path)

    assert cells == payload
    assert len(cells) == 564
    assert len(cells) == len(set(cells))


def test_default_model_h3_cells_path_loads_packaged_supported_h3():
    cells = load_model_h3_cells()

    assert len(cells) == 564
    assert len(cells) == len(set(cells))


def test_load_model_h3_cells_accepts_plain_cell_array(tmp_path):
    path = tmp_path / "cells.json"
    path.write_text(json.dumps(["h3_a", "h3_b"]), encoding="utf-8")

    assert load_model_h3_cells(path) == ["h3_a", "h3_b"]


def test_load_model_h3_cells_rejects_duplicate_cells(tmp_path):
    path = tmp_path / "cells.json"
    path.write_text(json.dumps({"cells": ["h3_a", "h3_a"]}), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        load_model_h3_cells(path)


def test_load_model_h3_cells_rejects_missing_supported_list(tmp_path):
    path = tmp_path / "cells.json"
    path.write_text(json.dumps({"metadata": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="supported cell list"):
        load_model_h3_cells(path)
