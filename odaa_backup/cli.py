"""Командная строка и простое меню (двойной щелчок по odaa-backup.exe открывает меню)."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import APP_NAME, __version__, runner, schedule, secretstore
from .config import Config, ConfigError, load_config
from .logsetup import setup_logging
from .manifest import verify_snapshot
from .paths import app_dir
from .retention import list_snapshots
from .ssh import KnownHosts, fetch_host_key, fingerprint, generate_key

log = logging.getLogger("odaa_backup")


def default_config_path() -> Path:
    env = os.environ.get("ODAA_CONFIG")
    return Path(env) if env else app_dir() / "config.yaml"


def _example_path() -> Path | None:
    for base in (app_dir(), Path(getattr(sys, "_MEIPASS", app_dir())), Path(__file__).resolve().parent.parent):
        p = base / "config.example.yaml"
        if p.exists():
            return p
    return None


def ensure_config(path: Path) -> bool:
    """Если config.yaml нет — создать из примера. Возвращает True, если файл только что создан."""
    if path.exists():
        return False
    ex = _example_path()
    if ex is None:
        return False
    shutil.copyfile(ex, path)
    return True


def _load(path: Path) -> Config:
    try:
        return load_config(path)
    except ConfigError as e:
        print(f"\n{e}\n\nИсправьте файл {path} и запустите снова.")
        raise SystemExit(2) from None


def _open_in_editor(path: Path) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["notepad.exe", str(path)])
    else:
        print(f"Откройте файл в редакторе: {path}")


# --------------------------------------------------------------------------- команды


def cmd_run(cfg: Config, args) -> int:
    results, report = runner.run(cfg, args.source or None, scheduled=args.scheduled, dry_run=args.dry_run,
                                 interactive=sys.stdin is not None and sys.stdin.isatty() and not args.scheduled)
    if report and not args.scheduled:
        print("\n" + report)
    return 1 if any(r["status"] == "failed" for r in results) else 0


def cmd_trust(cfg: Config, sid: str, assume_yes: bool = False) -> int:
    s = cfg.source(sid)
    if not s.ssh:
        print("У источника нет SSH.")
        return 1
    key = fetch_host_key(s.ssh.host, s.ssh.port, cfg.network.timeout)
    kh = KnownHosts(cfg.data_dir / "known_hosts")
    state = kh.check(s.ssh.host, s.ssh.port, key)
    fp = fingerprint(key)
    print(f"\nСервер {s.ssh.host}:{s.ssh.port}\nТип ключа: {key.get_name()}\nОтпечаток: {fp}")
    if state == "ok":
        print("Совпадает с сохранённым ранее — всё в порядке.")
        return 0
    if state == "mismatch":
        print("\n!!! ВНИМАНИЕ: отпечаток ОТЛИЧАЕТСЯ от сохранённого ранее (сервер переустановлен или подменён).")
    print("Сверьте отпечаток с данными хостинга (панель управления / поддержка).")
    if not assume_yes and input("Доверять этому серверу? (да/нет): ").strip().lower() not in ("да", "д", "yes", "y"):
        print("Отменено.")
        return 1
    kh.add(s.ssh.host, s.ssh.port, key)
    print("Сохранено.")
    return 0


def cmd_verify(cfg: Config, sid: str, snap: str | None) -> int:
    s = cfg.source(sid)
    snaps = list_snapshots(runner.source_dir(cfg, s))
    if snap:
        snaps = [x for x in snaps if x.path.name == snap]
    else:
        snaps = [x for x in snaps if x.complete][:1]
    if not snaps:
        print("Снимок не найден.")
        return 1
    rc = 0
    for sn in snaps:
        r = verify_snapshot(sn.path)
        print(f"{sn.path}: {r.summary()}")
        for m in (r.missing + r.mismatched)[:20]:
            print("   ", m)
        rc |= 0 if r.ok else 1
    return rc


def cmd_status(cfg: Config) -> int:
    from .report import build_report

    print(build_report(cfg, []))
    print(f"\nЗадача Планировщика: {schedule.task_status()}")
    return 0


def cmd_setup(cfg: Config) -> int:
    """Первичная настройка: ввести недостающие пароли и проверить отпечатки серверов."""
    print("\n== Пароли и токены ==")
    for name in cfg.all_secret_names():
        if secretstore.is_set(name):
            print(f"  ✓ {name} — задан")
            continue
        value = getpass.getpass(f"  Введите «{name}» (ввод скрыт, Enter — пропустить): ")
        if value:
            secretstore.save(name, value)
            print("    сохранено в Диспетчере учётных данных Windows")
    print("\n== Отпечатки серверов ==")
    seen = set()
    for s in cfg.sources:
        if s.ssh and (s.ssh.host, s.ssh.port) not in seen:
            seen.add((s.ssh.host, s.ssh.port))
            try:
                cmd_trust(cfg, s.id)
            except Exception as e:  # noqa: BLE001
                print(f"  {s.ssh.host}: не удалось подключиться: {e}")
    print("\n== Проверка подключений ==")
    for s in cfg.sources:
        try:
            print(f"\n[{s.id}]\n" + runner.test_connection(cfg, s, interactive=True))
        except Exception as e:  # noqa: BLE001
            print(f"\n[{s.id}] ОШИБКА: {e}")
    return 0


def cmd_keygen(cfg: Config) -> int:
    path = cfg.data_dir / "keys" / "odaa_backup_rsa"
    try:
        p, pub, secret_name = generate_key(path)
    except FileExistsError:
        pub = Path(str(path) + ".pub").read_text(encoding="utf-8").strip()
        print(f"Ключ уже создан: {path}")
        secret_name = "ssh-key-passphrase"
        p = path
    print(f"""
