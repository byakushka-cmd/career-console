"""Applications and jobs you add yourself: a link and a few words (pasted or dictated) become a record in your
pipeline, next to the imported tracker. The Google Sheet and the imported rows stay as they were; these records
live in the console's database. Their status is the latest decision event, as for every other record.

The text is read with fixed rules, no AI: a LinkedIn job card ("Company logo for, X." then the title), a labelled
line ("Company: X", "חברה: X"), the employer's job site in the link, and the line that names a role. Whatever is
not found is asked for, and anything read wrongly is corrected on the record's page."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app import db, tracker
from app.config import DISPLAY_TZ
from search import agencies, sources
from search import official
from search import profile as gates

SHEET = "Added in console"   # the source of these records, as the tracker's sheets are sources
CODE = "AD"
SUBJECT = "added"
EDITED = "edited"            # a corrected field: not a status
START = {"APPLIED": "Applied", "DISCOVERED": "Interested — not applied yet"}

URL = re.compile(r"https?://[^\s<>\"']+")
_LOGO = re.compile(r"^company logo for,?\s*(.+?)\.?$", re.I)             # LinkedIn job card
_LABELLED = re.compile(r"^(?:company|employer|חברה|מעסיק|компания)\s*[:：]\s*(.+)$", re.I)
_LEADING_ID = re.compile(r"^#?\s*(\d{3,9})\s*[-–—:.]?\s+(?=\S)")            # "8073 מוביל/ה טכנולוגי/ת QA…"
_ROLE_WORDS = ["engineer", "developer", "tester", "analyst", "architect", "specialist", "lead", "manager", "head",
               "director", "מהנדס", "מפתח", "בודק", "מוביל", "ראש צוות", "מנהל", "инженер", "тестировщик", "лид"]
# employer job sites that name the company in the address: <company>.zohorecruit.com, apply.workable.com/<company>
_HOSTED = [re.compile(r"^([a-z0-9-]+)\.(?:zohorecruit|breezy|bamboohr|recruitee|teamtailor|workable)\.(?:com|hr|eu)$"),
           re.compile(r"^(?:apply\.workable\.com|jobs\.smartrecruiters\.com|careers\.smartrecruiters\.com)/([^/]+)")]
_SITE_WORDS = {"www", "careers", "career", "jobs", "job", "apply", "hire", "join", "work", "co", "com", "il", "io",
               "net", "org", "ai"}


@dataclass
class Added:
    id: int
    created_at_utc: str
    url: str | None
    pasted: str
    company: str
    role: str
    job_ids: list
    location: str | None
    state: str                 # today's reading: a not-applied record expires after two weeks, as in the tracker
    status: str                # the status you set
    note: str
    changed_at_utc: str
    applied: str | None        # the day you first marked it Applied
    moved: str | None          # the day of your latest status
    history: list = field(default_factory=list)


def _clean_name(slug: str) -> str:
    name = re.sub(r"[-_]+", " ", slug).strip()
    return name.title() if name == name.casefold() else name


def company_from_url(url: str | None) -> str:
    """The employer named by its own job site: boards.greenhouse.io/acme -> Acme, eprfireworks.zohorecruit.com ->
    Eprfireworks, careers.spotower.com -> Spotower. LinkedIn and job boards name nobody."""
    if not url or official.channel(url) != official.EMPLOYER:
        return ""
    for _, board in sources.find_boards(url):
        return _clean_name(board.split("/")[0])
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    for pattern in _HOSTED:
        if m := pattern.match(host) or pattern.match(host + parts.path):
            return _clean_name(m.group(1))
    words = [w for w in host.split(".") if w not in _SITE_WORDS]
    return _clean_name(words[-1]) if words else ""


def parse(url: str | None, text: str, profile: dict | None = None) -> dict:
    """-> company, role, job_ids, location read from the link and the text ('' or [] when not found)."""
    profile = profile or gates.DEFAULT_PROFILE
    lines = [re.sub(r"\s+", " ", l).strip() for l in (text or "").splitlines()]
    lines = [l for l in lines if l and not URL.fullmatch(l)]
    company = ""
    for l in lines:
        if m := _LOGO.match(l) or _LABELLED.match(l):
            company = m.group(1).strip()
            break
    company = company or company_from_url(url)
    terms = list(profile.get("include_terms", [])) + list(profile.get("focus_level_terms", [])) + _ROLE_WORDS
    candidates = [l for l in lines if not _LOGO.match(l) and not _LABELLED.match(l)
                  and l.casefold() != company.casefold() and len(l) <= 160]
    role_line = next((l for l in candidates if gates.find_terms(l, terms)), "")
    if not company and role_line:   # a pasted card: the employer is the short line just before the title
        before = candidates[:candidates.index(role_line)]
        company = next((l for l in reversed(before) if len(l) <= 60 and not gates.find_terms(l, terms)), "")
    ids = []
    if m := _LEADING_ID.match(role_line):
        ids.append(m.group(1))
        role_line = role_line[m.end():].strip()
    for n in agencies.vacancy_numbers(text or ""):
        if n not in ids:
            ids.append(n)
    places = [p for p in gates.find_terms(text or "", gates.ISRAEL_TERMS) if p.casefold() not in ("israel", "ישראל", "израиль")]
    location = places[0] if places else ("Israel" if gates.find_terms(text or "", ["israel", "ישראל"]) else "")
    location = location.title() if location.isascii() else location     # "tel aviv" -> "Tel Aviv"
    return {"company": company, "role": role_line, "job_ids": ids, "location": location}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _day(stamp: str) -> str:
    return datetime.fromisoformat(stamp).astimezone(ZoneInfo(DISPLAY_TZ)).date().isoformat()


def add(conn: sqlite3.Connection, url: str = "", text: str = "", state: str = "APPLIED", company: str = "",
        role: str = "", note: str = "", profile: dict | None = None) -> int:
    """A new record from a link and a few words. Raises ValueError saying what is missing."""
    if state not in START:
        raise ValueError(f"Unknown status {state!r}")
    url, text = (url or "").strip(), (text or "").strip()
    if not url and (m := URL.search(text)):
        url = m.group(0)
    if not url and not text and not (company.strip() and role.strip()):
        raise ValueError("Paste a link or write a few words about the job")
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("A link starts with https://")
    found = parse(url, text, profile)
    company, role = company.strip() or found["company"], role.strip() or found["role"]
    if not company:
        raise ValueError("The company is not in the text: write it in the Company field")
    if not role:
        raise ValueError("The role is not in the text: write it in the Role field")
    with db.transaction(conn):
        added_id = conn.execute("""INSERT INTO added_records(created_at_utc, url, pasted, company, role, job_ids, location)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (_now(), url or None, text[:8000], company[:200], role[:300],
                                 ",".join(found["job_ids"]), found["location"] or None)).lastrowid
        conn.execute("""INSERT INTO decision_events(created_at_utc, subject_type, subject_id, decision, note, origin)
                        VALUES (?, ?, ?, ?, ?, 'web: quick add')""", (_now(), SUBJECT, added_id, state, note.strip()[:2000]))
    return added_id


