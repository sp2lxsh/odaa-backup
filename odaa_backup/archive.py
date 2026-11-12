"""Вторая копия (правило 3-2-1): архив снимка на другой диск, по желанию с шифрованием 7-Zip (AES-256)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from . import secretstore
from .config import Config, Source
from .paths import lp
from .retention import parse_stamp

log = logging.getLogger(__name__)

SEVEN_ZIP_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
]


def find_7z(configured: str = "") -> str | None:
    for c in [configured, *SEVEN_ZIP_CANDIDATES]:
        if c and os.path.exists(c):
            return c
    return shutil.which("7z") or shutil.which("7za")


def second_copy_available(cfg: Config) -> tuple[bool, str]:
    sc = cfg.second_copy
    if not sc.path:
        return False, "вторая копия не настроена (second_copy.path)"
    if not os.path.isdir(sc.path):
        return False, f"папка второй копии недоступна: {sc.path} (диск не подключён?)"
    try:
        if os.path.splitdrive(os.path.abspath(sc.path))[0].lower() == \
                os.path.splitdrive(os.path.abspath(cfg.backup_root))[0].lower() and os.name == "nt":
            return False, "вторая копия на том же диске, что и основная — это не защищает от поломки диска"
    except Exception:  # noqa: BLE001
        pass
    return True, ""


def make_second_copy(cfg: Config, src: Source, snap_dir: Path) -> str:
    """Положить архив снимка во вторую папку. Возвращает путь к архиву."""
    sc = cfg.second_copy
    dest_dir = Path(sc.path) / src.provider / src.id
    os.makedirs(lp(dest_dir), exist_ok=True)
    name = snap_dir.name
    if sc.encrypt:
        exe = find_7z(sc.seven_zip)
        if not exe:
            raise RuntimeError("для шифрования нужен 7-Zip (https://7-zip.org) — установите его или encrypt: false")
        password = secretstore.require(sc.password_secret)
        target = dest_dir / f"{name}.7z"
        tmp = dest_dir / f"{name}.7z.tmp"
        if tmp.exists():
            tmp.unlink()
        # -mhe=on шифрует и имена файлов; -mx=1 — быстро (данные сайтов и так часто сжаты)
        cmd = [exe, "a", "-t7z", "-mx=1", "-mhe=on", f"-p{password}", "-bso0", "-bsp0", "-y", str(tmp), "."]
        r = subprocess.run(cmd, cwd=str(snap_dir), capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            err = secretstore.redact(r.stderr.decode("utf-8", "replace"))[:400]
            raise RuntimeError(f"7-Zip вернул код {r.returncode}: {err}")
        os.replace(tmp, target)
    else:
        target = dest_dir / name
        if target.exists():
            shutil.rmtree(lp(target))
        shutil.copytree(lp(snap_dir), lp(target))
    _rotate(dest_dir, sc.keep)
    return str(target)


def _rotate(dest_dir: Path, keep: int) -> None:
    items = []
    for e in os.scandir(dest_dir):
        stem = e.name[:-3] if e.name.endswith(".7z") else e.name
        t = parse_stamp(stem)
        if t is not None:
            items.append((t, e))
    items.sort(key=lambda x: x[0], reverse=True)
    for _t, e in items[keep:]:
        log.info("Вторая копия: удаляю старый архив %s", e.path)
        if e.is_dir():
            shutil.rmtree(lp(e.path))
        else:
            os.remove(e.path)
