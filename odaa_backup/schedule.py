"""Расписание по каждому источнику и интеграция с Планировщиком заданий Windows.

Планировщик Windows запускает программу раз в час с ключом --scheduled,
а программа сама решает, какие источники «пора» копировать. Поэтому частоту
каждого сайта можно менять в интерфейсе, не трогая задачу Windows.

Форматы расписания:
  manual              — только вручную
  every 6h            — каждые N часов (1..168)
  daily 03:00         — ежедневно в указанное время
  weekly mon 03:00    — еженедельно (mon tue wed thu fri sat sun)
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS_RU = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
TASK_NAME = "ODAA Backup"

# Готовые варианты для выпадающего списка в интерфейсе.
PRESETS = [
    ("manual", "Только вручную"),
    ("every 1h", "Каждый час"),
    ("every 3h", "Каждые 3 часа"),
    ("every 6h", "Каждые 6 часов"),
    ("every 12h", "Каждые 12 часов"),
    ("daily 03:00", "Ежедневно в 03:00"),
    ("daily 13:00", "Ежедневно в 13:00"),
    ("weekly sun 03:00", "Еженедельно, вс 03:00"),
]


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Schedule:
    kind: str  # manual | interval | daily | weekly
    hours: int = 0
    hour: int = 0
    minute: int = 0
    weekday: int = 0


def _hhmm(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScheduleError(f"неверное время «{s}», нужно ЧЧ:ММ, например 03:00")
    return int(m.group(1)), int(m.group(2))


def parse_schedule(text: str) -> Schedule:
    t = " ".join(str(text).strip().lower().split())
    if t in ("manual", "вручную", "off", "never"):
        return Schedule("manual")
    m = re.fullmatch(r"every (\d+)\s*h", t)
    if m:
        h = int(m.group(1))
        if not 1 <= h <= 168:
            raise ScheduleError("интервал должен быть от 1 до 168 часов")
        return Schedule("interval", hours=h)
    m = re.fullmatch(r"daily (\S+)", t)
    if m:
        hh, mm = _hhmm(m.group(1))
        return Schedule("daily", hour=hh, minute=mm)
    m = re.fullmatch(r"weekly (\w+) (\S+)", t)
    if m:
        wd = m.group(1)[:3]
        if wd not in WEEKDAYS:
            raise ScheduleError(f"неизвестный день недели «{m.group(1)}», нужно mon/tue/wed/thu/fri/sat/sun")
        hh, mm = _hhmm(m.group(2))
        return Schedule("weekly", hour=hh, minute=mm, weekday=WEEKDAYS.index(wd))
    raise ScheduleError(
        f"непонятное расписание «{text}». Примеры: manual, every 6h, daily 03:00, weekly sun 03:00"
    )


def describe(text: str) -> str:
    try:
        s = parse_schedule(text)
    except ScheduleError:
        return f"ошибка: {text}"
    if s.kind == "manual":
        return "только вручную"
    if s.kind == "interval":
        return "каждый час" if s.hours == 1 else f"каждые {s.hours} ч"
    if s.kind == "daily":
        return f"ежедневно в {s.hour:02d}:{s.minute:02d}"
    return f"еженедельно в {WEEKDAYS_RU[s.weekday]}, {s.hour:02d}:{s.minute:02d}"


def nominal_interval(text: str) -> timedelta | None:
    """Номинальный интервал между копиями (для предупреждений «копия устарела»)."""
    s = parse_schedule(text)
    return {
        "manual": None,
        "interval": timedelta(hours=s.hours),
        "daily": timedelta(days=1),
        "weekly": timedelta(days=7),
    }[s.kind]


def last_slot(s: Schedule, now: datetime) -> datetime | None:
    """Самый поздний плановый момент запуска, не превышающий now (для daily/weekly)."""
    if s.kind == "daily":
        slot = now.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
        return slot if slot <= now else slot - timedelta(days=1)
    if s.kind == "weekly":
        slot = now.replace(hour=s.hour, minute=s.minute, second=0, microsecond=0)
        slot -= timedelta(days=(now.weekday() - s.weekday) % 7)
        return slot if slot <= now else slot - timedelta(days=7)
    return None


def is_due(
    text: str,
    now: datetime,
    last_success: datetime | None,
    last_attempt: datetime | None = None,
    retry_minutes: int = 60,
) -> bool:
    """Пора ли делать копию. Пропущенный запуск (компьютер был выключен) догоняется при следующей проверке."""
    s = parse_schedule(text)
    if s.kind == "manual":
        return False
    # После неудачи не долбим сервер чаще, чем раз в retry_minutes.
    if last_attempt and (last_success is None or last_attempt > last_success):
        if now - last_attempt < timedelta(minutes=retry_minutes):
            return False
    if last_success is None:
        return True
    if s.kind == "interval":
        # Небольшой допуск, чтобы «каждый час» не пропускал запуск из-за секунд.
        return now - last_success >= timedelta(hours=s.hours) - timedelta(minutes=5)
    slot = last_slot(s, now)
    return slot is not None and last_success < slot


# --------------------------------------------------------------------------- Планировщик Windows


def _decode(b: bytes) -> str:
    for enc in ("oem", "cp866", "utf-8"):
        try:
            return b.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return b.decode("utf-8", "replace")


def scheduled_command(config_path: str) -> str:
    """Командная строка, которую запускает Планировщик (без окна консоли, если есть odaa-backup-bg.exe)."""
    import os

    if getattr(sys, "frozen", False):
        exe = sys.executable
        bg = os.path.join(os.path.dirname(exe), "odaa-backup-bg.exe")
        if os.path.exists(bg):
            exe = bg
        return f'"{exe}" run --scheduled --config "{config_path}"'
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.exists(pyw):
        py = pyw
    return f'"{py}" -m odaa_backup run --scheduled --config "{config_path}"'


def install_task(config_path: str) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Планировщик заданий доступен только в Windows."
    cmd = scheduled_command(config_path)
    # Проверка каждый час; сам запуск копии — по расписанию каждого источника.
    args = ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "HOURLY", "/MO", "1", "/ST", "00:07", "/TR", cmd]
    r = subprocess.run(args, capture_output=True)
    out = (_decode(r.stdout) + _decode(r.stderr)).strip()
    return r.returncode == 0, out


def remove_task() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Планировщик заданий доступен только в Windows."
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True)
    return r.returncode == 0, (_decode(r.stdout) + _decode(r.stderr)).strip()


def task_status() -> str:
    if sys.platform != "win32":
        return "не Windows — задача недоступна"
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], capture_output=True)
    if r.returncode != 0:
        return "не создана"
    return "создана (проверка каждый час)"
