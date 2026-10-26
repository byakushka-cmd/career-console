"""A search run: Discovery -> Deduplication -> Verification -> Relevance -> Presentation.

Deterministic and free: official ATS boards only, no AI, no paid API. Every posting seen is stored,
and every candidate's path through the gates is logged with reasons before anything is filtered.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo

from app import db, legacy_import
from app.config import DISPLAY_TZ, local_time
from search import agencies, official, similar
from search import profile as gates
from search import sources

# Tracker states (provisional, from the imported text) and what an exact match means for a new posting.
HISTORY_STATES = {"APPLIED", "IN_PROCESS", "INTERVIEW", "OUTREACH", "REJECTED", "SKIPPED", "DUPLICATE",
                  "CONTACT_ONLY", "PROFILE_ONLY", "EXPIRED"}  # you acted on it (or let it pass): never "new" again
REOPEN_STATES = {"CLOSED", "HOLD", "BLOCKED"}             # shown again only because the official board lists it
# "already_applied" (stored name kept) = Duplicate: you have seen it already (in your tracker, in this list or
# elsewhere); the search brought it again, a duplicate it missed.
# "applied_now" = a good find you applied to: it becomes an application in My Pipeline.
# "skipped" (stored name kept) = Wrong match: the search should not have brought it (a search error).
# "declined" = Not for me: the right kind of job, but you decided not to apply (not a search error).
POSTING_DECISIONS = {"saved": "Saved", "maybe": "Maybe", "applied_now": "Good find — applied",
                     "already_applied": "Duplicate", "skipped": "Wrong match", "declined": "Not for me",
                     "hidden": "Hidden", "show_anyway": "Shown by your override", "restored": "Restored", "comment": "Note",
                     "overqualified": "Overqualified", "overqualified_off": "Not overqualified"}
HIDING = {"skipped", "declined", "hidden", "already_applied", "applied_now"}
# already in your tracker, decided by you, or the same text as a job you decided
KNOWN = {"HISTORY", "POSSIBLE_HISTORY", "REOPENED", "KNOWN_NOT_APPLIED", "SAME_TEXT"}
MARK_STATE = {"applied_now": "APPLIED", "already_applied": "DUPLICATE", "skipped": "SKIPPED", "declined": "DECLINED",
              "hidden": "HIDDEN"}
NOTE_ONLY = "comment"  # a note that does not change the decision
# Marks you set beside your decision (they never change it, never hide a job): "this job is below my level".
OVERQUALIFIED_MARKS = ("overqualified", "overqualified_off")
NOT_DECISIONS = (NOTE_ONLY, *OVERQUALIFIED_MARKS)
# One-tap reasons, stored as the decision's note. Wrong match tells which rule to tighten; Not for me does not.
WRONG_MATCH_REASONS = ["Regulatory / medical device", "Hardware / embedded", "Manufacturing / QC", "Not really QA",
                       "Too junior or manual"]
NOT_FOR_ME_REASONS = ["Overqualified", "Salary", "Seniority or scope", "Location / commute", "Company", "Applied there before"]
STALE_RUN_MINUTES = 30
CACHE_MINUTES = 10          # a board checked successfully this recently is not fetched again (repeat clicks)
VERIFIED_MAX_AGE_DAYS = 7   # older than this without a successful check: no longer shown as verified
POLITE_DELAY_S = 1.0


class RunInProgress(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def local_day(stamp: str | datetime | None) -> str | None:
    """The Israel calendar date of a timestamp (ISO text or datetime); None stays None."""
    if not stamp:
        return None
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if isinstance(stamp, str) else stamp
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(ZoneInfo(DISPLAY_TZ)).date().isoformat()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---- profile and sources -----------------------------------------------------------------

def current_profile(conn: sqlite3.Connection) -> tuple[int | None, dict]:
    row = conn.execute("SELECT id, profile_json FROM search_profile_versions ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None, dict(gates.DEFAULT_PROFILE)
    return row["id"], {**gates.DEFAULT_PROFILE, **json.loads(row["profile_json"])}


def save_profile(conn: sqlite3.Connection, raw: dict, origin: str = "web") -> int:
    clean = gates.validate_profile(raw)
    with db.transaction(conn):
        return conn.execute("INSERT INTO search_profile_versions(created_at_utc, profile_json, origin) VALUES (?, ?, ?)",
                            (_iso(_now()), json.dumps(clean, ensure_ascii=False), origin)).lastrowid


def ensure_defaults(conn: sqlite3.Connection) -> dict:
    """Seed the default profile and the boards already linked in the tracker. Idempotent; call inside a transaction."""
    added_profile = 0
    if not conn.execute("SELECT 1 FROM search_profile_versions").fetchone():
        conn.execute("INSERT INTO search_profile_versions(created_at_utc, profile_json, origin) VALUES (?, ?, ?)",
                     (_iso(_now()), json.dumps(gates.DEFAULT_PROFILE, ensure_ascii=False), "default: tracker screening rules"))
        added_profile = 1
    added_sources = 0
    for r in conn.execute("""SELECT values_json FROM legacy_rows
                             WHERE sheet_name IN (?, ?) AND source_row > 1 ORDER BY id""",
                          (legacy_import.MASTER, legacy_import.COMEET)).fetchall():
        values = json.loads(r["values_json"])
        text = " ".join(str(v) for v in values if v)
        for provider, board in sources.find_boards(text):
            cur = conn.execute("""INSERT OR IGNORE INTO search_sources(provider, board, company, enabled, origin, added_at_utc)
                                  VALUES (?, ?, ?, 1, 'tracker link', ?)""",
                               (provider, board, str(values[0] or board), _iso(_now())))
            added_sources += cur.rowcount
    return {"profile": added_profile, "sources": added_sources}


def collect_board_links(conn: sqlite3.Connection) -> int:
    """Every Greenhouse, Lever, Ashby or Comeet link you saved in the console (a lead, a note on a job or record)
    adds that employer's board to the search. A board you turned off stays off. Returns the number added."""
    texts = [(" ".join(filter(None, [r["post_url"], r["author_url"], r["excerpt"]])), r["company"])
             for r in conn.execute("SELECT post_url, author_url, excerpt, company FROM leads")]
    texts += [(r["note"], "") for r in conn.execute("SELECT note FROM lead_observations WHERE note <> ''")]
    texts += [(r["note"], "") for r in conn.execute("SELECT note FROM decision_events WHERE note LIKE '%http%'")]
    added = 0
    with db.transaction(conn):
        for text, company in texts:
            for provider, board in sources.find_boards(text):
                added += conn.execute("""INSERT OR IGNORE INTO search_sources(provider, board, company, enabled, origin, added_at_utc)
                                         VALUES (?, ?, ?, 1, 'a link you saved', ?)""",
                                      (provider, board, (company or board.split("/")[0])[:120], _iso(_now()))).rowcount
    return added


