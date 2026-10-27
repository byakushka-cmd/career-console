"""Local web console: read-only record views plus review decisions (append-only events)."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from collections import Counter
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import added as quick, db, my_links, review, tracker
from app.config import local_time
from search import pipeline as search_pipeline
from search import agencies as search_agencies
from search import official as search_official
from search import leads as search_leads
from search import mail as search_mail
from search import replies as search_replies
from search import sources as search_sources
from search import profile as search_profile
from search.profile import find_terms
from app.legacy_import import (C_CHECKED, C_DISCOVERY, C_URL, COMEET, MASTER, MASTER_DATE_COLUMNS, M_APPLIED,
                               M_CHANNEL, M_CONTACT, M_EVIDENCE, M_HISTORY, M_STATUS, M_UPDATED, M_URL, converted_date)

HERE = Path(__file__).resolve().parent
CONSOLE = search_pipeline.CONSOLE
SHEET_CODES = {MASTER: "MT", COMEET: "CI", CONSOLE: "CC", quick.SHEET: quick.CODE}
SOURCES = {"all": "All sources", "master": MASTER, "comeet": COMEET, "console": CONSOLE, "added": quick.SHEET}
SORTS = {"updated": "Last update", "applied": "Applied date", "company": "Company"}
URL_IN_TEXT = re.compile(r"https?://[^\s,;()\[\]<>\"']+")
NAV = [("opportunities", "New Opportunities", "/opportunities"), ("pipeline", "My Pipeline", "/pipeline"),
       ("diagnostics", "Diagnostics", "/diagnostics")]

# Colour carries one meaning each: green = a process is under way, amber = waiting on your decision,
# lavender = surfaced by discovery/automation, neutral = ended.
GROUPS = {
    "active": ("In progress", "pr", ("APPLIED", "IN_PROCESS", "INTERVIEW", "OUTREACH", "CONTACT_ONLY", "PROFILE_ONLY")),
    "decide": ("On hold", "hu", ("HOLD", "CONSIDERING", "REVIEW", "BLOCKED")),  # parked: close, pursue or keep
    "discovered": ("Not applied yet", "ai", ("DISCOVERED",)),
    "closed": ("Closed", "n", ("REJECTED", "CLOSED", "SKIPPED", "DUPLICATE", "NOTE", tracker.EXPIRED)),
}
STATE_LABELS = {
    "APPLIED": "Applied", "IN_PROCESS": "In process", "INTERVIEW": "Interview", "OUTREACH": "Outreach",
    "CONTACT_ONLY": "Recruiter contact", "PROFILE_ONLY": "Profile on file", "HOLD": "On hold",
    "CONSIDERING": "Considering", "REVIEW": "Unclear status", "BLOCKED": "Blocked", "DISCOVERED": "Not applied",
    "REJECTED": "Rejected by them", "CLOSED": "Closed", tracker.EXPIRED: "Expired", "SKIPPED": "Skipped", "DUPLICATE": "Duplicate", "NOTE": "Index note",
}
STATE_GROUP = {s: g for g, (_, _, states) in GROUPS.items() for s in states}


def tone(state: str | None) -> str:
    return GROUPS[STATE_GROUP[state]][1] if state in STATE_GROUP else "n"


@dataclass
class Record:
    id: int
    sheet: str
    row: int
    values: list
    state: str | None
    job_id: int | None
    group_size: int
    issues: int
    search_text: str = field(repr=False, default="")
    tracker_state: str | None = None          # the tracker's own (provisional) reading
    change: "tracker.StatusChange | None" = None  # your latest update, if any
    agency: str | None = None                 # staffing agency / contractor: identify by title + number
    vacancy: list = field(default_factory=list)
    official: list = field(default_factory=list)  # likely official postings when the link is an aggregator or missing
    moved_on: str | None = None                # last movement: tracker update/applied date or your latest status
    possibly_same: list = field(default_factory=list)  # other sources (recruiters) for maybe the same position
    lead_here: list = field(default_factory=list)      # an engineer record at an agency you applied to as a lead
    level: str = "engineer"                            # "lead" (lead, manager, head, director) or "engineer" (all else)

    @property
    def ref(self) -> str:
        return f"{SHEET_CODES.get(self.sheet, self.sheet)} {self.row}"

    @property
    def href(self) -> str:  # console records live on their posting's page, added ones on their own
        if self.sheet == quick.SHEET:
            return f"/added/{self.row}"
        return f"/postings/{self.row}" if self.sheet == CONSOLE else f"/records/{self.id}"

    @property
    def company(self) -> str:
        return str(self.values[0] or "—")

    @property
    def role(self) -> str:
        return str(self.values[1] or "—")

    @property
    def status_raw(self) -> str:
        v = self.values[C_DISCOVERY] if self.sheet == COMEET else self.values[M_STATUS]
        return str(v or "").strip()

    @property
    def applied(self) -> str | None:
        return None if self.sheet == COMEET else converted_date(self.values[M_APPLIED])

    @property
    def updated(self) -> str | None:
        return converted_date(self.values[C_CHECKED] if self.sheet == COMEET else self.values[M_UPDATED])

    @property
    def links(self) -> list[str]:
        """Links in the link, evidence and channel columns, best first: employer, LinkedIn, job board.
        Free-text notes are left out: they often point at other jobs."""
        cols = (C_URL,) if self.sheet == COMEET else (M_URL, M_EVIDENCE, M_CHANNEL)
        found = [u for i in cols if i < len(self.values) for u in URL_IN_TEXT.findall(str(self.values[i] or ""))]
        return search_official.ordered_links(found)

    @property
    def url(self) -> str | None:
        return self.links[0] if self.links else None

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state or "", self.state or "—")

    @property
    def tone(self) -> str:
        return tone(self.state)

    @property
    def raw_differs(self) -> bool:
        return bool(self.status_raw) and self.status_raw.casefold() != self.label.casefold()


@lru_cache(maxsize=None)
def asset_url(name: str) -> str:
    """/static/app.css?v=<content hash>: a changed file gets a new address, so the browser never pairs new pages
    with an old stylesheet it kept. Computed once per server start (a restart picks up new files)."""
    digest = sha256((HERE / "static" / name).read_bytes()).hexdigest()[:10]
    return f"/static/{name}?v={digest}"


SHARED_REQUIREMENTS = 3   # rare requirements in common at which two recruiters' records are possibly one job
RARE_SHARE = 0.12         # a requirement found in fewer than this share of records is rare enough to identify a job


def requirement_words(text: str, terms: list[str]) -> set[str]:
    """Requirements named in a record's text ("azure devops" also contains "azure": the longer one is kept)."""
    found = {t.casefold() for t in find_terms(text, terms)}
    return {t for t in found if not any(t != o and t in o for o in found)}


