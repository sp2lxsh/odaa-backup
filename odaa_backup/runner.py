"""Выполнение резервного копирования: снимки, дампы, манифест, ротация, отчёт."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from . import apis, secretstore
from .archive import make_second_copy, second_copy_available
from .config import Config, Source
from .manifest import read_manifest, sha256_file, write_manifest
from .mysql import DumpError, dump_database
from .paths import dir_size, human_size, lp
from .report import build_report, send_telegram
from .retention import SNAPSHOT_INFO, STAMP_FMT, list_snapshots, plan_rotation, remove_snapshot
from .schedule import is_due
from .ssh import AuthError, HostKeyError, SFTPRemote, connect_ssh
from .state import RunLock, load_state, parse_ts, ts, update_source
from .sync import SyncStats, sync_tree

log = logging.getLogger(__name__)


class Progress:
    """Текущее состояние выполнения (читается веб-интерфейсом)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.running = False
            self.dry_run = False
            self.source = ""
            self.phase = ""
            self.file = ""
            self.bytes = 0
            self.started: float | None = None
            self.finished_report = getattr(self, "finished_report", "")

    def update(self, **kw) -> None:
        with self._lock:
            if "bytes_" in kw:
                self.bytes += kw.pop("bytes_")
            for k, v in kw.items():
                setattr(self, k, v)

    def __call__(self, **kw) -> None:
        self.update(**kw)

    def as_dict(self) -> dict:
        with self._lock:
            elapsed = time.monotonic() - self.started if self.started else 0
            return {
                "running": self.running,
                "dry_run": self.dry_run,
                "source": self.source,
                "phase": self.phase,
                "file": self.file,
                "bytes": self.bytes,
                "bytes_h": human_size(self.bytes),
                "speed_h": human_size(self.bytes / elapsed) + "/с" if elapsed > 1 else "",
                "report": self.finished_report,
            }


PROGRESS = Progress()


def source_dir(cfg: Config, src: Source) -> Path:
    return cfg.backup_root / src.provider / src.id


def _new_snapshot_dir(base: Path, now: datetime) -> Path:
    name = now.strftime(STAMP_FMT)
    p = base / name
    n = 2
    while os.path.exists(lp(p)):
        p = base / f"{name}-{n}"
        n += 1
    return p