def add_source(conn: sqlite3.Connection, url_or_board: str, company: str, provider: str | None = None) -> int:
    detected = sources.detect_board(url_or_board) if provider is None else (provider, url_or_board.strip())
    if not detected or detected[0] not in sources.FETCHERS:
        raise ValueError("Paste a Greenhouse, Lever, Ashby or Comeet job or board link")
    provider, board = detected
    company = company.strip() or board.split("/")[0]
    with db.transaction(conn):
        existing = conn.execute("SELECT id FROM search_sources WHERE provider=? AND board=?", (provider, board)).fetchone()
        if existing:
            conn.execute("UPDATE search_sources SET enabled=1 WHERE id=?", (existing["id"],))
            return existing["id"]
        return conn.execute("""INSERT INTO search_sources(provider, board, company, enabled, origin, added_at_utc)
                               VALUES (?, ?, ?, 1, 'added by you', ?)""", (provider, board, company[:120], _iso(_now()))).lastrowid


def set_source_enabled(conn: sqlite3.Connection, source_id: int, enabled: bool) -> None:
    with db.transaction(conn):
        if not conn.execute("UPDATE search_sources SET enabled=? WHERE id=?", (int(enabled), source_id)).rowcount:
            raise ValueError("Unknown source")


# ---- history: what the tracker already knows ---------------------------------------------

CONSOLE = "Career Console"  # records created in the console itself (e.g. "Already applied" on a posting)
ADDED = "Added in console"  # records you added yourself (app/added.py): a link and a few words
SHEET_CODE = {legacy_import.MASTER: "MT", legacy_import.COMEET: "CI", CONSOLE: "CC", ADDED: "AD"}


@dataclass
class TrackerHit:
    legacy_row_id: int | None
    sheet: str
    row: int
    state: str
    role: str = ""
    applied: str | None = None
    acted_on: str | None = None   # your last action on it (date): a later publication means "posted again"

    @property
    def ref(self) -> str:
        return f"{SHEET_CODE.get(self.sheet, self.sheet)} {self.row}"

    @property
    def href(self) -> str:
        if self.sheet == ADDED:
            return f"/added/{self.row}"
        return f"/records/{self.legacy_row_id}" if self.legacy_row_id is not None else f"/postings/{self.row}"


# Order in "Earlier at this company": what matters most for applying again comes first.
EARLIER_ORDER = {"REJECTED": 0, "INTERVIEW": 1, "IN_PROCESS": 1, "APPLIED": 2, "OUTREACH": 2}
APPLIED_STATES = frozenset(EARLIER_ORDER)   # you sent a CV (whatever happened after)
LOWER_LEVELS = ("ENGINEER", "JUNIOR")


def lead_applications(records, profile: dict) -> list:
    """Your rule for staffing agencies (user, 2026-10-15): once you applied through an agency at your level (team
    lead, manager), you do not apply through it again as an engineer: the recruiter would see you go down a level.
    -> the applications among `records` (anything with .state, .role, .applied) at your level, latest first."""
    found = [r for r in records if r.state in APPLIED_STATES and gates.level(r.role, profile) == "FOCUS"]
    return sorted(found, key=lambda r: r.applied or "", reverse=True)


def company_name_key(company: str) -> str:
    """The employer part of a company name: 'Dialog / confidential fintech' -> 'dialog'."""
    return legacy_import.normalize(str(company or "").split(" / ")[0])


def core_title(title: str) -> str:
    """The role without the notes the tracker adds after it:
    'QA / E2E Engineer — LinkedIn #4474683430' -> 'qa / e2e engineer', 'QA Lead (Req 5)' -> 'qa lead'."""
    return legacy_import.normalize(re.split(r"\s[—–-]\s|\(", str(title or ""), maxsplit=1)[0])


def title_keys(company: str, title: str) -> set[tuple[str, str]]:
    who = company_name_key(company)
    return {(who, legacy_import.normalize(title)), (who, core_title(title))} - {(who, "")}


