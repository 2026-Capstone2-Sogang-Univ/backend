"""sumo_service application package.

Load environment variables from a local ``.env`` file *before* any submodule
reads configuration at import time. ``simulation.py`` evaluates
``DEMAND_FORECAST_*`` / ``PREDICTION_URL`` and ``prediction.py`` reads
``PREDICTION_*`` via ``os.getenv`` as soon as they are imported, so the ``.env``
must be applied here (the package root), which is imported before any of them.

Real environment variables take precedence over the ``.env`` file
(``override=False`` is the default), so docker-compose ``environment:`` entries
and shell exports still win over file values.
"""
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv not installed: fall back to real env vars only
    pass
else:
    # sumo_service/.env — one directory up from this package.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
