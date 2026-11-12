"""Локальный веб-интерфейс на http://127.0.0.1:<порт> (доступен только с этого компьютера).

Возможности: список сайтов и статус последней копии, «Скачать сейчас», пробный запуск,
включение/выключение и частота копирования каждого сайта, хранение копий, список
снимков и их проверка, ввод паролей, проверка отпечатков серверов, задача Планировщика.
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_NAME, __version__, runner, schedule, secretstore
from .archive import second_copy_available
from .config import Config, ConfigError, load_config, load_overrides, save_overrides
from .manifest import verify_snapshot
from .paths import human_size
from .report import human_age, source_health
from .retention import list_snapshots, parse_stamp
from .ssh import KnownHosts, fetch_host_key, fingerprint
from .state import load_state, parse_ts

log = logging.getLogger(__name__)
e = html.escape

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2329;--muted:#66707a;--line:#e3e6ea;--accent:#2563eb;
--ok:#15803d;--okbg:#e8f6ec;--warn:#a16207;--warnbg:#fdf6e3;--err:#b91c1c;--errbg:#fdecec}
@media (prefers-color-scheme: dark){:root{--bg:#14171a;--card:#1c2024;--text:#e6e8ea;--muted:#9aa3ab;
--line:#2c3237;--accent:#60a5fa;--ok:#4ade80;--okbg:#14301f;--warn:#facc15;--warnbg:#3a3214;--err:#f87171;--errbg:#3b1717}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.45 "Segoe UI",system-ui,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);padding:12px 20px;display:flex;gap:18px;
align-items:center;flex-wrap:wrap}header b{font-size:17px}header a{color:var(--text);text-decoration:none}
header a:hover{color:var(--accent)}a{color:var(--accent)}main{max-width:1150px;margin:0 auto;padding:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
h1{font-size:20px;margin:0 0 12px}h2{font-size:16px;margin:0 0 10px}
table{width:100%;border-collapse:collapse}td,th{padding:8px 6px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:middle}th{color:var(--muted);font-weight:600;font-size:13px}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;font-weight:600}
.ok{color:var(--ok);background:var(--okbg)}.warn{color:var(--warn);background:var(--warnbg)}
.error{color:var(--err);background:var(--errbg)}.none{color:var(--muted);background:var(--bg)}
.muted{color:var(--muted);font-size:13px}button,.btn{background:var(--accent);color:#fff;border:0;
border-radius:7px;padding:7px 12px;font:inherit;cursor:pointer;text-decoration:none;display:inline-block}
button.secondary,.btn.secondary{background:transparent;color:var(--accent);border:1px solid var(--line)}
button.danger{background:var(--err)}form.inline{display:inline}select,input{font:inherit;padding:6px;
border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--text)}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:16px;white-space:pre-wrap}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);padding:12px;border-radius:8px;
font:13px/1.4 Consolas,monospace;max-height:520px;overflow:auto}
.progress{display:none}.progress.on{display:block}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.scroll{overflow-x:auto}
"""

JS = """
async function poll(){try{const r=await fetch('/api/status');const s=await r.json();
const box=document.getElementById('progress');if(!box)return;
if(s.running){box.classList.add('on');document.getElementById('p-text').textContent=
(s.dry_run?'Пробный запуск. ':'')+s.source+' — '+s.phase+(s.file?(': '+s.file):'')+
'  · передано '+s.bytes_h+(s.speed_h?(' ('+s.speed_h+')'):'');window._wasRunning=true}
else{box.classList.remove('on');if(window._wasRunning){window._wasRunning=false;location.reload()}}}catch(e){}}
setInterval(poll,1500);poll();
"""


class _State:
    config_path: Path
    token: str = ""
    port: int = 8765


ST = _State()


def _cfg() -> Config:
    return load_config(ST.config_path)


