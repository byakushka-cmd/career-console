"""Notifications about new verified jobs: e-mail or Telegram, configured in data/notify.env (never committed).

Only new verified jobs are sent: company, title, freshness, location and the official link. Never contacts,
notes or history. Nothing is sent until you fill in the settings file; a failed send never breaks a search.
"""
from __future__ import annotations

import json
import smtplib
import sqlite3
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from app import db
from app.config import paths

CHANNELS = ("email", "telegram")
MAX_ITEMS = 15


class NotifyError(RuntimeError):
    pass


def config_path() -> Path:
    return paths().data / "notify.env"


def load_config(path: Path | None = None) -> dict:
    """KEY=VALUE lines; a line starting with '#' is a comment (no inline comments: secrets may contain '#').
    Missing file = notifications off."""
    path = path or config_path()
    if not path.exists():
        return {}
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    return cfg


def channel(cfg: dict) -> str | None:
    value = cfg.get("NOTIFY_CHANNEL", "").casefold()
    return value if value in CHANNELS else None


def compose(items: list[dict]) -> tuple[str, str]:
    n = len(items)
    subject = f"Career Console: {n} new verified job{'s' if n != 1 else ''}"
    lines = [subject, ""]
    for i, item in enumerate(items[:MAX_ITEMS], 1):
        lines += [f"{i}. {item['company']} — {item['title']}",
                  f"   {item['freshness']} · {item['location'] or 'location not given'}",
                  f"   {item['url']}", ""]
    if n > MAX_ITEMS:
        lines.append(f"…and {n - MAX_ITEMS} more in the console.")
    lines.append("Open the console: http://127.0.0.1:45627/opportunities")
    return subject, "\n".join(lines).strip()


def send_email(cfg: dict, subject: str, text: str) -> None:
    missing = [k for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "NOTIFY_EMAIL_TO") if not cfg.get(k)]
    if missing:
        raise NotifyError(f"Missing in notify.env: {', '.join(missing)}")
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, cfg["SMTP_USER"], cfg["NOTIFY_EMAIL_TO"]
    message.set_content(text)
    try:
        with smtplib.SMTP_SSL(cfg["SMTP_HOST"], int(cfg.get("SMTP_PORT", "465")), timeout=30,
                              context=ssl.create_default_context()) as smtp:
            smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as e:
        raise NotifyError(f"E-mail failed: {e.__class__.__name__}: {e}") from None


def _telegram(cfg: dict, method: str, params: dict | None = None) -> dict:
    token = cfg.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise NotifyError("Missing in notify.env: TELEGRAM_BOT_TOKEN")
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise NotifyError(f"Telegram {method} failed: HTTP {e.code}") from None  # never echo the token
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise NotifyError(f"Telegram {method} failed: {e.__class__.__name__}") from None
    if not body.get("ok"):
        raise NotifyError(f"Telegram {method} failed: {body.get('description', 'unknown error')}")
    return body


def telegram_chat_id(cfg: dict) -> str:
    """Your chat with the bot: set TELEGRAM_CHAT_ID, or send the bot any message once and it is found here."""
    if cfg.get("TELEGRAM_CHAT_ID"):
        return cfg["TELEGRAM_CHAT_ID"]
    updates = _telegram(cfg, "getUpdates").get("result", [])
    chats = [u["message"]["chat"]["id"] for u in updates if u.get("message", {}).get("chat", {}).get("type") == "private"]
    if not chats:
        raise NotifyError("Send your bot any message in Telegram first, then try again")
    return str(chats[-1])


def send_telegram(cfg: dict, subject: str, text: str) -> None:
    _telegram(cfg, "sendMessage", {"chat_id": telegram_chat_id(cfg), "text": text[:4000],
                                   "disable_web_page_preview": "true"})


SENDERS = {"email": send_email, "telegram": send_telegram}


def send(cfg: dict, subject: str, text: str, senders: dict | None = None) -> str:
    chosen = channel(cfg)
    if not chosen:
        raise NotifyError("Notifications are off: set NOTIFY_CHANNEL=email or telegram in data/notify.env")
    (senders or SENDERS)[chosen](cfg, subject, text)
    return chosen


def notify_new_jobs(conn: sqlite3.Connection, cfg: dict | None = None, senders: dict | None = None) -> dict:
    """Send new verified jobs that were not sent before. Records every attempt; never raises."""
    from search import pipeline  # late import: search depends on app
    cfg = load_config() if cfg is None else cfg
    if not channel(cfg):
        return {"status": "off", "items": 0}
    already = {r[0] for r in conn.execute("SELECT posting_id FROM notified_postings")}
    items = [{"id": v.posting["id"], "company": v.company, "title": v.posting["title"],
              "freshness": v.ev.freshness.label, "location": v.posting["location"],
              "url": v.posting["apply_url"] or v.posting["url"]}
             for v in pipeline.opportunities(conn)["verified"]
             if v.is_new and not v.ev.let_through and v.posting["id"] not in already]  # not jobs a filter you turned off let in
    if not items:
        return {"status": "nothing new", "items": 0}
    subject, text = compose(items)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        used = send(cfg, subject, text, senders)
        status, error = "sent", None
    except NotifyError as e:
        used, status, error = channel(cfg), "failed", str(e)
    with db.transaction(conn):
        nid = conn.execute("INSERT INTO notifications(created_at_utc, channel, status, items, error) VALUES (?,?,?,?,?)",
                           (now, used, status, len(items), error)).lastrowid
        if status == "sent":
            conn.executemany("INSERT OR IGNORE INTO notified_postings(posting_id, notification_id) VALUES (?, ?)",
                             [(item["id"], nid) for item in items])
    return {"status": status, "items": len(items), "error": error}