Файл ключа: {p}
Пароль ключа сохранён в Диспетчере учётных данных как «{secret_name}».

Публичный ключ (его нужно добавить на хостинг):

{pub}

Как добавить:
  • Beget / Reg.ru: подключитесь по SSH или через файловый менеджер и добавьте строку выше
    в файл ~/.ssh/authorized_keys (по одной строке на ключ).
  • Timeweb Cloud: панель → Серверы → ваш сервер → SSH-ключи, или в /root/.ssh/authorized_keys.

В config.yaml для источника укажите:
    key_file: "{p}"
    key_passphrase_secret: {secret_name}
""")
    return 0


def menu(config_path: Path) -> int:
    while True:
        try:
            cfg = load_config(config_path)
        except ConfigError as e:
            print(f"\n{e}\n")
            print("1 — открыть config.yaml в Блокноте   0 — выход")
            ch = input("> ").strip()
            if ch == "1":
                _open_in_editor(config_path)
                input("Исправьте файл, сохраните и нажмите Enter...")
                continue
            return 2
        setup_logging(cfg.data_dir)
        print(f"""
==================  {APP_NAME} {__version__}  ==================
 Папка бэкапов: {cfg.backup_root}
 Источников: {len(cfg.sources)} (включено {sum(1 for s in cfg.sources if s.enabled)})

 1 — Открыть окно управления (в браузере)      ← удобнее всего
 2 — Скачать бэкапы всех сайтов сейчас
 3 — Пробный запуск (показать, что будет скачано)
 4 — Состояние и возраст копий
 5 — Первичная настройка (пароли, отпечатки серверов, проверка)
 6 — Создать задачу в Планировщике Windows (автоматически)
 7 — Проверить целостность последних снимков
 8 — Открыть config.yaml в Блокноте
 9 — Создать SSH-ключ (вход без пароля)
 0 — Выход