def _page(title: str, body: str, msg: str = "", level: str = "ok") -> bytes:
    flash = f'<div class="flash {level}">{e(msg)}</div>' if msg else ""
    nav = (
        '<a href="/">Сайты</a><a href="/secrets">Пароли</a><a href="/schedule">Расписание</a>'
        '<a href="/report">Отчёт</a><a href="/log">Журнал</a>'
    )
    doc = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} — {APP_NAME}</title>
<style>{CSS}</style></head><body><header><b>🗄 {APP_NAME}</b>{nav}
<span class="muted" style="margin-left:auto">v{__version__} · только этот компьютер</span></header>
<main>{flash}<div id="progress" class="card progress"><b>Идёт копирование…</b>
<div id="p-text" class="muted"></div></div>{body}</main><script>{JS}</script></body></html>"""
    return doc.encode("utf-8")


def _form(action: str, inner: str, cls: str = "inline", confirm: str = "") -> str:
    onsub = f' onsubmit="return confirm({e(json.dumps(confirm))})"' if confirm else ""
    return (f'<form method="post" action="{e(action)}" class="{cls}"{onsub}>'
            f'<input type="hidden" name="token" value="{ST.token}">{inner}</form>')


def _badge(level: str, text: str) -> str:
    return f'<span class="badge {level}">{e(text)}</span>'


# --------------------------------------------------------------------------- страницы


def page_dashboard(msg: str = "", level: str = "ok") -> bytes:
    try:
        cfg = _cfg()
    except ConfigError as ex:
        return _page("Ошибка настроек", f'<div class="card"><h1>Ошибка в config.yaml</h1><pre>{e(str(ex))}</pre>'
                     f'<p>Файл: {e(str(ST.config_path))}</p></div>', msg, level)
    state = load_state(cfg.data_dir)
    rows = []
    for s in cfg.sources:
        st = state.get(s.id, {})
        lvl, why = source_health(cfg, s.id, st)
        label = {"ok": "в порядке", "warn": "внимание", "error": "проблема", "none": "нет копий"}[lvl]
        last = parse_ts(st.get("last_success"))
        sched = schedule.describe(s.schedule) if s.enabled else "выключен"
        rows.append(f"""<tr>
<td><a href="/source/{e(s.id)}"><b>{e(s.title)}</b></a><div class="muted">{e(s.provider)} · {e(s.id)}</div></td>
<td>{_badge(lvl, label)}<div class="muted">{e(why)}</div></td>
<td>{e(human_age(last))}<div class="muted">{e(last.strftime('%d.%m.%Y %H:%M') if last else '')}</div></td>
<td>{e(human_size(st.get('last_size')) if st.get('last_size') else '—')}</td>
<td>{e(sched)}</td>
<td><div class="row">{_form('/run', f'<input type="hidden" name="source" value="{e(s.id)}"><button>Скачать сейчас</button>')}
<a class="btn secondary" href="/source/{e(s.id)}">Управление</a></div></td></tr>""")
    ok2, why2 = second_copy_available(cfg)
    warn = "" if ok2 else (f'<div class="flash warn">⚠️ Копии лежат только на одном диске: {e(why2)}. '
                           "Подключите внешний диск или облачную папку и укажите её в second_copy.path "
                           "(правило 3-2-1).</div>")
    body = f"""{warn}<div class="card"><div class="row" style="justify-content:space-between">
<h1>Сайты и источники</h1><div class="row">
{_form('/run', '<input type="hidden" name="source" value="__all__"><button>Скачать всё сейчас</button>')}
{_form('/run', '<input type="hidden" name="source" value="__all__"><input type="hidden" name="dry" value="1">'
       '<button class="secondary">Пробный запуск (ничего не скачивать)</button>')}</div></div>
