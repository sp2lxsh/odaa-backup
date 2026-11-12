"""Загрузка и проверка конфигурации (config.yaml) и пользовательских настроек из интерфейса."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .paths import app_dir
from .schedule import ScheduleError, parse_schedule

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SECRET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,99}$")
API_TYPES = ("beget", "timeweb", "ispmanager")


class ConfigError(Exception):
    """Ошибка в config.yaml. В .errors — список понятных сообщений."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("Ошибки в настройках:\n  - " + "\n  - ".join(errors))


@dataclass
class Retention:
    daily: int = 7
    weekly: int = 4
    monthly: int = 6
    latest: int = 3  # плюс N самых свежих снимков (важно при расписании «каждые N часов»)


@dataclass
class Network:
    timeout: int = 60
    retries: int = 5
    retry_delay: int = 10
    rate_limit_kbps: int = 0  # 0 = без ограничения
    retry_failed_after_minutes: int = 60


@dataclass
class Telegram:
    enabled: bool = False
    bot_token_secret: str = "telegram-bot-token"
    chat_id: str = ""
    notify: str = "always"  # always | errors


@dataclass
class SecondCopy:
    path: str = ""
    encrypt: bool = True
    password_secret: str = "archive-password"
    keep: int = 3
    seven_zip: str = ""


@dataclass
class SSHConf:
    host: str
    user: str
    port: int = 22
    password_secret: str | None = None
    key_file: str | None = None
    key_passphrase_secret: str | None = None
    host_key: str | None = None  # SHA256:... — необязательное «закрепление» отпечатка


@dataclass
class FileSpec:
    remote: str
    name: str
    exclude: list[str] = field(default_factory=list)


@dataclass
class DBSpec:
    name: str
    user: str
    password_secret: str | None = None
    host: str | None = None
    # Если задано — логин/пароль базы читаются из конфига сайта (wp-config.php, .env…) при каждой копии.
    from_file: str | None = None
    kind: str | None = None  # wordpress | joomla | bitrix | opencart | env


@dataclass
class MySQLConf:
    databases: list[DBSpec]
    mysqldump: str = "mysqldump"
    host: str = "localhost"
    port: int | None = None
    password_via: str = "stdin"  # stdin | env
    extra_args: list[str] | None = None
    min_bytes: int = 1024
    allow_empty: bool = False


@dataclass
class APIConf:
    type: str
    login: str | None = None
    password_secret: str | None = None
    token_secret: str | None = None
    url: str | None = None
    server_id: str | None = None


@dataclass
class Source:
    id: str
    provider: str
    title: str
    enabled: bool = True
    schedule: str = "daily 03:00"
    ssh: SSHConf | None = None
    files: list[FileSpec] = field(default_factory=list)
    mysql: MySQLConf | None = None
    api: APIConf | None = None
    retention: Retention | None = None
    remote_checksum: bool = True
    managed: bool = False  # добавлен через окно программы (хранится в data/app.json)

    def secret_names(self) -> list[str]:
        names: list[str] = []
        if self.ssh:
            names += [self.ssh.password_secret, self.ssh.key_passphrase_secret]
        if self.mysql:
            names += [d.password_secret for d in self.mysql.databases if d.password_secret]
        if self.api:
            names += [self.api.password_secret, self.api.token_secret]
        return [n for n in names if n]


@dataclass
class Config:
    backup_root: Path
    data_dir: Path
    retention: Retention
    network: Network
    telegram: Telegram
    second_copy: SecondCopy
    ui_port: int
    sources: list[Source]
    path: Path | None = None
    auto_schedule: bool = True
    backup_root_is_default: bool = False

    def source(self, sid: str) -> Source:
        for s in self.sources:
            if s.id == sid:
                return s
        raise KeyError(sid)

    def retention_for(self, src: Source) -> Retention:
        return src.retention or self.retention

    def all_secret_names(self) -> list[str]:
        names: list[str] = []
        for s in self.sources:
            names += s.secret_names()
        if self.telegram.enabled:
            names.append(self.telegram.bot_token_secret)
        if self.second_copy.path and self.second_copy.encrypt:
            names.append(self.second_copy.password_secret)
        seen: list[str] = []
        for n in names:
            if n not in seen:
                seen.append(n)
        return seen

    @property
    def overrides_path(self) -> Path:
        return self.data_dir / "overrides.json"