def link_possibly_same(records: list[Record], agency_terms: list[str], profile: dict) -> None:
    """One employer often hires through several recruiters at once, and gives each the same requirements.
    Records from different sources, with a recruiter on at least one side and of the same level, are linked as
    "possibly the same position" when they name the same end client, or share at least SHARED_REQUIREMENTS rare
    requirements (and no different cities). Any status; nothing is merged; the reason is shown."""
    from search import profile as gates
    terms = profile.get("requirement_terms", gates.DEFAULT_PROFILE["requirement_terms"])
    info, by_client = {}, {}
    for r in records:
        if r.sheet == CONSOLE:
            continue
        text = " ".join(str(r.values[i]) for i in (1, 12, 13, 15) if i < len(r.values) and r.values[i]) \
            if r.sheet == MASTER else " ".join(str(v) for v in r.values[1:] if v)
        client = search_agencies.client_words(r.company, r.role, agency_terms)
        info[r.id] = {"level": gates.level(r.role, profile), "source": search_agencies.company_key(r.company, agency_terms),
                      "agency": bool(search_agencies.agency_key(r.company, agency_terms)), "client": client,
                      "needs": requirement_words(text, terms),
                      "cities": set(find_terms(f"{r.company} {text}", gates.ISRAEL_TERMS)) - {"israel", "Israel"}}
        for w in client:
            by_client.setdefault(w, []).append(r)
    counts = Counter(t for i in info.values() for t in i["needs"])
    rare = {t for t, n in counts.items() if n / max(len(info), 1) < RARE_SHARE}
    ids = [r for r in records if r.id in info]
    for r in ids:
        a, found = info[r.id], {}
        for o in ids:
            b = info[o.id]
            if (o.id == r.id or a["level"] != b["level"] or a["source"] == b["source"] or not (a["agency"] or b["agency"])
                    or (r.job_id and o.job_id == r.job_id)):
                continue
            if a["client"] & b["client"]:
                found[o.id] = (o, f"same end client: {', '.join(sorted(a['client'] & b['client']))}")
                continue
            shared = sorted(a["needs"] & b["needs"] & rare)
            far = a["cities"] and b["cities"] and not (a["cities"] & b["cities"])
            if len(shared) >= SHARED_REQUIREMENTS and not far:
                found[o.id] = (o, f"same requirements: {', '.join(shared)}")
        r.possibly_same = sorted(found.values(), key=lambda pair: pair[1])[:8]


def link_agency_leads(records: list[Record], profile: dict) -> None:
    """Your rule for staffing agencies: an engineer or junior record at an agency you applied to as a lead is
    marked with those applications. Nothing is hidden."""
    by_agency: dict[str, list[Record]] = {}
    for r in records:
        if r.agency:
            by_agency.setdefault(r.agency, []).append(r)
    for r in records:
        if r.agency and search_profile.level(r.role, profile) in search_pipeline.LOWER_LEVELS:
            r.lead_here = search_pipeline.lead_applications(by_agency[r.agency], profile)


class FormFields:
    """A posted form: field("name") is its first value; field.all("name") every value (checkboxes)."""

    def __init__(self, form: dict):
        self.form = form

    def __call__(self, name: str, default: str = "") -> str:
        return (self.form.get(name) or [default])[0]

    def all(self, name: str) -> list[str]:
        return list(self.form.get(name) or [])


ADDED_ID_BASE = -1_000_000   # record ids of added records: below every tracker row and console posting


def load_records(conn: sqlite3.Connection, open_rows: Counter | None = None) -> list[Record]:
    """All tracker and Comeet records plus applications marked in the console. `issues` counts open review
    items that need a decision; `state` is your latest status update, else the tracker's own reading."""
    if open_rows is None:
        open_rows = review.open_rows(review.review_items(conn))
    statuses = tracker.current_statuses(conn)
    agency_terms = search_pipeline.current_profile(conn)[1]["agency_companies"]
    official = search_official.OfficialIndex(conn, agency_terms)
    rows = conn.execute(f"""
        SELECT l.id, l.sheet_name, l.source_row, l.values_json, s.provisional_state AS state, j.job_id,
               (SELECT count(*) FROM job_legacy_links x WHERE x.job_id = j.job_id) AS group_size
        FROM legacy_rows l
        LEFT JOIN legacy_row_states s ON s.legacy_row_id = l.id
        LEFT JOIN job_legacy_links j ON j.legacy_row_id = l.id
        WHERE l.sheet_name IN ('{MASTER}', '{COMEET}') AND l.source_row > 1
        ORDER BY l.id""").fetchall()
    out = []
    for r in rows:
        values = json.loads(r["values_json"])
        change = statuses.get(r["id"])
        agency = search_agencies.agency_key(str(values[0] or ""), agency_terms)
        vacancy = search_pipeline.tracker_vacancy_numbers(r["sheet_name"], values,
                                                          change.note if change and change.source == "you" else "")
        moved = tracker.moved_on(r["sheet_name"], values, change)
        state = tracker.effective_state(change.state if change else r["state"], moved)
        record = Record(r["id"], r["sheet_name"], r["source_row"], values, state,
                        r["job_id"], r["group_size"] or 0, open_rows.get(r["id"], 0),
                        " ".join(str(v) for v in values if v is not None).casefold(), r["state"], change,
                        agency, vacancy, moved_on=moved)
        if not record.url or search_official.is_aggregator(record.url):
            record.official = official.find(record.company, record.role, vacancy)
        out.append(record)
    # Applications you marked in the console ("Good find — applied" on a posting), in the tracker's column layout,
    # with your status updates on them (e.g. Rejected by them).
    console = tracker.manual_statuses(conn, tracker.CONSOLE_SUBJECT)
    for p in search_pipeline.applied_postings(conn):
        day = local_time(p["applied_at_utc"])[:10]
        values = [None] * 17
        values[0], values[1] = p["company"], p["title"]
        values[M_APPLIED] = values[M_UPDATED] = day
        values[M_STATUS] = "Applied — good find from the search, marked in Career Console"
        values[M_URL] = p["apply_url"] or p["url"]
        values[M_HISTORY] = p["applied_note"] or None
        change = console.get(p["id"])
        if change:
            values[M_UPDATED] = local_time(change.at_utc)[:10]
        out.append(Record(-p["id"], CONSOLE, p["id"], values, change.state if change else "APPLIED", None, 0, 0,
                          " ".join(str(v) for v in values if v).casefold(), None, change,
                          moved_on=values[M_UPDATED]))
    # Records you added yourself (a link and a few words), in the same column layout.
    for a in quick.load(conn):
        values = [None] * 17
        values[0], values[1] = a.company, a.role
        values[M_APPLIED], values[M_UPDATED] = a.applied, a.moved
        values[M_STATUS] = "Added in the console"
        values[M_URL], values[M_HISTORY] = a.url, a.pasted or None
        record = Record(ADDED_ID_BASE - a.id, quick.SHEET, a.id, values, a.state, None, 0, 0,
                        " ".join(str(v) for v in values if v).casefold(), None,
                        tracker.StatusChange(a.status, a.note, a.changed_at_utc, "you"),
                        search_agencies.agency_key(a.company, agency_terms), a.job_ids, moved_on=a.moved)
        if not record.url or search_official.is_aggregator(record.url):
            record.official = official.find(record.company, record.role, a.job_ids)
        out.append(record)
    profile = search_pipeline.current_profile(conn)[1]
    for r in out:   # your first priority is lead roles: My Pipeline keeps them apart from engineer roles
        r.level = "lead" if search_profile.level(r.role, profile) in ("FOCUS", "ABOVE") else "engineer"
    link_possibly_same(out, agency_terms, profile)
    link_agency_leads(out, profile)
    return out


