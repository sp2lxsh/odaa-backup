"""Манифест SHA-256 снимка и проверка целостности."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .paths import local_path, lp

MANIFEST_NAME = "manifest.sha256"
SERVICE_FILES = {MANIFEST_NAME, "snapshot.json", "api_backups.json"}


def sha256_file(path: os.PathLike | str, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(lp(path), "rb") as f:
        while True:
            chunk = f.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def write_manifest(snap_dir: Path, entries: dict[str, str]) -> Path:
    """entries: относительный путь внутри снимка ('files/site/index.php') -> sha256."""
    p = snap_dir / MANIFEST_NAME
    tmp = snap_dir / (MANIFEST_NAME + ".tmp")
    with open(lp(tmp), "w", encoding="utf-8", newline="\n") as f:
        for rel in sorted(entries):
            f.write(f"{entries[rel]}  {rel}\n")
    os.replace(lp(tmp), lp(p))
    return p


def read_manifest(snap_dir: Path) -> dict[str, str]:
    p = snap_dir / MANIFEST_NAME
    out: dict[str, str] = {}
    if not os.path.exists(lp(p)):
        return out
    with open(lp(p), encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if len(line) < 67 or line[64:66] != "  ":
                continue
            out[line[66:]] = line[:64].lower()
    return out


@dataclass
class VerifyResult:
    checked: int = 0
    missing: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    no_manifest: bool = False

    @property
    def ok(self) -> bool:
        return not (self.missing or self.mismatched or self.no_manifest)

    def summary(self) -> str:
        if self.no_manifest:
            return "нет манифеста — снимок не завершён или повреждён"
        if self.ok:
            s = f"OK: проверено файлов {self.checked}, все контрольные суммы совпали"
            if self.extra:
                s += f" (лишних файлов вне манифеста: {len(self.extra)})"
            return s
        return (
            f"ОШИБКА: проверено {self.checked}, отсутствует {len(self.missing)}, "
            f"не совпало {len(self.mismatched)}"
        )


def verify_snapshot(snap_dir: Path, progress=None) -> VerifyResult:
    res = VerifyResult()
    entries = read_manifest(snap_dir)
    if not entries and not os.path.exists(lp(snap_dir / MANIFEST_NAME)):
        res.no_manifest = True
        return res
    for rel, expected in sorted(entries.items()):
        p = local_path(snap_dir, rel)
        if not os.path.exists(lp(p)):
            res.missing.append(rel)
            continue
        res.checked += 1
        if progress:
            progress(rel)
        if sha256_file(p) != expected:
            res.mismatched.append(rel)
    # Файлы, которых нет в манифесте (кроме служебных).
    base = lp(snap_dir)
    for root, _dirs, files in os.walk(base):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            if rel in SERVICE_FILES or rel in entries or name.endswith(".part"):
                continue
            res.extra.append(rel)
    return res