@dataclass
class History:
    by_identity: dict = field(default_factory=lambda: defaultdict(list))
    by_title: dict = field(default_factory=lambda: defaultdict(list))
    by_company: dict = field(default_factory=lambda: defaultdict(list))   # every record at a company
    copies: dict = field(default_factory=lambda: defaultdict(list))       # company -> jobs you decided, with their text
    by_agency: dict = field(default_factory=lambda: defaultdict(list))    # staffing agency -> every record through it
    agency_terms: list = field(default_factory=list)
    profile: dict = field(default_factory=dict)

    def applied_as_lead(self, company: str, level: str) -> list[TrackerHit]:
        """For an engineer or junior job at a staffing agency: your applications through it at your level."""
        agency = agencies.agency_key(company, self.agency_terms)
        if not agency or level not in LOWER_LEVELS:
            return []
        return lead_applications(self.by_agency.get(agency, []), self.profile)

    def copy_of(self, company: str, posting) -> tuple[TrackerHit, float] | None:
        """A job you already decided at this company that is the same position: the same level, and the same role
        text (or very close, with a similar title). Another city or a reworded title does not make a new job;
        a lead version of an engineer job is a new job."""
        lvl = gates.level(posting["title"], self.profile)
        best = None
        for posting_id, sha, text, title, hit in self.copies.get(company_name_key(company), []):
            if posting_id == posting["id"] or gates.level(title, self.profile) != lvl:
                continue
            score = similar.role_similarity(posting["description_sha"], posting["description"] or "", sha, text)
            if similar.same_position(score, official.title_similarity(posting["title"], title)) and (
                    best is None or score > best[1]):
                best = (hit, score)
        return best

    def at_company(self, company: str, exclude_posting: int | None = None) -> list[TrackerHit]:
        """Your records at this company, most telling first (a rejection, then an ongoing process, then
        applications). Empty for staffing agencies: their records are about many different clients."""
        if agencies.agency_key(company, self.agency_terms):
            return []
        hits = [h for h in self.by_company.get(company_name_key(company), [])
                if not (h.legacy_row_id is None and h.row == exclude_posting)]
        hits.sort(key=lambda h: h.applied or "", reverse=True)  # latest application first within each kind
        return sorted(hits, key=lambda h: EARLIER_ORDER.get(h.state, 3))

    def classify(self, identity: str, company: str, title: str, requisition_id: str | None,
                 agency: str | None = None, numbers: list[str] | None = None):
        """-> (dedup verdict, tracker hits, explanation). For agencies a vacancy is its number, not its title."""
        exact = list(self.by_identity.get(identity, []))
        if requisition_id:
            exact += self.by_identity.get(f"req:{legacy_import.normalize(company)}:{requisition_id.strip().lower()}", [])
        who = agencies.company_key(company, [agency] if agency else [])
        for number in numbers or []:
            exact += self.by_identity.get(f"vac:{who}:{number}", [])
        similar = list({id(h): h for key in title_keys(company, title) for h in self.by_title.get(key, [])}.values())
        if agency and not exact and similar:
            rows = ", ".join(f"{SHEET_CODE.get(h.sheet, h.sheet)} {h.row} ({h.state.lower()})" for h in similar)
            return "AGENCY_SIMILAR", similar, (f"{agency} lists many jobs with this title: same title as tracker {rows}"
                                               " — compare the job ID before applying")
        # an application or rejection of the same title outweighs an ID match that was only on hold or closed
        # (not for agencies, whose titles repeat across different vacancies)
        if (not agency and exact and not ({h.state for h in exact} & HISTORY_STATES)
                and {h.state for h in similar} & HISTORY_STATES):
            return self._verdict(similar, exact=False)
        if exact:
            return self._verdict(exact, exact=True)
        if similar:
            return self._verdict(similar, exact=False)
        n = len(self.by_company.get(company_name_key(company), []))
        return "NEW", [], (f"new requisition; {n} other record(s) at this company in your tracker" if n else "new")

    @staticmethod
    def _verdict(hits: list[TrackerHit], exact: bool):
        rows = ", ".join(f"{SHEET_CODE.get(h.sheet, h.sheet)} {h.row} ({h.state.lower()})" for h in hits)
        states = {h.state for h in hits}
        if states & HISTORY_STATES:
            return ("HISTORY" if exact else "POSSIBLE_HISTORY"), hits, \
                f"{'same requisition' if exact else 'same company and title'} as tracker {rows}"
        if states & REOPEN_STATES:
            return "REOPENED", hits, f"was closed/on hold in tracker {rows}; the official board lists it again"
        return "KNOWN_NOT_APPLIED", hits, f"also in your tracker, not applied: {rows}"


def tracker_vacancy_numbers(sheet: str, values: list, note: str = "") -> list[str]:
    """Vacancy numbers of a tracker record: its title, link and evidence columns, and your status note.
    Free-text history notes are left out: they often mention other vacancies."""
    cols = (1, 14, 15) if sheet == legacy_import.MASTER else (1, 10)
    return agencies.vacancy_numbers(" ".join(str(values[i]) for i in cols if i < len(values) and values[i]) + " " + note)


def history_index(conn: sqlite3.Connection, on: date | None = None) -> History:
    """What the tracker and your decisions already know. `on` is today (records not applied to within
    two weeks count as expired)."""
    from app import tracker  # your status updates override the tracker's own reading
    manual = tracker.manual_statuses(conn)
    agency_terms = current_profile(conn)[1]["agency_companies"]
    h = History(agency_terms=agency_terms, profile=current_profile(conn)[1])
    for r in conn.execute("""SELECT l.id, l.sheet_name, l.source_row, l.values_json, s.provisional_state
                             FROM legacy_rows l JOIN legacy_row_states s ON s.legacy_row_id = l.id
                             WHERE l.sheet_name IN (?, ?)""", (legacy_import.MASTER, legacy_import.COMEET)):
        values = json.loads(r["values_json"])
        change = manual.get(r["id"])
        moved = tracker.moved_on(r["sheet_name"], values, change)
        state = tracker.effective_state(change.state if change else r["provisional_state"], moved, on)
        applied = legacy_import.converted_date(values[2]) if r["sheet_name"] == legacy_import.MASTER else None
        hit = TrackerHit(r["id"], r["sheet_name"], r["source_row"], state, str(values[1] or ""), applied, moved)
        text = " ".join(str(v) for v in values if v)
        keys = set(sources.job_identities(text))
        if req := legacy_import.req_identity(values[0], values[1]):
            keys.add(req)
        note = manual[r["id"]].note if r["id"] in manual else ""
        who = agencies.company_key(str(values[0] or ""), agency_terms)
        keys |= {f"vac:{who}:{n}" for n in tracker_vacancy_numbers(r["sheet_name"], values, note)}
        for key in keys:
            h.by_identity[key].append(hit)
        if values[0] and values[1]:
            for key in title_keys(values[0], values[1]):
                h.by_title[key].append(hit)
        if r["source_row"] > 1 and values[0]:
            h.by_company[company_name_key(values[0])].append(hit)
            if agency := agencies.agency_key(str(values[0]), agency_terms):
                h.by_agency[agency].append(hit)
    # Records you added yourself (a link and a few words) are part of your tracker like the imported ones.
    from app import added
    for a in added.load(conn, on):
        hit = TrackerHit(None, ADDED, a.id, a.state, a.role, a.applied, a.moved)
        keys = set(sources.job_identities(f"{a.url or ''} {a.pasted}"))
        if req := legacy_import.req_identity(a.company, a.role):
            keys.add(req)
        who = agencies.company_key(a.company, agency_terms)
        keys |= {f"vac:{who}:{n}" for n in a.job_ids}
        for key in keys:
            h.by_identity[key].append(hit)
        for key in title_keys(a.company, a.role):
            h.by_title[key].append(hit)
        h.by_company[company_name_key(a.company)].append(hit)
        if agency := agencies.agency_key(a.company, agency_terms):
            h.by_agency[agency].append(hit)
    # Postings you applied to, or marked as already in your tracker, count as history: a repost is not new.
    from app import tracker   # late import: app depends on search
    console = tracker.manual_statuses(conn, tracker.CONSOLE_SUBJECT)   # e.g. Rejected by them, set on My Pipeline
    for p in marked_postings(conn, ("applied_now", "already_applied")):
        applied_now = p["decision"] == "applied_now"
        on = local_time(p["applied_at_utc"])[:10]
        change = console.get(p["id"]) if applied_now else None
        hit = TrackerHit(None, CONSOLE, p["id"], change.state if change else "APPLIED" if applied_now else "DUPLICATE",
                         p["title"], on if applied_now else None,
                         local_time(change.at_utc)[:10] if change else on)
        h.by_identity[p["identity"]].append(hit)
        for key in title_keys(p["company"], p["title"]):
            h.by_title[key].append(hit)
        if applied_now:  # a duplicate you marked is already one of the tracker's records
            h.by_company[company_name_key(p["company"])].append(hit)
            if agency := agencies.agency_key(p["company"], agency_terms):
                h.by_agency[agency].append(hit)
    # Every job you decided, with its text: a copy of it (another city, another title) is the same position.
    for p in marked_postings(conn, tuple(MARK_STATE)):
        on = local_time(p["applied_at_utc"])[:10]
        h.copies[company_name_key(p["company"])].append(
            (p["id"], p["description_sha"], p["description"] or "", p["title"],
             TrackerHit(None, CONSOLE, p["id"], MARK_STATE[p["decision"]], p["title"], None, on)))
    return h