def set_status(conn: sqlite3.Connection, added_id: int, state: str, note: str = "") -> int:
    if state not in tracker.STATUS_CHOICES:
        raise ValueError(f"Unknown status {state!r}")
    if not conn.execute("SELECT 1 FROM added_records WHERE id=?", (added_id,)).fetchone():
        raise ValueError("Unknown record")
    with db.transaction(conn):
        return conn.execute("""INSERT INTO decision_events(created_at_utc, subject_type, subject_id, decision, note, origin)
                               VALUES (?, ?, ?, ?, ?, 'web')""", (_now(), SUBJECT, added_id, state, note.strip()[:2000])).lastrowid


EDITABLE = ("company", "role", "url", "job_ids", "location")


def edit(conn: sqlite3.Connection, added_id: int, **fields) -> None:
    """Correct what was read from the text. The old values are kept in an 'edited' event."""
    row = conn.execute("SELECT * FROM added_records WHERE id=?", (added_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown record")
    new = {k: (fields.get(k) or "").strip() for k in EDITABLE if k in fields}
    if "job_ids" in new:
        new["job_ids"] = ",".join(dict.fromkeys(re.findall(r"\d{3,12}", new["job_ids"])))
    if new.get("url") and not new["url"].startswith(("http://", "https://")):
        raise ValueError("A link starts with https://")
    for key in ("company", "role"):
        if key in new and not new[key]:
            raise ValueError(f"The {key} cannot be empty")
    changed = {k: v for k, v in new.items() if (row[k] or "") != v}
    if not changed:
        return
    with db.transaction(conn):
        conn.execute(f"UPDATE added_records SET {', '.join(f'{k}=?' for k in changed)} WHERE id=?",
                     [v or None if k in ("url", "location") else v for k, v in changed.items()] + [added_id])
        conn.execute("""INSERT INTO decision_events(created_at_utc, subject_type, subject_id, decision, note, origin)
                        VALUES (?, ?, ?, ?, ?, 'web')""",
                     (_now(), SUBJECT, added_id, EDITED,
                      json.dumps({k: row[k] for k in changed}, ensure_ascii=False)[:2000]))


def load(conn: sqlite3.Connection, on: date | None = None) -> list[Added]:
    """Every record you added, with today's status (newest first)."""
    events: dict[int, list] = {}
    for e in conn.execute("""SELECT subject_id, decision, note, created_at_utc FROM decision_events
                             WHERE subject_type=? ORDER BY id""", (SUBJECT,)):
        events.setdefault(e["subject_id"], []).append(e)
    out = []
    for r in conn.execute("SELECT * FROM added_records ORDER BY id DESC"):
        history = events.get(r["id"], [])
        statuses = [e for e in history if e["decision"] in tracker.STATUS_CHOICES]
        last = statuses[-1] if statuses else None
        status = last["decision"] if last else "DISCOVERED"
        moved = _day(last["created_at_utc"]) if last else _day(r["created_at_utc"])
        applied = next((_day(e["created_at_utc"]) for e in statuses if e["decision"] == "APPLIED"), None)
        out.append(Added(r["id"], r["created_at_utc"], r["url"], r["pasted"], r["company"], r["role"],
                         [i for i in (r["job_ids"] or "").split(",") if i], r["location"],
                         tracker.effective_state(status, moved, on), status, last["note"] if last else "",
                         last["created_at_utc"] if last else r["created_at_utc"], applied, moved, history))
    return out