<div class="scroll"><table><tr><th>Источник</th><th>Статус</th><th>Последняя копия</th><th>Размер</th>
<th>Расписание</th><th></th></tr>{''.join(rows) or '<tr><td colspan=6>Нет источников в config.yaml</td></tr>'}
</table></div><div class="row" style="margin-top:12px"><span class="muted">Папка бэкапов: {e(str(cfg.backup_root))}</span>
{_form('/openroot', '<button class="secondary">Открыть папку</button>')}</div></div>"""
    return _page("Сайты", body, msg, level)


def page_source(sid: str, msg: str = "", level: str = "ok") -> bytes:
    cfg = _cfg()
    try:
        s = cfg.source(sid)
    except KeyError:
        return page_dashboard(f"Источник {sid} не найден", "error")
    st = load_state(cfg.data_dir).get(sid, {})
    ret = cfg.retention_for(s)
    snaps = list_snapshots(runner.source_dir(cfg, s))

    opts = []
    presets = dict(schedule.PRESETS)
    if s.schedule not in presets:
        presets[s.schedule] = "Своё: " + schedule.describe(s.schedule)
    for val, label in presets.items():
        sel = " selected" if val == s.schedule else ""
        opts.append(f'<option value="{e(val)}"{sel}>{e(label)}</option>')
    settings = _form(f"/settings/{sid}", f"""
<table><tr><td style="width:220px">Делать копии</td><td><label><input type="checkbox" name="enabled" value="1"
{' checked' if s.enabled else ''}> включено</label></td></tr>
<tr><td>Как часто</td><td><select name="schedule">{''.join(opts)}</select>
 или своё: <input name="custom" placeholder="every 4h / daily 02:30 / weekly sat 04:00" size="34"></td></tr>
<tr><td>Хранить копий</td><td>последних <input name="latest" type="number" min="0" max="100" value="{ret.latest}"
 style="width:70px"> ежедневных <input name="daily" type="number" min="0" max="365" value="{ret.daily}"
 style="width:70px"> еженедельных <input name="weekly" type="number" min="0" max="104" value="{ret.weekly}"
 style="width:70px"> ежемесячных <input name="monthly" type="number" min="0" max="120" value="{ret.monthly}"
 style="width:70px"></td></tr></table><p><button>Сохранить настройки</button></p>""", cls="")

    srows = []
    for sn in snaps:
        info = sn.info
        lvl = {"ok": "ok", "partial": "warn", "failed": "error"}.get(sn.status or "", "none")
        label = {"ok": "успешно", "partial": "с ошибками", "failed": "ошибка"}.get(sn.status or "", "не завершён")
        dbs = ", ".join(f"{k} ({human_size(v)})" for k, v in (info.get("databases") or {}).items()) or "—"
        srows.append(f"""<tr><td><b>{e(sn.time.strftime('%d.%m.%Y %H:%M'))}</b><div class="muted">{e(sn.path.name)}</div></td>
