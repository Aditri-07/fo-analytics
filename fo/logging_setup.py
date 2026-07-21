"""Central logging setup: console + rotating file handler."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-7s %(name)s :: %(message)s"


def setup_logging(level: str = "INFO", log_file: str | Path = "logs/fo.log") -> None:
    root = logging.getLogger()
    if root.handlers:            # already configured (tests, repeat calls)
        return
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FMT))
    root.addHandler(console)

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fileh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3)
    fileh.setFormatter(logging.Formatter(_FMT))
    root.addHandler(fileh)