def tracker_rows_applied_via_postings(conn: sqlite3.Connection) -> dict[int, tuple[int, str]]:
    """Tracker records with the same official job ID (or requisition ID) as a posting you marked Already applied:
    legacy_row_id -> (posting id, when). Title-only similarity never counts here."""
    applied = applied_postings(conn)
    if not applied:
        return {}
    h = history_index(conn)
    out = {}
    for p in applied:
        keys = [p["identity"]]
        if p["requisition_id"]:
            keys.append(f"req:{legacy_import.normalize(p['company'])}:{p['requisition_id'].strip().lower()}")
        for key in keys:
            for hit in h.by_identity.get(key, []):
                if hit.legacy_row_id is not None:
                    out[hit.legacy_row_id] = (p["id"], p["applied_at_utc"])
    return out


def marked_postings(conn: sqlite3.Connection, decisions: tuple[str, ...]) -> list[sqlite3.Row]:
    """Postings whose current decision (ignoring notes) is one of `decisions`."""
    marks = ",".join("?" * len(decisions))
    return conn.execute(f"""
        SELECT p.*, s.company, d.decision, d.created_at_utc AS applied_at_utc, d.note AS applied_note
        FROM decision_events d
        JOIN (SELECT subject_id, max(id) AS last_id FROM decision_events
              WHERE subject_type='posting' AND decision NOT IN {NOT_DECISIONS} GROUP BY subject_id) latest ON latest.last_id = d.id
        JOIN postings p ON p.id = d.subject_id JOIN search_sources s ON s.id = p.source_id
        WHERE d.decision IN ({marks}) ORDER BY d.id""", decisions).fetchall()