<td>{_badge(lvl, label)}</td><td>{e(str(info.get('files', '—')))} файлов<div class="muted">
скачано {e(str(info.get('downloaded', '—')))}, без изменений {e(str(info.get('unchanged', '—')))}</div></td>
<td>{e(dbs)}</td><td>{e(human_size(info.get('size')) if info.get('size') else '—')}</td>
<td><div class="row">{_form(f'/verify/{sid}/{sn.path.name}', '<button class="secondary">Проверить</button>')}
{_form(f'/open/{sid}/{sn.path.name}', '<button class="secondary">Открыть</button>')}</div></td></tr>""")
        if info.get("errors"):
            srows.append(f'<tr><td></td><td colspan=5 class="muted">Ошибки: {e("; ".join(info["errors"][:5]))}</td></tr>')

    api_html = ""
    if s.api:
        items = st.get("api_backups") or []
        err = st.get("api_error")
        lis = "".join(f"<tr><td>{e(str(i.get('date') or '—'))}</td><td>{e(str(i.get('type')))}</td>"
                      f"<td class='muted'>{e(json.dumps(i.get('info') or {}, ensure_ascii=False)[:200])}</td></tr>"
                      for i in items[:30])
        api_html = (f'<div class="card"><h2>Копии в панели хостинга ({e(s.api.type)})</h2>'
                    + (f'<div class="flash error">{e(err)}</div>' if err else "")
                    + (f"<div class='scroll'><table><tr><th>Дата</th><th>Тип</th><th>Сведения</th></tr>{lis}</table></div>"
                       if items else "<p class='muted'>Нет данных (появятся после запуска копирования).</p>")
                    + "<p class='muted'>Это копии на стороне хостинга — они пропадут вместе с хостингом при атаке, "
                      "поэтому программа хранит свои независимые копии.</p></div>")

    hk = ""
    if s.ssh:
        known = KnownHosts(cfg.data_dir / "known_hosts").known_fingerprint(s.ssh.host, s.ssh.port)
        hk = (f"<p>Сервер: <b>{e(s.ssh.user)}@{e(s.ssh.host)}:{s.ssh.port}</b> · отпечаток: "
              f"{e(known) if known else '<span class=\"badge warn\">не проверен</span>'} "
              f'<a class="btn secondary" href="/hostkey/{e(sid)}">Проверить отпечаток</a> '
              f"{_form(f'/test/{sid}', '<button class=\"secondary\">Проверить подключение</button>')}</p>")
    what = []
    for f in s.files:
        what.append(f"Папка <b>{e(f.remote)}</b>" + (f" <span class='muted'>(кроме: {e(', '.join(f.exclude))})</span>"
                                                    if f.exclude else ""))
    if s.mysql:
        what.append("Базы MySQL: <b>" + e(", ".join(d.name for d in s.mysql.databases)) + "</b>")
    last_err = st.get("last_error")
    body = f"""<div class="card"><div class="row" style="justify-content:space-between"><h1>{e(s.title)}</h1>
<div class="row">{_form('/run', f'<input type="hidden" name="source" value="{e(sid)}"><button>Скачать сейчас</button>')}
{_form('/run', f'<input type="hidden" name="source" value="{e(sid)}"><input type="hidden" name="dry" value="1">'
       '<button class="secondary">Пробный запуск</button>')}</div></div>
<p class="muted">{e(s.provider)} · {e(sid)} · последняя попытка: {e(st.get('last_attempt') or '—')}
 · результат: {e(st.get('last_status') or '—')}</p>
{f'<div class="flash error">Последняя ошибка: {e(last_err)}</div>' if last_err else ''}
<p>{'<br>'.join(what) or 'Только сведения из API'}</p>{hk}</div>
<div class="card"><h2>Настройки копирования</h2>{settings}
<p class="muted">Изменения сохраняются в data/overrides.json и действуют сразу; config.yaml не меняется.
Для работы по расписанию должна быть создана задача Планировщика (страница «Расписание»).</p></div>
{api_html}
<div class="card"><h2>Снимки ({len(snaps)})</h2><div class="scroll"><table><tr><th>Дата</th><th>Статус</th>
<th>Файлы</th><th>Базы</th><th>Размер</th><th></th></tr>{''.join(srows) or '<tr><td colspan=6>Снимков пока нет</td></tr>'}
</table></div><p class="muted">Размер — полный объём снимка; неизменённые файлы являются жёсткими ссылками
на предыдущий снимок и место повторно не занимают.</p></div>"""
    return _page(s.title, body, msg, level)


def page_secrets(msg: str = "", level: str = "ok") -> bytes:
    cfg = _cfg()
    rows = []
    for name in cfg.all_secret_names():
        is_set = secretstore.is_set(name)
        rows.append(f"""<tr><td><b>{e(name)}</b></td><td>{_badge('ok' if is_set else 'error', 'задан' if is_set else 'не задан')}</td>
<td>{_form('/secret', f'<input type="hidden" name="name" value="{e(name)}"><input type="password" name="value" '
           f'autocomplete="new-password" placeholder="новое значение" required> <button>Сохранить</button>')}</td></tr>""")
    body = f"""<div class="card"><h1>Пароли и токены</h1>
