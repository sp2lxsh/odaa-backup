import json
from datetime import datetime, timedelta

import pytest

from odaa_backup.retention import (
    list_snapshots,
    parse_stamp,
    plan_rotation,
    remove_snapshot,
    select_keep,
)


def test_parse_stamp():
    assert parse_stamp("2026-11-12_0300") == datetime(2026, 11, 12, 3, 0)
    assert parse_stamp("2026-11-12_0300-2") == datetime(2026, 11, 12, 3, 0, 2)
    assert parse_stamp("notes") is None
    assert parse_stamp("2026-13-40_9999") is None


def test_daily_snapshots_for_a_year():
    start = datetime(2026, 1, 1, 3, 0)
    times = [start + timedelta(days=i) for i in range(365)]
    keep = select_keep(times, daily=7, weekly=4, monthly=6)
    newest = max(times)
    # 7 последних дней
    for i in range(7):
        assert newest - timedelta(days=i) in keep
    # всего не больше 7 + 4 + 6
    assert len(keep) <= 17
    # самый старый сохранённый — не старше ~6 месяцев
    assert min(keep) > newest - timedelta(days=200)
    # месяцы различаются
    assert len({(t.year, t.month) for t in keep}) >= 6


def test_several_per_day_keeps_newest_of_day():
    base = datetime(2026, 5, 10)
    times = [base + timedelta(hours=h) for h in (1, 7, 13, 19)]
    keep = select_keep(times, daily=1, weekly=0, monthly=0)
    assert keep == {base + timedelta(hours=19)}


def test_latest_keeps_intraday_snapshots():
    base = datetime(2026, 5, 10)
    times = [base + timedelta(hours=h) for h in (0, 6, 12, 18)]
    keep = select_keep(times, daily=7, weekly=0, monthly=0, latest=3)
    assert keep == set(times[1:])


def test_zero_policy_still_keeps_latest():
    times = [datetime(2026, 1, d) for d in range(1, 10)]
    assert select_keep(times, 0, 0, 0) == {datetime(2026, 1, 9)}


def _mk(tmp_path, name, status):
    d = tmp_path / name
    d.mkdir()
    if status:
        (d / "snapshot.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    (d / "f.txt").write_text("x")
    return d


def test_plan_rotation_protects_last_ok_and_cleans_stale_incomplete(tmp_path):
    now = datetime(2026, 11, 12, 12, 0)
    _mk(tmp_path, "2026-11-01_0300", "ok")
    _mk(tmp_path, "2026-11-10_0300", "partial")
    _mk(tmp_path, "2026-11-11_0300", "partial")
    _mk(tmp_path, "2026-11-05_0300", None)  # не завершён, давно
    _mk(tmp_path, "2026-11-12_1100", None)  # не завершён, свежий — не трогаем
    snaps = list_snapshots(tmp_path)
    to_delete = {s.path.name for s in plan_rotation(snaps, daily=1, weekly=0, monthly=0, now=now)}
    assert "2026-11-01_0300" not in to_delete  # последний полностью успешный
    assert "2026-11-11_0300" not in to_delete  # самый свежий
    assert "2026-11-10_0300" in to_delete
    assert "2026-11-05_0300" in to_delete
    assert "2026-11-12_1100" not in to_delete


def test_remove_snapshot_refuses_foreign_paths(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    d = _mk(src, "2026-11-01_0300", "ok")
    snap = list_snapshots(src)[0]
    remove_snapshot(src, snap)
    assert not d.exists()
    other = tmp_path / "important"
    other.mkdir()
    snap.path = other
    with pytest.raises(RuntimeError):
        remove_snapshot(src, snap)
    assert other.exists()