def applied_postings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Good finds you applied to ("applied_now"): they are applications in My Pipeline; Restore takes them out."""
    return marked_postings(conn, ("applied_now",))


# ---- evaluation --------------------------------------------------------------------------

@dataclass
class Evaluation:
    dedup: str
    dedup_note: str
    tracker_hits: list
    verification: str
    verification_note: str
    relevance: gates.Verdict
    matched_terms: list
    low_priority: list
    mentions: list
    geo: gates.Verdict
    decision: str | None
    presented: bool
    freshness: gates.Freshness
    agency: str | None = None
    vacancy: list = field(default_factory=list)
    earlier: list = field(default_factory=list)  # your other records at this company (not this posting's matches)
    published_on: str | None = None   # the employer's posting date (Comeet: its last update)
    recent: bool = True               # published within the last max_age_days (or no date at all)
    last_action: str | None = None    # your latest action on this job: tracker record or decision
    reposted: bool = False            # known, and published again after your last action: still hiring
    level: str = "ENGINEER"           # FOCUS (your level), ENGINEER or ABOVE, from the title: marked, never hidden
    narrow: bool = False              # matched only on quality / validation / verification: check it is software QA
    overqualified: bool = False       # the description asks at most overqualified_max_years of QA experience
    years_asked: list = field(default_factory=list)
    held_by: list = field(default_factory=list)       # the board filters (FILTERS keys) that hold this job back
    let_through: list = field(default_factory=list)   # ...of them, the ones you turned off: why it is shown
    lead_at_agency: list = field(default_factory=list)  # an engineer job at an agency you applied to as a lead

    @property
    def candidate(self) -> bool:
        """A job you search for (a search term in its title), whether or not a filter holds it back."""
        return self.relevance.value == "MATCH" or (self.relevance.value == "EXCLUDED" and bool(self.matched_terms))

    @property
    def reasons(self) -> dict:
        return {"dedup": self.dedup_note, "verification": self.verification_note, "relevance": self.relevance.reason,
                "geo": self.geo.reason, "low_priority": self.low_priority, "mentions": self.mentions,
                "decision": self.decision, "published": self.published_on, "recent": self.recent,
                "reposted": self.reposted}


def evaluate(posting, company: str, prof: dict, history: History, verification: tuple[str, str],
             decision: str | None, now: datetime | None = None, decided_at: str | None = None) -> Evaluation:
    """Gates, deduplication and your decisions for one posting. Shown as new: not known, published within the
    last max_age_days. Shown as reposted: known (tracker or your decision), but published again after your
    last action on it, a sign the employer is still hiring."""
    now = now or _now()
    agency = agencies.agency_key(company, prof.get("agency_companies"))
    numbers = agencies.vacancy_numbers(" ".join(filter(None, [posting["title"],
                                                              f"req {posting['requisition_id']}" if posting["requisition_id"] else ""])))
    for n in agencies.vacancy_numbers((posting["description"] or "")[:3000], marked_only=True):
        if n not in numbers:
            numbers.append(n)
    dedup, hits, dedup_note = history.classify(posting["identity"], company, posting["title"], posting["requisition_id"],
                                               agency, numbers)
    if dedup == "NEW" and not agency and (found := history.copy_of(company, posting)):
        copy, score = found
        dedup, hits = "SAME_TEXT", [copy]
        dedup_note = (f"same position as {copy.ref} “{copy.role}” ({copy.state.lower().replace('_', ' ')}): "
                      f"same level, responsibilities and requirements {round(score * 100)}% alike")
    rel, matched, low, mentions = gates.relevance(posting["title"], prof, posting["description"])
    countries = [c for c in (posting["countries"] or "").split(",") if c]
    geo = gates.geography(posting["location"], countries, bool(posting["remote"]) if posting["remote"] is not None else None,
                          posting["description"])
    verified = verification[0] == "VERIFIED_ACTIONABLE"
    today, max_age = date.fromisoformat(local_day(now)), prof.get("max_age_days", 14)
    within = lambda day: day is None or (today - date.fromisoformat(day)).days <= max_age
    published = local_day(posting["posted_at"] or posting["source_updated_at"])
    recent = within(published)
    # The board filters that hold this job back, and the ones you turned off. A title with none of your search
    # terms is not held back by a filter: it is simply not a job you search for.
    place = gates.excluded_place(posting["location"], prof)
    held = [key for key, hit in (("title", rel.value == "EXCLUDED" and bool(matched) and not mentions),
                                 ("description", rel.value == "EXCLUDED" and bool(mentions)),
                                 ("outside", geo.value == "OUTSIDE"), ("place", bool(place)),
                                 ("geo_unclear", geo.value == "UNKNOWN"),
                                 ("older", not recent), ("known", dedup in KNOWN),
                                 ("junior", gates.asks_junior_years(posting["description"]))) if hit]
    off = set(prof.get("filters_off", []))
    candidate = rel.value == "MATCH" or bool(matched and rel.value == "EXCLUDED")
    let_through = [k for k in held if k in off]
    rel_ok = rel.value == "MATCH" or (candidate and ("title" in let_through or "description" in let_through))
    passes = rel_ok and (geo.value in gates.GEO_ELIGIBLE or "outside" in off and geo.value == "OUTSIDE"
                         or "geo_unclear" in off and geo.value == "UNKNOWN") and (not place or "place" in off) \
        and ("junior" not in held or "junior" in off)
    acted = [h.acted_on for h in hits if h.acted_on]
    if decision in HIDING and decision != "hidden" and decided_at:  # a final decision; Save/Maybe/notes are not
        acted.append(local_day(decided_at))
    last_action = max(acted) if acted else None
    known = dedup in KNOWN or decision in HIDING
    # "published again" needs a publication date: Comeet gives only the time of its last edit, which changes almost
    # daily, so a job you had decided came back every day (and was marked Duplicate again and again)
    republished = local_day(posting["posted_at"]) if posting["posted_at"] else None
    reposted = bool(known and decision != "hidden" and recent and republished and last_action and republished > last_action)
    if decision == "show_anyway":
        presented, let_through = verified, []
    elif decision in ("saved", "maybe"):
        # a job you saved stays while you think it over: two weeks from your decision, in the list it was in
        saved_recently = recent or within(local_day(decided_at))
        presented = verified and passes and (saved_recently or "older" in off)
        let_through = [k for k in let_through if k != "known" and (k != "older" or not saved_recently)]
    elif reposted:
        presented = verified and passes
        let_through = [k for k in let_through if k != "known"]
    else:
        presented = (verified and passes and (recent or "older" in off) and decision not in HIDING
                     and (dedup not in KNOWN or "known" in off))
    level = gates.level(posting["title"], prof)
    return Evaluation(dedup, dedup_note, hits, verification[0], verification[1], rel, matched, low, mentions, geo, decision,
                      presented, gates.freshness(posting["posted_at"], prof, now, posting["source_updated_at"]),
                      agency, numbers or ([posting["provider_job_id"]] if agency else []),
                      [h for h in history.at_company(company, posting["id"]) if h not in hits],
                      published, recent, last_action, reposted, level,
                      gates.narrow_match(matched), *gates.likely_overqualified(posting["description"], prof),
                      held_by=held, let_through=let_through if presented else [],
                      lead_at_agency=history.applied_as_lead(company, level))


def overqualified_marks(conn: sqlite3.Connection) -> set[int]:
    """Postings you marked Overqualified (your latest mark on each)."""
    marks = {}
    for r in conn.execute(f"""SELECT subject_id, decision FROM decision_events WHERE subject_type='posting'
                              AND decision IN {OVERQUALIFIED_MARKS} ORDER BY id"""):
        marks[r["subject_id"]] = r["decision"] == "overqualified"
    return {k for k, on in marks.items() if on}


def latest_decisions(conn: sqlite3.Connection) -> dict[int, str]:
    return {k: v[0] for k, v in latest_decision_events(conn).items()}


def latest_decision_events(conn: sqlite3.Connection) -> dict[int, tuple[str | None, str, str]]:
    """posting id -> (current decision or None after a restore, note, time)."""
    out = {}
    for r in conn.execute("""SELECT subject_id, decision, note, created_at_utc FROM decision_events
                             WHERE subject_type='posting' ORDER BY id"""):
        if r["decision"] in NOT_DECISIONS:
            continue  # notes and marks are kept and shown, but never change the decision
        out[r["subject_id"]] = (None if r["decision"] == "restored" else r["decision"], r["note"], r["created_at_utc"])
    return out


def verification_status(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Per source: (last successful check run id and time, latest check, enabled). Used to decide 'verified now'."""
    now = now or _now()
    out = {}
    for s in conn.execute("SELECT id, enabled FROM search_sources"):
        ok = conn.execute("""SELECT run_id, checked_at_utc FROM source_checks WHERE source_id=? AND ok=1
                             ORDER BY id DESC LIMIT 1""", (s["id"],)).fetchone()
        last = conn.execute("SELECT ok, http_status, error, checked_at_utc FROM source_checks WHERE source_id=? ORDER BY id DESC LIMIT 1",
                            (s["id"],)).fetchone()
        out[s["id"]] = (ok, last, bool(s["enabled"]))
    return out