<p>Значения хранятся в <b>Диспетчере учётных данных Windows</b> (служба «odaa-backup»), а не в файлах программы.
Здесь их можно задать или заменить; посмотреть сохранённое значение нельзя.</p>
<div class="scroll"><table><tr><th>Имя (из config.yaml)</th><th>Состояние</th><th>Задать / заменить</th></tr>
{''.join(rows) or '<tr><td colspan=3>В config.yaml нет ссылок на пароли</td></tr>'}</table></div></div>"""
    return _page("Пароли", body, msg, level)


def page_schedule(msg: str = "", level: str = "ok") -> bytes:
    cfg = _cfg()
    status = schedule.task_status()
    rows = "".join(f"<tr><td>{e(s.title)}</td><td>{e(schedule.describe(s.schedule) if s.enabled else 'выключен')}</td>"
                   f"<td><a href='/source/{e(s.id)}'>изменить</a></td></tr>" for s in cfg.sources)
    body = f"""<div class="card"><h1>Расписание</h1>
<p>Задача Планировщика Windows «{schedule.TASK_NAME}»: <b>{e(status)}</b></p>
<p>Задача запускает программу <b>каждый час</b>; программа сама решает, каким сайтам пора делать копию
(по их расписанию ниже). Если компьютер был выключен, пропущенная копия сделается при следующем включении.</p>
<div class="row">{_form('/task/install', '<button>Создать / обновить задачу</button>')}
{_form('/task/remove', '<button class="danger">Удалить задачу</button>', confirm='Удалить задачу Планировщика?')}</div>
<p class="muted">Задача выполняется, когда вы вошли в Windows (так пароли из Диспетчера учётных данных доступны).</p></div>
<div class="card"><h2>Расписание по сайтам</h2><table>{rows}</table></div>"""
    return _page("Расписание", body, msg, level)


def page_text(title: str, text: str) -> bytes:
    return _page(title, f'<div class="card"><h1>{e(title)}</h1><pre>{e(text)}</pre></div>')


def page_hostkey(sid: str) -> bytes:
    cfg = _cfg()
    s = cfg.source(sid)
    if not s.ssh:
        return page_source(sid, "У источника нет SSH", "error")
    try:
        key = fetch_host_key(s.ssh.host, s.ssh.port, cfg.network.timeout)
    except Exception as ex:  # noqa: BLE001
        return page_source(sid, f"Не удалось связаться с {s.ssh.host}: {ex}", "error")
    fp = fingerprint(key)
    kh = KnownHosts(cfg.data_dir / "known_hosts")
    st = kh.check(s.ssh.host, s.ssh.port, key)
    state_txt = {"ok": ("ok", "совпадает с сохранённым — всё в порядке"),
                 "unknown": ("warn", "сервер ещё не проверялся"),
                 "mismatch": ("error", "НЕ СОВПАДАЕТ с сохранённым ранее! Уточните у хостинга, прежде чем доверять.")}[st]
    body = f"""<div class="card"><h1>Отпечаток сервера {e(s.ssh.host)}</h1>
<p>Тип ключа: <b>{e(key.get_name())}</b></p><p style="font-size:18px"><code>{e(fp)}</code></p>
<p>{_badge(state_txt[0], state_txt[1])}</p>
<p>Отпечаток защищает от подмены сервера. Сверьте его с данными хостинга (панель, поддержка, или на сервере
командой <code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code>). Если совпадает — нажмите «Доверять».</p>
{'' if st == 'ok' else _form(f'/trust/{sid}', f'<input type="hidden" name="fp" value="{e(fp)}"><button>Доверять этому серверу</button>',
                             confirm='Вы сверили отпечаток с данными хостинга?')}
