"""Remove orphaned `docker compose run` sumo-service containers."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sumo_service"))

from scripts.docker_run_helpers import cleanup_sumo_run_containers  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Stop/remove backend-sumo-service-run-* containers")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    return cleanup_sumo_run_containers(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