LEVELS = {"lead": "Lead roles", "engineer": "Engineer & other"}


def filter_records(records: list[Record], q: str = "", group: str = "all", source: str = "all", sort: str = "updated",
                   review_only: str = "", level: str = "all") -> tuple[list[Record], dict]:
    """The My Pipeline list for these parameters, and the per-group counts before the group filter (per-level counts
    before the level filter, as "level:lead" / "level:engineer")."""
    terms = q.casefold().split()
    if terms:
        records = [r for r in records if all(t in r.search_text for t in terms)]
    if source in SOURCES and source != "all":
        records = [r for r in records if r.sheet == SOURCES[source]]
    if review_only:
        records = [r for r in records if r.issues]
    level_counts = {f"level:{key}": sum(1 for r in records if r.level == key) for key in LEVELS}
    level_counts["level:all"] = len(records)
    if level in LEVELS:
        records = [r for r in records if r.level == level]
    counts = {g: sum(1 for r in records if STATE_GROUP.get(r.state) == g) for g in GROUPS} | level_counts
    counts["all"] = len(records)
    if group in GROUPS:
        records = [r for r in records if STATE_GROUP.get(r.state) == group]
    if sort == "company":
        records = sorted(records, key=lambda r: (r.company.casefold(), r.row))
    else:
        key = (lambda r: r.applied) if sort == "applied" else (lambda r: r.updated)
        records = sorted(records, key=lambda r: (key(r) is not None, key(r) or "", -r.id), reverse=True)
    return records, counts


def pipeline_overview(records: list[Record], new_jobs: int = 0, potential_leads: int = 0, new_today: int = 0,
                      alert_jobs: int = 0) -> dict:
    """Five tiles in the order a job moves (new from the search, not applied yet, on hold, in progress, closed)
    and the status mix, always over all records (not the current filter)."""
    by_group = {g: sum(1 for r in records if STATE_GROUP.get(r.state) == g) for g in GROUPS}
    total = sum(by_group.values()) or 1
    segments, x = [], 0.0
    for g, (label, t, _) in GROUPS.items():
        width = 100 * by_group[g] / total
        segments.append({"group": g, "label": label, "tone": t, "count": by_group[g],
                         "pct": round(width), "x": round(x, 3), "w": round(width, 3)})
        x += width
    tiles = [
        # the career sites and your alert e-mails together: a quiet day on the sites is not "nothing new"
        {"label": "New discoveries", "value": new_jobs + alert_jobs, "tone": "pr",
         "href": "/opportunities?tab=" + ("verified" if new_jobs or not alert_jobs else "mail"),
         "caption": (f"{new_jobs} from career sites · {alert_jobs} from alerts" if alert_jobs
                     else f"{new_today} new today" if new_today else "found by the search")},
        {"label": "Not applied yet", "value": by_group["discovered"], "tone": "ai", "href": "/pipeline?group=discovered",
         "caption": "apply within two weeks"},
        {"label": "On hold", "value": by_group["decide"], "tone": "hu", "href": "/pipeline?group=decide",
         "caption": "close, pursue or keep"},
        {"label": "In progress", "value": by_group["active"], "tone": "pr", "href": "/pipeline?group=active",
         "caption": "active applications"},
        {"label": "Closed", "value": by_group["closed"], "tone": "n", "href": "/pipeline?group=closed",
         "caption": "no longer active"},
    ]
    return {"tiles": tiles, "segments": segments, "total": sum(by_group.values()), "leads": potential_leads}