def posting_verification(posting, status: dict, now: datetime | None = None) -> tuple[str, str]:
    now = now or _now()
    ok, last, enabled = status.get(posting["source_id"], (None, None, True))
    if not enabled:  # you turned the employer's board off: none of its jobs is shown, whatever was found before
        return "BOARD_OFF", "you turned this employer's board off in Search settings"
    if ok is None:
        return "UNVERIFIED", "the official board has not been checked successfully yet"
    if posting["last_run_id"] != ok["run_id"]:
        return "NOT_LISTED", f"not listed on the official board at the last successful check ({ok['checked_at_utc'][:10]})"
    checked = datetime.fromisoformat(ok["checked_at_utc"])
    if now - checked > timedelta(days=VERIFIED_MAX_AGE_DAYS):
        return "VERIFICATION_EXPIRED", f"last verified {ok['checked_at_utc'][:10]}; too long ago to count as live"
    note = f"listed on the official board at the check of {local_time(ok['checked_at_utc'])} (Israel time)"
    if last and not last["ok"]:
        note += f"; the latest check failed ({last['error']}), which does not mean it closed"
    return "VERIFIED_ACTIONABLE", note


# ---- a run -------------------------------------------------------------------------------

def _start_run(conn, trigger: str, mode: str, profile_id: int | None, now: datetime) -> int:
    with db.transaction(conn):
        conn.execute("UPDATE search_runs SET status='abandoned', finished_at_utc=? WHERE status='running' AND started_at_utc < ?",
                     (_iso(now), _iso(now - timedelta(minutes=STALE_RUN_MINUTES))))
        if conn.execute("SELECT 1 FROM search_runs WHERE status='running'").fetchone():
            raise RunInProgress("A search is already running")
        return conn.execute("""INSERT INTO search_runs(started_at_utc, status, mode, trigger, profile_version_id)
                               VALUES (?, 'running', ?, ?, ?)""", (_iso(now), mode, trigger, profile_id)).lastrowid


def is_running(conn) -> bool:
    cutoff = _iso(_now() - timedelta(minutes=STALE_RUN_MINUTES))
    return bool(conn.execute("SELECT 1 FROM search_runs WHERE status='running' AND started_at_utc >= ?", (cutoff,)).fetchone())


def run_search(conn: sqlite3.Connection, get=None, trigger: str = "cli", dry_run: bool = False, force: bool = False,
               now: datetime | None = None, sleep=time.sleep) -> dict:
    """One run. Network first (no database lock held), then all writes in one transaction.
    A dry run fetches and evaluates but writes nothing."""
    now = now or _now()
    get = get or sources.http_get
    profile_id, prof = current_profile(conn)
    if not dry_run:
        collect_board_links(conn)
    run_id = None if dry_run else _start_run(conn, trigger, "live", profile_id, now)
    try:
        enabled = conn.execute("SELECT * FROM search_sources WHERE enabled=1 ORDER BY id").fetchall()
        fetched = []
        for i, src in enumerate(enabled):
            recent = conn.execute("""SELECT checked_at_utc FROM source_checks WHERE source_id=? AND ok=1
                                     ORDER BY id DESC LIMIT 1""", (src["id"],)).fetchone()
            if not force and recent and now - datetime.fromisoformat(recent["checked_at_utc"]) < timedelta(minutes=CACHE_MINUTES):
                fetched.append((src, None, None, "cached", 0))
                continue
            if i and fetched and fetched[-1][3] != "cached":
                sleep(POLITE_DELAY_S)
            started = time.monotonic()
            try:
                postings = sources.FETCHERS[src["provider"]](src["board"], get)
                fetched.append((src, postings, None, "ok", int((time.monotonic() - started) * 1000)))
            except sources.SourceError as e:
                fetched.append((src, None, e, "error", int((time.monotonic() - started) * 1000)))
        workday_details(conn, fetched, prof, get, sleep)
        summary = _record(conn, run_id, fetched, prof, now, dry_run)
    except BaseException as e:
        if run_id:
            with db.transaction(conn):
                conn.execute("UPDATE search_runs SET status='failed', finished_at_utc=?, error=? WHERE id=?",
                             (_iso(_now()), str(e)[:500], run_id))
        raise
    return summary


WORKDAY_DETAILS_PER_RUN = 40


def workday_details(conn, fetched, prof: dict, get, sleep, budget: int = WORKDAY_DETAILS_PER_RUN) -> int:
    """Workday's job list has no descriptions. For a job with a title you search for, the description is read once
    (then kept), so the description rules and the card's summary work for Workday jobs too. -> jobs read."""
    read = 0
    for src, postings, _, kind, _ in fetched:
        if src["provider"] != "workday" or not postings:
            continue
        stored = {r["identity"]: (r["description"], r["posted_at"]) for r in conn.execute(
            "SELECT identity, description, posted_at FROM postings WHERE source_id=?", (src["id"],))}
        for p in postings:
            description, posted = stored.get(sources.posting_identity(p.provider, src["board"], p.provider_job_id), ("", None))
            if description:   # read on an earlier run
                p.description, p.posted_at = description, posted or p.posted_at
                continue
            if read >= budget or gates.relevance(p.title, prof)[0].value != "MATCH":
                continue
            if get is sources.http_get:
                sleep(sources.WORKDAY_PAGE_DELAY_S)
            try:
                p.description, exact = sources.workday_details(p.url, get)
                p.posted_at = exact or p.posted_at
            except sources.SourceError:
                pass   # the list entry is still good; the description is tried again next run
            read += 1
    return read


