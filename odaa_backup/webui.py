"""Окно программы: локальная страница http://127.0.0.1:<порт> (доступна только с этого компьютера).

Всё настраивается здесь: добавление хостинга (логин + пароль → сайты и базы находятся сами),
папка для копий, вторая копия, Telegram, расписание и вкл/выкл каждого сайта.
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
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import APP_NAME, __version__, discover, runner, schedule, secretstore
from .archive import find_7z, second_copy_available
from .config import (
    Config,
    ConfigError,
    load_app_store,
    load_config,
    load_overrides,
    save_app_store,
    save_overrides,
)
from .manifest import verify_snapshot
from .paths import human_size, list_drives
from .report import human_age, send_telegram, source_health
from .retention import list_snapshots, parse_stamp
from .ssh import HostKeyError, KnownHosts, fetch_host_key, fingerprint
from .state import load_state, parse_ts

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"


def e(x) -> str:
    return html.escape(str(x), quote=True)


# --------------------------------------------------------------------------- оформление

CSS = """
@font-face{font-family:"Inter Tight";font-style:normal;font-weight:100 900;font-display:swap;
src:url(/static/inter-tight-cyrillic-wght-normal.woff2) format("woff2");
unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}
@font-face{font-family:"Inter Tight";font-style:normal;font-weight:100 900;font-display:swap;
src:url(/static/inter-tight-latin-wght-normal.woff2) format("woff2");
unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
:root{--bg:#f5f6f8;--surface:#fff;--surface2:#f0f2f5;--text:#14171c;--muted:#626b77;--faint:#8b939e;
--line:#e4e7ec;--accent:#3b5bfd;--accent-h:#2f4be0;--accent-soft:#eaeeff;--on-accent:#fff;
--ok:#0f8a4b;--ok-soft:#e5f6ec;--warn:#9a6300;--warn-soft:#fdf3dc;--err:#c4291c;--err-soft:#fde9e7;
--shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.06);--r:14px}
@media (prefers-color-scheme:dark){:root{--bg:#0e1013;--surface:#171a1f;--surface2:#1f232a;--text:#eceef1;
--muted:#9aa2ad;--faint:#6e7782;--line:#2a2f37;--accent:#6c84ff;--accent-h:#859aff;--accent-soft:#1f2747;
--on-accent:#0b0d12;--ok:#4cd38a;--ok-soft:#12291d;--warn:#f2bd4b;--warn-soft:#2e2510;--err:#ff7b6e;
--err-soft:#341613;--shadow:none}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 "Inter Tight",system-ui,-apple-system,"Segoe UI",sans-serif;
-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}a:hover{color:var(--accent-h)}
.top{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}
.top-in{max-width:1080px;margin:0 auto;padding:12px 20px;display:flex;align-items:center;gap:22px;flex-wrap:wrap}
.brand{font-weight:700;font-size:17px;letter-spacing:-.01em;color:var(--text);display:flex;gap:9px;align-items:center}
.brand:hover{color:var(--text)}
.brand i{width:28px;height:28px;border-radius:8px;background:var(--accent);display:inline-grid;place-items:center;
color:var(--on-accent);font-style:normal;font-size:15px}
.nav{display:flex;gap:4px;flex-wrap:wrap}.nav a{color:var(--muted);padding:6px 12px;border-radius:9px;font-weight:550}
.nav a:hover{background:var(--surface2);color:var(--text)}.nav a.on{background:var(--surface);color:var(--text);box-shadow:var(--shadow);
border:1px solid var(--line)}
.top .spacer{flex:1}
main{max-width:1080px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:28px;line-height:1.2;letter-spacing:-.02em;margin:0 0 6px;font-weight:700}
h2{font-size:18px;letter-spacing:-.01em;margin:0 0 6px;font-weight:650;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.sub{color:var(--muted);margin:0 0 22px;font-size:16px}.muted{color:var(--muted)}.small{font-size:13px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:22px;box-shadow:var(--shadow);margin-bottom:16px}
.card.flat{box-shadow:none;background:transparent}
.head{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:20px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
.btn,button{appearance:none;border:1px solid transparent;background:var(--accent);color:var(--on-accent);font:inherit;
font-weight:600;padding:9px 16px;border-radius:10px;cursor:pointer;display:inline-flex;align-items:center;gap:8px;
line-height:1.25;white-space:nowrap;text-decoration:none;transition:background .12s}
.btn:hover,button:hover{background:var(--accent-h);color:var(--on-accent)}
.btn.ghost,button.ghost{background:var(--surface);color:var(--text);border-color:var(--line)}
.btn.ghost:hover,button.ghost:hover{background:var(--surface2);color:var(--text)}
button.danger{background:transparent;color:var(--err);border-color:var(--line)}button.danger:hover{background:var(--err-soft);color:var(--err)}
.btn.lg,button.lg{padding:13px 22px;font-size:16px;border-radius:12px}
button:disabled{opacity:.7;cursor:wait}
form.inline{display:inline}
.pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;font-size:13px;font-weight:600;white-space:nowrap}
.pill::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.ok{color:var(--ok);background:var(--ok-soft)}.warn{color:var(--warn);background:var(--warn-soft)}
.error{color:var(--err);background:var(--err-soft)}.none{color:var(--muted);background:var(--surface2)}
.banner{border-radius:12px;padding:14px 16px;margin-bottom:16px;display:flex;gap:12px;align-items:center;
justify-content:space-between;flex-wrap:wrap;white-space:pre-wrap;border:1px solid transparent}
.banner.ok{background:var(--ok-soft);color:var(--text)}.banner.warn{background:var(--warn-soft);color:var(--text)}
.banner.error{background:var(--err-soft);color:var(--text)}
.site{display:flex;flex-direction:column;gap:14px;margin:0}
.site-top{display:flex;gap:12px;align-items:center}
.ava{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;font-weight:700;font-size:19px;flex:none;color:#fff}
.site-title{font-weight:650;font-size:17px;color:var(--text);display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.site-title:hover{color:var(--accent)}
.site-meta{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px;padding:14px 0;border-top:1px solid var(--line);
border-bottom:1px solid var(--line)}
.k{color:var(--faint);font-size:12px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}.v{font-weight:550}
.site-actions{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.switch{position:relative;display:inline-flex;align-items:center;gap:10px;cursor:pointer;font-size:14px;color:var(--text);font-weight:500}
.switch input{appearance:none;width:40px;height:23px;border-radius:99px;background:var(--line);position:relative;
cursor:pointer;transition:.15s;margin:0;flex:none}
.switch input::after{content:"";position:absolute;top:3px;left:3px;width:17px;height:17px;border-radius:50%;
background:#fff;transition:.15s;box-shadow:0 1px 2px rgba(0,0,0,.25)}
.switch input:checked{background:var(--ok)}.switch input:checked::after{left:20px}
label.f{display:block;font-weight:600;margin:18px 0 7px}
.hint{color:var(--muted);font-size:13px;margin-top:6px}
input[type=text],input[type=password],input[type=number],select{width:100%;font:inherit;padding:11px 13px;border-radius:10px;
border:1px solid var(--line);background:var(--surface);color:var(--text);outline:none}
input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
input[type=number]{width:84px;padding:8px 10px}
.choices{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
.choice{border:1.5px solid var(--line);border-radius:12px;padding:15px 16px;cursor:pointer;display:block;background:var(--surface);color:var(--text)}
.choice:hover{border-color:var(--faint);color:var(--text)}.choice input{display:none}
.choice:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}
.choice b{display:block;font-size:16px;margin-bottom:2px}.choice span{color:var(--muted);font-size:13px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:9px}
.chip{border:1px solid var(--line);background:var(--surface2);color:var(--text);border-radius:99px;padding:5px 12px;
font-size:13px;font-weight:500;cursor:pointer}
.chip:hover{border-color:var(--accent);background:var(--accent-soft);color:var(--text)}
.found{display:flex;gap:14px;align-items:flex-start;padding:16px 0;border-top:1px solid var(--line);cursor:pointer}
.found:first-child{border-top:0;padding-top:0}.found input[type=checkbox]{width:19px;height:19px;margin-top:3px;accent-color:var(--accent);flex:none}
.found>div>b{font-size:16px}
.tag{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:6px;background:var(--surface2);
color:var(--muted);margin-left:8px;vertical-align:2px}
details summary{cursor:pointer;color:var(--accent);font-weight:600;margin-top:18px;list-style:none}
details summary::before{content:"▸ "}details[open] summary::before{content:"▾ "}
details.card summary{margin-top:0}
table{width:100%;border-collapse:collapse}td,th{padding:12px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{color:var(--faint);font-size:12px;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
tr:last-child td{border-bottom:0}.scroll{overflow-x:auto}
pre{white-space:pre-wrap;word-break:break-word;background:var(--surface2);padding:14px;border-radius:10px;
font:13px/1.55 ui-monospace,Consolas,monospace;max-height:560px;overflow:auto;margin:0}
code{font:13px ui-monospace,Consolas,monospace;background:var(--surface2);padding:2px 6px;border-radius:6px;word-break:break-all}
.progress{display:none;align-items:center}.progress.on{display:flex}
.bar{height:6px;border-radius:99px;background:var(--surface2);overflow:hidden;flex:1;min-width:120px}
.bar i{display:block;height:100%;width:40%;background:var(--accent);border-radius:99px;animation:slide 1.2s infinite ease-in-out}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
.empty{text-align:center;padding:44px 24px}.empty h1{margin-bottom:10px}
.steps{display:flex;gap:6px;margin-bottom:14px;color:var(--faint);font-size:13px;font-weight:600;flex-wrap:wrap}
.steps b{color:var(--accent)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}.two>.card{margin:0}
.stack>*+*{margin-top:16px}
.hide{display:none!important}
.spinner{width:16px;height:16px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;
animation:spin .7s linear infinite;display:none}button.busy .spinner{display:inline-block}
@keyframes spin{to{transform:rotate(360deg)}}
.numbers{display:flex;gap:10px 18px;flex-wrap:wrap;align-items:center}.numbers label{display:flex;gap:8px;align-items:center;font-size:14px}
@media (max-width:700px){.two{grid-template-columns:1fr}h1{font-size:23px}
main{padding:20px 16px 50px}.top-in{padding:10px 16px;gap:12px}.top .btn{padding:8px 12px}}
"""

JS = """
async function poll(){try{const r=await fetch('/api/status');const s=await r.json();
const box=document.getElementById('progress');if(!box)return;
if(s.running){box.classList.add('on');document.getElementById('p-text').textContent=
(s.dry_run?'Пробный запуск · ':'')+s.source+' — '+s.phase+(s.file?(': '+s.file):'');
document.getElementById('p-bytes').textContent=s.bytes_h+(s.speed_h?(' · '+s.speed_h):'');window._wasRunning=true}
else{box.classList.remove('on');if(window._wasRunning){window._wasRunning=false;location.reload()}}}catch(e){}}
setInterval(poll,1500);poll();
document.addEventListener('change',ev=>{const t=ev.target;if(t.matches('[data-autosubmit]'))t.form.submit();
if(t.name==='provider')showProvider(t.value)});
document.addEventListener('click',ev=>{const c=ev.target.closest('[data-fill]');if(c){const i=document.getElementById(c.dataset.target);
i.value=c.dataset.fill;i.focus()}});
document.addEventListener('submit',ev=>{const b=ev.target.querySelector('button[data-busy]');if(b){b.classList.add('busy');
b.disabled=true;const l=b.querySelector('.lbl');if(l)l.textContent=b.dataset.busy}});
function showProvider(p){document.querySelectorAll('[data-for]').forEach(el=>{
el.classList.toggle('hide',!el.dataset.for.split(' ').includes(p))});}
const sel=document.querySelector('input[name=provider]:checked');if(sel)showProvider(sel.value);
"""

AVATAR_COLORS = ["#3b5bfd", "#0f8a4b", "#c2410c", "#7c3aed", "#0e7490", "#be185d", "#4d7c0f", "#b45309"]
HEALTH_LABEL = {"ok": "В порядке", "warn": "Внимание", "error": "Проблема", "none": "Ещё нет копий"}


class _State:
    config_path: Path
    token: str = ""
    port: int = 8765
    pending: dict = {}


ST = _State()


def _cfg() -> Config:
    return load_config(ST.config_path)


def _layout(title: str, body: str, active: str = "", msg: str = "", level: str = "ok") -> bytes:
    flash = f'<div class="banner {e(level)}">{e(msg)}</div>' if msg else ""
    nav = "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
        for key, href, label in (("home", "/", "Сайты"), ("settings", "/settings", "Настройки"),
                                 ("log", "/log", "Журнал"))
    )
    doc = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)} · {APP_NAME}</title>
<link rel="preload" href="/static/inter-tight-cyrillic-wght-normal.woff2" as="font" type="font/woff2" crossorigin>
<style>{CSS}</style></head><body>
<div class="top"><div class="top-in"><a class="brand" href="/"><i>↓</i>{APP_NAME}</a><nav class="nav">{nav}</nav>
<span class="spacer"></span><a class="btn" href="/add">＋ Добавить хостинг</a></div></div>
<main>{flash}<div id="progress" class="card progress" style="gap:16px"><div style="flex:1;min-width:220px">
<b>Идёт копирование</b><div id="p-text" class="muted small"></div></div><div class="bar"><i></i></div>
<span id="p-bytes" class="muted small"></span></div>{body}</main><script>{JS}</script></body></html>"""
    return doc.encode("utf-8")


def _form(action: str, inner: str, cls: str = "inline", confirm: str = "") -> str:
    onsub = f' onsubmit="return confirm({e(json.dumps(confirm))})"' if confirm else ""
    return (f'<form method="post" action="{e(action)}" class="{cls}"{onsub}>'
            f'<input type="hidden" name="token" value="{ST.token}">{inner}</form>')


def _pill(level: str, text: str) -> str:
    return f'<span class="pill {e(level)}">{e(text)}</span>'


def _avatar(title: str) -> str:
    color = AVATAR_COLORS[sum(map(ord, title)) % len(AVATAR_COLORS)]
    letter = (title.strip()[:1] or "?").upper()
    return f'<span class="ava" style="background:{color}">{e(letter)}</span>'


def _when(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "—"
    if dt <= now:
        return "в течение часа"
    if dt.date() == now.date():
        return f"сегодня в {dt:%H:%M}"
    if (dt.date() - now.date()).days == 1:
        return f"завтра в {dt:%H:%M}"
    return f"{dt:%d.%m} в {dt:%H:%M}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _schedule_select(name: str, current: str) -> str:
    presets = dict(schedule.PRESETS)
    if current not in presets:
        presets = {current: "Своё: " + schedule.describe(current), **presets}
    opts = "".join(f'<option value="{e(v)}"{" selected" if v == current else ""}>{e(lbl)}</option>'
                   for v, lbl in presets.items())
    return f'<select name="{name}">{opts}</select>'


def _hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{e(name)}" value="{e(value)}">'


# --------------------------------------------------------------------------- главная


def page_home(msg: str = "", level: str = "ok") -> bytes:
    try:
        cfg = _cfg()
    except ConfigError as ex:
        return _layout("Ошибка настроек", f'<div class="card"><h2>Ошибка в config.yaml</h2><pre>{e(ex)}</pre>'
                       f'<p class="muted">Файл: {e(ST.config_path)}. Его можно просто удалить — '
                       "всё настраивается в этом окне.</p></div>", "home", msg, level)
    if not cfg.sources:
        return _layout("Начало", _onboarding(cfg), "home", msg, level)

    state = load_state(cfg.data_dir)
    now = datetime.now()
    cards, problems = [], 0
    for s in cfg.sources:
        st = state.get(s.id, {})
        lvl, why = source_health(cfg, s.id, st, now)
        problems += lvl in ("error", "warn")
        last = parse_ts(st.get("last_success"))
        nxt = schedule.next_run(s.schedule, now, last) if s.enabled else None
        what = []
        if s.files:
            what.append("файлы")
        if s.mysql:
            n = len(s.mysql.databases)
            what.append("база данных" if n == 1 else f"{n} {_plural(n, 'база', 'базы', 'баз')}")
        prov = discover.PROVIDERS.get(s.provider, {}).get("title", s.provider)
        toggle = _form(f"/toggle/{s.id}", '<label class="switch"><input type="checkbox" data-autosubmit'
                                          f'{" checked" if s.enabled else ""}>{"Включено" if s.enabled else "Выключено"}</label>')
        problem = (f'<div class="small" style="color:var(--{"err" if lvl == "error" else "warn"})">{e(why)}</div>'
                   if lvl in ("error", "warn") else "")
        size = human_size(st.get("last_size")) if st.get("last_size") else "—"
        cards.append(f"""<div class="card site"><div class="site-top">{_avatar(s.title)}
<div style="min-width:0;flex:1"><a class="site-title" href="/source/{e(s.id)}">{e(s.title)}</a>
<div class="muted small">{e(prov)} · {e(', '.join(what))}</div></div>{_pill(lvl, HEALTH_LABEL[lvl])}</div>
<div class="site-meta"><div><div class="k">Последняя копия</div><div class="v">{e(human_age(last, now))}</div></div>
<div><div class="k">Размер</div><div class="v">{e(size)}</div></div>
<div><div class="k">Как часто</div><div class="v">{e(schedule.describe(s.schedule) if s.enabled else 'не копируется')}</div></div>
<div><div class="k">Следующая</div><div class="v">{e(_when(nxt, now) if s.enabled else '—')}</div></div></div>
{problem}<div class="site-actions">{toggle}<div class="row">
<a class="btn ghost" href="/source/{e(s.id)}">Подробнее</a>
{_form('/run', _hidden('source', s.id) + '<button>Скачать сейчас</button>')}</div></div></div>""")

    ok2, why2 = second_copy_available(cfg)
    banners = []
    if not ok2:
        banners.append('<div class="banner warn"><span>⚠️ Копии лежат только на одном диске. '
                       "Подключите внешний диск или облачную папку — так копии переживут поломку компьютера.</span>"
                       '<a class="btn ghost" href="/settings#second">Настроить вторую копию</a></div>')
    if sys.platform == "win32" and schedule.task_status() == "не создана":
        banners.append('<div class="banner warn"><span>Автоматическое копирование выключено.</span>'
                       + _form("/task/install", '<button class="ghost">Включить</button>') + "</div>")
    total = sum((state.get(s.id, {}).get("last_size") or 0) for s in cfg.sources)
    n = len(cfg.sources)
    no_copies = sum(1 for s in cfg.sources if source_health(cfg, s.id, state.get(s.id, {}), now)[0] == "none")
    if problems:
        title = f"{problems} {_plural(problems, 'сайт требует', 'сайта требуют', 'сайтов требуют')} внимания"
    elif no_copies:
        title = "Делаются первые копии" if runner.PROGRESS.as_dict()["running"] or _QUEUE else "Ещё не у всех есть копии"
    else:
        title = "Все копии в порядке"
    body = f"""<div class="head"><div><h1>{e(title)}</h1>
<p class="sub" style="margin:0">{n} {_plural(n, 'источник', 'источника', 'источников')} · всего {e(human_size(total))}
· папка <code>{e(cfg.backup_root)}</code></p></div>
<div class="row">{_form('/openroot', '<button class="ghost">Открыть папку с копиями</button>')}
{_form('/run', _hidden('source', '__all__') + '<button class="ghost">Скачать всё сейчас</button>')}</div></div>
{''.join(banners)}<div class="grid">{''.join(cards)}</div>"""
    return _layout("Сайты", body, "home", msg, level)


def _onboarding(cfg: Config) -> str:
    provs = "".join(f'<a class="choice" href="/add?p={k}"><b>{e(v["title"])}</b><span>{e(v["hint"])}</span></a>'
                    for k, v in discover.PROVIDERS.items())
    return f"""<div class="card empty"><h1>Добавьте первый хостинг</h1>
<p class="sub">Нужны только логин и пароль. Программа сама найдёт сайты и базы данных<br>
и будет регулярно складывать их копии на этот компьютер.</p>
<div class="choices" style="max-width:920px;margin:0 auto;text-align:left">{provs}</div>
<p class="muted small" style="margin-top:24px">Копии будут лежать в <code>{e(cfg.backup_root)}</code> ·
<a href="/settings">изменить</a></p></div>"""


# --------------------------------------------------------------------------- мастер добавления


def page_add(provider: str = "beget", values: dict | None = None, msg: str = "", level: str = "ok") -> bytes:
    v = values or {}
    provider = provider if provider in discover.PROVIDERS else "beget"
    choices = "".join(
        f'<label class="choice"><input type="radio" name="provider" value="{k}"{" checked" if k == provider else ""}>'
        f'<b>{e(p["title"])}</b><span>{e(p["hint"])}</span></label>' for k, p in discover.PROVIDERS.items())
    keys = discover.default_key_files()
    key_note = (f"Входите по SSH-ключу? Оставьте пароль пустым — программа попробует найденные ключи: "
                f"{', '.join('<code>' + e(k.name) + '</code>' for k in keys[:4])}."
                if keys else "Входите по SSH-ключу? Оставьте пароль пустым и укажите файл ключа в «Дополнительно».")

    def val(k: str, d: str = "") -> str:
        return e(v.get(k, d))

    inner = f"""<div class="card"><h2>Где сайт?</h2><div class="choices" style="margin-top:12px">{choices}</div></div>
<div class="card">
<div data-for="regru timeweb other"><label class="f" for="host" style="margin-top:0">
<span data-for="regru">Сервер</span><span data-for="timeweb">IP-адрес сервера</span><span data-for="other">Адрес сервера</span></label>
<input type="text" id="host" name="host" value="{val('host')}" autocomplete="off" spellcheck="false">
<div class="hint" data-for="regru">Личный кабинет Reg.ru → Хостинг → ваша услуга → вкладка «Доступы». Подойдёт и домен сайта.</div>
<div class="hint" data-for="timeweb">Панель Timeweb Cloud → ваш сервер → IP-адрес.</div></div>
<div class="two"><div><label class="f" for="user">Логин</label>
<input type="text" id="user" name="user" value="{val('user')}" autocomplete="username" spellcheck="false">
<div class="hint" data-for="beget">Тот же, что для входа в панель Beget.</div>
<div class="hint" data-for="regru">Вида u1234567 — во вкладке «Доступы».</div>
<div class="hint" data-for="timeweb">Обычно <code>root</code>.</div></div>
<div><label class="f" for="password">Пароль</label>
<input type="password" id="password" name="password" autocomplete="current-password">
<div class="hint" data-for="beget regru">Пароль от хостинга. Сохранится в Диспетчере учётных данных Windows.
Если добавили на хостинг свой SSH-ключ — пароль можно не вводить.</div>
<div class="hint" data-for="timeweb other">{key_note}</div></div></div>
<details><summary>Дополнительно</summary>
<div class="two"><div><label class="f" for="port">Порт SSH</label>
<input type="number" id="port" name="port" value="{val('port', '22')}" min="1" max="65535"></div>
<div><label class="f" for="key_file">Файл SSH-ключа (если входите по ключу)</label>
<input type="text" id="key_file" name="key_file" value="{val('key_file')}" placeholder="C:\\Users\\вы\\.ssh\\id_ed25519"></div></div>
<div><label class="f" for="key_pass">Пароль ключа (если есть)</label>
<input type="password" id="key_pass" name="key_pass" autocomplete="off"></div>
<div data-for="beget"><label class="f" for="api">API-пароль Beget (необязательно)</label>
<input type="password" id="api" name="api" autocomplete="off">
<div class="hint">Панель Beget → «API». Тогда программа покажет и копии, которые хранит сам Beget.</div></div>
<div data-for="timeweb"><label class="f" for="api_tw">Токен API Timeweb Cloud (необязательно)</label>
<input type="password" id="api_tw" name="api_tw" autocomplete="off">
<div class="hint">Панель → «API и Terraform». Тогда программа покажет копии дисков VPS у Timeweb.</div></div>
</details></div>
<button class="lg" data-busy="Подключаюсь и ищу сайты…"><span class="spinner"></span><span class="lbl">Подключиться и найти сайты</span></button>
<p class="muted small" style="margin-top:12px">Программа только читает данные с сервера — ничего не меняет и не удаляет.</p>"""
    body = f"""<div class="steps"><b>1. Доступ</b><span>→</span><span>2. Что копировать</span><span>→</span><span>3. Готово</span></div>
<h1>Добавить хостинг</h1><p class="sub">Введите данные для входа — сайты и базы данных программа найдёт сама.</p>
{_form('/add/scan', inner, cls="")}"""
    return _layout("Добавить хостинг", body, "", msg, level)


def page_add_results(pid: str, msg: str = "", level: str = "ok") -> bytes:
    pend = ST.pending.get(pid)
    if not pend:
        return page_add(msg="Время ожидания истекло — подключитесь ещё раз.", level="warn")
    res: discover.ScanResult = pend["res"]
    rows = []
    for i in res.sites + res.extras:
        dbs = "".join(
            f'<div class="small muted" style="margin-top:4px">🗄 База данных <b>{e(d.name)}</b> — пароль программа '
            f'возьмёт из <code>{e(d.from_file.rsplit("/", 1)[-1])}</code> сама</div>' for d in i.dbs)
        kind = discover.CMS_TITLES.get(i.kind, "") if i.kind != "config" else ""
        note = f'<div class="small" style="color:var(--warn)">{e(i.note)}</div>' if i.note else ""
        rows.append(f"""<label class="found"><input type="checkbox" name="pick" value="{e(i.key)}"{' checked' if i.checked else ''}>
<div><b>{e(i.title)}</b>{f'<span class="tag">{e(kind)}</span>' if kind else ''}
<div class="small muted">📁 <code>{e(i.remote)}</code></div>{dbs}{note}</div></label>""")
    warn = "".join(f'<div class="banner warn">{e(w)}</div>' for w in res.warnings)
    default_sched = "every 6h" if res.provider == "timeweb" else "daily 03:00"
    auth = "по SSH-ключу" if res.key_file else "по паролю"
    found = "".join(rows) or '<p class="muted" style="margin:0">Автоматически ничего не найдено — укажите папку ниже.</p>'
    inner = f"""{_hidden('pid', pid)}
<div class="card">{found}
<details{' open' if not rows else ''}><summary>Добавить папку вручную</summary>
<label class="f" for="manual">Путь на сервере</label><input type="text" id="manual" name="manual" spellcheck="false"
placeholder="например mysite.ru/public_html или /opt/app"></details></div>
<div class="card"><h2>Как часто делать копию</h2>{_schedule_select("schedule", default_sched)}
<div class="hint">Можно поменять в любой момент — для каждого сайта отдельно.</div></div>
<div class="row"><button class="lg" data-busy="Сохраняю…"><span class="spinner"></span><span class="lbl">Добавить и сделать первую копию</span></button>
<a class="btn ghost lg" href="/add?p={e(res.provider)}">Назад</a></div>
<p class="muted small" style="margin-top:16px">Отпечаток сервера сохранён: <code>{e(res.fingerprint)}</code>.
Если он когда-нибудь изменится, программа остановит копирование и предупредит — это защита от подмены сервера.</p>"""
    body = f"""<div class="steps"><span>1. Доступ</span><span>→</span><b>2. Что копировать</b><span>→</span><span>3. Готово</span></div>
<h1>Нашлось на {e(res.host)}</h1><p class="sub">Подключение {auth} прошло успешно. Отметьте, что копировать.</p>
{warn}{_form('/add/save', inner, cls="")}"""
    return _layout("Что копировать", body, "", msg, level)


# --------------------------------------------------------------------------- страница сайта


def page_source(sid: str, msg: str = "", level: str = "ok") -> bytes:
    cfg = _cfg()
    try:
        s = cfg.source(sid)
    except KeyError:
        return page_home(f"Источник {sid} не найден", "error")
    st = load_state(cfg.data_dir).get(sid, {})
    now = datetime.now()
    lvl, why = source_health(cfg, sid, st, now)
    ret = cfg.retention_for(s)
    snaps = list_snapshots(runner.source_dir(cfg, s))

    srows = []
    for sn in snaps:
        info = sn.info
        sl = {"ok": "ok", "partial": "warn", "failed": "error"}.get(sn.status or "", "none")
        lab = {"ok": "Успешно", "partial": "С ошибками", "failed": "Ошибка"}.get(sn.status or "", "Не завершена")
        dbs = ", ".join(f"{k} · {human_size(v)}" for k, v in (info.get("databases") or {}).items()) or "—"
        size = human_size(info.get("size")) if info.get("size") else "—"
        acts = (_form(f"/verify/{sid}/{sn.path.name}", '<button class="ghost">Проверить</button>')
                + _form(f"/open/{sid}/{sn.path.name}", '<button class="ghost">Открыть</button>'))
        srows.append(f"""<tr><td><b>{e(sn.time.strftime('%d.%m.%Y, %H:%M'))}</b></td><td>{_pill(sl, lab)}</td>
<td>{e(info.get('files', '—'))} <span class="muted small">· изменилось {e(info.get('downloaded', '—'))}</span></td>
<td class="small">{e(dbs)}</td><td>{e(size)}</td>
<td><div class="row" style="justify-content:flex-end;flex-wrap:nowrap">{acts}</div></td></tr>""")
        if info.get("errors"):
            srows.append(f'<tr><td></td><td colspan=5 class="small" style="color:var(--err)">'
                         f'{e("; ".join(info["errors"][:5]))}</td></tr>')

    what = []
    for f in s.files:
        excl = (f' <span class="muted small" title="{e(", ".join(f.exclude))}">· без кэша и служебных файлов</span>'
                if f.exclude else "")
        what.append(f"<div style='margin:6px 0'>📁 <code>{e(f.remote)}</code>{excl}</div>")
    if s.mysql:
        for d in s.mysql.databases:
            src = (f"пароль из <code>{e(d.from_file.rsplit('/', 1)[-1])}</code>" if d.from_file
                   else "сохранённый пароль")
            what.append(f"<div style='margin:6px 0'>🗄 база <b>{e(d.name)}</b> <span class='muted small'>({src})</span></div>")

    server = ""
    if s.ssh:
        known = KnownHosts(cfg.data_dir / "known_hosts").known_fingerprint(s.ssh.host, s.ssh.port)
        server = (f'<p class="small muted" style="margin-top:14px">Сервер {e(s.ssh.user)}@{e(s.ssh.host)} · '
                  f'{"отпечаток сохранён" if known else "отпечаток не проверен"} · '
                  f'<a href="/hostkey/{e(sid)}">подробнее</a></p>')

    api_html = ""
    if s.api:
        items = st.get("api_backups") or []
        err = st.get("api_error")
        lis = "".join(f"<tr><td>{e(i.get('date') or '—')}</td><td>{e(i.get('type'))}</td></tr>" for i in items[:20])
        api_html = ('<div class="card"><h2>Копии на стороне хостинга</h2>'
                    + (f'<div class="banner error">{e(err)}</div>' if err else "")
                    + (f"<table>{lis}</table>" if items else '<p class="muted">Появятся после ближайшего копирования.</p>')
                    + '<p class="muted small">Их хранит сам хостинг — при атаке на хостинг они тоже могут '
                      "пропасть, поэтому программа делает свои.</p></div>")

    extra_cards = ""
    if s.managed and s.ssh and s.ssh.password_secret:
        extra_cards += ('<div class="card"><h2>Пароль от хостинга</h2><p class="muted small">Поменяли пароль на '
                        "хостинге? Введите новый.</p>" + _form(
                            f"/password/{sid}",
                            '<div class="row"><input type="password" name="value" placeholder="новый пароль" '
                            'style="max-width:320px" required><button>Сохранить</button></div>') + "</div>")
    if s.managed:
        extra_cards += ('<div class="card flat"><h2>Удалить из программы</h2><p class="muted small">Копирование '
                        "прекратится. Уже сделанные копии останутся на диске.</p>"
                        + _form(f"/delete/{sid}", '<button class="danger">Удалить источник</button>',
                                confirm="Удалить источник? Уже сделанные копии останутся на диске.") + "</div>")

    last = parse_ts(st.get("last_success"))
    problem = ""
    if lvl in ("warn", "error"):
        detail = f'<br><span class="small">{e(st.get("last_error"))}</span>' if st.get("last_error") else ""
        problem = f'<div class="banner {lvl}"><span>{e(why)}{detail}</span></div>'
    settings_form = f"""<label class="switch" style="margin-top:6px"><input type="checkbox" name="enabled" value="1"{' checked' if s.enabled else ''}>
Копировать автоматически</label>
<label class="f">Как часто</label>{_schedule_select("schedule", s.schedule)}
<label class="f">Сколько копий хранить</label>
<div class="numbers"><label>последних <input type="number" name="latest" min="0" max="100" value="{ret.latest}"></label>
<label>по дням <input type="number" name="daily" min="0" max="365" value="{ret.daily}"></label>
<label>по неделям <input type="number" name="weekly" min="0" max="104" value="{ret.weekly}"></label>
<label>по месяцам <input type="number" name="monthly" min="0" max="120" value="{ret.monthly}"></label></div>
<div class="hint">Неизменённые файлы в копиях не дублируются, поэтому много копий почти не занимает места.</div>
<p style="margin-bottom:0"><button>Сохранить</button></p>"""
    test_btn = _form(f"/test/{sid}", '<button class="ghost" data-busy="Проверяю…"><span class="spinner"></span>'
                                     '<span class="lbl">Проверить подключение</span></button>')
    snaps_html = "".join(srows) or '<tr><td colspan=6 class="muted">Копий пока нет — нажмите «Скачать сейчас».</td></tr>'
    body = f"""<p class="small" style="margin-top:0"><a href="/">← Все сайты</a></p>
<div class="head"><div class="row" style="gap:14px">{_avatar(s.title)}<div><h1 style="margin:0">{e(s.title)}</h1>
<div class="row" style="gap:8px;margin-top:4px">{_pill(lvl, HEALTH_LABEL[lvl])}<span class="muted small">последняя копия: {e(human_age(last, now))}</span></div></div></div>
<div class="row">{_form('/run', _hidden('source', sid) + _hidden('dry', '1') + '<button class="ghost">Пробный запуск</button>')}
{_form('/run', _hidden('source', sid) + '<button>Скачать сейчас</button>')}</div></div>
{problem}
<div class="two" style="margin-bottom:16px"><div class="card"><h2>Настройки</h2>{_form(f'/settings/{sid}', settings_form, cls="")}</div>
<div class="card"><h2>Что копируется</h2>{''.join(what) or '<p class="muted">Только сведения из API.</p>'}{server}
<div class="row" style="margin-top:12px">{test_btn}</div></div></div>
<div class="card"><h2>Копии <span class="muted" style="font-weight:500">{len(snaps)}</span></h2><div class="scroll"><table>
<tr><th>Дата</th><th>Статус</th><th>Файлы</th><th>Базы</th><th>Размер</th><th></th></tr>{snaps_html}</table></div></div>
{api_html}{extra_cards}"""
    return _layout(s.title, body, "", msg, level)


# --------------------------------------------------------------------------- настройки


def page_settings(msg: str = "", level: str = "ok") -> bytes:
    cfg = _cfg()
    try:
        drives = list_drives()
    except Exception:  # noqa: BLE001
        drives = []

    def chips(target: str, only_other: bool = False) -> str:
        out = []
        for d in drives:
            if only_other and d["system"]:
                continue
            path = os.path.join(d["root"], "OdaaBackups")
            out.append(f'<button type="button" class="chip" data-target="{target}" data-fill="{e(path)}">'
                       f'{e(d["root"])} · {e(d["kind"])} · свободно {e(human_size(d["free"]))}</button>')
        return f'<div class="chips">{"".join(out)}</div>' if out else ""

    sc, tg = cfg.second_copy, cfg.telegram
    seven = find_7z(sc.seven_zip)
    ok2, why2 = second_copy_available(cfg)
    task = schedule.task_status()
    arch_set = secretstore.is_set(sc.password_secret)
    tg_set = secretstore.is_set(tg.bot_token_secret)
    no7z = '' if seven else ' — <a href="https://7-zip.org" target="_blank" rel="noopener">установите 7-Zip</a>'
    form = f"""<div class="card"><h2>Где хранить копии</h2>
<label class="f" for="root">Папка</label><input type="text" id="root" name="backup_root" value="{e(cfg.backup_root)}" spellcheck="false">
{chips("root")}<div class="hint">Лучше не системный диск C:. Если сменить папку, старые копии останутся на прежнем месте.</div></div>

<div class="card" id="second"><h2>Вторая копия {_pill("ok" if ok2 else "warn", "включена" if ok2 else "не настроена")}</h2>
<p class="muted small" style="margin:0">После каждой успешной копии программа кладёт архив на второй диск: внешний диск, флешку
или папку Яндекс.Диска / OneDrive. Это защита от поломки диска, кражи или вируса-шифровальщика (правило 3-2-1).</p>
<label class="f" for="second_path">Папка второй копии</label>
<input type="text" id="second_path" name="second_path" value="{e(sc.path)}" placeholder="например E:\\OdaaBackups" spellcheck="false">
{chips("second_path", only_other=True)}
{f'<div class="hint" style="color:var(--warn)">{e(why2)}</div>' if sc.path and not ok2 else ''}
<label class="switch" style="margin-top:18px"><input type="checkbox" name="second_encrypt" value="1"{' checked' if sc.encrypt else ''}>
Шифровать паролем (AES-256){no7z}</label>
<label class="f" for="archive_password">Пароль для архивов {_pill('ok', 'задан') if arch_set else ''}</label>
<input type="password" id="archive_password" name="archive_password" autocomplete="new-password"
placeholder="{'оставьте пустым, чтобы не менять' if arch_set else 'придумайте и запишите на бумаге'}">
<div class="hint">Без этого пароля архив не открыть. Запишите его и храните отдельно от компьютера.</div>
<label class="f" for="keep">Сколько архивов хранить на втором диске</label>
<input type="number" id="keep" name="second_keep" min="1" max="100" value="{sc.keep}"></div>

<div class="card"><h2>Уведомления в Telegram</h2>
<label class="switch" style="margin-top:6px"><input type="checkbox" name="tg_enabled" value="1"{' checked' if tg.enabled else ''}>Присылать отчёт о копировании</label>
<div class="two"><div><label class="f" for="tg_token">Токен бота {_pill('ok', 'задан') if tg_set else ''}</label>
<input type="password" id="tg_token" name="tg_token" autocomplete="off" placeholder="{'оставьте пустым, чтобы не менять' if tg_set else 'от @BotFather'}">
<div class="hint">Telegram → @BotFather → /newbot. Затем напишите своему боту любое сообщение.</div></div>
<div><label class="f" for="tg_chat">Ваш chat_id</label><input type="text" id="tg_chat" name="tg_chat" value="{e(tg.chat_id)}" placeholder="например 123456789">
<div class="hint">Узнать: напишите боту @userinfobot.</div></div></div>
<label class="f" for="tg_notify">Когда присылать</label><select id="tg_notify" name="tg_notify">
<option value="always"{' selected' if tg.notify == 'always' else ''}>После каждого копирования</option>
<option value="errors"{' selected' if tg.notify == 'errors' else ''}>Только если что-то пошло не так</option></select></div>
<button class="lg">Сохранить настройки</button>"""
    tg_test = _form("/tgtest", '<button class="ghost">Отправить тест в Telegram</button>') if tg.enabled else ""
    body = f"""<h1>Настройки</h1><p class="sub">Всё, что нужно для работы, — на этой странице.</p>
{_form('/settings', form, cls="")}
<div class="card" style="margin-top:22px"><h2>Автоматическое копирование {_pill('ok' if task.startswith('создана') else 'warn', 'включено' if task.startswith('создана') else 'выключено')}</h2>
<p class="muted small">Windows раз в час запускает программу, и она сама решает, каким сайтам пора делать копию.
Если компьютер был выключен, копия сделается после включения. Окно программы держать открытым не нужно.</p>
<div class="row">{_form('/task/install', '<button class="ghost">Включить / обновить</button>')}
{_form('/task/remove', '<button class="danger">Выключить</button>', confirm='Выключить автоматическое копирование?')}
{tg_test}</div></div>
<details class="card flat"><summary>Для опытных пользователей</summary>
<p class="small muted">Файл настроек (необязательный): <code>{e(ST.config_path)}</code><br>
Данные программы: <code>{e(cfg.data_dir)}</code></p>
<p class="small"><a href="/secrets">Сохранённые пароли и токены</a></p></details>"""
    return _layout("Настройки", body, "settings", msg, level)


def page_secrets(msg: str = "", level: str = "ok") -> bytes:
    cfg = _cfg()
    rows = []
    for name in cfg.all_secret_names():
        is_set = secretstore.is_set(name)
        frm = _form("/secret", '<div class="row" style="flex-wrap:nowrap">' + _hidden("name", name)
                    + '<input type="password" name="value" autocomplete="new-password" placeholder="новое значение" '
                    'required style="max-width:240px"><button class="ghost">Сохранить</button></div>')
        rows.append(f"<tr><td><b>{e(name)}</b></td><td>{_pill('ok' if is_set else 'error', 'задан' if is_set else 'не задан')}"
                    f"</td><td>{frm}</td></tr>")
    body = f"""<p class="small" style="margin-top:0"><a href="/settings">← Настройки</a></p><h1>Пароли и токены</h1>
<p class="sub">Хранятся в Диспетчере учётных данных Windows (служба «odaa-backup»). Посмотреть сохранённое значение нельзя.</p>
<div class="card"><div class="scroll"><table><tr><th>Имя</th><th>Состояние</th><th>Заменить</th></tr>
{''.join(rows) or '<tr><td colspan=3 class="muted">Нет сохранённых паролей</td></tr>'}</table></div></div>"""
    return _layout("Пароли", body, "settings", msg, level)


def page_log() -> bytes:
    cfg = _cfg()
    rp = cfg.data_dir / "reports" / "last_report.txt"
    lpath = cfg.data_dir / "logs" / "odaa-backup.log"
    report = rp.read_text(encoding="utf-8") if rp.exists() else "Отчётов пока нет."
    lines = lpath.read_text(encoding="utf-8", errors="replace").splitlines()[-400:] if lpath.exists() else []
    events = "\n".join(reversed(lines)) or "Пусто"
    body = f"""<h1>Журнал</h1><p class="sub">Последний отчёт и подробности работы программы.</p>
<div class="card"><h2>Последний отчёт</h2><pre>{e(report)}</pre></div>
<div class="card"><h2>События <span class="muted small" style="font-weight:500">новые сверху</span></h2><pre>{e(events)}</pre></div>"""
    return _layout("Журнал", body, "log")


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
    st = KnownHosts(cfg.data_dir / "known_hosts").check(s.ssh.host, s.ssh.port, key)
    lvl, txt = {"ok": ("ok", "Совпадает с сохранённым — всё в порядке"),
                "unknown": ("warn", "Сервер ещё не проверялся"),
                "mismatch": ("error", "НЕ СОВПАДАЕТ с сохранённым ранее")}[st]
    extra = ("<p>Так бывает, если хостинг переустановил сервер, но может быть и попыткой подмены. "
             "Спросите поддержку хостинга, менялся ли ключ сервера, и только потом нажмите «Доверять».</p>"
             if st == "mismatch" else "")
    trust = "" if st == "ok" else _form(f"/trust/{sid}", _hidden("fp", fp) + "<button>Доверять этому серверу</button>",
                                        confirm="Доверять серверу с этим отпечатком?")
    body = f"""<p class="small" style="margin-top:0"><a href="/source/{e(sid)}">← {e(s.title)}</a></p><h1>Отпечаток сервера</h1>
<div class="card"><p style="margin-top:0">{_pill(lvl, txt)}</p><p>{e(s.ssh.host)} · {e(key.get_name())}</p>
<p><code style="font-size:15px">{e(fp)}</code></p>{extra}
<p class="muted small">Отпечаток — «паспорт» сервера. Он защищает от подмены сервера злоумышленником.</p>{trust}</div>"""
    return _layout("Отпечаток сервера", body)


# --------------------------------------------------------------------------- действия


_QUEUE: list[tuple[list[str] | None, bool]] = []
_QLOCK = threading.Lock()
_WORKER: list[threading.Thread] = []


def _worker() -> None:
    while True:
        with _QLOCK:
            if not _QUEUE:
                _WORKER.clear()
                return
            sources, dry = _QUEUE.pop(0)
        try:
            cfg = _cfg()
            for _attempt in range(120):  # если копирует Планировщик — подождать до ~10 минут
                results, report = runner.run(cfg, sources, dry_run=dry)
                if results or "уже выполняется" not in (report or ""):
                    break
                time.sleep(5)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка запуска из окна программы")


def _start_run(sources: list[str] | None, dry: bool) -> tuple[str, str]:
    """Поставить копирование в очередь (если что-то уже копируется — начнётся сразу после)."""
    with _QLOCK:
        busy = runner.PROGRESS.as_dict()["running"] or bool(_QUEUE)
        _QUEUE.append((sources, dry))
        if not _WORKER:
            t = threading.Thread(target=_worker, daemon=True)
            _WORKER.append(t)
            t.start()
    what = "Пробный запуск" if dry else "Копирование"
    if busy:
        return f"{what}: в очереди — начнётся сразу после текущего.", "ok"
    return f"{what} началось. Ход работы — вверху страницы.", "ok"


def _open_path(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
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


def _ensure_task(cfg: Config) -> str:
    if sys.platform != "win32" or not cfg.auto_schedule:
        return ""
    if schedule.task_status() != "не создана":
        return ""
    ok, out = schedule.install_task(str(Path(ST.config_path).resolve()))
    return " Автоматическое копирование включено." if ok else f" Не удалось включить расписание: {out}"


def _cleanup_pending() -> None:
    now = time.monotonic()
    for k in [k for k, v in ST.pending.items() if now - v["t"] > 1800]:
        ST.pending.pop(k, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "odaa-backup"

    def log_message(self, fmt, *args):
        log.debug("web: " + fmt, *args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{ST.port}", f"localhost:{ST.port}")

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", code: int = 200, cache: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                         "font-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str, msg: str = "", level: str = "ok"):
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
        lvl = lvl if lvl in ("ok", "warn", "error") else "ok"
        parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
        try:
            if not parts:
                return self._send(page_home(msg, lvl))
            if parts[0] == "static" and len(parts) == 2:
                f = STATIC / parts[1]
                if f.parent == STATIC and f.suffix == ".woff2" and f.exists():
                    return self._send(f.read_bytes(), "font/woff2", cache=True)
                return self._send(b"not found", "text/plain", 404)
            if parts == ["api", "version"]:
                return self._send(json.dumps({"app": "odaa-backup", "version": __version__}).encode(),
                                  "application/json")
            if parts == ["api", "status"]:
                return self._send(json.dumps(runner.PROGRESS.as_dict(), ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            if parts == ["add"]:
                return self._send(page_add(q.get("p", "beget"), msg=msg, level=lvl))
            if parts[0] == "add" and len(parts) == 3 and parts[1] == "found":
                return self._send(page_add_results(parts[2], msg, lvl))
            if parts[0] == "source" and len(parts) == 2:
                return self._send(page_source(parts[1], msg, lvl))
            if parts[0] == "hostkey" and len(parts) == 2:
                return self._send(page_hostkey(parts[1]))
            if parts == ["settings"]:
                return self._send(page_settings(msg, lvl))
            if parts == ["secrets"]:
                return self._send(page_secrets(msg, lvl))
            if parts in (["log"], ["report"]):
                return self._send(page_log())
            return self._send(_layout("Не найдено", "<div class='card'>Страница не найдена</div>"), code=404)
        except ConfigError as ex:
            return self._send(_layout("Ошибка настроек", f"<div class='card'><pre>{e(ex)}</pre></div>"))
        except Exception as ex:  # noqa: BLE001
            log.exception("Ошибка окна программы")
            return self._send(_layout("Ошибка", f"<div class='card'><pre>{e(ex)}</pre></div>"), code=500)

    def do_POST(self):  # noqa: N802
        if not self._host_ok():
            return self._send(b"forbidden", "text/plain", 403)
        length = min(int(self.headers.get("Content-Length") or 0), 200_000)
        raw = self.rfile.read(length).decode("utf-8")
        form = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
        multi = urllib.parse.parse_qs(raw, keep_blank_values=True)
        if not secrets.compare_digest(form.get("token", ""), ST.token):
            return self._send("Страница устарела — обновите её (F5).".encode(), "text/plain; charset=utf-8", 403)
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlsplit(self.path).path.split("/") if p]
        try:
            return self._post(parts, form, multi)
        except Exception as ex:  # noqa: BLE001
            log.exception("Ошибка действия в окне программы")
            return self._redirect("/", f"Ошибка: {ex}", "error")

    def _back(self, default: str = "/") -> str:
        ref = self.headers.get("Referer") or ""
        u = urllib.parse.urlsplit(ref)
        return u.path if u.netloc in (f"127.0.0.1:{ST.port}", f"localhost:{ST.port}") and u.path else default

    # ------------------------------------------------------------------ обработка форм
    def _post(self, parts: list[str], form: dict, multi: dict):
        act = parts[0] if parts else ""
        if act == "add" and parts[1:] == ["scan"]:
            return self._add_scan(form)
        if act == "add" and parts[1:] == ["save"]:
            return self._add_save(form, multi)
        cfg = _cfg()
        if act == "run":
            src = form.get("source", "__all__")
            ids = None if src == "__all__" else [cfg.source(src).id]
            msg, lvl = _start_run(ids, form.get("dry") == "1")
            return self._redirect(self._back(), msg, lvl)
        if act == "toggle" and len(parts) == 2:
            s = cfg.source(parts[1])
            ov = load_overrides(cfg)
            ov.setdefault(s.id, {})["enabled"] = not s.enabled
            save_overrides(cfg, ov)
            return self._redirect(self._back(), f"«{s.title}»: копирование {'выключено' if s.enabled else 'включено'}")
        if act == "settings" and len(parts) == 2:
            return self._source_settings(cfg, parts[1], form)
        if act == "settings":
            return self._global_settings(cfg, form)
        if act == "password" and len(parts) == 2:
            s = cfg.source(parts[1])
            if s.ssh and s.ssh.password_secret and form.get("value"):
                secretstore.save(s.ssh.password_secret, form["value"])
                return self._redirect(f"/source/{s.id}", "Пароль сохранён")
            return self._redirect(f"/source/{s.id}", "Пустой пароль", "error")
        if act == "delete" and len(parts) == 2:
            store = load_app_store(cfg.data_dir)
            before = len(store["sources"])
            store["sources"] = [x for x in store["sources"] if x.get("id") != parts[1]]
            if len(store["sources"]) == before:
                return self._redirect(f"/source/{parts[1]}", "Этот источник задан в config.yaml — удалите его там",
                                      "warn")
            save_app_store(cfg.data_dir, store)
            return self._redirect("/", "Источник удалён. Сделанные копии остались на диске.")
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
                return self._redirect(f"/source/{s.id}", runner.test_connection(cfg, s))
            except Exception as ex:  # noqa: BLE001
                return self._redirect(f"/source/{s.id}", f"Проверка не прошла: {ex}", "error")
        if act == "verify" and len(parts) == 3:
            r = verify_snapshot(_snapshot_path(cfg, parts[1], parts[2]))
            return self._redirect(f"/source/{parts[1]}", f"Проверка копии: {r.summary()}", "ok" if r.ok else "error")
        if act == "open" and len(parts) == 3:
            _open_path(_snapshot_path(cfg, parts[1], parts[2]))
            return self._redirect(f"/source/{parts[1]}")
        if act == "openroot":
            _open_path(cfg.backup_root)
            return self._redirect("/")
        if act == "task":
            if parts[1:] == ["install"]:
                ok, out = schedule.install_task(str(Path(ST.config_path).resolve()))
                text = "Автоматическое копирование включено." if ok else f"Не удалось: {out}"
            else:
                ok, out = schedule.remove_task()
                text = "Автоматическое копирование выключено." if ok else f"Не удалось: {out}"
            return self._redirect(self._back("/settings"), text, "ok" if ok else "error")
        if act == "tgtest":
            send_telegram(cfg, "ODAA Backup: тестовое сообщение — уведомления работают ✅", has_errors=True)
            return self._redirect("/settings", "Тестовое сообщение отправлено (если не пришло — загляните в Журнал)")
        return self._redirect("/", "Неизвестное действие", "error")

    def _add_scan(self, form: dict):
        provider = form.get("provider", "beget")
        values = {k: form.get(k, "") for k in ("host", "user", "port", "key_file")}
        try:
            port = int(form.get("port") or 22)
        except ValueError:
            port = 22
        try:
            cfg = _cfg()
            res = discover.scan(provider, form.get("host", ""), port, form.get("user", ""),
                                form.get("password") or None, cfg.data_dir,
                                key_file=form.get("key_file") or None, key_passphrase=form.get("key_pass") or None,
                                timeout=min(cfg.network.timeout, 30))
        except (HostKeyError, discover.DiscoverError) as ex:
            return self._send(page_add(provider, values, str(ex), "error"))
        res.api_secret = form.get("api") or form.get("api_tw") or None
        _cleanup_pending()
        pid = secrets.token_urlsafe(12)
        ST.pending[pid] = {"res": res, "t": time.monotonic()}
        return self._redirect(f"/add/found/{pid}")

    def _add_save(self, form: dict, multi: dict):
        pid = form.get("pid", "")
        pend = ST.pending.get(pid)
        if not pend:
            return self._redirect("/add", "Время ожидания истекло — подключитесь ещё раз.", "warn")
        res: discover.ScanResult = pend["res"]
        chosen = multi.get("pick", [])
        manual = (form.get("manual") or "").strip() or None
        if not chosen and not manual:
            return self._redirect(f"/add/found/{pid}", "Отметьте хотя бы один сайт или укажите папку", "warn")
        sched = form.get("schedule") or "daily 03:00"
        schedule.parse_schedule(sched)
        cfg = _cfg()
        store = load_app_store(cfg.data_dir)
        existing = {s.id for s in cfg.sources}
        new_sources, new_secrets = discover.build_sources(res, chosen, sched, existing, manual)
        if res.api_secret and new_sources:
            if res.provider == "beget":
                name = f"beget-{discover.slug(res.user)}-api"
                new_sources[0]["api"] = {"type": "beget", "login": res.user, "password_secret": name}
                new_secrets[name] = res.api_secret
            elif res.provider == "timeweb":
                new_sources[0]["api"] = {"type": "timeweb", "token_secret": "timeweb-token"}
                new_secrets["timeweb-token"] = res.api_secret
        for name, value in new_secrets.items():
            secretstore.save(name, value)
        store["sources"].extend(new_sources)
        save_app_store(cfg.data_dir, store)
        try:
            cfg = _cfg()
        except ConfigError as ex:
            store["sources"] = store["sources"][: len(store["sources"]) - len(new_sources)]
            save_app_store(cfg.data_dir, store)
            return self._redirect(f"/add/found/{pid}", str(ex), "error")
        ST.pending.pop(pid, None)
        log.info("Добавлены источники: %s", ", ".join(s["id"] for s in new_sources))
        extra = _ensure_task(cfg)
        msg, lvl = _start_run([s["id"] for s in new_sources], False)
        n = len(new_sources)
        first = "Первая копия уже скачивается." if "началось" in msg else "Первая копия начнётся сразу после текущей."
        return self._redirect("/", f"Добавлено: {n}. {first}{extra}", lvl)

    def _source_settings(self, cfg: Config, sid: str, form: dict):
        s = cfg.source(sid)
        sched = form.get("schedule", "")
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
        ov[s.id] = {"enabled": form.get("enabled") == "1", "schedule": sched, "retention": ret}
        save_overrides(cfg, ov)
        log.info("Настройки %s изменены: %s", s.id, ov[s.id])
        return self._redirect(f"/source/{s.id}", "Сохранено")

    def _global_settings(self, cfg: Config, form: dict):
        store = load_app_store(cfg.data_dir)
        st = store["settings"]
        root = (form.get("backup_root") or "").strip()
        if root:
            p = Path(os.path.expandvars(root))
            if not p.is_absolute():
                return self._redirect("/settings", "Укажите полный путь к папке, например D:\\OdaaBackups", "error")
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as ex:
                return self._redirect("/settings", f"Не удалось создать папку {p}: {ex}", "error")
            st["backup_root"] = str(p)
        second = (form.get("second_path") or "").strip()
        try:
            keep = max(1, int(form.get("second_keep") or 3))
        except ValueError:
            keep = 3
        st["second_copy"] = {"path": second, "encrypt": form.get("second_encrypt") == "1", "keep": keep}
        if form.get("archive_password"):
            secretstore.save(cfg.second_copy.password_secret, form["archive_password"])
        st["telegram"] = {"enabled": form.get("tg_enabled") == "1", "chat_id": (form.get("tg_chat") or "").strip(),
                          "notify": form.get("tg_notify") if form.get("tg_notify") in ("always", "errors") else "always"}
        if form.get("tg_token"):
            secretstore.save(cfg.telegram.bot_token_secret, form["tg_token"].strip())
        warn = ""
        if st["telegram"]["enabled"] and not st["telegram"]["chat_id"]:
            st["telegram"]["enabled"] = False
            warn = "Для Telegram укажите chat_id."
        if second and st["second_copy"]["encrypt"] and not secretstore.is_set(cfg.second_copy.password_secret):
            warn += " Задайте пароль для архивов второй копии."
        save_app_store(cfg.data_dir, store)
        try:
            _cfg()
        except ConfigError as ex:
            return self._redirect("/settings", str(ex), "error")
        return self._redirect("/settings", warn.strip() or "Настройки сохранены", "warn" if warn else "ok")


def _running_version(port: int) -> str | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=2) as r:
            d = json.loads(r.read().decode())
            return d.get("version") if d.get("app") == "odaa-backup" else None
    except Exception:  # noqa: BLE001
        return None


def serve(config_path: Path, open_browser: bool = True) -> None:
    ST.config_path = Path(config_path)
    ST.token = secrets.token_urlsafe(24)
    try:
        ST.port = load_config(ST.config_path).ui_port
    except ConfigError:
        ST.port = 8765
    httpd = None
    base_port = ST.port
    for port in range(base_port, base_port + 10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            ST.port = port
            break
        except OSError:
            if _running_version(port) == __version__:
                # Эта же версия уже открыта — просто покажем её окно.
                url = f"http://127.0.0.1:{port}/"
                print(f"Программа уже запущена: {url}")
                if open_browser:
                    webbrowser.open(url)
                return
            if port == base_port:
                print(f"Порт {port} занят (возможно, открыта старая версия программы — закройте её чёрное окно).")
    if httpd is None:
        print("Не удалось открыть окно программы: все порты заняты.")
        return
    url = f"http://127.0.0.1:{ST.port}/"
    print(f"\n  {APP_NAME} {__version__}\n  Окно программы: {url}\n"
          "  Не закрывайте это окно, пока работаете с программой.\n"
          "  Автоматическое копирование работает и без него.\n")
    log.info("Окно программы запущено на %s", url)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
