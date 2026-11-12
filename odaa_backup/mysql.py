"""Дампы MySQL/MariaDB через SSH (mysqldump на сервере) и их проверка.

Пароль базы НЕ передаётся в командной строке: по умолчанию mysqldump читает его
из «файла настроек», который подаётся через stdin SSH-канала (--defaults-extra-file=/dev/stdin).
Дамп передаётся по SSH со сжатием и упаковывается в .sql.gz уже на компьютере.
"""

from __future__ import annotations

import gzip
import logging
import os
import shlex
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from . import secretstore
from .config import DBSpec, MySQLConf, Network
from .paths import lp
from .ssh import NETWORK_ERRORS

log = logging.getLogger(__name__)

DEFAULT_ARGS = [
    "--single-transaction",
    "--quick",
    "--no-tablespaces",
    "--skip-lock-tables",
    "--default-character-set=utf8mb4",
    "--hex-blob",
    "--triggers",
]


class DumpError(Exception):
    pass


@dataclass
class DumpResult:
    database: str
    path: Path
    size: int  # размер .gz
    raw_size: int  # размер SQL


def _cnf_value(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_command(conf: MySQLConf, db: DBSpec) -> tuple[str, bytes | None, dict]:
    """Команда для сервера, данные для stdin (файл настроек с паролем) и переменные окружения."""
    password = secretstore.require(db.password_secret)
    host = db.host or conf.host
    args = list(conf.extra_args) if conf.extra_args is not None else list(DEFAULT_ARGS)
    if conf.password_via == "stdin":
        cnf = f"[client]\nuser={_cnf_value(db.user)}\npassword={_cnf_value(password)}\nhost={_cnf_value(host)}\n"
        if conf.port:
            cnf += f"port={conf.port}\n"
        parts = [conf.mysqldump, "--defaults-extra-file=/dev/stdin", *args, "--", db.name]
        return " ".join(shlex.quote(p) for p in parts), cnf.encode(), {}
    # password_via: env — пароль в переменной MYSQL_PWD (виден в списке процессов сервера, менее безопасно)
    parts = [conf.mysqldump, f"--user={db.user}", f"--host={host}"]
    if conf.port:
        parts.append(f"--port={conf.port}")
    parts += [*args, "--", db.name]
    cmd = "MYSQL_PWD=" + shlex.quote(password) + " " + " ".join(shlex.quote(p) for p in parts)
    return cmd, None, {}


def dump_database(client, conf: MySQLConf, db: DBSpec, out_path: Path, net: Network, progress=None) -> DumpResult:
    """Снять дамп одной базы в out_path (.sql.gz) с проверкой. Повторяет при сетевых ошибках."""
    last: Exception | None = None
    for attempt in range(net.retries + 1):
        try:
            res = _dump_once(client() if callable(client) else client, conf, db, out_path, net, progress)
            validate_dump(out_path, conf.min_bytes, conf.allow_empty)
            return res
        except DumpError:
            raise
        except NETWORK_ERRORS as e:
            last = e
            if attempt < net.retries:
                wait = net.retry_delay * (attempt + 1)
                log.warning("Сбой при дампе %s: %s — повтор через %s с", db.name, e, wait)
                time.sleep(wait)
    raise DumpError(f"{db.name}: не удалось снять дамп: {last}")


def _dump_once(client, conf: MySQLConf, db: DBSpec, out_path: Path, net: Network, progress) -> DumpResult:
    cmd, stdin, _env = build_command(conf, db)
    part = out_path.with_name(out_path.name + ".part")
    os.makedirs(lp(out_path.parent), exist_ok=True)
    ch = client.get_transport().open_session()
    ch.settimeout(max(net.timeout, 300))
    ch.exec_command(cmd)
    if stdin is not None:
        ch.sendall(stdin)
    ch.shutdown_write()
    raw = 0
    rate = net.rate_limit_kbps * 1024
    start = time.monotonic()
    try:
        with open(lp(part), "wb") as fobj, gzip.GzipFile(filename=db.name + ".sql", mode="wb",
                                                          fileobj=fobj, compresslevel=6) as gz:
            while True:
                data = ch.recv(1 << 20)
                if not data:
                    break
                gz.write(data)
                raw += len(data)
                if progress:
                    progress(bytes_=len(data))
                if rate > 0:
                    ahead = raw / rate - (time.monotonic() - start)
                    if ahead > 0:
                        time.sleep(ahead)
        err = b""
        while ch.recv_stderr_ready():
            err += ch.recv_stderr(1 << 16)
        code = ch.recv_exit_status()
    finally:
        ch.close()
    err_text = secretstore.redact(err.decode("utf-8", "replace").strip())
    if code != 0:
        try:
            os.remove(lp(part))
        except OSError:
            pass
        hint = ""
        if "using password: NO" in err_text:
            hint = " (сервер не прочитал пароль из stdin — попробуйте «password_via: env»)"
        elif "command not found" in err_text or code == 127:
            hint = " (на сервере не найден mysqldump — укажите полный путь в «mysqldump:»)"
        raise DumpError(f"{db.name}: mysqldump завершился с кодом {code}: {err_text[:500]}{hint}")
    if err_text:
        log.warning("mysqldump %s: %s", db.name, err_text[:500])
    os.replace(lp(part), lp(out_path))
    return DumpResult(db.name, out_path, os.path.getsize(lp(out_path)), raw)


def validate_dump(path: Path, min_bytes: int = 1024, allow_empty: bool = False) -> int:
    """Проверить дамп: целостность gzip, минимальный размер, SQL-заголовок, CREATE TABLE, финальная метка.

    Возвращает размер распакованного SQL. Бросает DumpError с понятным описанием.
    """
    name = Path(path).name
    if not os.path.exists(lp(path)):
        raise DumpError(f"{name}: файл дампа не найден")
    gz_size = os.path.getsize(lp(path))
    head = b""
    tail = b""
    raw = 0
    has_create = False
    try:
        with gzip.open(lp(path), "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                if len(head) < 4096:
                    head += chunk[: 4096 - len(head)]
                if not has_create and b"CREATE TABLE" in (tail[-64:] + chunk):
                    has_create = True
                tail = (tail + chunk)[-4096:]
                raw += len(chunk)
    except (OSError, EOFError, zlib.error) as e:
        raise DumpError(f"{name}: архив повреждён (проверка gzip не пройдена): {e}") from None
    if raw < min_bytes:
        raise DumpError(f"{name}: дамп слишком маленький ({raw} байт < {min_bytes}) — вероятно, ошибка выгрузки")
    head_text = head.decode("utf-8", "replace")
    if "-- MySQL dump" not in head_text and "-- MariaDB dump" not in head_text:
        raise DumpError(f"{name}: в начале файла нет заголовка mysqldump — это не похоже на дамп")
    if not has_create and not allow_empty:
        raise DumpError(f"{name}: в дампе нет ни одного CREATE TABLE (база пустая? тогда allow_empty: true)")
    if b"-- Dump completed" not in tail:
        raise DumpError(f"{name}: дамп оборван — нет финальной строки «-- Dump completed»")
    log.debug("Дамп %s проверен: %d байт SQL, %d байт gzip", name, raw, gz_size)
    return raw