# --------------------------------------------------------------------------- разбор


class _Ctx:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")


def _dict(ctx: _Ctx, where: str, v: Any) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict):
        ctx.err(where, "ожидался блок с параметрами (ключ: значение)")
        return {}
    return v


def _int(ctx: _Ctx, where: str, v: Any, default: int, minimum: int = 0) -> int:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            v = int(str(v).strip())
        except ValueError:
            ctx.err(where, f"ожидалось целое число, получено «{v}»")
            return default
    if v < minimum:
        ctx.err(where, f"значение должно быть не меньше {minimum}")
        return default
    return v


def _bool(ctx: _Ctx, where: str, v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("yes", "true", "1", "да", "on"):
        return True
    if s in ("no", "false", "0", "нет", "off"):
        return False
    ctx.err(where, f"ожидалось да/нет (true/false), получено «{v}»")
    return default


def _str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _secret(ctx: _Ctx, where: str, v: Any) -> str | None:
    s = _str(v)
    if s is not None and not SECRET_RE.match(s):
        ctx.err(where, f"имя секрета «{s}» может содержать только латиницу, цифры и _ . @ -")
    return s


def _check_no_plain_secrets(ctx: _Ctx, where: str, d: dict) -> None:
    for key in ("password", "passwd", "token", "api_key", "passphrase", "bot_token"):
        if key in d:
            ctx.err(
                where,
                f"параметр «{key}» запрещён: пароли и токены не хранятся в конфиге. "
                f"Используйте «{key}_secret: имя» и сохраните значение командой "
                "«odaa-backup secret set имя» или на странице «Пароли».",
            )


def _retention(ctx: _Ctx, where: str, v: Any, default: Retention | None) -> Retention:
    d = _dict(ctx, where, v)
    base = default or Retention()
    return Retention(
        daily=_int(ctx, f"{where}.daily", d.get("daily"), base.daily),
        weekly=_int(ctx, f"{where}.weekly", d.get("weekly"), base.weekly),
        monthly=_int(ctx, f"{where}.monthly", d.get("monthly"), base.monthly),
        latest=_int(ctx, f"{where}.latest", d.get("latest"), base.latest),
    )


def _source(ctx: _Ctx, idx: int, raw: Any, seen_ids: set[str]) -> Source | None:
    where = f"sources[{idx + 1}]"
    d = _dict(ctx, where, raw)
    if not d:
        return None
    sid = _str(d.get("id"))
    if not sid or not ID_RE.match(sid):
        ctx.err(where, "нужен «id»: латиница/цифры/-/_ без пробелов (например beget-mysite)")
        return None
    where = f"источник «{sid}»"
    if sid in seen_ids:
        ctx.err(where, "такой id уже есть — id должен быть уникальным")
    seen_ids.add(sid)
    _check_no_plain_secrets(ctx, where, d)

    provider = _str(d.get("provider")) or "other"
    if not ID_RE.match(provider):
        ctx.err(where, "«provider» — латиница/цифры без пробелов (beget, regru, timeweb...)")

    schedule = _str(d.get("schedule")) or "daily 03:00"
    try:
        parse_schedule(schedule)
    except ScheduleError as e:
        ctx.err(f"{where}.schedule", str(e))

    ssh = None
    if d.get("ssh") is not None:
        sd = _dict(ctx, f"{where}.ssh", d.get("ssh"))
        _check_no_plain_secrets(ctx, f"{where}.ssh", sd)
        host, user = _str(sd.get("host")), _str(sd.get("user"))
        if not host or not user:
            ctx.err(f"{where}.ssh", "нужны «host» и «user»")
        else:
            ssh = SSHConf(
                host=host,
                user=user,
                port=_int(ctx, f"{where}.ssh.port", sd.get("port"), 22, 1),
                password_secret=_secret(ctx, f"{where}.ssh.password_secret", sd.get("password_secret")),
                key_file=_str(sd.get("key_file")),
                key_passphrase_secret=_secret(
                    ctx, f"{where}.ssh.key_passphrase_secret", sd.get("key_passphrase_secret")
                ),
                host_key=_str(sd.get("host_key")),
            )
            if not ssh.password_secret and not ssh.key_file:
                ctx.err(f"{where}.ssh", "укажите «password_secret» или «key_file»")
            if ssh.host_key and not ssh.host_key.startswith("SHA256:"):
                ctx.err(f"{where}.ssh.host_key", "отпечаток должен начинаться с «SHA256:»")

    files: list[FileSpec] = []
    raw_files = d.get("files") or []
    if not isinstance(raw_files, list):
        ctx.err(f"{where}.files", "ожидался список папок")
        raw_files = []
    names: set[str] = set()
    for j, fr in enumerate(raw_files):
        fw = f"{where}.files[{j + 1}]"
        if isinstance(fr, str):
            fr = {"remote": fr}
        fd = _dict(ctx, fw, fr)
        remote = _str(fd.get("remote"))
        if not remote:
            ctx.err(fw, "нужен «remote» — путь на сервере")
            continue
        name = _str(fd.get("name")) or _default_name(remote)
        if not ID_RE.match(name):
            ctx.err(fw, f"«name» ({name}) — латиница/цифры/-/_ без пробелов")
        if name in names:
            ctx.err(fw, f"имя «{name}» повторяется — задайте разные «name»")
        names.add(name)
        excl = fd.get("exclude") or []
        if not isinstance(excl, list):
            ctx.err(f"{fw}.exclude", "ожидался список шаблонов")
            excl = []
        files.append(FileSpec(remote=remote, name=name, exclude=[str(x) for x in excl]))

    mysql = None
    if d.get("mysql") is not None:
        mw = f"{where}.mysql"
        md = _dict(ctx, mw, d.get("mysql"))
        _check_no_plain_secrets(ctx, mw, md)
        dbs: list[DBSpec] = []
        for j, dr in enumerate(md.get("databases") or []):
            dw = f"{mw}.databases[{j + 1}]"
            dd = _dict(ctx, dw, dr)
            _check_no_plain_secrets(ctx, dw, dd)
            name, user = _str(dd.get("name")), _str(dd.get("user"))
            psec = _secret(ctx, f"{dw}.password_secret", dd.get("password_secret"))
            from_file = _str(dd.get("from_file"))
            if not name or not (psec or from_file):
                ctx.err(dw, "нужны «name» и «password_secret» (или «from_file» — конфиг сайта с паролем базы)")
                continue
            if not re.match(r"^[A-Za-z0-9_$-]+$", name):
                ctx.err(dw, f"недопустимое имя базы «{name}»")
                continue
            dbs.append(DBSpec(name=name, user=user or name, password_secret=psec, host=_str(dd.get("host")),
                              from_file=from_file, kind=_str(dd.get("kind"))))
        if not dbs:
            ctx.err(mw, "нужен список «databases»")
        via = (_str(md.get("password_via")) or "stdin").lower()
        if via not in ("stdin", "env"):
            ctx.err(f"{mw}.password_via", "допустимо stdin или env")
        extra = md.get("extra_args")
        if extra is not None and not isinstance(extra, list):
            ctx.err(f"{mw}.extra_args", "ожидался список")
            extra = None
        mysql = MySQLConf(
            databases=dbs,
            mysqldump=_str(md.get("mysqldump")) or "mysqldump",
            host=_str(md.get("host")) or "localhost",
            port=_int(ctx, f"{mw}.port", md.get("port"), 0, 0) or None,
            password_via=via,
            extra_args=[str(x) for x in extra] if extra is not None else None,
            min_bytes=_int(ctx, f"{mw}.min_bytes", md.get("min_bytes"), 1024),
            allow_empty=_bool(ctx, f"{mw}.allow_empty", md.get("allow_empty"), False),
        )

    api = None
    if d.get("api") is not None:
        aw = f"{where}.api"
        ad = _dict(ctx, aw, d.get("api"))
        _check_no_plain_secrets(ctx, aw, ad)
        atype = (_str(ad.get("type")) or "").lower()
        if atype not in API_TYPES:
            ctx.err(f"{aw}.type", f"допустимо: {', '.join(API_TYPES)}")
        else:
            api = APIConf(
                type=atype,
                login=_str(ad.get("login")),
                password_secret=_secret(ctx, f"{aw}.password_secret", ad.get("password_secret")),
                token_secret=_secret(ctx, f"{aw}.token_secret", ad.get("token_secret")),
                url=_str(ad.get("url")),
                server_id=_str(ad.get("server_id")),
            )
            if atype in ("beget", "ispmanager") and (not api.login or not api.password_secret):
                ctx.err(aw, "для этого API нужны «login» и «password_secret»")
            if atype == "ispmanager" and not api.url:
                ctx.err(aw, "для ISPmanager нужен «url» панели, например https://server.hosting.reg.ru:1500")
            if atype == "timeweb" and not api.token_secret:
                ctx.err(aw, "для Timeweb нужен «token_secret»")

    if (files or mysql) and not ssh:
        ctx.err(where, "для копирования файлов и баз нужен блок «ssh»")
    if not files and not mysql and not api:
        ctx.err(where, "нечего копировать: добавьте «files», «mysql» или «api»")

    retention = None
    if d.get("retention") is not None:
        retention = _retention(ctx, f"{where}.retention", d.get("retention"), None)

    return Source(
        id=sid,
        provider=provider,
        title=_str(d.get("title")) or sid,
        enabled=_bool(ctx, f"{where}.enabled", d.get("enabled"), True),
        schedule=schedule,
        ssh=ssh,
        files=files,
        mysql=mysql,
        api=api,
        retention=retention,
        remote_checksum=_bool(ctx, f"{where}.remote_checksum", d.get("remote_checksum"), True),
    )


def _default_name(remote: str) -> str:
    parts = [p for p in remote.replace("\\", "/").split("/") if p and p not in (".", "~")]
    name = parts[-1] if parts else "home"
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name).lstrip(".") or "files"
    return name[:64]


