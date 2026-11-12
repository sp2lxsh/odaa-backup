"""Автонастройка: подключиться по логину/паролю, найти сайты, базы данных и их пароли.

Пользователь вводит только логин и пароль от хостинга. Программа сама:
  * определяет адрес SSH-сервера (Beget — <логин>.beget.tech);
  * находит SSH-ключ в C:\\Users\\<имя>\\.ssh, если пароль не указан (для VPS);
  * находит папки сайтов (Beget: <домен>/public_html, Reg.ru: www/<домен>, VPS: /var/www, /opt …);
  * читает логин/пароль базы из конфига сайта (wp-config.php, configuration.php, .settings.php, .env).
    Пароль базы не сохраняется — он читается из конфига при каждой копии, поэтому смена
    пароля базы ничего не ломает.

Все операции — только чтение.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

import paramiko

from .ssh import HostKeyError, KnownHosts, exec_read, fetch_host_key, fingerprint

log = logging.getLogger(__name__)

MAX_CONFIG_BYTES = 512 * 1024

PROVIDERS = {
    "beget": {"title": "Beget", "hint": "Логин и пароль от панели Beget (cp.beget.com)"},
    "regru": {"title": "Reg.ru", "hint": "Сервер, логин и пароль — во вкладке «Доступы» услуги хостинга"},
    "timeweb": {"title": "VPS (Timeweb и др.)", "hint": "IP-адрес сервера и пользователь (обычно root)"},
    "other": {"title": "Другой хостинг", "hint": "Любой хостинг с доступом по SSH/SFTP"},
}

EXCLUDES = {
    "wordpress": ["wp-content/cache", "wp-content/upgrade", "wp-content/ai1wm-backups", "wp-content/updraft",
                  "wp-content/backups-dup-lite", "wp-content/backup-db", "*.log"],
    "joomla": ["cache", "administrator/cache", "tmp", "administrator/backups", "*.log"],
    "bitrix": ["bitrix/cache", "bitrix/managed_cache", "bitrix/stack_cache", "bitrix/backup",
               "upload/resize_cache", "*.log"],
    "opencart": ["system/storage/cache", "system/storage/logs", "*.log"],
    "app": ["node_modules", ".git", "*.log", ".cache"],
    "site": ["*.log"],
}
CMS_TITLES = {"wordpress": "WordPress", "joomla": "Joomla", "bitrix": "1С-Битрикс", "opencart": "OpenCart",
              "app": "Приложение", "site": "Сайт"}
VPS_SKIP_OPT = {"containerd", "google", "microsoft", "digitalocean", "cni", "zabbix", "VBoxGuestAdditions"}


class DiscoverError(Exception):
    pass


# --------------------------------------------------------------------------- разбор конфигов CMS

_QSTR = r"""(['"])((?:\\.|(?!\1).)*)\1"""


def _unq(s: str) -> str:
    return s.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def _php_define(text: str, name: str) -> str | None:
    m = re.search(r"define\s*\(\s*['\"]" + re.escape(name) + r"['\"]\s*,\s*" + _QSTR, text)
    return _unq(m.group(2)) if m else None


def _php_prop(text: str, name: str) -> str | None:
    m = re.search(r"(?:public|var)\s+\$" + re.escape(name) + r"\s*=\s*" + _QSTR, text)
    return _unq(m.group(2)) if m else None


def _php_key(text: str, name: str) -> str | None:
    m = re.search(r"['\"]" + re.escape(name) + r"['\"]\s*=>\s*" + _QSTR, text)
    return _unq(m.group(2)) if m else None


def _strip_php_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*(//|#).*$", "", text)


def parse_env(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        out[k] = v
    return out


def _split_host(host: str | None) -> tuple[str | None, int | None]:
    if not host:
        return None, None
    if host.startswith("localhost:/") or host.startswith("/"):  # сокет — mysqldump найдёт сам
        return "localhost", None
    if ":" in host:
        h, p = host.rsplit(":", 1)
        if p.isdigit():
            return h, int(p)
    return host, None


def parse_db_config(kind: str, text: str) -> dict | None:
    """Вернуть {name, user, password, host, port} из текста конфига сайта или None."""
    t = _strip_php_comments(text) if kind != "env" else text
    if kind == "wordpress":
        name, user, pw, host = (_php_define(t, k) for k in ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST"))
    elif kind == "opencart":
        name, user, pw, host = (_php_define(t, k) for k in ("DB_DATABASE", "DB_USERNAME", "DB_PASSWORD",
                                                             "DB_HOSTNAME"))
        port = _php_define(t, "DB_PORT")
        if host and port and port.isdigit() and ":" not in host:
            host = f"{host}:{port}"
    elif kind == "joomla":
        name, user, pw, host = (_php_prop(t, k) for k in ("db", "user", "password", "host"))
    elif kind == "bitrix":
        name, user, pw, host = (_php_key(t, k) for k in ("database", "login", "password", "host"))
    elif kind == "env":
        env = parse_env(text)
        url = env.get("DATABASE_URL", "")
        m = re.match(r"mysql2?://([^:@/]+)(?::([^@/]*))?@([^/:]+)(?::(\d+))?/([^?\s]+)", url)
        if m:
            from urllib.parse import unquote

            return {"name": m.group(5), "user": unquote(m.group(1)), "password": unquote(m.group(2) or ""),
                    "host": m.group(3), "port": int(m.group(4)) if m.group(4) else None}
        conn = env.get("DB_CONNECTION", "mysql").lower()
        if conn not in ("mysql", "mariadb"):
            return None
        name = env.get("DB_DATABASE") or env.get("DB_NAME") or env.get("MYSQL_DATABASE")
        user = env.get("DB_USERNAME") or env.get("DB_USER") or env.get("MYSQL_USER")
        pw = env.get("DB_PASSWORD") or env.get("DB_PASS") or env.get("MYSQL_PASSWORD")
        host = env.get("DB_HOST") or "localhost"
        if env.get("DB_PORT", "").isdigit() and ":" not in host:
            host = f"{host}:{env['DB_PORT']}"
    else:
        return None
    if not name or not user or pw is None or not re.match(r"^[A-Za-z0-9_$-]+$", name):
        return None
    h, port = _split_host(host or "localhost")
    return {"name": name, "user": user, "password": pw, "host": h or "localhost", "port": port}


# --------------------------------------------------------------------------- результаты поиска


@dataclass
class FoundDB:
    name: str
    user: str
    host: str
    port: int | None
    from_file: str
    kind: str


@dataclass
class FoundItem:
    key: str  # уникальный ключ для формы
    title: str  # домен или название
    remote: str  # путь на сервере
    kind: str  # wordpress | joomla | … | app | site | config
    exclude: list[str] = field(default_factory=list)
    dbs: list[FoundDB] = field(default_factory=list)
    checked: bool = True
    note: str = ""


@dataclass
class ScanResult:
    provider: str
    host: str
    port: int
    user: str
    fingerprint: str
    password: str | None = None
    key_file: str | None = None
    key_passphrase: str | None = None
    api_secret: str | None = None
    sites: list[FoundItem] = field(default_factory=list)
    extras: list[FoundItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    has_mysqldump: bool = True


# --------------------------------------------------------------------------- подключение


SSH_DIR_SKIP = {"config", "known_hosts", "known_hosts.old", "authorized_keys", "environment"}


def ssh_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".ssh"


def ssh_config_for(host: str, base: Path | None = None) -> dict:
    """Настройки сервера из ~/.ssh/config (HostName, User, Port, IdentityFile), если есть."""
    path = (base or ssh_dir()) / "config"
    if not host or not path.exists():
        return {}
    try:
        return dict(paramiko.SSHConfig.from_path(str(path)).lookup(host))
    except Exception:  # noqa: BLE001
        return {}


def default_key_files(host: str | None = None, base: Path | None = None) -> list[Path]:
    """SSH-ключи пользователя: сначала указанные для этого сервера в ~/.ssh/config, затем все остальные
    (любые имена, например id_ed25519_vps — у каждого ключа рядом лежит .pub)."""
    d = base or ssh_dir()
    out: list[Path] = []
    for f in ssh_config_for(host or "", d).get("identityfile", []):
        p = Path(os.path.expanduser(f))
        if p.exists() and p not in out:
            out.append(p)
    if d.is_dir():
        std = ["id_ed25519", "id_ecdsa", "id_rsa"]
        files = sorted(d.iterdir(), key=lambda p: (std.index(p.name) if p.name in std else 9, p.name))
        for p in files:
            if (p.is_file() and p.suffix != ".pub" and p.name not in SSH_DIR_SKIP and not p.name.endswith(".old")
                    and (p.with_name(p.name + ".pub").exists() or p.name in std) and p not in out):
                out.append(p)
    return out


def user_known_key(host: str, port: int, key_type: str, path: Path | None = None) -> paramiko.PKey | None:
    """Ключ этого сервера из known_hosts Windows/OpenSSH пользователя (C:\\Users\\<имя>\\.ssh\\known_hosts)."""
    path = path or Path(os.path.expanduser("~")) / ".ssh" / "known_hosts"
    if not path.exists():
        return None
    hk = paramiko.HostKeys()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("@"):
                    continue
                try:
                    entry = paramiko.hostkeys.HostKeyEntry.from_line(line)
                except Exception:  # noqa: BLE001
                    continue
                if entry is not None:
                    hk._entries.append(entry)  # noqa: SLF001
    except OSError:
        return None
    found = hk.lookup(host if port == 22 else f"[{host}]:{port}")
    return found.get(key_type) if found else None


class _TrustOnFirstUse(paramiko.MissingHostKeyPolicy):
    """При добавлении хостинга новый сервер запоминается (его отпечаток показывается пользователю).

    Но если компьютер пользователя уже помнит ДРУГОЙ ключ этого сервера (~/.ssh/known_hosts),
    подключение останавливается: это может быть подмена сервера.
    """

    def __init__(self, known: KnownHosts, host: str, port: int, user_known_hosts: Path | None = None):
        self.known, self.host, self.port = known, host, port
        self.user_known_hosts = user_known_hosts

    def missing_host_key(self, client, hostname, key):
        old = user_known_key(self.host, self.port, key.get_name(), self.user_known_hosts)
        if old is not None and old.asbytes() != key.asbytes():
            raise HostKeyError(self.host, self.port, fingerprint(key), key.get_name(), mismatch=True,
                               expected=fingerprint(old))
        self.known.add(self.host, self.port, key)
        log.info("Новый сервер %s:%s, отпечаток %s сохранён", self.host, self.port, fingerprint(key))


def wizard_connect(host: str, port: int, user: str, password: str | None, key_file: str | None,
                   key_passphrase: str | None, data_dir: Path, timeout: int = 30,
                   user_known_hosts: Path | None = None):
    """Подключиться для мастера. Возвращает (client, key_file_used). Бросает DiscoverError с понятным текстом."""
    known = KnownHosts(Path(data_dir) / "known_hosts")
    # До отправки пароля: сверить ключ сервера с тем, что помнит компьютер пользователя.
    try:
        server_key = fetch_host_key(host, port, timeout)
    except Exception:  # noqa: BLE001 — сетевые ошибки покажет основное подключение ниже
        server_key = None
    if server_key is not None:
        old = user_known_key(host, port, server_key.get_name(), user_known_hosts)
        if old is not None and old.asbytes() != server_key.asbytes():
            raise HostKeyError(host, port, fingerprint(server_key), server_key.get_name(), mismatch=True,
                               expected=fingerprint(old))
    attempts: list[tuple[str | None, paramiko.PKey | None, str | None]] = []
    errors: list[str] = []
    if password:
        attempts.append((None, None, password))
        if password.strip() != password and password.strip():
            # Частая ошибка при копировании: лишний пробел или перевод строки в конце.
            attempts.append((None, None, password.strip()))
    keys = [Path(key_file)] if key_file else ([] if password else default_key_files(host))
    for kf in keys:
        try:
            attempts.append((str(kf), paramiko.PKey.from_path(str(kf), password=key_passphrase or None), None))
        except (paramiko.PasswordRequiredException, TypeError):
            errors.append(f"ключ {kf.name} защищён паролем — укажите «Пароль ключа» в дополнительных настройках")
        except (paramiko.SSHException, ValueError, OSError) as e:
            errors.append(f"ключ {kf.name} не прочитан: {e}")
    if not attempts:
        raise DiscoverError("Введите пароль. " + ("; ".join(errors) if errors else
                            "SSH-ключ в папке .ssh не найден."))
    for kf, pkey, pw in attempts:
        client = paramiko.SSHClient()
        if known.path.exists():
            client.load_host_keys(str(known.path))
        client.set_missing_host_key_policy(_TrustOnFirstUse(known, host, port, user_known_hosts))
        try:
            client.connect(host, port=port, username=user, password=pw,
                           pkey=pkey, timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                           allow_agent=False, look_for_keys=False, compress=True)
            if pw is not None and pw != password:
                log.info("Пароль подошёл без пробелов по краям")
            return client, kf
        except paramiko.BadHostKeyException as e:
            client.close()
            raise HostKeyError(host, port, fingerprint(e.key), e.key.get_name(), True,
                               fingerprint(e.expected_key)) from None
        except paramiko.BadAuthenticationType as ex:
            client.close()
            allowed = ", ".join(ex.allowed_types or []) or "неизвестно"
            log.warning("%s: сервер не принимает этот способ входа, разрешено: %s", host, allowed)
            if "password" not in (ex.allowed_types or []) and "keyboard-interactive" not in (ex.allowed_types or []):
                raise DiscoverError(f"Сервер {host} не принимает вход по паролю — только по SSH-ключу "
                                    f"(разрешено: {allowed}). Добавьте SSH-ключ в панели хостинга.") from None
            errors.append(f"способ входа не принят (сервер разрешает: {allowed})")
        except paramiko.AuthenticationException as ex:
            client.close()
            log.warning("%s: отказ в авторизации для %s: %s", host, user, ex)
            errors.append("неверный логин или пароль" if pkey is None else f"ключ {Path(kf).name} не подошёл")
        except (OSError, EOFError, paramiko.SSHException) as e:
            client.close()
            msg = str(e) or e.__class__.__name__
            if "getaddrinfo" in msg or "Name or service not known" in msg or "11001" in msg:
                raise DiscoverError(f"Сервер «{host}» не найден. Проверьте адрес.") from None
            if "timed out" in msg or "10060" in msg:
                raise DiscoverError(f"Сервер «{host}» не отвечает. Проверьте адрес и что SSH включён.") from None
            if "refused" in msg or "10061" in msg:
                raise DiscoverError(f"Сервер «{host}» отказал в подключении — SSH выключен или другой порт.") \
                    from None
            raise DiscoverError(f"Не удалось подключиться к {host}: {msg}") from None
    uniq = list(dict.fromkeys(errors))
    raise DiscoverError(f"Сервер {host} не пустил пользователя «{user}»: " + "; ".join(uniq) + ".")


# --------------------------------------------------------------------------- поиск сайтов


class _FS:
    def __init__(self, sftp):
        self.sftp = sftp

    def isdir(self, p: str) -> bool:
        try:
            return stat.S_ISDIR(self.sftp.stat(p).st_mode)
        except OSError:
            return False

    def isfile(self, p: str) -> bool:
        try:
            return stat.S_ISREG(self.sftp.stat(p).st_mode)
        except OSError:
            return False

    def dirs(self, p: str) -> list[str]:
        try:
            items = self.sftp.listdir_attr(p)
        except OSError:
            return []
        return sorted(a.filename for a in items if stat.S_ISDIR(a.st_mode or 0) and not a.filename.startswith("."))

    def read(self, p: str) -> str | None:
        try:
            with self.sftp.open(p, "rb") as f:
                return f.read(MAX_CONFIG_BYTES).decode("utf-8", "replace")
        except OSError:
            return None


def read_db_config(sftp, path: str, kind: str) -> dict | None:
    text = _FS(sftp).read(path)
    return parse_db_config(kind, text) if text is not None else None


def detect_site(fs: _FS, path: str) -> tuple[str, list[tuple[str, str]]]:
    """Определить CMS. Возвращает (kind, [(путь к конфигу с базой, вид конфига)])."""
    j = posixpath.join
    for cand in (j(path, "wp-config.php"), j(posixpath.dirname(path.rstrip("/")) or ".", "wp-config.php")):
        if fs.isfile(cand):
            return "wordpress", [(cand, "wordpress")]
    if fs.isfile(j(path, "bitrix/.settings.php")):
        return "bitrix", [(j(path, "bitrix/.settings.php"), "bitrix")]
    if fs.isfile(j(path, "configuration.php")):
        return "joomla", [(j(path, "configuration.php"), "joomla")]
    if fs.isfile(j(path, "config.php")) and fs.isdir(j(path, "system")):
        return "opencart", [(j(path, "config.php"), "opencart")]
    if fs.isfile(j(path, ".env")):
        return ("app" if fs.isfile(j(path, "package.json")) or fs.isdir(j(path, "node_modules")) else "site"), \
            [(j(path, ".env"), "env")]
    if fs.isfile(j(path, "package.json")):
        return "app", []
    return "site", []


def _item(fs: _FS, key: str, title: str, remote: str, seen_db: set) -> FoundItem:
    kind, configs = detect_site(fs, remote)
    item = FoundItem(key=key, title=title, remote=remote, kind=kind, exclude=list(EXCLUDES.get(kind, ["*.log"])))
    for cfg_path, cfg_kind in configs:
        text = fs.read(cfg_path)
        info = parse_db_config(cfg_kind, text) if text else None
        if info and (info["name"], info["host"]) not in seen_db:
            seen_db.add((info["name"], info["host"]))
            item.dbs.append(FoundDB(info["name"], info["user"], info["host"], info["port"], cfg_path, cfg_kind))
    return item


def find_items(sftp, provider: str, home: str = ".") -> tuple[list[FoundItem], list[FoundItem]]:
    """Найти сайты (sites) и дополнительные папки (extras — для VPS: nginx, systemd)."""
    fs = _FS(sftp)
    sites: list[FoundItem] = []
    extras: list[FoundItem] = []
    seen_paths: set[str] = set()
    seen_db: set = set()

    def add(title: str, remote: str) -> None:
        norm = remote.rstrip("/")
        if norm in seen_paths:
            return
        seen_paths.add(norm)
        sites.append(_item(fs, f"s{len(sites)}", title, remote, seen_db))

    # Виртуальный хостинг: Beget — <домен>/public_html, Reg.ru/ISPmanager — www/<домен>
    for d in fs.dirs(home):
        if d in ("www", "logs", "tmp", "mail", "etc", "bin", "private"):
            continue
        if fs.isdir(posixpath.join(d, "public_html")):
            add(d, f"{d}/public_html")
    for d in fs.dirs("www"):
        add(d, f"www/{d}")
    if fs.isdir("public_html") and not sites:
        add("public_html", "public_html")

    # VPS: проекты в /var/www, /opt, /srv
    if provider in ("timeweb", "other") or not sites:
        for base in ("/var/www", "/srv"):
            for d in fs.dirs(base):
                p = f"{base}/{d}"
                if d == "html" and fs.isfile(f"{p}/index.nginx-debian.html"):
                    continue
                add(d, p)
        for d in fs.dirs("/opt"):
            if d not in VPS_SKIP_OPT:
                add(d, f"/opt/{d}")
                if sites and sites[-1].remote == f"/opt/{d}" and sites[-1].kind == "site" and not sites[-1].dbs:
                    # В /opt часто лежат установленные программы, а не проекты — галочку не ставим.
                    sites[-1].checked = False
                    sites[-1].note = "похоже на установленную программу, а не ваш проект"
        # конфиги сервера
        if fs.isdir("/etc/nginx"):
            extras.append(FoundItem("x-nginx", "Настройки nginx", "/etc/nginx", "config", ["*.log"]))
        for name in _custom_units(sftp):
            extras.append(FoundItem(f"x-unit-{name}", f"Служба {name}", f"/etc/systemd/system/{name}", "config"))
        if fs.isdir("/var/spool/cron/crontabs"):
            extras.append(FoundItem("x-cron", "Задания cron", "/var/spool/cron/crontabs", "config"))
        if fs.isdir("/etc/letsencrypt"):
            extras.append(FoundItem("x-le", "SSL-сертификаты (Let's Encrypt)", "/etc/letsencrypt", "config",
                                    checked=False, note="содержит закрытые ключи — включите шифрование 2-й копии"))
    return sites, extras


def _custom_units(sftp) -> list[str]:
    try:
        items = sftp.listdir_attr("/etc/systemd/system")
    except OSError:
        return []
    return sorted(a.filename for a in items
                  if a.filename.endswith(".service") and stat.S_ISREG(a.st_mode or 0))


PANEL_PORTS = {80, 443, 1500, 1501, 2222, 8080, 8083, 8443}


def normalize_host(text: str) -> tuple[str, int | None]:
    """Из того, что вставил пользователь, достать имя сервера (и SSH-порт, если он явно указан).

    'https://server165.hosting.reg.ru:1500/' -> ('server165.hosting.reg.ru', None)
    'root@1.2.3.4'                            -> ('1.2.3.4', None)
    'example.com:2200'                        -> ('example.com', 2200)
    """
    t = (text or "").strip().strip("/").strip()
    if "://" in t:
        t = t.split("://", 1)[1]
    t = t.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in t:
        t = t.rsplit("@", 1)[1]
    port = None
    if t.startswith("["):  # [IPv6]:port
        h, _, rest = t[1:].partition("]")
        t = h
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif t.count(":") == 1:
        h, p = t.split(":")
        t = h
        if p.isdigit():
            port = int(p)
    if port in PANEL_PORTS:  # это порт панели управления/сайта, а не SSH
        port = None
    return t.strip().lower(), port


def scan(provider: str, host: str, port: int, user: str, password: str | None, data_dir: Path,
         key_file: str | None = None, key_passphrase: str | None = None, timeout: int = 30) -> ScanResult:
    """Подключиться и найти всё, что стоит копировать."""
    provider = provider if provider in PROVIDERS else "other"
    user = (user or "").strip()
    host, explicit_port = normalize_host(host)
    if explicit_port and port == 22:
        port = explicit_port
    if "@" in user:  # вставили «логин@сервер»
        user, _, h = user.partition("@")
        host = host or normalize_host(h)[0]
    if provider == "beget" and not host:
        host = f"{user}.beget.tech"
    sc = ssh_config_for(host)
    if sc.get("hostname") and sc["hostname"] != host:
        log.info("Сервер «%s» найден в ~/.ssh/config: %s", host, sc["hostname"])
        host = sc["hostname"]
        user = user or sc.get("user", "")
        if port == 22 and str(sc.get("port", "22")).isdigit():
            port = int(sc.get("port", 22))
    if provider == "timeweb" and not user:
        user = "root"
    if not host:
        raise DiscoverError("Укажите адрес сервера.")
    if not user:
        raise DiscoverError("Укажите логин.")
    client, used_key = wizard_connect(host, port, user, password, key_file, key_passphrase, data_dir, timeout)
    try:
        fp = fingerprint(client.get_transport().get_remote_server_key())
        sftp = client.open_sftp()
        sites, extras = find_items(sftp, provider)
        res = ScanResult(provider=provider, host=host, port=port, user=user, fingerprint=fp,
                         password=password if not used_key else None, key_file=used_key,
                         key_passphrase=key_passphrase if used_key else None, sites=sites, extras=extras)
        if any(i.dbs for i in sites):
            r = exec_read(client, "command -v mysqldump || command -v mariadb-dump", timeout=timeout)
            res.has_mysqldump = bool(r and r[0] == 0 and r[1].strip())
            if not res.has_mysqldump:
                res.warnings.append("на сервере не найден mysqldump — базы данных скопировать не получится")
        if not sites:
            res.warnings.append("сайты не найдены автоматически — укажите папку вручную")
        return res
    finally:
        client.close()


# --------------------------------------------------------------------------- превращение в источники


def slug(s: str) -> str:
    """Имя для папки/идентификатора: латиница; кириллические домены — в виде xn--… (punycode)."""
    if not s.isascii():
        try:
            s = s.encode("idna").decode()
        except UnicodeError:
            s = s.encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", s).strip("-.")
    return (s or "site")[:50]


def build_sources(res: ScanResult, chosen: list[str], schedule: str, existing_ids: set[str],
                  manual_path: str | None = None) -> tuple[list[dict], dict[str, str]]:
    """Собрать описания источников (как блоки config.yaml) и секреты для сохранения.

    Каждый сайт — отдельный источник (своё расписание и вкл/выкл), настройки сервера — один источник.
    """
    account = slug(f"{res.provider}-{res.user}-{res.host}" if res.provider in ("timeweb", "other")
                   else f"{res.provider}-{res.user}")
    secrets: dict[str, str] = {}
    ssh: dict = {"host": res.host, "port": res.port, "user": res.user}
    if res.key_file:
        ssh["key_file"] = res.key_file
        if res.key_passphrase:
            ssh["key_passphrase_secret"] = f"{account}-key"
            secrets[f"{account}-key"] = res.key_passphrase
    else:
        ssh["password_secret"] = f"{account}-ssh"
        secrets[f"{account}-ssh"] = res.password or ""

    def unique(base: str) -> str:
        sid, n = base, 2
        while sid in existing_ids:
            sid, n = f"{base}-{n}", n + 1
        existing_ids.add(sid)
        return sid

    sources: list[dict] = []
    items = [i for i in res.sites if i.key in chosen]
    if manual_path:
        items.append(FoundItem("manual", posixpath.basename(manual_path.rstrip("/")) or manual_path,
                               manual_path, "site", ["*.log"]))
    vps = res.provider in ("timeweb", "other") and not any(i.dbs for i in items) and len(items) > 0
    extras = [i for i in res.extras if i.key in chosen]

    if vps or (extras and not items):
        # VPS: один источник «сервер» со всеми папками
        files, names = [], set()
        for i in items + extras:
            name = slug(i.title if i.kind != "config" else posixpath.basename(i.remote.rstrip("/")))
            while name in names:
                name += "_"
            names.add(name)
            files.append({"remote": i.remote, "name": name, "exclude": i.exclude})
        sources.append({"id": unique(slug(f"{res.provider}-{res.host}")), "provider": res.provider,
                        "title": f"Сервер {res.host}", "schedule": schedule, "ssh": ssh, "files": files})
        return sources, secrets

    for i in items:
        src: dict = {"id": unique(f"{res.provider}-{slug(i.title)}"[:64]), "provider": res.provider,
                     "title": i.title, "schedule": schedule, "ssh": ssh,
                     "files": [{"remote": i.remote, "name": "site", "exclude": i.exclude}]}
        if i.dbs and res.has_mysqldump:
            src["mysql"] = {"databases": [
                {"name": d.name, "user": d.user, "host": d.host, "from_file": d.from_file, "kind": d.kind}
                for d in i.dbs]}
            if any(d.port for d in i.dbs):
                src["mysql"]["port"] = next(d.port for d in i.dbs if d.port)
        sources.append(src)
    if extras:
        files = [{"remote": e.remote, "name": slug(posixpath.basename(e.remote.rstrip("/"))), "exclude": e.exclude}
                 for e in extras]
        sources.append({"id": unique(slug(f"{res.provider}-{res.host}-server")), "provider": res.provider,
                        "title": f"Настройки сервера {res.host}", "schedule": schedule, "ssh": ssh, "files": files})
    return sources, secrets
