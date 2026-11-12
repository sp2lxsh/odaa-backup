"""Сведения о бэкапах из API хостингов (только чтение: список копий для отчёта).

- Beget:      https://api.beget.com/api/backup/getFileBackupList, getMysqlBackupList
- Timeweb:    https://api.timeweb.cloud/api/v1/servers, …/disks/{id}/backups
- ISPmanager: <url>/ispmgr?func=backup2.list (экспериментально, зависит от версии панели)

Запросы на создание/восстановление копий НЕ отправляются — это изменило бы данные на хостинге.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import __version__, secretstore
from .config import APIConf

log = logging.getLogger(__name__)
UA = f"odaa-backup/{__version__}"


class APIError(Exception):
    pass


def _get_json(url: str, headers: dict | None = None, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise APIError(f"HTTP {e.code} от {urllib.parse.urlsplit(url).netloc}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        # В URL Beget/ISPmanager есть пароль — не выводим URL, только хост.
        raise APIError(f"{urllib.parse.urlsplit(url).netloc}: {secretstore.redact(str(e))}") from None


def beget_backups(api: APIConf, timeout: int = 60) -> list[dict]:
    password = secretstore.require(api.password_secret)
    out: list[dict] = []
    for method, kind in (("getFileBackupList", "файлы"), ("getMysqlBackupList", "MySQL")):
        q = urllib.parse.urlencode({"login": api.login, "passwd": password, "output_format": "json"})
        data = _get_json(f"https://api.beget.com/api/backup/{method}?{q}", timeout=timeout)
        if data.get("status") != "success":
            raise APIError(f"Beget API: {_beget_error(data)}")
        answer = data.get("answer") or {}
        if answer.get("status") != "success":
            raise APIError(f"Beget API: {_beget_error(answer)}")
        for item in answer.get("result") or []:
            out.append({"type": kind, "id": item.get("backup_id"), "date": item.get("date"),
                        "info": {k: v for k, v in item.items() if k not in ("backup_id", "date")}})
    return out


def _beget_error(d: dict) -> str:
    errs = d.get("errors") or d.get("error_text") or d
    return secretstore.redact(str(errs))[:300]


def timeweb_backups(api: APIConf, timeout: int = 60) -> list[dict]:
    token = secretstore.require(api.token_secret)
    base = (api.url or "https://api.timeweb.cloud").rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    servers = _get_json(f"{base}/api/v1/servers", headers, timeout).get("servers") or []
    out: list[dict] = []
    for srv in servers:
        if api.server_id and str(srv.get("id")) != str(api.server_id):
            continue
        disks = srv.get("disks")
        if disks is None:
            disks = _get_json(f"{base}/api/v1/servers/{srv['id']}/disks", headers, timeout).get("server_disks") or []
        for disk in disks:
            data = _get_json(f"{base}/api/v1/servers/{srv['id']}/disks/{disk['id']}/backups", headers, timeout)
            for b in data.get("backups") or []:
                out.append({
                    "type": f"VPS «{srv.get('name', srv.get('id'))}», диск {disk.get('id')}",
                    "id": b.get("id"),
                    "date": b.get("created_at"),
                    "info": {k: b.get(k) for k in ("name", "status", "size", "type", "comment") if k in b},
                })
        if not disks:
            out.append({"type": f"VPS «{srv.get('name')}»", "id": None, "date": None,
                        "info": {"note": "дисков не найдено"}})
    return out


def ispmanager_backups(api: APIConf, timeout: int = 60) -> list[dict]:
    password = secretstore.require(api.password_secret)
    base = api.url.rstrip("/")
    if not base.endswith("/ispmgr"):
        base += "/ispmgr"
    q = urllib.parse.urlencode({"authinfo": f"{api.login}:{password}", "out": "json", "func": "backup2.list"})
    data = _get_json(f"{base}?{q}", timeout=timeout)
    doc = data.get("doc", data)
    if "error" in doc:
        raise APIError(f"ISPmanager: {secretstore.redact(json.dumps(doc['error'], ensure_ascii=False))[:300]}")
    elems = doc.get("elem") or []
    if isinstance(elems, dict):
        elems = [elems]
    out = []
    for e in elems:
        flat = {k: (v.get("$") if isinstance(v, dict) else v) for k, v in e.items()}
        out.append({"type": "ISPmanager", "id": flat.get("id") or flat.get("name"),
                    "date": flat.get("date") or flat.get("cdate") or flat.get("name"), "info": flat})
    return out


def list_backups(api: APIConf, timeout: int = 60) -> list[dict]:
    fn = {"beget": beget_backups, "timeweb": timeweb_backups, "ispmanager": ispmanager_backups}[api.type]
    return fn(api, timeout)