def parse_config(data: Any, base_dir: Path | None = None, path: Path | None = None) -> Config:
    """Разобрать уже загруженный YAML (dict). Бросает ConfigError со всеми найденными ошибками."""
    ctx = _Ctx()
    base_dir = base_dir or app_dir()
    d = _dict(ctx, "config", data)
    _check_no_plain_secrets(ctx, "config", d)

    root_s = _str(d.get("backup_root"))
    root_is_default = not root_s
    if not root_s:
        from .paths import default_backup_root

        root = default_backup_root()
    else:
        root = Path(os.path.expandvars(os.path.expanduser(root_s)))
        if not root.is_absolute():
            root = base_dir / root
    data_s = _str(d.get("data_dir"))
    data_dir = Path(os.path.expandvars(os.path.expanduser(data_s))) if data_s else base_dir / "data"
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir

    retention = _retention(ctx, "retention", d.get("retention"), Retention())

    nd = _dict(ctx, "network", d.get("network"))
    network = Network(
        timeout=_int(ctx, "network.timeout", nd.get("timeout"), 60, 5),
        retries=_int(ctx, "network.retries", nd.get("retries"), 5, 0),
        retry_delay=_int(ctx, "network.retry_delay", nd.get("retry_delay"), 10, 0),
        rate_limit_kbps=_int(ctx, "network.rate_limit_kbps", nd.get("rate_limit_kbps"), 0, 0),
        retry_failed_after_minutes=_int(
            ctx, "network.retry_failed_after_minutes", nd.get("retry_failed_after_minutes"), 60, 0
        ),
    )

    td = _dict(ctx, "telegram", d.get("telegram"))
    _check_no_plain_secrets(ctx, "telegram", td)
    telegram = Telegram(
        enabled=_bool(ctx, "telegram.enabled", td.get("enabled"), False),
        bot_token_secret=_secret(ctx, "telegram.bot_token_secret", td.get("bot_token_secret"))
        or "telegram-bot-token",
        chat_id=_str(td.get("chat_id")) or "",
        notify=(_str(td.get("notify")) or "always").lower(),
    )
    if telegram.enabled and not telegram.chat_id:
        ctx.err("telegram", "укажите «chat_id»")
    if telegram.notify not in ("always", "errors"):
        ctx.err("telegram.notify", "допустимо always или errors")

    cd = _dict(ctx, "second_copy", d.get("second_copy"))
    _check_no_plain_secrets(ctx, "second_copy", cd)
    second = SecondCopy(
        path=_str(cd.get("path")) or "",
        encrypt=_bool(ctx, "second_copy.encrypt", cd.get("encrypt"), True),
        password_secret=_secret(ctx, "second_copy.password_secret", cd.get("password_secret"))
        or "archive-password",
        keep=_int(ctx, "second_copy.keep", cd.get("keep"), 3, 1),
        seven_zip=_str(cd.get("seven_zip")) or "",
    )

    ud = _dict(ctx, "ui", d.get("ui"))
    port = _int(ctx, "ui.port", ud.get("port"), 8765, 1024)

    raw_sources = d.get("sources") or []
    if not isinstance(raw_sources, list):
        ctx.err("sources", "ожидался список источников")
        raw_sources = []
    seen: set[str] = set()
    sources = [s for i, r in enumerate(raw_sources) if (s := _source(ctx, i, r, seen))]

    if ctx.errors:
        raise ConfigError(ctx.errors)
    managed_ids = set(d.get("_managed_ids") or [])
    for s in sources:
        s.managed = s.id in managed_ids
    return Config(
        auto_schedule=_bool(ctx, "auto_schedule", d.get("auto_schedule"), True),
        backup_root_is_default=root_is_default,
        backup_root=root,
        data_dir=data_dir,
        retention=retention,
        network=network,
        telegram=telegram,
        second_copy=second,
        ui_port=port,
        sources=sources,
        path=path,
    )


