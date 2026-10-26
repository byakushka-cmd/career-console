"""Potential Leads: hiring posts, recruiters and referrals you capture by hand.

No scraping and no automation of LinkedIn: you paste the link and what the post says. A lead never
counts as a verified job. The strict Israel gate looks only at where the JOB is, as the post states it;
the author's own location or network degree is never evidence.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

from app import db
from search import profile as gates

KINDS = {"POST_LEAD": "Hiring post", "RECRUITER_LEAD": "Recruiter / HR", "REFERRAL": "Referral", "OTHER": "Other"}
LEAD_DECISIONS = {"saved": "Saved", "maybe": "Maybe", "skipped": "Not a fit", "hidden": "Hidden",
                  "contacted": "Contacted", "confirm_israel": "Confirmed open to Israel", "restored": "Restored"}
HIDING = {"skipped", "hidden"}
_ACTIVITY = re.compile(r"(?:activity[:\-]|urn:li:(?:activity|share|ugcPost):)(\d{15,25})", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def post_identity(url: str) -> str:
    """The same LinkedIn post has many URL shapes (/posts/name_slug-activity-ID, /feed/update/urn:li:activity:ID…)."""
    m = _ACTIVITY.search(url or "")
    if m:
        return f"linkedin-activity:{m.group(1)}"
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Paste the full link to the post (https://…)")
    host = parts.hostname.lower().removeprefix("www.")
    return urlunsplit(("https", host, parts.path.rstrip("/"), "", ""))


@dataclass
class LeadView:
    lead: sqlite3.Row
    geo: gates.Verdict
    decision: str | None
    decision_note: str
    observations: int
    confirmed_israel: bool = False

    @property
    def eligible(self) -> bool:
        return self.geo.value in gates.GEO_ELIGIBLE or self.confirmed_israel


def capture(conn: sqlite3.Connection, post_url: str, *, kind: str = "POST_LEAD", author: str = "", author_role: str = "",
            author_url: str = "", company: str = "", role_signal: str = "", job_location: str = "",
            posted_on: str = "", excerpt: str = "", note: str = "") -> tuple[int, bool]:
    """Add a lead, or a new observation of one already captured. -> (lead id, created)."""
    identity = post_identity(post_url)
    kind = kind if kind in KINDS else "OTHER"
    if posted_on and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted_on):
        raise ValueError("Posted date must look like 2026-10-14")
    if author_url and not author_url.startswith(("https://", "http://")):
        raise ValueError("The person's link must start with https://")
    clip = lambda text, n: (text or "").strip()[:n]
    now = _now()
    with db.transaction(conn):
        row = conn.execute("SELECT * FROM leads WHERE identity=?", (identity,)).fetchone()
        if row is None:
            lead_id = conn.execute("""INSERT INTO leads(identity, post_url, kind, author, author_role, author_url, company,
                                      role_signal, job_location, posted_on, excerpt, created_at_utc, updated_at_utc)
                                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                   (identity, clip(post_url, 2000), kind, clip(author, 120), clip(author_role, 120),
                                    clip(author_url, 2000) or None, clip(company, 120), clip(role_signal, 200),
                                    clip(job_location, 200), posted_on or None, clip(excerpt, 4000), now, now)).lastrowid
            created = True
        else:  # same post again: keep the first capture, fill gaps, record the new observation
            lead_id, created = row["id"], False
            updates = {k: v for k, v in {"author": clip(author, 120), "author_role": clip(author_role, 120),
                                         "company": clip(company, 120), "role_signal": clip(role_signal, 200),
                                         "job_location": clip(job_location, 200), "excerpt": clip(excerpt, 4000)}.items()
                       if v and not row[k]}
            if posted_on and not row["posted_on"]:
                updates["posted_on"] = posted_on
            sets = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE leads SET {sets + ', ' if sets else ''}updated_at_utc=? WHERE id=?",
                         (*updates.values(), now, lead_id))
        conn.execute("""INSERT INTO lead_observations(lead_id, captured_at_utc, post_url, job_location, excerpt, note)
                        VALUES (?,?,?,?,?,?)""", (lead_id, now, clip(post_url, 2000), clip(job_location, 200),
                                                 clip(excerpt, 4000), clip(note, 2000)))
    return lead_id, created


def geo_verdict(lead: sqlite3.Row) -> gates.Verdict:
    """Only the job location the post states decides; the excerpt can only add a remote restriction."""
    if not (lead["job_location"] or "").strip():
        return gates.Verdict("UNKNOWN", "the post does not say where the job is")
    return gates.geography(lead["job_location"], description=lead["excerpt"] or "")


def record_decision(conn: sqlite3.Connection, lead_id: int, decision: str, note: str = "") -> int:
    if decision not in LEAD_DECISIONS:
        raise ValueError(f"Unknown decision {decision!r}")
    if not conn.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
        raise ValueError("Unknown lead")
    with db.transaction(conn):
        return conn.execute("""INSERT INTO decision_events(created_at_utc, subject_type, subject_id, decision, note, origin)
                               VALUES (?, 'lead', ?, ?, ?, 'web')""", (_now(), lead_id, decision, note.strip()[:2000])).lastrowid


