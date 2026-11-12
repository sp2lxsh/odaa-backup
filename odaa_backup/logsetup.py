"""Настройка журнала: файл data/logs/odaa-backup.log (с ротацией) + консоль. Секреты вырезаются."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .secretstore import RedactFilter

FMT = "%(asctime)s %(levelname)-7s %(message)s"


def setup_logging(data_dir: Path | None, console: bool = True, verbose: bool = False) -> Path | None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    flt = RedactFilter()
    log_path = None
    if data_dir is not None:
        log_dir = Path(data_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "odaa-backup.log"
        fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=10, encoding="utf-8")
        fh.setFormatter(logging.Formatter(FMT, "%Y-%m-%d %H:%M:%S"))
        fh.addFilter(flt)
        root.addHandler(fh)
    if console and sys.stderr is not None:
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # кириллица в консоли Windows
        except Exception:  # noqa: BLE001
            pass
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        ch.addFilter(flt)
        root.addHandler(ch)
    return log_path
