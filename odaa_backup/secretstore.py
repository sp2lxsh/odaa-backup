"""Хранение паролей и токенов в Диспетчере учётных данных Windows (через keyring).

В config.yaml хранятся только ИМЕНА секретов (например «beget-ssh»),
сами значения — в Windows Credential Manager под службой «odaa-backup».
Все известные значения секретов вырезаются из логов.
"""

from __future__ import annotations

import getpass
import logging
import os
import threading

SERVICE = "odaa-backup"

_lock = threading.Lock()
_known_values: set[str] = set()
_cache: dict[str, str] = {}


class MissingSecret(Exception):
    def __init__(self, name: str):
        self.name = name
        super().__init__(
            f"не задан пароль/токен «{name}». Задайте его: меню → «Пароли», "
            f"или командой: odaa-backup secret set {name}"
        )


_backend_ready = False


def _keyring():
    import keyring  # импорт здесь, чтобы тесты работали без бэкенда keyring

    global _backend_ready
    if not _backend_ready:
        _backend_ready = True
        if os.name == "nt":
            # Явно выбираем Диспетчер учётных данных Windows (надёжно и в собранном .exe).
            try:
                from keyring.backends.Windows import WinVaultKeyring

                keyring.set_keyring(WinVaultKeyring())
            except Exception:  # noqa: BLE001
                pass
    return keyring


def register(value: str | None) -> None:
    """Запомнить значение, чтобы оно никогда не попало в лог."""
    if value and len(value) >= 3:
        with _lock:
            _known_values.add(value)


def redact(text: str) -> str:
    with _lock:
        values = sorted(_known_values, key=len, reverse=True)
    for v in values:
        if v in text:
            text = text.replace(v, "***")
    return text


def get(name: str) -> str | None:
    with _lock:
        if name in _cache:
            return _cache[name]
    try:
        value = _keyring().get_password(SERVICE, name)
    except Exception as e:  # нет бэкенда keyring и т.п.
        logging.getLogger(__name__).warning("Хранилище паролей недоступно: %s", e)
        value = None
    if value:
        register(value)
        with _lock:
            _cache[name] = value
    return value


def save(name: str, value: str) -> None:
    _keyring().set_password(SERVICE, name, value)
    register(value)
    with _lock:
        _cache[name] = value


def delete(name: str) -> bool:
    with _lock:
        _cache.pop(name, None)
    try:
        _keyring().delete_password(SERVICE, name)
        return True
    except Exception:
        return False


def is_set(name: str) -> bool:
    return bool(get(name))


def require(name: str, interactive: bool = False, prompt: str | None = None) -> str:
    """Получить секрет; если его нет и есть консоль — спросить и сохранить."""
    value = get(name)
    if value:
        return value
    if interactive:
        value = getpass.getpass(prompt or f"Введите значение для «{name}» (ввод не отображается): ")
        if value:
            save(name, value)
            return value
    raise MissingSecret(name)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        return True