def lead_views(conn: sqlite3.Connection) -> dict[str, list[LeadView]]:
    """Shown (Israel or worldwide remote, or you confirmed it), geo review, outside Israel, hidden by you."""
    decision, confirmed = {}, set()
    for r in conn.execute("SELECT subject_id, decision, note FROM decision_events WHERE subject_type='lead' ORDER BY id"):
        if r["decision"] == "confirm_israel":
            confirmed.add(r["subject_id"])
        else:
            decision[r["subject_id"]] = (None, "") if r["decision"] == "restored" else (r["decision"], r["note"])
    out = {"shown": [], "geo_review": [], "outside": [], "hidden": []}
    for lead in conn.execute("""SELECT l.*, (SELECT count(*) FROM lead_observations o WHERE o.lead_id=l.id) AS seen
                                FROM leads l ORDER BY coalesce(l.posted_on, substr(l.created_at_utc, 1, 10)) DESC, l.id DESC"""):
        current, note = decision.get(lead["id"], (None, ""))
        view = LeadView(lead, geo_verdict(lead), current, note, lead["seen"], lead["id"] in confirmed)
        if current in HIDING:
            out["hidden"].append(view)
        elif view.eligible:
            out["shown"].append(view)
        elif view.geo.value == "OUTSIDE":
            out["outside"].append(view)
        else:
            out["geo_review"].append(view)
    return out


ISRAEL_GEO = '["101620260"]'  # LinkedIn's location ID for Israel


def contact_links(company: str) -> list[dict]:
    """Possible contacts for a job: LinkedIn people searches you open yourself (in Israel). Nothing is fetched,
    collected or stored; whether you know someone there is for you to see."""
    name = re.sub(r"\s+", " ", (company or "").split(" / ")[0]).strip()
    if not name:
        return []
    enc = lambda text: quote(text, safe="")
    people = "https://www.linkedin.com/search/results/people/?keywords={}&geoUrn=" + enc(ISRAEL_GEO)
    return [
        {"label": "Recruiters", "url": people.format(enc(f'"{name}" recruiter OR "talent acquisition"')),
         "title": f"Recruiters and talent acquisition at {name}"},
        {"label": "QA leaders", "url": people.format(enc(f'"{name}" "QA manager" OR "QA lead" OR "head of QA" OR "QA director"')),
         "title": f"QA managers and leads at {name}: often the hiring manager"},
        {"label": "People you know there", "url": people.format(enc(f'"{name}"')) + "&network=" + enc('["F","S"]'),
         "title": f"Your 1st and 2nd degree connections who mention {name}"},
    ]


# Every search is tied to Israel (user, 2026-10-16: anything else brings jobs from the other end of the world).
# Job and people searches use LinkedIn's Israel filter. Posts cannot be filtered by place, so they are searched
# in Hebrew (Hebrew posts are Israeli) or with Israeli cities named in the text. Your level first.
LEAD_TITLES = '"QA lead" OR "QA team lead" OR "QA manager" OR "head of QA"'
ISRAEL_CITIES = '"Tel Aviv" OR Herzliya OR "Petah Tikva" OR "Ramat Gan" OR Raanana OR Haifa OR Jerusalem OR Netanya'
DAY, WEEK = 86400, 7 * 86400


def search_links() -> list[dict]:
    """Searches you open and run yourself on LinkedIn; nothing is automated or collected."""
    li = "https://www.linkedin.com"
    enc = lambda text: quote(text, safe="")
    israel_id = ISRAEL_GEO.strip("[]\"")

    def jobs(text, seconds):
        return f"{li}/jobs/search/?keywords={enc(text)}&geoId={israel_id}&f_TPR=r{seconds}&sortBy=DD"

    def posts(text, when='"past-24h"'):
        return f"{li}/search/results/content/?keywords={enc(text)}&datePosted={enc(when)}&sortBy={enc('\"date_posted\"')}"

    def people(text, network):
        return f"{li}/search/results/people/?keywords={enc(text)}&geoUrn={enc(ISRAEL_GEO)}&network={enc(network)}"

    return [
        {"label": "Jobs in Israel: QA lead / manager / head, last 24 h", "url": jobs(LEAD_TITLES, DAY)},
        {"label": "Jobs in Israel: QA lead / manager / head, past week", "url": jobs(LEAD_TITLES, WEEK)},
        {"label": "Jobs in Israel: any QA, last 24 h (then look for the official link)",
         "url": jobs('QA OR SDET OR "test automation"', DAY)},
        {"label": "Hebrew posts: QA team lead (ראש צוות QA / בדיקות), past week",
         "url": posts('"ראש צוות QA" OR "ראש צוות בדיקות" OR "מנהל QA" OR "מנהלת QA"', '"past-week"')},
        {"label": "Hebrew hiring posts: QA (דרושים / מגייסים), last 24 h",
         "url": posts("דרושים QA OR מגייסים QA OR בודק תוכנה OR בודקת תוכנה")},
        {"label": "Posts naming Israeli cities: QA lead / manager, past week",
         "url": posts(f"({LEAD_TITLES}) ({ISRAEL_CITIES})", '"past-week"')},
        {"label": "Technical recruiters in Israel (2nd and 3rd degree)", "url": people("technical recruiter", '["S","O"]')},
        {"label": "Talent acquisition in Israel (2nd degree)", "url": people("talent acquisition", '["S"]')},
    ]