<p><a href="/source/{e(sid)}">← назад</a></p></div>"""
    return _page("Отпечаток сервера", body)


# --------------------------------------------------------------------------- действия


def _start_run(sources: list[str] | None, dry: bool) -> tuple[str, str]:
    if runner.PROGRESS.as_dict()["running"]:
        return "Копирование уже идёт — дождитесь окончания.", "warn"
    cfg = _cfg()

    def work():
        try:
            runner.run(cfg, sources, dry_run=dry)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка запуска из интерфейса")

    threading.Thread(target=work, daemon=True).start()
    return ("Пробный запуск начат." if dry else "Копирование начато.") + " Прогресс — вверху страницы.", "ok"


def _open_path(p: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(p))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


def _snapshot_path(cfg: Config, sid: str, name: str) -> Path:
    if parse_stamp(name) is None:
        raise ValueError("неверное имя снимка")
    return runner.source_dir(cfg, cfg.source(sid)) / name


class Handler(BaseHTTPRequestHandler):
    server_version = "odaa-backup"

    def log_message(self, fmt, *args):  # не засоряем консоль
        log.debug("web: " + fmt, *args)

    # -- защита: только localhost и только наш токен для POST
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{ST.port}", f"localhost:{ST.port}")

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str, msg: str = "", level: str = "ok") -> None:
        if msg:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode({"msg": msg, "lvl": level})
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if not self._host_ok():
            return self._send(b"forbidden", "text/plain", 403)
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        msg, lvl = q.get("msg", ""), q.get("lvl", "ok")
        if lvl not in ("ok", "warn", "error"):
            lvl = "ok"
        parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
        try:
            if not parts:
                return self._send(page_dashboard(msg, lvl))
            if parts[0] == "api" and parts[1:] == ["status"]:
                return self._send(json.dumps(runner.PROGRESS.as_dict(), ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            if parts[0] == "source" and len(parts) == 2:
                return self._send(page_source(parts[1], msg, lvl))
            if parts[0] == "hostkey" and len(parts) == 2:
                return self._send(page_hostkey(parts[1]))
            if parts == ["secrets"]:
                return self._send(page_secrets(msg, lvl))
            if parts == ["schedule"]:
                return self._send(page_schedule(msg, lvl))
            if parts == ["report"]:
                cfg = _cfg()
                p = cfg.data_dir / "reports" / "last_report.txt"
                text = p.read_text(encoding="utf-8") if p.exists() else "Отчётов пока нет."
                return self._send(page_text("Последний отчёт", text))
            if parts == ["log"]:
                cfg = _cfg()
                p = cfg.data_dir / "logs" / "odaa-backup.log"
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-400:] if p.exists() else []
                return self._send(page_text("Журнал (последние 400 строк)", "\n".join(reversed(lines))))
            return self._send(_page("Не найдено", "<div class='card'>Страница не найдена</div>"), code=404)
        except ConfigError as ex:
            return self._send(_page("Ошибка настроек", f"<div class='card'><pre>{e(str(ex))}</pre></div>"))
        except Exception as ex:  # noqa: BLE001
            log.exception("Ошибка веб-интерфейса")
            return self._send(_page("Ошибка", f"<div class='card'><pre>{e(str(ex))}</pre></div>"), code=500)

    def do_POST(self):  # noqa: N802
        if not self._host_ok():
            return self._send(b"forbidden", "text/plain", 403)
        length = min(int(self.headers.get("Content-Length") or 0), 100_000)
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode("utf-8"), keep_blank_values=True))
        if not secrets.compare_digest(form.get("token", ""), ST.token):
            return self._send(b"bad token", "text/plain", 403)
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlsplit(self.path).path.split("/") if p]
        try:
            return self._post(parts, form)
        except Exception as ex:  # noqa: BLE001
            log.exception("Ошибка действия в веб-интерфейсе")
            return self._redirect("/", f"Ошибка: {ex}", "error")

    def _post(self, parts: list[str], form: dict):
        cfg = _cfg()
        act = parts[0] if parts else ""
        if act == "run":
            src = form.get("source", "__all__")
            ids = None if src == "__all__" else [cfg.source(src).id]
            msg, lvl = _start_run(ids, form.get("dry") == "1")
            return self._redirect("/" if ids is None else f"/source/{ids[0]}", msg, lvl)
        if act == "settings" and len(parts) == 2:
            sid = cfg.source(parts[1]).id
            sched = (form.get("custom") or "").strip() or form.get("schedule", "")
            try:
                schedule.parse_schedule(sched)
            except schedule.ScheduleError as ex:
                return self._redirect(f"/source/{sid}", str(ex), "error")
            try:
                ret = {k: max(0, int(form.get(k, "0"))) for k in ("latest", "daily", "weekly", "monthly")}
            except ValueError:
                return self._redirect(f"/source/{sid}", "Количество копий — целые числа", "error")
            if sum(ret.values()) == 0:
                return self._redirect(f"/source/{sid}", "Нужно хранить хотя бы одну копию", "error")
            ov = load_overrides(cfg)
            ov[sid] = {"enabled": form.get("enabled") == "1", "schedule": sched, "retention": ret}
            save_overrides(cfg, ov)
            log.info("Настройки %s изменены в интерфейсе: %s", sid, ov[sid])
            return self._redirect(f"/source/{sid}", "Настройки сохранены")
        if act == "secret":
            name, value = form.get("name", ""), form.get("value", "")
            if name not in cfg.all_secret_names() or not value:
                return self._redirect("/secrets", "Неизвестное имя или пустое значение", "error")
            secretstore.save(name, value)
            return self._redirect("/secrets", f"Сохранено: {name}")
        if act == "trust" and len(parts) == 2:
            s = cfg.source(parts[1])
            key = fetch_host_key(s.ssh.host, s.ssh.port, cfg.network.timeout)
            if fingerprint(key) != form.get("fp"):
                return self._redirect(f"/hostkey/{s.id}", "Отпечаток изменился во время проверки — повторите", "error")
            KnownHosts(cfg.data_dir / "known_hosts").add(s.ssh.host, s.ssh.port, key)
            log.info("Пользователь подтвердил отпечаток %s для %s", fingerprint(key), s.ssh.host)
            return self._redirect(f"/source/{s.id}", "Сервер добавлен в доверенные")
        if act == "test" and len(parts) == 2:
            s = cfg.source(parts[1])
            try:
                text = runner.test_connection(cfg, s)
                return self._redirect(f"/source/{s.id}", text, "ok")
            except Exception as ex:  # noqa: BLE001
                return self._redirect(f"/source/{s.id}", f"Проверка не прошла: {ex}", "error")
        if act == "verify" and len(parts) == 3:
            p = _snapshot_path(cfg, parts[1], parts[2])
            r = verify_snapshot(p)
            return self._redirect(f"/source/{parts[1]}", f"Снимок {parts[2]}: {r.summary()}", "ok" if r.ok else "error")
        if act == "open" and len(parts) == 3:
            _open_path(_snapshot_path(cfg, parts[1], parts[2]))
            return self._redirect(f"/source/{parts[1]}")
        if act == "openroot":
            cfg.backup_root.mkdir(parents=True, exist_ok=True)
            _open_path(cfg.backup_root)
            return self._redirect("/")
        if act == "task":
            if parts[1:] == ["install"]:
                ok, out = schedule.install_task(str(Path(ST.config_path).resolve()))
            else:
                ok, out = schedule.remove_task()
            return self._redirect("/schedule", out or ("Готово" if ok else "Ошибка"), "ok" if ok else "error")
        return self._redirect("/", "Неизвестное действие", "error")


def serve(config_path: Path, open_browser: bool = True) -> None:
    ST.config_path = Path(config_path)
    ST.token = secrets.token_urlsafe(24)
    try:
        ST.port = load_config(ST.config_path).ui_port
    except ConfigError:
        ST.port = 8765
    httpd = ThreadingHTTPServer(("127.0.0.1", ST.port), Handler)
    url = f"http://127.0.0.1:{ST.port}/"
    print(f"\n  Интерфейс открыт: {url}\n  Не закрывайте это окно, пока работаете с программой. Выход — Ctrl+C.\n")
    log.info("Веб-интерфейс запущен на %s (%s)", url, datetime.now())
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