def _record(conn, run_id, fetched, prof, now, dry_run) -> dict:
    history = history_index(conn, date.fromisoformat(local_day(now)))
    events = latest_decision_events(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if dry_run:  # a throwaway run row so the log tables can be filled; everything is rolled back below
            run_id = conn.execute("""INSERT INTO search_runs(started_at_utc, status, mode, trigger)
                                     VALUES (?, 'running', 'dry-run', 'cli')""", (_iso(now),)).lastrowid
        counts = defaultdict(int)
        seen_ids = []
        for src, postings, error, kind, ms in fetched:
            counts["sources_total"] += 1
            if kind == "cached":
                counts["sources_cached"] += 1
                continue
            if kind == "error":
                counts["sources_failed"] += 1
                conn.execute("""INSERT INTO source_checks(run_id, source_id, checked_at_utc, ok, http_status, postings, error, duration_ms)
                                VALUES (?, ?, ?, 0, ?, 0, ?, ?)""", (run_id, src["id"], _iso(now), error.status, str(error), ms))
                continue
            counts["sources_ok"] += 1
            conn.execute("""INSERT INTO source_checks(run_id, source_id, checked_at_utc, ok, http_status, postings, duration_ms)
                            VALUES (?, ?, ?, 1, 200, ?, ?)""", (run_id, src["id"], _iso(now), len(postings), ms))
            for p in postings:
                if not p.title or not p.url:
                    continue
                identity = sources.posting_identity(p.provider, src["board"], p.provider_job_id)
                row = conn.execute("SELECT id FROM postings WHERE identity=?", (identity,)).fetchone()
                values = (src["id"], p.title, p.location, ",".join(p.countries), None if p.remote is None else int(p.remote),
                          p.workplace, p.url, p.apply_url, p.requisition_id, p.posted_at, p.source_updated_at,
                          p.description, sha256(p.description.encode()).hexdigest(), _iso(now), run_id)
                if row:
                    conn.execute("""UPDATE postings SET source_id=?, title=?, location=?, countries=?, remote=?, workplace=?, url=?,
                                    apply_url=?, requisition_id=?, posted_at=?, source_updated_at=?, description=?,
                                    description_sha=?, last_seen_at_utc=?, last_run_id=? WHERE id=?""", values + (row["id"],))
                    posting_id, is_new = row["id"], False
                else:
                    posting_id = conn.execute("""INSERT INTO postings(source_id, title, location, countries, remote, workplace, url,
                                    apply_url, requisition_id, posted_at, source_updated_at, description, description_sha,
                                    last_seen_at_utc, last_run_id, identity, provider, provider_job_id, first_seen_at_utc, first_run_id)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                              values + (identity, p.provider, p.provider_job_id, _iso(now), run_id)).lastrowid
                    is_new = True
                    counts["postings_new"] += 1
                counts["postings_seen"] += 1
                seen_ids.append((posting_id, is_new, src))
        status = verification_status(conn, now)
        for posting_id, is_new, src in seen_ids:
            posting = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
            decision, _, decided_at = events.get(posting_id, (None, "", ""))
            ev = evaluate(posting, src["company"], prof, history, posting_verification(posting, status, now),
                          decision, now, decided_at)
            counts["presented"] += ev.presented
            counts[f"geo_{ev.geo.value}"] += 1
            counts[f"rel_{ev.relevance.value}"] += 1
            counts[f"dedup_{ev.dedup}"] += 1
            conn.execute("""INSERT INTO run_candidates(run_id, posting_id, is_new, dedup, verification, relevance, geo, presented, reasons_json)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (run_id, posting_id, int(is_new), ev.dedup, ev.verification, ev.relevance.value, ev.geo.value,
                          int(ev.presented), json.dumps(ev.reasons, ensure_ascii=False)))
        requests = counts["sources_ok"] + counts["sources_failed"]
        if not dry_run:
            conn.execute("""INSERT INTO cost_ledger(run_id, created_at_utc, provider, kind, requests, reserved_usd, actual_usd, note)
                            VALUES (?, ?, 'official ATS boards', 'http', ?, 0, 0, 'free public endpoints; no paid calls')""",
                         (run_id, _iso(now), requests))
            conn.execute("""UPDATE search_runs SET status='finished', finished_at_utc=?, sources_total=?, sources_ok=?, sources_failed=?,
                            postings_seen=?, postings_new=?, presented=? WHERE id=?""",
                         (_iso(_now()), counts["sources_total"], counts["sources_ok"], counts["sources_failed"],
                          counts["postings_seen"], counts["postings_new"], counts["presented"], run_id))
        conn.execute("ROLLBACK" if dry_run else "COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"run_id": None if dry_run else run_id, "dry_run": dry_run, **dict(counts), "requests": requests, "cost_usd": 0.0,
            "errors": [(src["company"], str(err)) for src, _, err, kind, _ in fetched if kind == "error"]}


# ---- views -------------------------------------------------------------------------------

NEW_HOURS = 48  # "New": appeared on a board since its previous successful check, within this window


@dataclass
class OpportunityView:
    posting: sqlite3.Row
    company: str
    provider: str
    ev: Evaluation
    decision_note: str = ""
    decided_at: str = ""
    is_new: bool = False
    similar: list = field(default_factory=list)  # [(OpportunityView, score)]: possibly the same vacancy
    marked_overqualified: bool = False             # your own mark: below your level (the job stays)


def posting_view(conn: sqlite3.Connection, posting_id: int, now: datetime | None = None) -> OpportunityView | None:
    """One posting with today's evaluation, whatever list it is in."""
    now = now or _now()
    p = conn.execute("""SELECT p.*, s.company FROM postings p JOIN search_sources s ON s.id = p.source_id
                        WHERE p.id = ?""", (posting_id,)).fetchone()
    if p is None:
        return None
    _, prof = current_profile(conn)
    events = latest_decision_events(conn)
    decision, note, decided_at = events.get(p["id"], (None, "", ""))
    ev = evaluate(p, p["company"], prof, history_index(conn, date.fromisoformat(local_day(now))),
                  posting_verification(p, verification_status(conn, now), now), decision, now, decided_at)
    return OpportunityView(p, p["company"], sources.PROVIDER_NAMES.get(p["provider"], p["provider"]), ev,
                           note if decision else "", decided_at if decision else "",
                           marked_overqualified=p["id"] in overqualified_marks(conn))


def opportunities(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Current evaluation of every stored posting with today's profile, decisions and verification."""
    now = now or _now()
    _, prof = current_profile(conn)
    history, status = history_index(conn, date.fromisoformat(local_day(now))), verification_status(conn, now)
    events = latest_decision_events(conn)
    marked = overqualified_marks(conn)
    # verified: new jobs · reposted: known jobs published again · older: relevant, but published too long ago
    out = {"verified": [], "reposted": [], "hidden": [], "older": [], "geo_review": [], "excluded": [], "history": []}
    # the first successful check of each board: everything seen then was already there, so it is not "new"
    pool = []  # every relevant posting, whatever its list: what a shown job may be "possibly the same" as
    first_ok = {r["source_id"]: r["first_run"] for r in conn.execute(
        "SELECT source_id, min(run_id) AS first_run FROM source_checks WHERE ok=1 GROUP BY source_id")}
    off = set(prof.get("filters_off", []))
    filter_counts = {key: 0 for key in gates.FILTERS}  # per filter: the jobs it holds back (on) or lets through (off)
    for p in conn.execute("""SELECT p.*, s.company, s.provider AS src_provider FROM postings p
                             JOIN search_sources s ON s.id = p.source_id"""):
        decision, note, decided_at = events.get(p["id"], (None, "", ""))
        if decision is None:
            title_verdict, title_terms = gates.relevance(p["title"], prof)[:2]
            if title_verdict.value == "NO_MATCH" or (title_verdict.value == "EXCLUDED" and not title_terms):
                continue  # not a title you search for, never decided: it is in no list (the run log keeps its reason)
        ev = evaluate(p, p["company"], prof, history, posting_verification(p, status, now), decision, now, decided_at)
        is_new = (p["first_run_id"] is not None and p["first_run_id"] != first_ok.get(p["source_id"])
                  and now - datetime.fromisoformat(p["first_seen_at_utc"]) <= timedelta(hours=NEW_HOURS))
        view = OpportunityView(p, p["company"], sources.PROVIDER_NAMES.get(p["provider"], p["provider"]), ev,
                               note if ev.decision else "", decided_at if ev.decision else "", is_new,
                               marked_overqualified=p["id"] in marked)
        if ev.relevance.value == "MATCH":
            pool.append(view)
        if ev.verification != "VERIFIED_ACTIONABLE":
            continue
        if ev.presented:
            for key in ev.let_through:
                filter_counts[key] += 1
        elif ev.candidate and ev.decision not in HIDING and len(rest := set(ev.held_by) - off) == 1:
            filter_counts[rest.pop()] += 1  # held back by this filter alone: turning it off would show the job
        relevant = ev.relevance.value == "MATCH"
        if ev.presented:
            out["reposted" if ev.reposted or (ev.dedup in KNOWN and ev.decision in ("saved", "maybe")) else "verified"].append(view)
        elif ev.decision in HIDING:
            out["hidden"].append(view)
        elif ev.dedup in KNOWN:
            out["history"].append(view)
        elif relevant and ev.geo.value in gates.GEO_ELIGIBLE and not ev.recent:
            out["older"].append(view)
        elif relevant and ev.geo.value == "UNKNOWN" and ev.recent:
            out["geo_review"].append(view)
        elif relevant and ev.geo.value == "OUTSIDE":
            out["excluded"].append(view)
    similar.link_similar(out["verified"] + out["reposted"] + out["hidden"], pool)
    rank = {"saved": 0, "maybe": 1}
    for key in ("verified", "hidden", "geo_review", "excluded", "history"):
        out[key].sort(key=lambda v: v.posting["first_seen_at_utc"], reverse=True)  # newest first among equals
    for key in ("reposted", "older"):
        out[key].sort(key=lambda v: v.ev.published_on or "", reverse=True)
    level_rank = {"FOCUS": 0, "ENGINEER": 1, "JUNIOR": 2, "ABOVE": 3}  # your level first; nothing hidden for it
    for key in ("verified", "reposted"):
        # jobs shown only because you turned a filter off come after the ones every filter lets through
        out[key].sort(key=lambda v: (rank.get(v.ev.decision, 2), bool(v.ev.let_through), level_rank.get(v.ev.level, 1),
                                     v.ev.narrow, not v.is_new,
                                     v.ev.freshness.days if v.ev.freshness.days is not None else 10_000))
    out["filter_counts"] = filter_counts
    return out


def set_filter(conn: sqlite3.Connection, key: str, on: bool) -> int:
    """Turn one board filter on or off: a new profile version, so the change is kept with the others."""
    if key not in gates.FILTERS:
        raise ValueError(f"Unknown filter {key!r}")
    _, prof = current_profile(conn)
    off = [k for k in prof.get("filters_off", []) if k != key] + ([] if on else [key])
    return save_profile(conn, {**prof, "filters_off": off}, origin=f"board filter {key}: {'on' if on else 'off'}")


def feedback_summary(conn: sqlite3.Connection) -> dict:
    """How the search is doing, from your own decisions: good finds, duplicates it missed, misses by reason."""
    latest = latest_decision_events(conn)
    counts = {k: 0 for k in ("applied_now", "declined", "already_applied", "skipped", "hidden", "saved", "maybe")}
    reasons: dict[str, int] = {}          # wrong matches: what to tighten
    mine: dict[str, int] = {}             # not for me: the search was right
    for decision, note, _ in latest.values():
        if decision in counts:
            counts[decision] += 1
        if decision in ("skipped", "declined"):
            bucket = reasons if decision == "skipped" else mine
            bucket[note or "no reason given"] = bucket.get(note or "no reason given", 0) + 1
    notes = conn.execute("""SELECT d.created_at_utc, d.decision, d.note, p.id AS posting_id, p.title, s.company
                            FROM decision_events d JOIN postings p ON p.id = d.subject_id
                            JOIN search_sources s ON s.id = p.source_id
                            WHERE d.subject_type = 'posting' AND d.note <> '' ORDER BY d.id DESC LIMIT 200""").fetchall()
    counts["overqualified"] = len(overqualified_marks(conn))  # a mark, not a decision: not part of "judged"
    judged = counts["applied_now"] + counts["declined"] + counts["already_applied"] + counts["skipped"]
    share = lambda n: round(100 * n / judged) if judged else None
    return {"counts": counts, "reasons": sorted(reasons.items(), key=lambda kv: -kv[1]),
            "mine": sorted(mine.items(), key=lambda kv: -kv[1]), "notes": notes, "judged": judged,
            "good_share": share(counts["applied_now"]),
            "right_share": share(counts["applied_now"] + counts["declined"])}  # the search brought the right kind of job


def record_posting_decision(conn: sqlite3.Connection, posting_id: int, decision: str, note: str = "") -> int:
    if decision not in POSTING_DECISIONS:
        raise ValueError(f"Unknown decision {decision!r}")
    if decision == NOTE_ONLY and not note.strip():
        raise ValueError("Write a note first")
    if not conn.execute("SELECT 1 FROM postings WHERE id=?", (posting_id,)).fetchone():
        raise ValueError("Unknown posting")
    with db.transaction(conn):
        return conn.execute("""INSERT INTO decision_events(created_at_utc, subject_type, subject_id, decision, note, origin)
                               VALUES (?, 'posting', ?, ?, ?, 'web')""",
                            (_iso(_now()), posting_id, decision, note.strip()[:2000])).lastrowid
