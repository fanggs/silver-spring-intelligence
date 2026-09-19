"""Shared paths and private configuration for the data pipeline."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIRECTORY = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIRECTORY / "silverspring.db"


def load_project_env() -> None:
    """Load simple KEY=VALUE entries from the uncommitted project .env file."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_project_env()
CENSUS_API_KEY = os.getenv("CENSUS_API_KEY", "")
