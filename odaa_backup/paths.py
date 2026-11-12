"""Работа с путями: длинные пути Windows, безопасные имена файлов, папка программы."""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Символы, запрещённые в именах файлов Windows.
_BAD_CHARS = set('<>:"\\|?*')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}


def app_dir() -> Path:
    """Папка программы: рядом с .exe (PyInstaller) или текущая папка при запуске из исходников."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def lp(path: os.PathLike | str) -> str:
    """Вернуть путь в виде, пригодном для очень длинных путей Windows (\\\\?\\C:\\...).

    На Linux/macOS возвращает абсолютный путь без изменений.
    """
    s = os.path.abspath(os.fspath(path))
    if not IS_WINDOWS:
        return s
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def safe_component(name: str) -> str:
    """Сделать имя файла/папки допустимым для Windows (кириллица сохраняется как есть).

    Запрещённые символы кодируются как %XX, зарезервированные имена (CON, NUL...)
    получают суффикс «_», точки и пробелы в конце кодируются.
    """
    if name in ("", ".", ".."):
        return name.replace(".", "%2E") or "%00"
    out = []
    for ch in name:
        if ch in _BAD_CHARS or ord(ch) < 32:
            out.append("%{:02X}".format(ord(ch)))
        else:
            out.append(ch)
    s = "".join(out)
    stripped = s.rstrip(". ")
    if stripped != s:
        tail = s[len(stripped):]
        s = stripped + "".join("%2E" if c == "." else "%20" for c in tail)
    stem = s.split(".", 1)[0].upper()
    if stem in _RESERVED:
        s = s[: len(stem)] + "_" + s[len(stem):]
    return s


def safe_relpath(rel: str) -> str:
    """Применить safe_component к каждой части пути вида 'a/b/c'."""
    return "/".join(safe_component(p) for p in rel.split("/") if p != "")


def local_path(base: Path, rel: str) -> Path:
    """Собрать локальный путь из базовой папки и относительного пути с '/'."""
    parts = [p for p in rel.split("/") if p]
    return base.joinpath(*parts) if parts else base


def human_size(n: float | int | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def dir_size(path: Path) -> int:
    """Логический размер папки (жёсткие ссылки считаются полностью)."""
    total = 0
    for root, _dirs, files in os.walk(lp(path)):
        for f in files:
            try:
                total += os.stat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total
