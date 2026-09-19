"""Shared paths and private configuration for the data pipeline."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIRECTORY = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIRECTORY / "silverspring.db"


def load_project_env() -> None:
    """Load simple KEY=VALUE entries from the uncommitted project .env file."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        # If an activation URL was pasted rather than its key, recover just
        # the key parameter. A normal key is left unchanged.
        if key.strip() == "CENSUS_API_KEY" and value.startswith(("http://", "https://")):
            value = parse_qs(urlparse(value).query).get("key", [value])[0]
        # This project file is the source of truth for local development.
        # ``setdefault`` could silently retain an old system-level API key.
        os.environ[key.strip()] = value


load_project_env()
CENSUS_API_KEY = os.getenv("CENSUS_API_KEY", "")