def create_app(db_path: Path, allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost"), search_get=None,
               mail_imap=None) -> FastAPI:
    """search_get replaces the HTTP fetcher (tests use fake boards; None means the real official boards);
    mail_imap replaces the IMAP connection the same way (None: your real mailbox, if one is set up)."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise SystemExit(f"No database at {db_path}. Run `career-console import --snapshot …` first.")
    check = db.connect(db_path, readonly=True, create=False)
    try:
        version, latest = db.current_version(check), db.latest_version()
    finally:
        check.close()
    if version < latest:
        raise SystemExit(f"Database schema is v{version}, this version of the app needs v{latest}. "
                         "Run `career-console migrate` (it makes a verified backup first).")

    links_path = my_links.path_for(db_path)  # your own links (LinkedIn…): next to the database, private
    mail_path = search_mail.config_path(db_path)  # your mailboxes for job-alert e-mails: next to it, private
    app = FastAPI(title="Career Console", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(nav=NAV, groups=GROUPS, tone=tone, state_labels=STATE_LABELS, asset=asset_url,
                                 board_filters=search_profile.FILTERS)
    templates.env.filters["local_time"] = local_time
    # Templates are read once, at start: pages always match the code that is running, even while files change.
    templates.env.auto_reload = False
    templates.env.globals.update(status_choices=tracker.STATUS_CHOICES, status_hints=tracker.STATUS_HINTS,
                                 posting_decisions=search_pipeline.POSTING_DECISIONS,
                                 provider_names=search_sources.PROVIDER_NAMES,
                                 wrong_match_reasons=search_pipeline.WRONG_MATCH_REASONS,
                                 not_for_me_reasons=search_pipeline.NOT_FOR_ME_REASONS,
                                 lead_kinds=search_leads.KINDS, lead_decisions=search_leads.LEAD_DECISIONS,
                                 role_summary=search_sources.role_summary, apply_destination=search_sources.apply_destination,
                                 link_channel=search_official.channel, link_label=search_official.link_label,
                                 expire_days=tracker.EXPIRE_DAYS, my_links=lambda: my_links.load(links_path),
                                 contact_links=search_leads.contact_links,
                                 linkedin_search=search_official.linkedin_search,
                                 employer_search=search_official.employer_search)
    for name in templates.env.list_templates(extensions=["html"]):
        templates.env.get_template(name)
    app.state.search_thread = None

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"  # private data: keep it out of caches
        else:
            response.headers["Cache-Control"] = "no-cache"  # always revalidated: a new version is never missed
        return response

    def get_conn():
        conn = db.connect(db_path, readonly=True, create=False)
        try:
            yield conn
        finally:
            conn.close()

    def render(request: Request, name: str, status_code: int = 200, **context):
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return render(request, "not_found.html", status_code=404, active=None)
        return HTMLResponse(str(exc.detail), status_code=exc.status_code)

    def same_origin(request: Request) -> bool:
        """CSRF guard for a localhost app: only forms served by this app may post to it."""
        site = request.headers.get("sec-fetch-site")
        if site is not None:
            return site == "same-origin"
        origin = request.headers.get("origin")
        return origin is not None and urlsplit(origin).netloc == request.headers.get("host")

    async def read_form(request: Request):
        """CSRF check, size limit and urlencoded parsing for every POST. Returns a field getter."""
        if not same_origin(request):
            raise HTTPException(403, "Cross-site request refused")
        body = await request.body()
        if len(body) > 16_384:
            raise HTTPException(413, "Request too large")
        return FormFields(parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True))

    def write_conn():
        return db.connect(db_path, create=False)

    @app.post("/review/{kind}/{item_id}")
    async def decide(request: Request, kind: str, item_id: int):
        field = await read_form(request)
        base = field("back", "/diagnostics").split("#")[0].split("?")[0]
        if not base.startswith("/") or base.startswith("//") or "\\" in base:
            base = "/diagnostics"
        conn = write_conn()
        try:
            review.record_decision(conn, kind, item_id, field("decision"), field("note"))
            item = next(i for i in review.review_items(conn) if i.kind == kind and i.id == item_id)
        except review.DecisionError as e:
            raise HTTPException(400, str(e))
        finally:
            conn.close()
        if base == "/diagnostics":  # land in the section the item now belongs to, opened, scrolled to it
            base += "" if item.is_open and item.needs_attention else ("?open=info" if item.is_open else "?open=done")
        return RedirectResponse(f"{base}#{item.anchor}", status_code=303)

    @app.post("/postings/{posting_id}/decision")
    async def posting_decision(request: Request, posting_id: int):
        """After a decision you stay in the list you were in, at the next card, with an Undo notice."""
        field = await read_form(request)
        view = field("view", "verified")
        lists = [view] if view in ("hidden", "older") else ["verified", "reposted"]  # in the order the page shows them
        listed = lambda: [] if view == "posting" else [  # on a job's own page there is no list to continue in
            v.posting["id"] for views in [search_pipeline.opportunities(conn)] for k in lists for v in views[k]]
        conn = write_conn()
        try:
            before = listed()
            note = field("note")
            if field("decision") == "already_applied" and not note.strip() and field("same_as"):
                note = f"same as {field('same_as')[:300]}"   # the console saw a possibly-same job: say which
            whole_company = field("decision") == "skipped" and field("whole_company") == "1"
            if whole_company:
                note = (note.strip() + " — " if note.strip() else "") + "whole company is not for me: board turned off"
            search_pipeline.record_posting_decision(conn, posting_id, field("decision"), note)
            if whole_company:  # the employer's board is no longer checked, and its jobs leave every list
                source = conn.execute("SELECT source_id FROM postings WHERE id=?", (posting_id,)).fetchone()
                search_pipeline.set_source_enabled(conn, source["source_id"], False)
            after = listed()
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            conn.close()
        if view == "posting":
            return RedirectResponse(f"/postings/{posting_id}", status_code=303)
        if posting_id in after:
            anchor = f"posting-{posting_id}"
        else:  # the card left this list: continue at the card that followed it
            following = before[before.index(posting_id) + 1:] if posting_id in before else []
            anchor = next((f"posting-{i}" for i in following if i in after), "jobs")
        show = f"&show={view}" if view in ("hidden", "older") else ""
        flag = ("noted" if field("decision") == search_pipeline.NOTE_ONLY
                else "marked" if field("decision") in search_pipeline.OVERQUALIFIED_MARKS else "done")
        return RedirectResponse(f"/opportunities?tab=verified{show}&{flag}={posting_id}#{anchor}", status_code=303)

    @app.post("/opportunities/filters")
    async def toggle_filter(request: Request):
        """Turn one board filter on or off; you stay on the board, at the filters."""
        field = await read_form(request)
        conn = write_conn()
        try:
            search_pipeline.set_filter(conn, field("filter"), field("state") == "on")
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            conn.close()
        # the panel stays open: you can go on turning filters on and off
        return RedirectResponse("/opportunities?tab=verified&filters=open#filters", status_code=303)

    @app.post("/mail/read")
    async def read_mail(request: Request):
        await read_form(request)
        conn = write_conn()
        try:
            search_mail.read(conn, db_path, mail_imap)
        finally:
            conn.close()
        return RedirectResponse("/opportunities?tab=mail#mail", status_code=303)

    @app.post("/mail/{job_id}/decision")
    async def mail_decision(request: Request, job_id: int):
        """Applied / Interested put the job in your tracker (an added record); Already applied, Not a fit and Hide take it off."""
        field = await read_form(request)
        decision = field("decision")
        conn = write_conn()
        try:
            job = conn.execute("SELECT * FROM mail_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise HTTPException(404)
            if decision in quick.START:
                added_id = quick.add(conn, job["url"], search_mail.tracker_text(job), decision,
                                     company=job["company"] or f"Company not named ({job['source']})", role=job["title"],
                                     note=field("note"), profile=search_pipeline.current_profile(conn)[1])
                search_mail.record_decision(conn, job_id, "added", f"{quick.CODE} {added_id} · {quick.START[decision]}")
            else:
                search_mail.record_decision(conn, job_id, decision, field("note"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            conn.close()
        return RedirectResponse(f"/opportunities?tab=mail&done={job_id}#mail-list", status_code=303)

    @app.post("/leads")
    async def add_lead(request: Request):
        field = await read_form(request)
        conn = write_conn()
        try:
            lead_id, created = search_leads.capture(
                conn, field("post_url"), kind=field("kind", "POST_LEAD"), author=field("author"),
                author_role=field("author_role"), author_url=field("author_url"), company=field("company"),
                role_signal=field("role_signal"), job_location=field("job_location"), posted_on=field("posted_on"),
                excerpt=field("excerpt"), note=field("note"))
        except ValueError as e:
            form = {k: field(k) for k in ("post_url", "kind", "author", "author_role", "author_url", "company",
                                          "role_signal", "job_location", "posted_on", "excerpt", "note")}
            return render_opportunities(request, conn, "leads", lead_error=str(e), lead_form=form, status_code=400)
        finally:
            conn.close()
        return RedirectResponse(f"/opportunities?tab=leads&lead_added={lead_id}&merged={int(not created)}#lead-{lead_id}",
                                status_code=303)

    @app.post("/leads/{lead_id}/decision")
    async def lead_decision(request: Request, lead_id: int):
        field = await read_form(request)
        conn = write_conn()
        try:
            search_leads.record_decision(conn, lead_id, field("decision"), field("note"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            conn.close()
        return RedirectResponse(f"/opportunities?tab=leads#lead-{lead_id}", status_code=303)

    @app.post("/search/run")
    async def start_search(request: Request):
        """Run search now, from My Pipeline or New Opportunities: you come back to the page you pressed it on."""
        field = await read_form(request)
        back = "/pipeline?" if field("back") == "/pipeline" else "/opportunities?tab=verified&"
        conn = db.connect(db_path, readonly=True, create=False)
        try:
            busy = search_pipeline.is_running(conn)
        finally:
            conn.close()
        thread = app.state.search_thread
        if busy or (thread is not None and thread.is_alive()):
            return RedirectResponse(f"{back}run=busy#search", status_code=303)

        def work():
            from app import notify
            c = write_conn()
            try:
                search_pipeline.run_search(c, get=search_get, trigger="web")
                if search_get is None:  # real boards only: tests never send anything
                    notify.notify_new_jobs(c)
                if search_get is None or mail_imap is not None:
                    search_mail.read(c, db_path, mail_imap)
                    search_mail.probe_boards(c, search_get)   # employers named in the alerts: their own boards
            except search_pipeline.RunInProgress:
                pass
            except Exception:  # the run row records the failure; nothing else to do in a background thread
                pass
            finally:
                c.close()

        app.state.search_thread = threading.Thread(target=work, name="search-run", daemon=True)
        app.state.search_thread.start()
        return RedirectResponse(f"{back}run=started#search", status_code=303)

    @app.get("/opportunities/settings", response_class=HTMLResponse)
    def search_settings(request: Request, conn: sqlite3.Connection = Depends(get_conn), saved: str = ""):
        return render_settings(request, conn, saved=saved)

    @app.post("/opportunities/settings/links")
    async def save_my_links(request: Request):
        field = await read_form(request)
        try:
            my_links.save(links_path, {k: field(k) for k in my_links.FIELDS})
        except ValueError as e:
            conn = db.connect(db_path, readonly=True, create=False)
            try:
                return render_settings(request, conn, error=str(e), status_code=400,
                                       links_form={k: field(k) for k in my_links.FIELDS})
            finally:
                conn.close()
        return RedirectResponse("/opportunities/settings?saved=links#links", status_code=303)

    def render_settings(request, conn, error: str = "", saved: str = "", form: dict | None = None, status_code: int = 200,
                        links_form: dict | None = None):
        version_id, prof = search_pipeline.current_profile(conn)
        status = search_pipeline.verification_status(conn)
        rows = []
        for s in conn.execute("SELECT * FROM search_sources ORDER BY enabled DESC, lower(company)"):
            ok, last, _ = status.get(s["id"], (None, None, True))
            rows.append({"s": s, "last": last, "ok": ok})
        mail_cfg = search_mail.load_config(mail_path)
        return render(request, "settings.html", status_code=status_code, active="opportunities", profile=form or prof,
                      mailboxes=[address for address, _ in mail_cfg["mailboxes"]], mail_days=mail_cfg["days"],
                      mail_reads={r["mailbox"]: r for r in search_mail.last_reads(conn)}, regions=search_profile.REGIONS,
                      links=links_form if links_form is not None else my_links.load(links_path), link_fields=my_links.FIELDS,
                      versions=conn.execute("SELECT count(*) FROM search_profile_versions").fetchone()[0],
                      sources=rows, error=error, saved=saved)

    @app.post("/opportunities/settings/mail")
    async def save_mailbox(request: Request):
        """You type the app password here yourself; it is written to data/mail.env (yours only) and never shown again."""
        field = await read_form(request)
        conn = write_conn()
        try:
            if field("remove"):
                search_mail.remove_mailbox(mail_path, field("remove"))
            else:
                search_mail.save_mailbox(mail_path, field("address"), field("password"))
        except ValueError as e:
            return render_settings(request, conn, error=str(e), status_code=400)
        finally:
            conn.close()
        return RedirectResponse(f"/opportunities/settings?saved={'mail_removed' if field('remove') else 'mail'}#mail",
                                status_code=303)

    @app.post("/opportunities/settings/profile")
    async def save_search_profile(request: Request):
        field = await read_form(request)
        raw = {k: field(k) for k in ("include_terms", "exclude_terms", "low_priority_terms", "exclude_description_terms",
                                     "description_min_hits", "agency_companies", "software_title_terms",
                                     "focus_level_terms", "above_level_terms", "junior_level_terms",
                                     "overqualified_max_years", "exclude_places",
                                     "fresh_days", "max_age_days")}
        conn = write_conn()
        try:
            current = search_pipeline.current_profile(conn)[1]  # what the form does not edit stays as it is
            raw |= {k: current[k] for k in ("filters_off", "requirement_terms") if k in current}
            # regions: the boxes you leave ticked are wanted (a form without the boxes keeps them as they were)
            raw["exclude_regions"] = ([k for k in search_profile.REGIONS if k not in field.all("regions")]
                                      if field("regions_form") else current.get("exclude_regions", []))
            search_pipeline.save_profile(conn, raw)
        except ValueError as e:
            return render_settings(request, conn, error=str(e), form={**raw, **{k: raw[k].splitlines() for k in
                                   ("include_terms", "exclude_terms", "low_priority_terms", "exclude_description_terms",
                                    "agency_companies")}},
                                   status_code=400)
        finally:
            conn.close()
        return RedirectResponse("/opportunities/settings?saved=profile", status_code=303)

    @app.post("/opportunities/sources")
    async def add_search_source(request: Request):
        field = await read_form(request)
        conn = write_conn()
        try:
            search_pipeline.add_source(conn, field("link"), field("company"))
        except ValueError as e:
            return render_settings(request, conn, error=str(e), status_code=400)
        finally:
            conn.close()
        return RedirectResponse("/opportunities/settings?saved=source#sources", status_code=303)

    @app.post("/opportunities/sources/{source_id}/toggle")
    async def toggle_search_source(request: Request, source_id: int):
        field = await read_form(request)
        conn = write_conn()
        try:
            search_pipeline.set_source_enabled(conn, source_id, field("enabled") == "1")
        except ValueError as e:
            raise HTTPException(404, str(e))
        finally:
            conn.close()
        return RedirectResponse(f"/opportunities/settings#source-{source_id}", status_code=303)

    @app.get("/postings/{posting_id}", response_class=HTMLResponse)
    def posting_page(request: Request, posting_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        view = search_pipeline.posting_view(conn, posting_id)
        if view is None:
            raise HTTPException(404)
        history = conn.execute("""SELECT created_at_utc, decision, note FROM decision_events
                                  WHERE subject_type='posting' AND subject_id=? ORDER BY id""", (posting_id,)).fetchall()
        rec = next((r for r in load_records(conn) if r.sheet == CONSOLE and r.row == posting_id), None) \
            if view.ev.decision == "applied_now" else None
        return render(request, "posting.html", active="opportunities", v=view, history=history,
                      hidden=view.ev.decision in search_pipeline.HIDING, rec=rec,
                      status_history=tracker.status_history(conn, posting_id, tracker.CONSOLE_SUBJECT) if rec else [])

    @app.get("/diagnostics/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        run = conn.execute("SELECT * FROM search_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(404)
        checks = conn.execute("""SELECT c.*, s.company, s.provider, s.board FROM source_checks c
                                 JOIN search_sources s ON s.id=c.source_id WHERE c.run_id=? ORDER BY c.ok, lower(s.company)""",
                              (run_id,)).fetchall()
        candidates = conn.execute("""SELECT c.*, p.title, p.location, p.url, s.company FROM run_candidates c
                                     JOIN postings p ON p.id=c.posting_id JOIN search_sources s ON s.id=p.source_id
                                     WHERE c.run_id=? ORDER BY lower(s.company), p.title""", (run_id,)).fetchall()
        rows = [{**dict(c), "reasons": json.loads(c["reasons_json"])} for c in candidates]
        funnel = {
            "presented": [r for r in rows if r["presented"]],
            "geo_review": [r for r in rows if r["relevance"] == "MATCH" and r["geo"] == "UNKNOWN" and not r["presented"]],
            "outside": [r for r in rows if r["relevance"] == "MATCH" and r["geo"] == "OUTSIDE" and not r["presented"]],
            "history": [r for r in rows if r["dedup"] in ("HISTORY", "POSSIBLE_HISTORY")],
            "excluded": [r for r in rows if r["relevance"] == "EXCLUDED"],
        }
        cost = conn.execute("SELECT coalesce(sum(requests),0) AS requests, coalesce(sum(actual_usd),0) AS usd FROM cost_ledger WHERE run_id=?",
                            (run_id,)).fetchone()
        return render(request, "run.html", active="diagnostics", run=run, checks=checks, funnel=funnel,
                      total=len(rows), no_match=sum(1 for r in rows if r["relevance"] == "NO_MATCH"), cost=cost)

    @app.get("/")
    def home():
        return RedirectResponse("/pipeline", status_code=307)

    def run_state(conn, notice: str = "") -> dict:
        """The search for the run bar on My Pipeline and New Opportunities."""
        thread = app.state.search_thread
        return {"last": conn.execute("SELECT * FROM search_runs WHERE mode='live' ORDER BY id DESC LIMIT 1").fetchone(),
                "running": search_pipeline.is_running(conn) or bool(thread and thread.is_alive()),
                "notice": notice if notice in ("started", "busy") else "",
                "boards": conn.execute("SELECT count(*) FROM search_sources WHERE enabled=1").fetchone()[0]}

    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline(request: Request, q: str = "", group: str = "all", source: str = "all", sort: str = "updated",
                 review: str = "", updated: int | None = None, prev: str = "", run: str = "", level: str = "all",
                 conn: sqlite3.Connection = Depends(get_conn)):
        return render_pipeline(request, conn, q, group, source, sort, review, updated, prev, run, level=level)

    def render_pipeline(request, conn, q="", group="all", source="all", sort="updated", review="", updated=None, prev="",
                        run="", quick_form=None, quick_error="", status_code=200, level="all"):
        group = group if group in GROUPS or group == "all" else "all"
        level = level if level in LEVELS else "all"
        source = source if source in SOURCES else "all"
        sort = sort if sort in SORTS else "updated"
        all_records = load_records(conn)
        total = len(all_records)
        new_jobs = search_pipeline.opportunities(conn)["verified"]
        overview = pipeline_overview(all_records, len(new_jobs), len(search_leads.lead_views(conn)["shown"]),
                                     sum(1 for v in new_jobs if v.is_new), len(search_mail.views(conn)["shown"]))
        records, counts = filter_records(all_records, q, group, source, sort, review, level)
        terms = q.casefold().split()
        notice = next((r for r in all_records if r.id == updated), None) if updated is not None else None

        def link(**change):
            params = {"q": q, "group": group, "source": source, "sort": sort, "review": review, "level": level} | change
            defaults = {"group": "all", "source": "all", "sort": "updated", "level": "all"}
            return "/pipeline?" + urlencode({k: v for k, v in params.items() if v and defaults.get(k) != v})

        return render(request, "pipeline.html", status_code=status_code, active="pipeline", records=records, total=total,
                      counts=counts, overview=overview, quick_form=quick_form or {}, quick_error=quick_error,
                      quick_start=quick.START,
                      q=q, group=group, source=source, sort=sort, review=review, sources=SOURCES, sorts=SORTS,
                      level=level, levels=LEVELS,
                      link=link, filtered=bool(terms or source != "all" or review), notice=notice, prev=prev,
                      list_url=link(), search_run=run_state(conn, run))

    def safe_back(value: str) -> str:
        """Only pages of this console: My Pipeline, or a job you came from (its list or its page)."""
        ok = value.startswith(("/pipeline", "/opportunities", "/postings/")) and "//" not in value and "\\" not in value
        return value if ok else "/pipeline"

    @app.post("/added")
    async def quick_add(request: Request):
        """A link and a few words (pasted or dictated) become a record in My Pipeline."""
        field = await read_form(request)
        form = {k: field(k) for k in ("url", "text", "company", "role", "state")}
        conn = write_conn()
        try:
            added_id = quick.add(conn, form["url"], form["text"], form["state"] or "APPLIED", form["company"],
                                 form["role"], profile=search_pipeline.current_profile(conn)[1])
        except ValueError as e:
            return render_pipeline(request, conn, quick_form=form, quick_error=str(e), status_code=400)
        finally:
            conn.close()
        return RedirectResponse(f"/added/{added_id}?new=1", status_code=303)

    @app.get("/added/{added_id}", response_class=HTMLResponse)
    def added_record(request: Request, added_id: int, new: int = 0, conn: sqlite3.Connection = Depends(get_conn)):
        a = next((a for a in quick.load(conn) if a.id == added_id), None)
        if a is None:
            raise HTTPException(404)
        rec = next(r for r in load_records(conn) if r.sheet == quick.SHEET and r.row == added_id)
        # the same position from other alerts (another city, another job number): their links stay with the record
        key, role = search_pipeline.company_name_key(a.company), search_pipeline.core_title(a.role)
        also = [j for j in conn.execute("SELECT url, location, company, title FROM mail_jobs WHERE url <> ?", (a.url or "",))
                if search_pipeline.company_name_key(j["company"]) == key and search_pipeline.core_title(j["title"]) == role]
        return render(request, "added.html", active="pipeline", a=a, rec=rec, new=bool(new), also=also[:4],
                      history=[e for e in a.history])

    @app.post("/added/{added_id}/status")
    async def added_status(request: Request, added_id: int):
        field = await read_form(request)
        conn = write_conn()
        try:
            quick.set_status(conn, added_id, field("state"), field("note"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            conn.close()
        return RedirectResponse(f"/added/{added_id}#status", status_code=303)

    @app.post("/added/{added_id}/edit")
    async def added_edit(request: Request, added_id: int):
        field = await read_form(request)
        conn = write_conn()
        try:
            quick.edit(conn, added_id, **{k: field(k) for k in quick.EDITABLE})
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            conn.close()
        return RedirectResponse(f"/added/{added_id}", status_code=303)

    @app.post("/records/{record_id}/status")
    async def update_status(request: Request, record_id: int):
        field = await read_form(request)
        came_from = safe_back(field("back", "/pipeline"))
        back = came_from.split("#")[0]
        params = dict(parse_qsl(urlsplit(back).query))
        conn = write_conn()
        try:
            before_all = load_records(conn)
            previous = next((r for r in before_all if r.id == record_id), None)
            if previous is None:
                raise HTTPException(404)
            listed = lambda recs: [r.id for r in filter_records(recs, params.get("q", ""), params.get("group", "all"),
                                                                 params.get("source", "all"), params.get("sort", "updated"),
                                                                 params.get("review", ""), params.get("level", "all"))[0]]
            before = listed(before_all)
            if previous.sheet == CONSOLE:     # an application you marked in the console: its posting id
                tracker.record_console_status(conn, previous.row, field("state"), field("note"))
            else:
                tracker.record_status(conn, record_id, field("state"), field("note"))
            after = listed(load_records(conn))
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            conn.close()
        if not back.startswith("/pipeline"):  # opened from a job card: back to that job, which shows the new status
            return RedirectResponse(came_from, status_code=303)
        prev = previous.change.state if previous.change and previous.change.source == "you" else tracker.TRACKER
        if record_id in after:
            anchor = f"rec-{record_id}"
        else:
            following = before[before.index(record_id) + 1:] if record_id in before else []
            anchor = next((f"rec-{i}" for i in following if i in after), "list")
        sep = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{sep}updated={record_id}&prev={prev}#{anchor}", status_code=303)

    @app.get("/records/{record_id}", response_class=HTMLResponse)
    def record(request: Request, record_id: int, back: str = "/pipeline", conn: sqlite3.Connection = Depends(get_conn)):
        items = review.review_items(conn)
        records = load_records(conn, review.open_rows(items))
        rec = next((r for r in records if r.id == record_id), None)
        if rec is None:
            raise HTTPException(404)
        header = json.loads(conn.execute("SELECT values_json FROM legacy_rows WHERE sheet_name=? AND source_row=1",
                                         (rec.sheet,)).fetchone()[0])
        fields = []
        for i, (name, value) in enumerate(zip(header, rec.values)):
            date = converted_date(value) if rec.sheet == MASTER and i in MASTER_DATE_COLUMNS else None
            text = "" if value is None else str(value)
            fields.append({"name": name or f"Column {i + 1}", "text": text, "date": date,
                           "not_a_date": rec.sheet == MASTER and i in MASTER_DATE_COLUMNS and text and not date,
                           "is_url": text.startswith(("http://", "https://")), "private": i == M_CONTACT and rec.sheet == MASTER})
        linked = [r for r in records if rec.job_id and r.job_id == rec.job_id and r.id != rec.id]
        by_id = {r.id: r for r in records}
        q = lambda sql: conn.execute(sql, (rec.id,)).fetchall()
        return render(
            request, "record.html", active="pipeline", rec=rec, fields=fields, linked=linked,
            items=[i for i in items if rec.id in i.row_ids], by_id=by_id,
            applications=q("""SELECT a.state, a.applied_on, a.application_type, a.review_required, a.current_status_raw
                              FROM applications a JOIN application_sources s ON s.application_id=a.id
                              WHERE s.legacy_row_id=?"""),
            events=q("SELECT event_type, event_text, event_time, provenance FROM history_events WHERE legacy_row_id=?"),
            contacts=q("""SELECT m.raw_contact, c.display_name FROM contact_mentions m
                          LEFT JOIN contacts c ON c.id=m.contact_id WHERE m.legacy_row_id=?"""),
            row_sha=conn.execute("SELECT row_sha256 FROM legacy_rows WHERE id=?", (rec.id,)).fetchone()[0],
            back=safe_back(back), status_history=tracker.status_history(conn, rec.id),
            snapshot=conn.execute("SELECT * FROM source_snapshots WHERE id=1").fetchone())

    @app.get("/opportunities", response_class=HTMLResponse)
    def opportunities(request: Request, tab: str = "verified", show: str = "", run: str = "", done: int | None = None,
                      noted: int | None = None, marked: int | None = None, lead_added: int | None = None, merged: int = 0,
                      filters: str = "", conn: sqlite3.Connection = Depends(get_conn)):
        return render_opportunities(request, conn, tab, show=show, run=run, done=done or noted or marked,
                                    noted=bool(noted), marked=bool(marked),
                                    lead_added=lead_added, merged=merged, filters_open=filters == "open")

    def render_opportunities(request, conn, tab, show="", run="", done=None, noted=False, lead_added=None, merged=0, marked=False,
                             lead_error="", lead_form=None, status_code=200, filters_open=False):
        tab = tab if tab in ("verified", "leads", "mail") else "verified"
        views = search_pipeline.opportunities(conn)
        mail_views = search_mail.views(conn)
        lead_lists = search_leads.lead_views(conn)
        index = search_official.OfficialIndex(conn, search_pipeline.current_profile(conn)[1]["agency_companies"])
        lead_official = {v.lead["id"]: index.find(v.lead["company"], v.lead["role_signal"])
                         for group in lead_lists.values() for v in group if v.lead["company"]}
        search_run = run_state(conn, run)
        notice = search_pipeline.posting_view(conn, done) if done and tab == "verified" else None
        prof = search_pipeline.current_profile(conn)[1]
        filters = [{"key": key, "label": label.format(max_age=prof.get("max_age_days", 14)), "hint": hint,
                    "on": key not in prof.get("filters_off", []), "count": views["filter_counts"][key]}
                   for key, (label, hint, _) in search_profile.FILTERS.items()]
        return render(request, "opportunities.html", status_code=status_code, active="opportunities", tab=tab, views=views,
                      shown=views[show] if show in ("hidden", "older") else views["verified"], showing_hidden=show == "hidden",
                      showing=show if show in ("hidden", "older") else "",
                      max_age=search_pipeline.current_profile(conn)[1].get("max_age_days", 14),
                      verified_count=len(views["verified"]), leads_count=len(lead_lists["shown"]), leads=lead_lists,
                      lead_links=search_leads.search_links(), lead_added=lead_added, merged=merged, lead_official=lead_official,
                      lead_error=lead_error, lead_form=lead_form or {}, show_hidden_leads=show == "hidden",
                      last_run=search_run["last"], search_run=search_run, notice=notice, noted=noted, marked=marked,
                      mail=mail_views, mail_count=len(mail_views["shown"]), mail_reads=search_mail.last_reads(conn),
                      mail_boxes=bool(search_mail.load_config(mail_path)["mailboxes"]), mail_held=search_mail.HELD,
                      mail_decisions=search_mail.DECISIONS, mail_done=done if tab == "mail" else None,
                      mail_replies=search_replies.recent(conn) if tab == "mail" else [],
                      filters=filters, filters_open=filters_open,
                      boards_off=conn.execute("SELECT count(*) FROM search_sources WHERE enabled=0").fetchone()[0])

    @app.get("/diagnostics", response_class=HTMLResponse)
    def diagnostics(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        count = lambda t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        items = review.review_items(conn)
        records = {r.id: r for r in load_records(conn, review.open_rows(items))}
        queue = {"decide": [i for i in items if i.is_open and i.needs_attention],
                 "info": [i for i in items if i.is_open and not i.needs_attention],
                 "done": [i for i in items if not i.is_open]}
        mapping = Counter((r.status_raw, r.state) for r in records.values() if r.sheet == MASTER)
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        return render(
            request, "diagnostics.html", active="diagnostics",
            integrity=conn.execute("PRAGMA integrity_check").fetchone()[0], fk_violations=len(fk),
            schema_version=db.current_version(conn), latest_version=db.latest_version(),
            journal=conn.execute("PRAGMA journal_mode").fetchone()[0],
            snapshot=conn.execute("SELECT * FROM source_snapshots WHERE id=1").fetchone(),
            batch=conn.execute("SELECT * FROM import_batches ORDER BY id DESC LIMIT 1").fetchone(),
            stats={"master": sum(1 for r in records.values() if r.sheet == MASTER),
                   "comeet": sum(1 for r in records.values() if r.sheet == COMEET),
                   "jobs": count("jobs"), "applications": count("applications"), "companies": count("companies"),
                   "contact_mentions": count("contact_mentions"), "rules": count("screening_rules"),
                   "legacy": count("legacy_rows")},
            queue=queue, by_id=records, decisions_logged=count("decision_events"),
            runs=conn.execute("SELECT * FROM search_runs WHERE mode='live' ORDER BY id DESC LIMIT 10").fetchall(),
            lead_lists=search_leads.lead_views(conn), feedback=search_pipeline.feedback_summary(conn),
            mapping=sorted(mapping.items(), key=lambda kv: (tone(kv[0][1]), kv[0][1] or "", -kv[1], kv[0][0])),
            rules=conn.execute("SELECT rule_name, guidance FROM screening_rules ORDER BY id").fetchall())

    return app
