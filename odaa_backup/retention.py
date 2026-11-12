"""Ротация снимков: N ежедневных, N еженедельных, N ежемесячных (схема «дед-отец-сын»).

Удаляются только ЛОКАЛЬНЫЕ снимки в папке бэкапов. На серверах ничего не удаляется.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .paths import lp

STAMP_FMT = "%Y-%m-%d_%H%M"
STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{4})(?:-(\d+))?$")
SNAPSHOT_INFO = "snapshot.json"


def parse_stamp(name: str) -> datetime | None:
    m = STAMP_RE.match(name)
    if not m:
        return None
    try:
        t = datetime.strptime(m.group(1), STAMP_FMT)
    except ValueError:
        return None
    # Суффикс -2, -3 … для нескольких снимков в одну минуту.
    return t + timedelta(seconds=int(m.group(2) or 0))


@dataclass
class SnapshotInfo:
    path: Path
    time: datetime
    status: str | None  # ok | partial | None (не завершён)
    info: dict

    @property
    def complete(self) -> bool:
        return self.status in ("ok", "partial")


def list_snapshots(source_dir: Path) -> list[SnapshotInfo]:
    """Все снимки источника, от новых к старым."""
    out: list[SnapshotInfo] = []
    if not source_dir.is_dir():
        return out
    for entry in os.scandir(lp(source_dir)):
        if not entry.is_dir(follow_symlinks=False):
            continue
        t = parse_stamp(entry.name)
        if t is None:
            continue
        p = source_dir / entry.name
        info: dict = {}
        try:
            with open(lp(p / SNAPSHOT_INFO), encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, ValueError):
            info = {}
        out.append(SnapshotInfo(path=p, time=t, status=info.get("status"), info=info))
    out.sort(key=lambda s: s.time, reverse=True)
    return out


def select_keep(times: list[datetime], daily: int, weekly: int, monthly: int, latest: int = 0) -> set[datetime]:
    """Какие снимки оставить. Всегда оставляется самый свежий."""
    s = sorted(set(times), reverse=True)
    keep: set[datetime] = set()
    if not s:
        return keep
    keep.add(s[0])
    keep.update(s[:latest])

    def bucket(key, n: int) -> None:
        seen: list = []
        for t in s:
            k = key(t)
            if k in seen:
                continue
            if len(seen) >= n:
                break
            seen.append(k)
            keep.add(t)  # самый новый снимок в своём дне/неделе/месяце

    bucket(lambda t: t.date(), daily)
    bucket(lambda t: tuple(t.isocalendar())[:2], weekly)
    bucket(lambda t: (t.year, t.month), monthly)
    return keep


def plan_rotation(
    snapshots: list[SnapshotInfo],
    daily: int,
    weekly: int,
    monthly: int,
    now: datetime | None = None,
    latest: int = 0,
    protect: Path | None = None,
    stale_incomplete_days: int = 3,
) -> list[SnapshotInfo]:
    """Список снимков к удалению."""
    now = now or datetime.now()
    complete = [s for s in snapshots if s.complete]
    keep_times = select_keep([s.time for s in complete], daily, weekly, monthly, latest)
    ok = [s for s in complete if s.status == "ok"]
    if ok:  # последний полностью успешный снимок не удаляем никогда
        keep_times.add(max(ok, key=lambda s: s.time).time)
    delete = [s for s in complete if s.time not in keep_times]
    for s in snapshots:
        if not s.complete and now - s.time > timedelta(days=stale_incomplete_days):
            delete.append(s)
    if protect is not None:
        delete = [s for s in delete if s.path != protect]
    return delete


def remove_snapshot(source_dir: Path, snap: SnapshotInfo) -> None:
    """Удалить локальный снимок (с защитой от удаления чего-то вне папки источника)."""
    src = os.path.realpath(lp(source_dir))
    target = os.path.realpath(lp(snap.path))
    if os.path.dirname(target) != src or parse_stamp(os.path.basename(target)) is None:
        raise RuntimeError(f"отказ удалять подозрительный путь: {snap.path}")

    def on_error(func, path, _exc):
        # Файлы «только для чтения» в Windows: снимаем атрибут и пробуем снова.
        try:
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=on_error)
    else:
        shutil.rmtree(target, onerror=on_error)
