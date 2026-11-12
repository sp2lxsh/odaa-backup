"""Итоговый отчёт и уведомления в Telegram."""

from __future__ import annotations

import json
import logging
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from . import secretstore
from .archive import second_copy_available
from .config import Config
from .paths import human_size
from .schedule import describe, nominal_interval
from .state import load_state, parse_ts

log = logging.getLogger(__name__)


def human_age(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "никогда"
    d = (now or datetime.now()) - dt
    if d < timedelta(minutes=1):
        return "только что"
    if d < timedelta(hours=1):
        return f"{int(d.total_seconds() // 60)} мин назад"
    if d < timedelta(days=1):
        return f"{int(d.total_seconds() // 3600)} ч назад"
    return f"{d.days} дн назад"


def source_health(cfg: Config, sid: str, entry: dict, now: datetime | None = None) -> tuple[str, str]:
    """('ok'|'warn'|'error'|'none', пояснение) для источника."""
    now = now or datetime.now()
    src = cfg.source(sid)
    last_ok = parse_ts(entry.get("last_success"))
    status = entry.get("last_status")
    if last_ok is None:
        if status in ("failed", "error"):
            return "error", "ни одной успешной копии, последняя попытка с ошибкой"
        return "none", "копий ещё не было"
    interval = nominal_interval(src.schedule) if src.enabled else None
    if interval and now - last_ok > interval * 2 + timedelta(hours=1):
        return "error", f"копия устарела ({human_age(last_ok, now)})"
    if status in ("failed", "error"):
        return "warn", "последняя попытка завершилась ошибкой"
    if status == "partial":
        return "warn", "последняя копия с ошибками по отдельным файлам"
    return "ok", "в порядке"


def build_report(cfg: Config, results: list[dict], dry_run: bool = False) -> str:
    now = datetime.now()
    state = load_state(cfg.data_dir)
    lines = [f"ODAA Backup — {'ПРОБНЫЙ ЗАПУСК (ничего не скачано)' if dry_run else 'отчёт'} {now:%d.%m.%Y %H:%M}", ""]
    if results:
        lines.append("Этот запуск:")
        for r in results:
            mark = {"ok": "✅", "partial": "⚠️", "failed": "❌", "dry-run": "🔎", "skipped": "⏭"}.get(r["status"], "•")
            lines.append(f"{mark} {r['title']} ({r['id']}): {r['summary']}")
            for e in (r.get("errors") or [])[:5]:
                lines.append(f"     ошибка: {e}")
            if len(r.get("errors") or []) > 5:
                lines.append(f"     … и ещё ошибок: {len(r['errors']) - 5} (см. лог)")
            for w in (r.get("warnings") or [])[:3]:
                lines.append(f"     предупреждение: {w}")
        lines.append("")
    lines.append("Возраст последней успешной копии:")
    for s in cfg.sources:
        e = state.get(s.id, {})
        level, why = source_health(cfg, s.id, e, now)
        mark = {"ok": "✅", "warn": "⚠️", "error": "❌", "none": "➖"}[level]
        size = human_size(e.get("last_size")) if e.get("last_size") else "—"
        onoff = describe(s.schedule) if s.enabled else "выключен"
        lines.append(f"{mark} {s.title}: {human_age(parse_ts(e.get('last_success')), now)}, {size} [{onoff}] — {why}")
    ok, why = second_copy_available(cfg)
    lines.append("")
    if not ok:
        lines.append(f"⚠️ Копия лежит только на одном диске: {why}.")
        lines.append("   Правило 3-2-1: подключите внешний диск или облачную папку и укажите её в second_copy.path.")
    else:
        lines.append(f"💾 Вторая копия: {cfg.second_copy.path}")
    return "\n".join(lines)


def send_telegram(cfg: Config, text: str, has_errors: bool) -> None:
    tg = cfg.telegram
    if not tg.enabled:
        return
    if tg.notify == "errors" and not has_errors:
        return
    try:
        token = secretstore.require(tg.bot_token_secret)
    except secretstore.MissingSecret as e:
        log.warning("Telegram: %s", e)
        return
    body = urllib.parse.urlencode({"chat_id": tg.chat_id, "text": text[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
            resp = json.loads(r.read().decode())
            if not resp.get("ok"):
                log.warning("Telegram ответил ошибкой: %s", resp.get("description"))
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось отправить уведомление в Telegram: %s", secretstore.redact(str(e)))