""")
        ch = input("Выберите пункт и нажмите Enter: ").strip()
        try:
            if ch == "1":
                from .webui import serve

                serve(config_path)
            elif ch == "2":
                runner.run(cfg, interactive=True)
                print("\n" + (cfg.data_dir / "reports" / "last_report.txt").read_text(encoding="utf-8")
                      if (cfg.data_dir / "reports" / "last_report.txt").exists() else "")
            elif ch == "3":
                _r, report = runner.run(cfg, dry_run=True, interactive=True)
                print("\n" + report)
                for r in _r:
                    for line in r.get("dry_run") or []:
                        print(f"  [{r['id']}] {line}")
            elif ch == "4":
                cmd_status(cfg)
            elif ch == "5":
                cmd_setup(cfg)
            elif ch == "6":
                ok, out = schedule.install_task(str(config_path.resolve()))
                print(out or ("Готово" if ok else "Ошибка"))
            elif ch == "7":
                for s in cfg.sources:
                    cmd_verify(cfg, s.id, None)
            elif ch == "8":
                _open_in_editor(config_path)
            elif ch == "9":
                cmd_keygen(cfg)
            elif ch == "0":
                return 0
        except KeyboardInterrupt:
            print("\nПрервано.")
        except Exception as e:  # noqa: BLE001
            print(f"\nОшибка: {e}")
            log.exception("Ошибка в меню")
        input("\nНажмите Enter, чтобы вернуться в меню...")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="odaa-backup", description=f"{APP_NAME} — локальные бэкапы сайтов с хостингов")
    p.add_argument("--config", type=Path, default=None, help="путь к config.yaml (по умолчанию рядом с программой)")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный журнал")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="сделать бэкап")
    r.add_argument("--source", "-s", action="append", help="только этот источник (можно несколько)")
    r.add_argument("--scheduled", action="store_true", help="плановый запуск: только тех, кому пора по расписанию")
    r.add_argument("--dry-run", action="store_true", help="показать, что будет сделано, ничего не скачивая")
    sub.add_parser("ui", help="открыть окно программы в браузере (то же, что запуск без параметров)")
    sub.add_parser("menu", help="текстовое меню в консоли")
    sub.add_parser("status", help="возраст последних копий")
    sub.add_parser("setup", help="первичная настройка: пароли, отпечатки, проверка")
    sub.add_parser("check", help="проверить config.yaml")
    t = sub.add_parser("trust", help="проверить и сохранить отпечаток SSH-сервера")
    t.add_argument("source")
    t.add_argument("--yes", action="store_true")
    ts_ = sub.add_parser("test", help="проверить подключение к источнику")
    ts_.add_argument("source")
    v = sub.add_parser("verify", help="проверить целостность снимка по манифесту SHA-256")
    v.add_argument("source")
    v.add_argument("snapshot", nargs="?")
    s = sub.add_parser("secret", help="пароли и токены в Диспетчере учётных данных Windows")
    s.add_argument("action", choices=["set", "delete", "list"])
    s.add_argument("name", nargs="?")
    sc = sub.add_parser("schedule", help="задача Планировщика Windows")
    sc.add_argument("action", choices=["install", "remove", "status"])
    sub.add_parser("keygen", help="создать SSH-ключ для входа без пароля")
    sub.add_parser("init", help="создать config.yaml из примера")
    return p


def _fix_streams() -> None:
    # Окно без консоли (odaa-backup-bg.exe): stdout/stderr отсутствуют.
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        else:
            try:
                stream.reconfigure(errors="replace")
            except Exception:  # noqa: BLE001
                pass


def main(argv: list[str] | None = None) -> int:
    _fix_streams()
    args = build_parser().parse_args(argv)
    config_path = (args.config or default_config_path()).resolve()
    if args.cmd is None or args.cmd == "ui":
        # Двойной щелчок по программе — сразу открывается окно в браузере.
        try:
            cfg = load_config(config_path)
            setup_logging(cfg.data_dir)
        except ConfigError as e:
            setup_logging(None)
            print(f"\n{e}\n")
        from .webui import serve

        serve(config_path)
        return 0
    if args.cmd == "menu":
        return menu(config_path)
    if args.cmd == "init":
        print("Создан" if ensure_config(config_path) else "Уже существует", config_path)
        return 0
    cfg = _load(config_path)
    background = args.cmd == "run" and args.scheduled
    setup_logging(cfg.data_dir, console=not background or sys.stderr is not None, verbose=args.verbose)
    if args.cmd == "run":
        return cmd_run(cfg, args)
    if args.cmd == "status":
        return cmd_status(cfg)
    if args.cmd == "setup":
        return cmd_setup(cfg)
    if args.cmd == "check":
        print(f"config.yaml в порядке: источников {len(cfg.sources)}")
        missing = [n for n in cfg.all_secret_names() if not secretstore.is_set(n)]
        if missing:
            print("Не заданы пароли/токены:", ", ".join(missing))
        return 0
    if args.cmd == "trust":
        return cmd_trust(cfg, args.source, args.yes)
    if args.cmd == "test":
        print(runner.test_connection(cfg, cfg.source(args.source), interactive=True))
        return 0
    if args.cmd == "verify":
        return cmd_verify(cfg, args.source, args.snapshot)
    if args.cmd == "secret":
        if args.action == "list":
            for n in cfg.all_secret_names():
                print(f"{'✓' if secretstore.is_set(n) else '✗'} {n}")
            return 0
        if not args.name:
            print("Укажите имя секрета")
            return 2
        if args.action == "set":
            v = getpass.getpass(f"Значение для «{args.name}» (ввод скрыт): ")
            if not v:
                print("Пустое значение — не сохранено")
                return 1
            secretstore.save(args.name, v)
            print("Сохранено в Диспетчере учётных данных Windows")
        else:
            print("Удалено" if secretstore.delete(args.name) else "Не найдено")
        return 0
    if args.cmd == "schedule":
        if args.action == "status":
            print(schedule.task_status())
            return 0
        ok, out = (schedule.install_task(str(config_path)) if args.action == "install" else schedule.remove_task())
        print(out)
        return 0 if ok else 1
    if args.cmd == "keygen":
        return cmd_keygen(cfg)
    return 0