def _write_info(snap: Path, info: dict) -> None:
    tmp = snap / (SNAPSHOT_INFO + ".tmp")
    with open(lp(tmp), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    os.replace(lp(tmp), lp(snap / SNAPSHOT_INFO))


def due_sources(cfg: Config, now: datetime | None = None) -> list[Source]:
    now = now or datetime.now()
    state = load_state(cfg.data_dir)
    out = []
    for s in cfg.sources:
        if not s.enabled:
            continue
        e = state.get(s.id, {})
        if is_due(s.schedule, now, parse_ts(e.get("last_success")), parse_ts(e.get("last_attempt")),
                  cfg.network.retry_failed_after_minutes):
            out.append(s)
    return out


def run(cfg: Config, source_ids: list[str] | None = None, scheduled: bool = False, dry_run: bool = False,
        interactive: bool = False) -> tuple[list[dict], str]:
    """Главная точка входа. Возвращает (результаты по источникам, текст отчёта)."""
    lock = RunLock(cfg.data_dir)
    if not lock.acquire():
        msg = "Копирование уже выполняется (окно программы или Планировщик). Попробуйте позже."
        log.warning(msg)
        return [], msg
    PROGRESS.reset()
    PROGRESS.update(running=True, dry_run=dry_run, started=time.monotonic(), finished_report="")
    try:
        if source_ids:
            sources = [cfg.source(sid) for sid in source_ids]
        elif scheduled:
            sources = due_sources(cfg)
            if not sources:
                log.info("Плановая проверка: копировать пока нечего")
                return [], ""
        else:
            sources = [s for s in cfg.sources if s.enabled]
        log.info("Запуск%s: %s", " (пробный)" if dry_run else "", ", ".join(s.id for s in sources) or "—")
        results = []
        for src in sources:
            PROGRESS.update(source=src.title, phase="подготовка", file="")
            results.append(run_source(cfg, src, dry_run=dry_run, interactive=interactive))
        report = build_report(cfg, results, dry_run=dry_run)
        _save_report(cfg, report)
        log.info("Отчёт:\n%s", report)
        if not dry_run and results:
            has_errors = any(r["status"] != "ok" for r in results)
            send_telegram(cfg, report, has_errors)
        PROGRESS.update(finished_report=report)
        return results, report
    finally:
        PROGRESS.update(running=False, phase="готово", file="")
        lock.release()


def _save_report(cfg: Config, report: str) -> None:
    d = cfg.data_dir / "reports"
    d.mkdir(parents=True, exist_ok=True)
    (d / "last_report.txt").write_text(report, encoding="utf-8")
    (d / f"report_{datetime.now():%Y-%m-%d_%H%M%S}.txt").write_text(report, encoding="utf-8")
    reports = sorted(d.glob("report_*.txt"))
    for old in reports[:-200]:
        old.unlink(missing_ok=True)


def run_source(cfg: Config, src: Source, dry_run: bool = False, interactive: bool = False) -> dict:
    started = datetime.now()
    res: dict = {"id": src.id, "title": src.title, "status": "failed", "summary": "", "errors": [], "warnings": []}
    if not dry_run:
        update_source(cfg.data_dir, src.id, last_attempt=ts(started), last_status="running")
    try:
        _run_source_inner(cfg, src, res, dry_run, interactive, started)
    except (HostKeyError, AuthError, secretstore.MissingSecret, ConnectionError) as e:
        res["status"] = "failed"
        res["errors"].append(str(e))
        res["summary"] = "не выполнено"
        log.error("%s: %s", src.id, e)
    except Exception as e:  # noqa: BLE001
        res["status"] = "failed"
        res["errors"].append(f"непредвиденная ошибка: {e}")
        res["summary"] = "не выполнено"
        log.exception("%s: непредвиденная ошибка", src.id)
    if not dry_run:
        fields = {"last_status": res["status"], "last_error": "; ".join(res["errors"][:3]),
                  "last_finished": ts()}
        if res["status"] in ("ok", "partial"):
            # «partial» (часть файлов не прочиталась) тоже считается копией — иначе расписание
            # повторяло бы запуск каждый час из-за одного нечитаемого файла.
            fields.update(last_success=ts(started), last_snapshot=res.get("snapshot"), last_size=res.get("size"))
        if res.get("api_backups") is not None:
            fields["api_backups"] = res["api_backups"][:50]
        if res.get("api_error"):
            fields["api_error"] = res["api_error"]
        update_source(cfg.data_dir, src.id, **fields)
    return res


def _run_source_inner(cfg: Config, src: Source, res: dict, dry_run: bool, interactive: bool,
                      started: datetime) -> None:
    base = source_dir(cfg, src)
    snaps = list_snapshots(base)
    prev = next((s for s in snaps if s.complete), None)
    resume = next((s for s in snaps if not s.complete and (datetime.now() - s.time).days < 2), None)
    if resume and prev and resume.time < prev.time:
        resume = None

    # --- Сведения из API хостинга (не критично для успеха копии)
    if src.api:
        PROGRESS.update(phase="API хостинга")
        try:
            res["api_backups"] = apis.list_backups(src.api, cfg.network.timeout)
            log.info("%s: в API хостинга найдено копий: %d", src.id, len(res["api_backups"]))
        except Exception as e:  # noqa: BLE001
            res["api_error"] = str(e)
            res["warnings"].append(f"API хостинга: {e}")
            log.warning("%s: API хостинга: %s", src.id, e)

    if not src.files and not src.mysql:
        res["status"] = "ok" if "api_error" not in res else "failed"
        n = len(res.get("api_backups") or [])
        res["summary"] = f"в панели хостинга {n} копий" if "api_error" not in res else "API недоступно"
        if "api_error" in res:
            res["errors"].append(res["api_error"])
        return

    PROGRESS.update(phase="подключение")

    def connect():
        return connect_ssh(src.ssh, cfg.data_dir, cfg.network, interactive)

    remote = SFTPRemote(connect, cfg.network.timeout)
    try:
        if dry_run:
            _dry_run(cfg, src, remote, prev.path if prev else None, res)
            return
        snap = resume.path if resume else _new_snapshot_dir(base, datetime.now())
        if resume:
            log.info("%s: продолжаю незавершённый снимок %s", src.id, snap.name)
        os.makedirs(lp(snap), exist_ok=True)
        res["snapshot"] = str(snap)
        prev_manifest = read_manifest(prev.path) if prev else {}
        manifest: dict[str, str] = {}
        total = SyncStats()

        for spec in src.files:
            PROGRESS.update(phase=f"файлы: {spec.remote}")
            log.info("%s: копирую %s", src.id, spec.remote)
            try:
                st = sync_tree(remote, spec, snap, prev.path if prev else None, prev_manifest, cfg.network,
                               manifest, verify_remote=src.remote_checksum, progress=PROGRESS)
            except (FileNotFoundError, PermissionError) as e:
                total.errors.append(f"папка {spec.remote} недоступна: {e}")
                continue
            total.merge(st)
            log.info("%s: %s — файлов %d (%s), скачано %d (%s), без изменений %d", src.id, spec.remote,
                     st.files, human_size(st.total_bytes), st.downloaded, human_size(st.downloaded_bytes),
                     st.linked + st.copied_local + st.reused)

        dumps = []
        if src.mysql:
            def client():
                t = remote.client.get_transport() if remote.client else None
                if t is None or not t.is_active():
                    remote.reconnect()
                return remote.client

            for db in src.mysql.databases:
                PROGRESS.update(phase=f"база данных {db.name}", file="")
                out = snap / "db" / f"{db.name}.sql.gz"
                try:
                    d = dump_database(client, src.mysql, db, out, cfg.network, progress=PROGRESS)
                    manifest[f"db/{db.name}.sql.gz"] = sha256_file(out)
                    dumps.append(d)
                    log.info("%s: дамп %s — %s (SQL %s), проверка пройдена", src.id, db.name,
                             human_size(d.size), human_size(d.raw_size))
                except (DumpError, secretstore.MissingSecret) as e:
                    total.errors.append(str(e))
                    log.error("%s: %s", src.id, e)

        if res.get("api_backups") is not None:
            with open(lp(snap / "api_backups.json"), "w", encoding="utf-8") as f:
                json.dump(res["api_backups"], f, ensure_ascii=False, indent=2, default=str)

        PROGRESS.update(phase="манифест и проверка", file="")
        write_manifest(snap, manifest)
        size = dir_size(snap)
        nothing = total.files == 0 and not dumps
        status = "ok" if not total.errors and not nothing else ("partial" if manifest else "failed")
        if nothing and not total.errors:
            total.errors.append("ничего не скопировано — проверьте пути в настройках")
        info = {
            "status": status, "source": src.id, "title": src.title, "started": ts(started),
            "finished": ts(), "files": total.files, "files_bytes": total.total_bytes,
            "downloaded": total.downloaded, "downloaded_bytes": total.downloaded_bytes,
            "unchanged": total.linked + total.copied_local + total.reused, "hardlinked": total.linked,
            "databases": {d.database: d.size for d in dumps}, "size": size,
            "errors": total.errors, "warnings": total.warnings, "renamed": total.renamed[:200],
            "previous": prev.path.name if prev else None, "skipped_symlinks": total.skipped_links,
        }
        _write_info(snap, info)
        res.update(status=status, size=size, errors=total.errors, warnings=res["warnings"] + total.warnings)
        res["summary"] = (
            f"{total.files} файлов ({human_size(total.total_bytes)}), скачано {total.downloaded} "
            f"({human_size(total.downloaded_bytes)}), баз {len(dumps)}/{len(src.mysql.databases) if src.mysql else 0}"
            f", снимок {snap.name}"
        )
        if total.linked == 0 and total.copied_local > 0:
            res["warnings"].append("диск не поддерживает жёсткие ссылки — неизменённые файлы копируются (больше места)")

        # --- Ротация локальных снимков
        if status in ("ok", "partial"):
            ret = cfg.retention_for(src)
            for old in plan_rotation(list_snapshots(base), ret.daily, ret.weekly, ret.monthly,
                                     latest=ret.latest, protect=snap):
                try:
                    remove_snapshot(base, old)
                    log.info("%s: ротация — удалён старый снимок %s", src.id, old.path.name)
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: не удалось удалить старый снимок %s: %s", src.id, old.path.name, e)

        # --- Вторая копия
        if status == "ok":
            ok, why = second_copy_available(cfg)
            if ok:
                PROGRESS.update(phase="вторая копия")
                try:
                    target = make_second_copy(cfg, src, snap)
                    log.info("%s: вторая копия: %s", src.id, target)
                except Exception as e:  # noqa: BLE001
                    res["warnings"].append(f"вторая копия не создана: {e}")
                    log.warning("%s: вторая копия не создана: %s", src.id, e)
            elif cfg.second_copy.path:
                res["warnings"].append(why)
    finally:
        remote.close()


def _dry_run(cfg: Config, src: Source, remote, prev_path: Path | None, res: dict) -> None:
    total = SyncStats()
    lines = []
    for spec in src.files:
        PROGRESS.update(phase=f"анализ: {spec.remote}")
        try:
            st = sync_tree(remote, spec, Path("/nonexistent"), prev_path, {}, cfg.network, {}, dry_run=True,
                           progress=PROGRESS)
        except (FileNotFoundError, PermissionError) as e:
            total.errors.append(f"папка {spec.remote} недоступна: {e}")
            continue
        total.merge(st)
        lines.append(f"{spec.remote}: всего {st.files} файлов ({human_size(st.total_bytes)}); новых {st.plan_new}, "
                     f"изменённых {st.plan_changed}, без изменений {st.plan_unchanged}; "
                     f"будет скачано {human_size(st.plan_bytes)}; исключено {st.excluded}")
    if src.mysql:
        lines.append("базы: " + ", ".join(d.name for d in src.mysql.databases) + " (mysqldump → .sql.gz)")
    res["status"] = "dry-run" if not total.errors else "failed"
    res["errors"] = total.errors
    res["warnings"] += total.warnings
    res["summary"] = "; ".join(lines) or "нечего копировать"
    res["dry_run"] = lines


def test_connection(cfg: Config, src: Source, interactive: bool = False) -> str:
    """Проверить SSH-подключение и наличие путей (без копирования)."""
    if not src.ssh:
        if src.api:
            items = apis.list_backups(src.api, cfg.network.timeout)
            return f"API работает, найдено копий в панели: {len(items)}"
        return "нет SSH и API — нечего проверять"
    client = connect_ssh(src.ssh, cfg.data_dir, cfg.network, interactive)
    try:
        sftp = client.open_sftp()
        out = [f"SSH: подключение к {src.ssh.host} успешно"]
        for spec in src.files:
            try:
                sftp.stat(spec.remote)
                out.append(f"  ✓ {spec.remote}")
            except OSError as e:
                out.append(f"  ✗ {spec.remote}: {e}")
        if src.mysql:
            from .ssh import exec_read

            r = exec_read(client, f"command -v {src.mysql.mysqldump} || which {src.mysql.mysqldump}")
            found = r and r[0] == 0 and r[1].strip()
            out.append(f"  {'✓' if found else '✗'} mysqldump: "
                       f"{r[1].decode().strip() if found else 'не найден — укажите полный путь'}")
        return "\n".join(out)
    finally:
        client.close()