# Строка в двойных кавычках, похожая на путь Windows: "C:\..." или "\\server\...".
_DQ_WITH_BACKSLASH = re.compile(r'"((?:[A-Za-z]:\\|\\\\)[^"\n]*)"')


def _windows_paths_to_single_quotes(text: str) -> str:
    """"C:\\Users\\..." в двойных кавычках -> 'C:\\Users\\...' (в одинарных обратная черта не спецсимвол)."""

    def fix(m: re.Match) -> str:
        inner = m.group(1)
        if "'" in inner:
            return m.group(0)
        if "\\\\" in inner:  # уже записано правильно, с двойной чертой
            inner = inner.replace("\\\\", "\\")
        return "'" + inner + "'"

    return _DQ_WITH_BACKSLASH.sub(fix, text)


def parse_yaml_text(text: str, name: str = "config.yaml") -> Any:
    """Разобрать YAML. Пути Windows в двойных кавычках с одной обратной чертой исправляются автоматически."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        first_error = e
    fixed = _windows_paths_to_single_quotes(text)
    if fixed != text:
        try:
            return yaml.safe_load(fixed)
        except yaml.YAMLError:
            pass
    hint = ""
    if "escape" in str(first_error):
        hint = (" — путь Windows пишите в одинарных кавычках: 'C:\\Users\\sp\\Backups' "
                "(или в двойных, но с двойной чертой: \"C:\\\\Users\\\\sp\\\\Backups\")")
    raise ConfigError([f"{name}: ошибка синтаксиса YAML: {first_error}{hint}"]) from None


APP_FILE = "app.json"
SETTING_KEYS = ("backup_root", "retention", "network", "telegram", "second_copy", "auto_schedule")


def _data_dir_for(data: dict, base_dir: Path) -> Path:
    s = _str(data.get("data_dir")) if isinstance(data, dict) else None
    p = Path(os.path.expandvars(os.path.expanduser(s))) if s else base_dir / "data"
    return p if p.is_absolute() else base_dir / p


def load_app_store(data_dir: Path) -> dict:
    """Настройки и источники, созданные в окне программы (data/app.json)."""
    p = Path(data_dir) / APP_FILE
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"settings": {}, "sources": []}
    if not isinstance(d, dict):
        return {"settings": {}, "sources": []}
    d.setdefault("settings", {})
    d.setdefault("sources", [])
    return d


def save_app_store(data_dir: Path, store: dict) -> None:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    p = Path(data_dir) / APP_FILE
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def merge_store(data: dict, store: dict) -> dict:
    """config.yaml (если есть) + настройки и источники из окна программы."""
    merged = dict(data or {})
    for k, v in (store.get("settings") or {}).items():
        if k in SETTING_KEYS and v is not None:
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
    app_sources = [s for s in store.get("sources") or [] if isinstance(s, dict)]
    merged["sources"] = list(merged.get("sources") or []) + app_sources
    merged["_managed_ids"] = [s.get("id") for s in app_sources]
    return merged


EXAMPLE_HOSTS = {"mylogin.beget.tech", "server123.hosting.reg.ru", "203.0.113.10"}


def _is_example_source(x: Any) -> bool:
    try:
        return str(x["ssh"]["host"]).strip().lower() in EXAMPLE_HOSTS
    except (KeyError, TypeError):
        return False


def load_config(path: Path, apply_user_overrides: bool = True) -> Config:
    """Загрузить настройки. config.yaml необязателен: всё можно настроить в окне программы."""
    path = Path(path)
    base_dir = path.resolve().parent
    data: Any = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
        except OSError as e:
            raise ConfigError([f"не удалось прочитать {path}: {e}"]) from None
        data = parse_yaml_text(text, path.name) or {}
        if not isinstance(data, dict):
            raise ConfigError([f"{path.name}: ожидался набор параметров «ключ: значение»"])
        # Примеры из config.example.yaml, которые так и не заполнили, не показываем.
        if isinstance(data.get("sources"), list):
            data["sources"] = [x for x in data["sources"] if not _is_example_source(x)]
    store = load_app_store(_data_dir_for(data, base_dir))
    data = merge_store(data, store)
    cfg = parse_config(data, base_dir=base_dir, path=path)
    if apply_user_overrides:
        cfg = apply_overrides(cfg, load_overrides(cfg))
    return cfg


# --------------------------------------------------------------------------- настройки из интерфейса

OVERRIDE_KEYS = ("enabled", "schedule", "retention")


def load_overrides(cfg: Config) -> dict:
    p = cfg.overrides_path
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_overrides(cfg: Config, overrides: dict) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    tmp = cfg.overrides_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(overrides, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg.overrides_path)


def apply_overrides(cfg: Config, overrides: dict) -> Config:
    """Наложить настройки, изменённые в интерфейсе (вкл/выкл, расписание, ротация), на config.yaml."""
    new_sources = []
    for s in cfg.sources:
        o = overrides.get(s.id) or {}
        if not isinstance(o, dict):
            o = {}
        changes: dict[str, Any] = {}
        if isinstance(o.get("enabled"), bool):
            changes["enabled"] = o["enabled"]
        if isinstance(o.get("schedule"), str):
            try:
                parse_schedule(o["schedule"])
                changes["schedule"] = o["schedule"]
            except ScheduleError:
                pass
        r = o.get("retention")
        if isinstance(r, dict):
            ctx = _Ctx()
            ret = _retention(ctx, "override", r, cfg.retention_for(s))
            if not ctx.errors:
                changes["retention"] = ret
        new_sources.append(replace(s, **changes) if changes else s)
    return replace(cfg, sources=new_sources)
