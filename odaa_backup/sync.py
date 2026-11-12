"""Инкрементальное копирование папок с сервера в снимок.

Неизменённые файлы (тот же размер и время изменения) не скачиваются заново:
на них ставится жёсткая ссылка на файл из предыдущего снимка (место на диске
не тратится, а каждый снимок выглядит как полная копия). Если жёсткие ссылки
не поддерживаются (FAT/exFAT, сетевой диск) — файл копируется локально.

Прерванная загрузка продолжается с места остановки (файлы *.part).
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import FileSpec, Network
from .manifest import sha256_file
from .paths import local_path, lp, safe_relpath
from .ssh import NETWORK_ERRORS, is_dir, is_link, is_regular

log = logging.getLogger(__name__)
CHUNK = 1 << 20


class IncompleteDownload(Exception):
    pass


# --------------------------------------------------------------------------- исключения (exclude)


def _glob_to_regex(pat: str) -> str:
    i, out = 0, []
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


class Excluder:
    """Шаблоны исключений в стиле .gitignore (упрощённо).

    'node_modules'        — папка/файл с таким именем на любом уровне
    '*.log'               — по имени на любом уровне
    'wp-content/cache'    — путь от корня копируемой папки (вместе с содержимым)
    'wp-content/cache/**' — то же самое
    """

    def __init__(self, patterns: list[str]):
        self.name_res: list[re.Pattern] = []
        self.path_res: list[re.Pattern] = []
        for p in patterns:
            p = p.strip().replace("\\", "/")
            if not p:
                continue
            anchored = "/" in p.rstrip("/")
            p = p.strip("/")
            if p.endswith("/**"):
                p = p[:-3]
            rx = re.compile(_glob_to_regex(p) + r"\Z")
            (self.path_res if anchored else self.name_res).append(rx)

    def excluded(self, rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        if any(r.match(name) for r in self.name_res):
            return True
        return any(r.match(rel) for r in self.path_res)


# --------------------------------------------------------------------------- вспомогательное


class RateLimiter:
    def __init__(self, kbps: int):
        self.rate = kbps * 1024
        self.start = time.monotonic()
        self.sent = 0

    def consume(self, n: int) -> None:
        if self.rate <= 0:
            return
        self.sent += n
        ahead = self.sent / self.rate - (time.monotonic() - self.start)
        if ahead > 0:
            time.sleep(ahead)


@dataclass
class SyncStats:
    files: int = 0
    total_bytes: int = 0
    downloaded: int = 0
    downloaded_bytes: int = 0
    linked: int = 0
    copied_local: int = 0
    reused: int = 0
    excluded: int = 0
    skipped_links: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    # для режима «показать, что будет сделано»
    plan_new: int = 0
    plan_changed: int = 0
    plan_unchanged: int = 0
    plan_bytes: int = 0

    def merge(self, o: "SyncStats") -> None:
        for k, v in o.__dict__.items():
            cur = getattr(self, k)
            setattr(self, k, cur + v)


@dataclass
class RemoteFile:
    rel: str  # относительный путь с '/'
    path: str  # полный путь на сервере
    size: int
    mtime: int


def _listdir(remote, path: str, net: Network | None):
    retries = net.retries if net else 0
    for attempt in range(retries + 1):
        try:
            return remote.listdir_attr(path)
        except (PermissionError, FileNotFoundError):
            raise
        except NETWORK_ERRORS as e:
            if attempt >= retries:
                raise
            wait = net.retry_delay * (attempt + 1)
            log.warning("Ошибка чтения папки %s: %s — повтор через %s с", path, e, wait)
            time.sleep(wait)
            try:
                remote.reconnect()
            except Exception as e2:  # noqa: BLE001
                log.warning("Переподключение не удалось: %s", e2)
    return []


def walk_remote(remote, root: str, excluder: Excluder, stats: SyncStats, net: Network | None = None):
    """Обойти папку на сервере, возвращая обычные файлы. Символьные ссылки пропускаются."""
    st = remote.stat(root)
    if is_regular(st.st_mode):
        yield RemoteFile(posixpath.basename(root), root, st.st_size or 0, int(st.st_mtime or 0))
        return
    stack = [("", root)]
    while stack:
        rel_dir, abs_dir = stack.pop()
        try:
            entries = _listdir(remote, abs_dir, net)
        except (PermissionError, FileNotFoundError) as e:
            stats.warnings.append(f"нет доступа к папке {abs_dir}: {e}")
            continue
        for a in sorted(entries, key=lambda x: x.filename):
            rel = f"{rel_dir}/{a.filename}" if rel_dir else a.filename
            full = posixpath.join(abs_dir, a.filename)
            if excluder.excluded(rel):
                stats.excluded += 1
                continue
            if is_link(a.st_mode):
                stats.skipped_links += 1
                continue
            if is_dir(a.st_mode):
                stack.append((rel, full))
            elif is_regular(a.st_mode):
                yield RemoteFile(rel, full, a.st_size or 0, int(a.st_mtime or 0))


def _same(local: Path, size: int, mtime: int) -> bool:
    try:
        st = os.stat(lp(local))
    except OSError:
        return False
    return st.st_size == size and int(st.st_mtime) == mtime


def download_file(remote, rf: RemoteFile, local: Path, limiter: RateLimiter, progress=None) -> int:
    """Скачать файл с докачкой. Возвращает число реально переданных байт."""
    part = local.with_name(local.name + ".part")
    os.makedirs(lp(local.parent), exist_ok=True)
    offset = os.path.getsize(lp(part)) if os.path.exists(lp(part)) else 0
    if offset > rf.size:
        offset = 0
        os.remove(lp(part))
    transferred = 0
    with remote.open(rf.path) as src:
        if offset:
            src.seek(offset)
            log.info("Докачка %s с %d байт", rf.rel, offset)
        if limiter.rate <= 0 and rf.size - offset > CHUNK and hasattr(src, "prefetch"):
            try:
                src.prefetch(rf.size, max_concurrent_requests=64)
            except TypeError:
                src.prefetch(rf.size)
        with open(lp(part), "ab") as dst:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
                transferred += len(chunk)
                limiter.consume(len(chunk))
                if progress:
                    progress(len(chunk))
    got = os.path.getsize(lp(part))
    if got != rf.size:
        if got > rf.size:
            os.remove(lp(part))
        raise IncompleteDownload(f"{rf.rel}: получено {got} из {rf.size} байт (файл меняется на сервере?)")
    os.replace(lp(part), lp(local))
    os.utime(lp(local), (rf.mtime, rf.mtime))
    return transferred


def link_or_copy(src: Path, dst: Path) -> bool:
    """Жёсткая ссылка на файл прошлого снимка; при невозможности — копия. True = ссылка."""
    os.makedirs(lp(dst.parent), exist_ok=True)
    if os.path.exists(lp(dst)):
        os.remove(lp(dst))
    try:
        os.link(lp(src), lp(dst))
        return True
    except OSError:
        shutil.copy2(lp(src), lp(dst))
        return False


def sync_tree(
    remote,
    spec: FileSpec,
    snap_dir: Path,
    prev_dir: Path | None,
    prev_manifest: dict[str, str],
    net: Network,
    manifest: dict[str, str],
    dry_run: bool = False,
    verify_remote: bool = True,
    progress=None,
) -> SyncStats:
    """Скопировать одну папку (spec.remote) в snap_dir/files/<spec.name>/."""
    stats = SyncStats()
    excluder = Excluder(spec.exclude)
    prefix = f"files/{spec.name}"
    dest_root = local_path(snap_dir, prefix)
    prev_root = local_path(prev_dir, prefix) if prev_dir else None
    limiter = RateLimiter(net.rate_limit_kbps)
    downloaded: list[tuple[RemoteFile, Path, str]] = []

    def on_bytes(n: int) -> None:
        if progress:
            progress(bytes_=n)

    for rf in walk_remote(remote, spec.remote, excluder, stats, net):
        stats.files += 1
        stats.total_bytes += rf.size
        safe_rel = safe_relpath(rf.rel)
        if safe_rel != rf.rel:
            stats.renamed.append(f"{rf.rel} -> {safe_rel}")
        snap_rel = f"{prefix}/{safe_rel}"
        local = local_path(dest_root, safe_rel)
        prev = local_path(prev_root, safe_rel) if prev_root else None
        if progress:
            progress(file=rf.rel)

        if dry_run:
            if prev is not None and _same(prev, rf.size, rf.mtime):
                stats.plan_unchanged += 1
            else:
                if prev is not None and os.path.exists(lp(prev)):
                    stats.plan_changed += 1
                else:
                    stats.plan_new += 1
                stats.plan_bytes += rf.size
            continue

        # 1) уже есть в этом снимке (возобновлённый запуск)
        if _same(local, rf.size, rf.mtime):
            stats.reused += 1
            known = prev_manifest.get(snap_rel) if prev is not None and _same(prev, rf.size, rf.mtime) else None
            manifest[snap_rel] = known or sha256_file(local)
            continue
        # 2) не изменился с прошлого снимка — жёсткая ссылка
        if prev is not None and _same(prev, rf.size, rf.mtime):
            try:
                if link_or_copy(prev, local):
                    stats.linked += 1
                else:
                    stats.copied_local += 1
                manifest[snap_rel] = prev_manifest.get(snap_rel) or sha256_file(local)
                continue
            except OSError as e:
                log.warning("Не удалось взять %s из прошлого снимка (%s), скачиваю заново", rf.rel, e)
        # 3) скачать
        ok = _download_with_retries(remote, rf, local, limiter, net, stats, on_bytes)
        if ok:
            h = sha256_file(local)
            manifest[snap_rel] = h
            downloaded.append((rf, local, snap_rel))

    if not dry_run and verify_remote and downloaded:
        _verify_against_server(remote, downloaded, manifest, limiter, net, stats, on_bytes)
    return stats


def _download_with_retries(remote, rf, local, limiter, net, stats, on_bytes) -> bool:
    for attempt in range(net.retries + 1):
        try:
            n = download_file(remote, rf, local, limiter, on_bytes)
            stats.downloaded += 1
            stats.downloaded_bytes += n
            return True
        except PermissionError as e:
            stats.errors.append(f"{rf.path}: нет доступа ({e})")
            return False
        except (IncompleteDownload, *NETWORK_ERRORS) as e:
            if isinstance(e, FileNotFoundError):
                stats.warnings.append(f"{rf.path}: файл исчез с сервера во время копирования")
                return False
            if attempt >= net.retries:
                stats.errors.append(f"{rf.path}: {e}")
                return False
            wait = net.retry_delay * (attempt + 1)
            log.warning("Ошибка при скачивании %s: %s — повтор через %s с", rf.rel, e, wait)
            time.sleep(wait)
            if not isinstance(e, IncompleteDownload):
                try:
                    remote.reconnect()
                except Exception as e2:  # noqa: BLE001
                    log.warning("Переподключение не удалось: %s", e2)
    return False


def _verify_against_server(remote, downloaded, manifest, limiter, net, stats, on_bytes) -> None:
    """Сравнить SHA-256 скачанных файлов с посчитанными на сервере (sha256sum)."""
    try:
        remote_hashes = remote.sha256_many([rf.path for rf, _l, _r in downloaded])
    except Exception as e:  # noqa: BLE001
        remote_hashes = None
        log.warning("Проверка контрольных сумм на сервере не удалась: %s", e)
    if remote_hashes is None:
        stats.warnings.append("на сервере недоступен sha256sum — проверены только размеры файлов")
        return
    for rf, local, snap_rel in downloaded:
        rh = remote_hashes.get(rf.path)
        if rh is None or rh == manifest.get(snap_rel):
            continue
        log.warning("Контрольная сумма %s не совпала с сервером — скачиваю повторно", rf.rel)
        try:
            os.remove(lp(local))
        except OSError:
            pass
        if _download_with_retries(remote, rf, local, limiter, net, stats, on_bytes):
            h = sha256_file(local)
            manifest[snap_rel] = h
            if h != rh:
                stats.warnings.append(f"{rf.path}: файл изменился на сервере во время проверки")
