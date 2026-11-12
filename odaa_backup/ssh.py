"""SSH/SFTP: подключение с проверкой отпечатка сервера, повторы, чтение файлов.

Программа ТОЛЬКО ЧИТАЕТ с серверов: никаких команд записи/удаления не выполняется.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import socket
import stat
import threading
import time
from pathlib import Path

import paramiko

from . import secretstore
from .config import Network, SSHConf

log = logging.getLogger(__name__)

NETWORK_ERRORS = (OSError, EOFError, socket.timeout, paramiko.SSHException)


class HostKeyError(Exception):
    """Отпечаток сервера неизвестен или изменился."""

    def __init__(self, host: str, port: int, fingerprint: str, key_type: str, mismatch: bool = False,
                 expected: str | None = None):
        self.host, self.port = host, port
        self.fingerprint, self.key_type = fingerprint, key_type
        self.mismatch, self.expected = mismatch, expected
        if mismatch:
            msg = (
                f"ВНИМАНИЕ! Отпечаток сервера {host}:{port} ИЗМЕНИЛСЯ: сейчас {fingerprint}"
                + (f", ожидался {expected}" if expected else "")
                + ". Это может быть атака «человек посередине» или переустановка сервера. "
                "Уточните у хостинга и только потом нажмите «Доверять»."
            )
        else:
            msg = (
                f"Сервер {host}:{port} ещё не проверен. Отпечаток {key_type} {fingerprint}. "
                "Сверьте его с панелью хостинга/поддержкой и подтвердите (кнопка «Доверять» "
                "или команда «odaa-backup trust ИСТОЧНИК»)."
            )
        super().__init__(msg)


class AuthError(Exception):
    pass


def fingerprint(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _hostname(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


class KnownHosts:
    """Собственный файл known_hosts программы (data/known_hosts)."""

    _lock = threading.Lock()

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> paramiko.HostKeys:
        hk = paramiko.HostKeys()
        if self.path.exists():
            hk.load(str(self.path))
        return hk

    def check(self, host: str, port: int, key: paramiko.PKey) -> str:
        """'ok' | 'unknown' | 'mismatch'"""
        hk = self._load()
        entry = hk.lookup(_hostname(host, port))
        if not entry:
            return "unknown"
        known = entry.get(key.get_name())
        if known is None:
            # Сервер прислал ключ другого типа, а прежний тип нам известен — считаем подозрительным.
            return "mismatch"
        return "ok" if known.asbytes() == key.asbytes() else "mismatch"

    def known_fingerprint(self, host: str, port: int) -> str | None:
        entry = self._load().lookup(_hostname(host, port))
        if not entry:
            return None
        return ", ".join(fingerprint(k) for k in entry.values())

    def add(self, host: str, port: int, key: paramiko.PKey) -> None:
        with self._lock:
            hk = self._load()
            name = _hostname(host, port)
            # Заменяем все прежние ключи этого хоста.
            entries = [e for e in hk._entries if name not in e.hostnames]  # noqa: SLF001
            hk._entries = entries  # noqa: SLF001
            hk.add(name, key.get_name(), key)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            hk.save(str(self.path))


def fetch_host_key(host: str, port: int, timeout: int = 30) -> paramiko.PKey:
    """Получить ключ сервера без авторизации (для показа отпечатка пользователю)."""
    sock = socket.create_connection((host, port), timeout=timeout)
    t = paramiko.Transport(sock)
    try:
        t.banner_timeout = timeout
        t.start_client(timeout=timeout)
        return t.get_remote_server_key()
    finally:
        t.close()


class _CheckPolicy(paramiko.MissingHostKeyPolicy):
    """Неизвестный ключ: принимаем только если он совпадает с закреплённым в конфиге отпечатком."""

    def __init__(self, conf: SSHConf, known: KnownHosts):
        self.conf, self.known = conf, known

    def missing_host_key(self, client, hostname, key):
        fp = fingerprint(key)
        if self.conf.host_key and fp == self.conf.host_key:
            self.known.add(self.conf.host, self.conf.port, key)
            log.info("Отпечаток %s совпал с указанным в конфиге и сохранён", fp)
            return
        raise HostKeyError(self.conf.host, self.conf.port, fp, key.get_name(),
                           mismatch=bool(self.conf.host_key), expected=self.conf.host_key)


def load_private_key(conf: SSHConf, interactive: bool = False) -> paramiko.PKey:
    passphrase = None
    if conf.key_passphrase_secret:
        passphrase = secretstore.require(conf.key_passphrase_secret, interactive)
    path = os.path.expandvars(os.path.expanduser(conf.key_file or ""))
    try:
        return paramiko.PKey.from_path(path, password=passphrase)
    except FileNotFoundError:
        raise AuthError(f"не найден файл SSH-ключа: {path}") from None
    except (paramiko.PasswordRequiredException, TypeError):
        raise AuthError("SSH-ключ защищён паролем — задайте «key_passphrase_secret» и введите пароль ключа") from None
    except (paramiko.SSHException, ValueError) as e:
        raise AuthError(f"не удалось прочитать SSH-ключ {path}: {e}") from None


def connect_ssh(conf: SSHConf, data_dir: Path, net: Network, interactive: bool = False) -> paramiko.SSHClient:
    """Подключиться по SSH с проверкой отпечатка. Повторяет попытки при сетевых ошибках."""
    known = KnownHosts(Path(data_dir) / "known_hosts")
    pkey = load_private_key(conf, interactive) if conf.key_file else None
    password = secretstore.require(conf.password_secret, interactive) if conf.password_secret else None

    last_exc: Exception | None = None
    for attempt in range(net.retries + 1):
        client = paramiko.SSHClient()
        if known.path.exists():
            client.load_host_keys(str(known.path))
        client.set_missing_host_key_policy(_CheckPolicy(conf, known))
        try:
            client.connect(
                conf.host, port=conf.port, username=conf.user, password=password, pkey=pkey,
                timeout=net.timeout, banner_timeout=net.timeout, auth_timeout=net.timeout,
                allow_agent=False, look_for_keys=False, compress=True,
            )
            key = client.get_transport().get_remote_server_key()
            if conf.host_key and fingerprint(key) != conf.host_key:
                client.close()
                raise HostKeyError(conf.host, conf.port, fingerprint(key), key.get_name(), True, conf.host_key)
            client.get_transport().set_keepalive(30)
            return client
        except HostKeyError:
            client.close()
            raise
        except paramiko.BadHostKeyException as e:
            client.close()
            raise HostKeyError(conf.host, conf.port, fingerprint(e.key), e.key.get_name(), True,
                               fingerprint(e.expected_key)) from None
        except paramiko.AuthenticationException as e:
            client.close()
            raise AuthError(f"сервер {conf.host} отклонил логин/пароль или ключ ({e})") from None
        except NETWORK_ERRORS as e:
            client.close()
            last_exc = e
            if attempt < net.retries:
                wait = net.retry_delay * (attempt + 1)
                log.warning("Не удалось подключиться к %s (%s), повтор через %s с", conf.host, e, wait)
                time.sleep(wait)
    raise ConnectionError(f"не удалось подключиться к {conf.host}:{conf.port}: {last_exc}")


class SFTPRemote:
    """Обёртка над SFTP с переподключением. Только операции чтения."""

    def __init__(self, connect_fn, timeout: int = 60):
        self._connect_fn = connect_fn
        self.timeout = timeout
        self.client: paramiko.SSHClient | None = None
        self.sftp: paramiko.SFTPClient | None = None
        self.reconnect()

    def reconnect(self) -> None:
        self.close()
        self.client = self._connect_fn()
        self.sftp = self.client.open_sftp()
        self.sftp.get_channel().settimeout(self.timeout)

    def close(self) -> None:
        for obj in (self.sftp, self.client):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        self.sftp = self.client = None

    # интерфейс «удалённой файловой системы» (см. sync.py)
    def stat(self, path: str):
        return self.sftp.stat(path)

    def listdir_attr(self, path: str):
        return self.sftp.listdir_attr(path)

    def open(self, path: str):
        f = self.sftp.open(path, "rb")
        f.set_pipelined(True)
        return f

    def sha256_many(self, paths: list[str]) -> dict[str, str] | None:
        """Посчитать SHA-256 файлов НА СЕРВЕРЕ (sha256sum) одной командой. None — если недоступно."""
        if not paths:
            return {}
        result: dict[str, str] = {}
        for i in range(0, len(paths), 2000):
            part = paths[i:i + 2000]
            out = exec_read(self.client, "xargs -0 sha256sum --", stdin=b"\0".join(p.encode() for p in part),
                            timeout=max(self.timeout, 600))
            if out is None:
                return None
            code, stdout, _stderr = out
            if code not in (0, 1):  # 1 — часть файлов не прочитана; разберём что есть
                return None
            for line in stdout.decode("utf-8", "replace").splitlines():
                if len(line) < 67:
                    continue
                escaped = line.startswith("\\")
                if escaped:
                    line = line[1:]
                h, name = line[:64], line[66:]
                if escaped:
                    name = name.replace("\\n", "\n").replace("\\\\", "\\")
                result[name] = h.lower()
        return result


def exec_read(client: paramiko.SSHClient, command: str, stdin: bytes | None = None, timeout: int = 120):
    """Выполнить команду чтения на сервере. Возвращает (код, stdout, stderr) или None при ошибке канала."""
    try:
        ch = client.get_transport().open_session()
        ch.settimeout(timeout)
        ch.exec_command(command)
        if stdin is not None:
            ch.sendall(stdin)
        ch.shutdown_write()
        out = bytearray()
        while True:
            data = ch.recv(1 << 16)
            if not data:
                break
            out += data
        err = bytearray()
        while ch.recv_stderr_ready():
            err += ch.recv_stderr(1 << 16)
        code = ch.recv_exit_status()
        ch.close()
        return code, bytes(out), bytes(err)
    except NETWORK_ERRORS as e:
        log.warning("Команда на сервере не выполнилась: %s", e)
        return None


def is_dir(mode: int | None) -> bool:
    return mode is not None and stat.S_ISDIR(mode)


def is_link(mode: int | None) -> bool:
    return mode is not None and stat.S_ISLNK(mode)


def is_regular(mode: int | None) -> bool:
    return mode is not None and stat.S_ISREG(mode)


def generate_key(path: Path) -> tuple[Path, str, str]:
    """Создать новый SSH-ключ RSA-4096, защищённый случайной фразой (хранится в Диспетчере учётных данных).

    Возвращает (путь, публичный ключ, имя секрета с фразой).
    """
    import secrets as _secrets

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"ключ уже существует: {path}")
    passphrase = _secrets.token_urlsafe(24)
    secret_name = "ssh-key-passphrase"
    key = paramiko.RSAKey.generate(4096)
    key.write_private_key_file(str(path), password=passphrase)
    secretstore.save(secret_name, passphrase)
    pub = f"{key.get_name()} {key.get_base64()} odaa-backup"
    with open(str(path) + ".pub", "w", encoding="utf-8") as f:
        f.write(pub + "\n")
    return path, pub, secret_name
