"""Environment configuration.

Select environment with FO_ENV (dev|uat|prod), default dev. Each env
gets its own database and feed directory — the "simulated environments"
production control, kept deliberately simple: config profiles, not
infrastructure.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    env: str
    db_path: Path
    feed_dir: Path
    stale_days_warn: int = 3      # event older than N days vs feed date -> WARN
    log_level: str = "INFO"


_PROFILES = {
    "dev":  Settings("dev",  Path("data/dev/fo_analytics.db"),  Path("data/feeds")),
    "uat":  Settings("uat",  Path("data/uat/fo_analytics.db"),  Path("data/uat/feeds")),
    "prod": Settings("prod", Path("data/prod/fo_analytics.db"), Path("data/prod/feeds"),
                     log_level="WARNING"),
}


def get_settings(env: str | None = None) -> Settings:
    name = (env or os.environ.get("FO_ENV", "dev")).lower()
    try:
        return _PROFILES[name]
    except KeyError:
        raise ValueError(f"unknown FO_ENV '{name}' (expected dev|uat|prod)") from None
