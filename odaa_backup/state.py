"""Состояние источников (последний запуск, последний успех) и блокировка от двойного запуска."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()
TIME_FMT = "%Y-%m-%dT%H:%M:%S"


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / "state.json"


def load_state(data_dir: Path) -> dict:
    p = _path(data_dir)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def update_source(data_dir: Path, source_id: str, **fields) -> dict:
    with _lock:
        state = load_state(data_dir)
        entry = state.setdefault(source_id, {})
        entry.update(fields)
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        tmp = _path(data_dir).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _path(data_dir))
        return entry


def ts(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).strftime(TIME_FMT)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, TIME_FMT)
    except ValueError:
        return None


class RunLock:
    """Файловая блокировка: не даёт запустить два копирования одновременно (Планировщик + кнопка)."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "run.lock"
        self._f = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self) -> None:
        if not self._f:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._f.close()
        self._f = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("копирование уже выполняется (другое окно или Планировщик)")
        return self

    def __exit__(self, *exc):
        self.release()
