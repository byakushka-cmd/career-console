"""Jobs from your job-alert e-mails. LinkedIn, AllJobs and Drushim send the alerts to you; the console reads them from your
mailbox instead of visiting those sites (whose terms forbid automated visits). Nothing else is read.

- The mailbox is opened read-only (IMAP EXAMINE, BODY.PEEK): no e-mail is marked read, moved, labelled or deleted.
- Only alerts from the senders below, from the last days (two by default: you follow them yourself).
- Each e-mail is read once (by its Message-ID); each job is stored once, however many e-mails named it.
- The jobs go through the same rules as the boards' jobs: excluded title words, Israel, your tracker,
  companies you turned off, your level. No AI; the address and the app password stay in data/mail.env.
"""
from __future__ import annotations

import email
import email.policy
import html
import imaplib
import re
import time
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from hashlib import sha256
from pathlib import Path

from app import db
from search import agencies, official
from search import profile as gates

SENDERS = {"jobalerts-noreply@linkedin.com": "LinkedIn", "jobs-noreply@linkedin.com": "LinkedIn",
           "alljob.co.il": "AllJobs", "drushim.co.il": "Drushim"}
DAYS = 2              # your rule (2026-10-15): you follow the alerts yourself, a day or two back is enough
TIMEOUT_S = 30
# "already_applied": you applied to this job before (it may have been rejected) and do not apply again: the job is
# fine, so it is not a "Not a fit" (a job that does not suit you).
DECISIONS = {"added": "Added to your tracker", "already_applied": "Already applied — not again", "skipped": "Not a fit",
             "hidden": "Hidden", "restored": "Restored"}
HIDING = {"added", "already_applied", "skipped", "hidden"}
HELD = {"not_qa": "Not a QA title", "title": "Excluded word in the title", "outside": "Outside Israel",
        "geo_unclear": "Location unclear", "place": "A place you excluded", "known": "Already in your tracker",
        "board_off": "A company you turned off",
        "older": "Older than the days shown"}


@dataclass
class MailJob:
    identity: str
    source: str
    title: str
    company: str
    location: str
    url: str
    easy_apply: bool = False    # LinkedIn's "Apply with resume & profile": the weakest way to apply


# ---- reading the e-mails -------------------------------------------------------------------------------------

_VIEW = re.compile(r"View job:\s*(https?://\S+)", re.I)
_LI_ID = re.compile(r"/jobs/view/(?:[a-z0-9%-]*-)?(\d{8,12})", re.I)
_LI_NOISE = re.compile(
    r"^(your job alert for|new jobs? match|a new job matches|manage your job alerts|jobs that match your profile|"
    r"based on your title|update:|your saved job|your other saved jobs|apply now|see all|this email was intended|"
    r"learn why|you are receiving|unsubscribe|help:|©|linkedin and the|top applicant|apply with resume|"
    r"this company is actively hiring|actively recruiting|fast growing|easy apply|promoted|be an early applicant|"
    r"view jobs|view all jobs|expand your search|recommendations based on|"
    r"\d+ (?:connections?|school alum\w*|alumni)|https?://)", re.I)
_SUBJECTS = [re.compile(r"^You may be a fit for (?P<company>.+?)[’']s? (?P<title>.+?) role", re.I),
             re.compile(r"^(?P<company>.+?) is hiring(?: an?|:)?\s+(?P<title>.+)$", re.I),
             re.compile(r"^(?P<title>.+?) (?:job )?at (?P<company>.+?)(?: - .*)?$", re.I)]


_EASY_APPLY = re.compile(r"^(?:apply with resume|easy apply)", re.I | re.M)


def _li_job(title: str, company: str, location: str, url: str, easy_apply: bool = False) -> MailJob | None:
    m = _LI_ID.search(url)
    if not m or not title.strip():
        return None
    return MailJob(f"linkedin:{m.group(1)}", "LinkedIn", title.strip(), company.strip(), location.strip(),
                   f"https://www.linkedin.com/jobs/view/{m.group(1)}/", easy_apply)


def _title_company_place(lines: list[str]) -> tuple[str, str, str]:
    """A job block ends with title, company, place (section headers such as 'Software jobs' can come before it):
    the place is the last line naming a known place, the two lines before it are the title and the company."""
    for i in range(len(lines) - 1, 1, -1):
        if gates.geography(lines[i]).value != "UNKNOWN":
            return lines[i - 2], lines[i - 1], lines[i]
    return (lines[-3], lines[-2], lines[-1]) if len(lines) >= 3 else (lines[0], lines[1], "")


def parse_linkedin(text: str, subject: str = "") -> list[MailJob]:
    """Job-alert digests, 'jobs that match your profile' and saved-job reminders: blocks of title, company,
    location, badges and 'View job: <link>', separated by dashed lines. A one-job e-mail is read from its subject."""
    jobs = []
    for chunk in re.split(r"\n\s*-{5,}\s*\n", text or ""):
        m = _VIEW.search(chunk)
        if not m:
            continue
        lines = [l.strip() for l in chunk[:m.start()].splitlines()]
        lines = [l for l in lines if l and not _LI_NOISE.match(l)]
        easy = bool(_EASY_APPLY.search(chunk[:m.start()]))
        if len(lines) >= 2 and (job := _li_job(*_title_company_place(lines), m.group(1), easy)):
            jobs.append(job)
    if not jobs and (link := _LI_ID.search(text or "")):
        for pattern in _SUBJECTS:
            if s := pattern.match((subject or "").strip()):
                place = "Israel" if re.search(r"\bIsrael\b", text or "") else ""
                if job := _li_job(s.group("title"), s.group("company"), place, link.group(0)):
                    jobs.append(job)
                break
    return list({j.identity: j for j in jobs}.values())


_AJ_ID = re.compile(r"JobID=(\d{5,9})", re.I)
URL = re.compile(r"https?://\S+")
_AJ_LABEL = re.compile(r"(?:^|\s)חברה\s*:\s*")


def parse_alljobs(text: str) -> list[MailJob]:
    """AllJobs alerts and daily digests: a title, 'חברה: <company>', 'מקום העבודה: <place>', a link with JobID."""
    lines = [re.sub(r"^\|\s*|\s*\|$", "", l).strip() for l in (text or "").splitlines()]
    lines = [l for l in lines if l and l != "|"]
    jobs, starts = [], [i for i, l in enumerate(lines) if _AJ_LABEL.search(l)]
    is_title = lambda l: not l.endswith(":") and not URL.match(l) and l not in (">", "לצפייה במשרה")
    for n, i in enumerate(starts):
        before, rest = _AJ_LABEL.split(lines[i], maxsplit=1)
        company, _, same_line = rest.partition("|")      # a digest: "חברה: <field> | אזור: <area>"
        title = before.strip() or next((lines[k] for k in range(i - 1, -1, -1) if is_title(lines[k])), "")
        title = re.sub(r"^משרה חמה!\s*", "", title).strip()
        company = company.strip()
        if company.endswith(("...", "…")) or company in ("אחר", "חסוי"):
            company = ""   # "אחר" is "other"; a value cut with "..." is a field of work, not a company
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        place = next((re.split(r":\s*", l, maxsplit=1)[1] for l in [same_line.strip(), *lines[i + 1:end]]
                      if re.match(r"^(?:מקום העבודה|איזור|אזור)\s*:", l)), "")
        # the job's link: in a digest it comes before the title (the link around the job), in a single alert
        # after it ("לצפייה במשרה"); the closest one before the title, else the first one after
        begin = starts[n - 1] + 1 if n else 0
        before_ids = [m.group(1) for l in lines[begin:i] if (m := _AJ_ID.search(l))]
        link = before_ids[-1] if before_ids else next((m.group(1) for l in lines[i + 1:end] if (m := _AJ_ID.search(l))), None)
        if link and title:
            jobs.append(MailJob(f"alljobs:{link}", "AllJobs", title, company, place.strip(),
                                f"https://www.alljobs.co.il/Search/UploadSingle.aspx?JobID={link}"))
    return list({j.identity: j for j in jobs}.values())


# The daily digest has its jobs only in the HTML: a link (with the JobID) around the title, then
# "חברה: <field or company> | אזור: <area>". A value cut short with "..." is a field of work, not a company.
_AJ_HTML_JOB = re.compile(r'<a\s[^>]*href="[^"]*JobID=(\d{5,9})[^"]*"[^>]*>([^<]*[^<\s>][^<]*)</a>(.*?)'
                          r'(?=<a\s[^>]*href="[^"]*JobID=|$)', re.S | re.I)


def parse_alljobs_html(markup: str) -> list[MailJob]:
    jobs = []
    for m in _AJ_HTML_JOB.finditer(markup or ""):
        title = re.sub(r"\s+", " ", html.unescape(m.group(2))).strip()
        rest = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(3))))
        company = (re.search(r"חברה\s*:\s*([^|]*)", rest) or [None, ""])[1].strip()
        place = (re.search(r"(?:מקום העבודה|איזור|אזור)\s*:\s*([^|]*)", rest) or [None, ""])[1].strip()
        if company.endswith(("...", "…")) or company in ("אחר", "חסוי"):
            company = ""
        if title and not title.startswith(">") and title not in ("לצפייה במשרה", "למשרה"):
            jobs.append(MailJob(f"alljobs:{m.group(1)}", "AllJobs", title, company, place,
                                f"https://www.alljobs.co.il/Search/UploadSingle.aspx?JobID={m.group(1)}"))
    return list({j.identity: j for j in jobs}.values())


# Drushim's smart agent ("הסוכן החכם"), one job or several per e-mail, in the HTML only: a link to the job
# (drushim.co.il/job/<number>/<code>/) around its title, then a small table: company, place(s), scope, years.
# The same job's "לצפייה במשרה" link comes after it. Only the job's own link is kept, without the tracking part.
_DR_JOB = re.compile(r'<a\s[^>]*href="(https://www\.drushim\.co\.il/job/(\d{6,10})/[0-9a-z]+/)[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_clean = lambda s: re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_drushim_html(markup: str) -> list[MailJob]:
    jobs, found = [], list(_DR_JOB.finditer(markup or ""))
    for n, m in enumerate(found):
        title = _clean(m.group(3))
        if not title or title.startswith("לצפייה"):
            continue
        rest = markup[m.end():found[n + 1].start() if n + 1 < len(found) else len(markup)]
        table = re.search(r"<table\b.*?</table>", rest, re.S | re.I)
        cells = [c for c in (_clean(t) for t in re.split(r"<[^>]+>", table.group(0) if table else "")) if c]
        company = cells[0] if cells and cells[0] not in ("חסוי", "אחר") else ""
        place = cells[1] if len(cells) > 1 else ""
        jobs.append(MailJob(f"drushim:{m.group(2)}", "Drushim", title, company, place, m.group(1)))
    return list({j.identity: j for j in jobs}.values())


# LinkedIn's "<name>, your application was sent to <company>": its first job is the one you applied to, the others
# are "similar jobs". The console records the application, so that job is never shown as new (until 2026-10-22
# it was: the job you had applied to came back the next day as a new job from the alerts).
_APPLIED_SUBJECT = re.compile(r"your application was sent to", re.I)


def applied_job(msg: email.message.EmailMessage, jobs: list[MailJob]) -> MailJob | None:
    """The job a LinkedIn confirmation e-mail says you applied to, else None."""
    if jobs and sender_source(parseaddr(msg.get("From", ""))[1]) == "LinkedIn" and _APPLIED_SUBJECT.search(msg.get("Subject", "")):
        return jobs[0]
    return None


def sender_source(address: str) -> str | None:
    address = (address or "").casefold()
    return next((source for s, source in SENDERS.items() if address == s or address.endswith("@" + s)), None)


def message_text(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except LookupError:   # a charset Python does not know: the Employment Service sends "iso-8859-8-i" (Hebrew)
        charset = (part.get_content_charset() or "utf-8").removesuffix("-i").removesuffix("-e")
        text = (part.get_payload(decode=True) or b"").decode(charset, errors="replace")
    return html_text(text) if part.get_content_type() == "text/html" else text


def html_text(markup: str) -> str:
    """An HTML e-mail as lines of text: one line per table cell or block; every link's address on its own line
    (AllJobs sends HTML only, and its job links carry the JobID)."""
    markup = re.sub(r"<(script|style)\b.*?</\1>", " ", markup or "", flags=re.S | re.I)
    markup = re.sub(r"\s+", " ", markup)   # line breaks inside the HTML source are not lines of the e-mail
    markup = re.sub(r'<a\s[^>]*href="([^"]+)"[^>]*>', lambda m: f"\n{html.unescape(m.group(1))}\n", markup, flags=re.I)
    markup = re.sub(r"<(?:br|/?p|/?div|/?td|/?tr|/?table|/?li|/?h\d)\b[^>]*>", "\n", markup, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    return "\n".join(re.sub(r"\s+", " ", l).strip() for l in text.splitlines() if l.strip())


def jobs_in(msg: email.message.EmailMessage) -> list[MailJob]:
    source = sender_source(parseaddr(msg.get("From", ""))[1])
    if source == "LinkedIn":
        return parse_linkedin(message_text(msg), msg.get("Subject", ""))
    if source == "AllJobs":
        found = parse_alljobs(message_text(msg))
        if not found and (part := msg.get_body(preferencelist=("html",))) is not None:
            found = parse_alljobs_html(part.get_content())   # a daily digest
        return found
    if source == "Drushim" and (part := msg.get_body(preferencelist=("html",))) is not None:
        return parse_drushim_html(part.get_content())
    return []


# ---- your mailboxes ------------------------------------------------------------------------------------------

def config_path(db_path: Path) -> Path:
    return Path(db_path).parent / "mail.env"


_ADDRESS = re.compile(r"^[^@\s:]+@[^@\s:]+\.[a-z]{2,}$", re.I)


def load_config(path: Path) -> dict:
    """'MAILBOX=address:app password' lines (one per mailbox) and an optional 'MAIL_DAYS=2'."""
    out = {"mailboxes": [], "days": DAYS}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.strip().partition("=")
        if key.strip() == "MAILBOX" and ":" in value:
            address, _, password = value.strip().partition(":")
            if _ADDRESS.match(address.strip()) and password.strip():
                out["mailboxes"].append((address.strip(), re.sub(r"\s+", "", password)))
        elif key.strip() == "MAIL_DAYS" and value.strip().isdigit():
            out["days"] = max(1, min(14, int(value.strip())))
    return out


def save_mailbox(path: Path, address: str, password: str) -> None:
    """Add or replace one mailbox. The file is readable by you only."""
    address, password = address.strip(), re.sub(r"\s+", "", password or "")
    if not _ADDRESS.match(address):
        raise ValueError("Write the full e-mail address, e.g. you@gmail.com")
    if not password:
        raise ValueError("Paste the app password (16 letters) you created in your Google account")
    keep = [l for l in (path.read_text(encoding="utf-8").splitlines() if path.exists() else [])
            if not l.strip().startswith(f"MAILBOX={address}:")]
    if not any(l.strip() and not l.startswith("#") for l in keep):
        keep = ["# Job-alert e-mails: one line per mailbox, MAILBOX=address:app password. Private: never in git."]
    path.write_text("\n".join(keep + [f"MAILBOX={address}:{password}"]) + "\n", encoding="utf-8")
    path.chmod(0o600)


def remove_mailbox(path: Path, address: str) -> None:
    if path.exists():
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.strip().startswith(f"MAILBOX={address}:")]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)


def imap_host(address: str) -> str:
    domain = address.rsplit("@", 1)[-1].casefold()
    return "imap.gmail.com" if domain in ("gmail.com", "googlemail.com") else f"imap.{domain}"


def _all_mail(imap) -> str:
    """Gmail's 'All Mail' (alerts you archived are there too), found by its \\All flag in any UI language."""
    typ, boxes = imap.list()
    for raw in boxes or []:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if "\\All" in line and (m := re.search(r'"([^"]+)"\s*$', line)):
            return f'"{m.group(1)}"'
    return "INBOX"


def fetch(address: str, password: str, days: int, imap_factory=None) -> list[bytes]:
    """The raw alert e-mails of the last `days` days, read-only. Raises on a wrong password or no connection."""
    host = imap_host(address)
    imap = imaplib.IMAP4_SSL(host, timeout=TIMEOUT_S) if imap_factory is None else imap_factory(host)
    try:
        imap.login(address, password)
        typ, _ = imap.select(_all_mail(imap), readonly=True)
        if typ != "OK":
            imap.select("INBOX", readonly=True)
        senders = " OR ".join(SENDERS)
        try:
            typ, data = imap.search(None, "X-GM-RAW", f'"newer_than:{days}d from:({senders})"')
        except imaplib.IMAP4.error:
            typ, data = "NO", None
        if typ != "OK":   # not Gmail: the standard search
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
            ids = set()
            for s in SENDERS:
                t, d = imap.search(None, "SINCE", since, "FROM", f'"{s}"')
                ids |= set((d[0] or b"").split()) if t == "OK" else set()
            data = [b" ".join(sorted(ids, key=int))]
        out = []
        for num in (data[0] or b"").split():
            t, parts = imap.fetch(num, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if t == "OK" and raw:
                out.append(raw)
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def fetch_query(address: str, password: str, query: str, days: int, imap_factory=None) -> list[bytes]:
    """The raw e-mails of the last `days` days a Gmail search finds, read-only (Gmail only: other mailboxes give none)."""
    host = imap_host(address)
    imap = imaplib.IMAP4_SSL(host, timeout=TIMEOUT_S) if imap_factory is None else imap_factory(host)
    try:
        imap.login(address, password)
        imap.select(_all_mail(imap), readonly=True)
        imap.literal = f"newer_than:{days}d {query}".encode("utf-8")
        try:
            typ, data = imap.search("UTF-8", "X-GM-RAW")
        except imaplib.IMAP4.error:
            return []
        out = []
        for num in (data[0] or b"").split() if typ == "OK" else []:
            t, parts = imap.fetch(num, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if t == "OK" and raw:
                out.append(raw)
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store(conn: sqlite3.Connection, mailbox: str, raw_messages: list[bytes]) -> dict:
    """Read each e-mail not read before; add its jobs, or count one more e-mail for a job already known."""
    counts, applied = Counter(), []
    with db.transaction(conn):
        for raw in raw_messages:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            message_id = (msg.get("Message-ID") or "").strip() or f"sha256:{sha256(raw).hexdigest()}"
            if conn.execute("SELECT 1 FROM mail_messages WHERE message_id=?", (message_id,)).fetchone():
                continue
            try:
                received = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                received = _now()
            jobs = jobs_in(msg)
            if job := applied_job(msg, jobs):
                applied.append((job.identity, received))
            counts["messages"] += 1
            counts["jobs"] += len(jobs)
            for j in jobs:
                row = conn.execute("SELECT id, first_seen_at_utc FROM mail_jobs WHERE identity=?", (j.identity,)).fetchone()
                if row:
                    conn.execute("""UPDATE mail_jobs SET last_seen_at_utc=max(last_seen_at_utc, ?), mails=mails+1,
                                    first_seen_at_utc=min(first_seen_at_utc, ?), easy_apply=max(easy_apply, ?) WHERE id=?""",
                                 (received, received, int(j.easy_apply), row["id"]))
                else:
                    conn.execute("""INSERT INTO mail_jobs(identity, source, title, company, location, url, first_seen_at_utc,
                                    last_seen_at_utc, easy_apply) VALUES (?,?,?,?,?,?,?,?,?)""",
                                 (j.identity, j.source, j.title[:300], j.company[:200], j.location[:120], j.url, received,
                                  received, int(j.easy_apply)))
                    counts["new_jobs"] += 1
            conn.execute("""INSERT INTO mail_messages(message_id, mailbox, sender, received_at_utc, jobs, read_at_utc)
                            VALUES (?,?,?,?,?,?)""",
                         (message_id, mailbox, parseaddr(msg.get("From", ""))[1].casefold(), received, len(jobs), _now()))
    if applied:
        counts["applications"] = record_applications(conn, applied)
    return dict(counts)


def record_applications(conn: sqlite3.Connection, applied: list[tuple[str, str]]) -> int:
    """Jobs a LinkedIn confirmation e-mail says you applied to, as (identity, when): into your tracker as Applied.
    A job your tracker already has as an application (same LinkedIn job number) is left as it is; a record of it
    that is not an application yet (Interested, On hold, Expired…) becomes Applied; otherwise an added record."""
    from app import added as quick, tracker
    from search import pipeline
    history = pipeline.history_index(conn)
    prof = pipeline.current_profile(conn)[1]
    decided = decisions(conn)
    done = 0
    for identity, received in applied:
        job = conn.execute("SELECT * FROM mail_jobs WHERE identity=?", (identity,)).fetchone()
        hits = history.by_identity.get(identity, [])
        if job is None or any(h.state in pipeline.APPLIED_STATES for h in hits):
            continue
        if decided.get(job["id"], (None,))[0] == "added":
            continue   # you put it in your tracker already (maybe under another job number: an agency's and a job board's)
        note = f"Applied on LinkedIn on {pipeline.local_day(received)} (LinkedIn's confirmation e-mail, read by the console)"
        record = next((h for h in hits if h.sheet == pipeline.ADDED or h.legacy_row_id is not None), None)
        if record is None:
            ref = f"{quick.CODE} " + str(quick.add(conn, job["url"], tracker_text(job), "APPLIED", note=note, profile=prof,
                                                   company=job["company"] or "Company not named (LinkedIn)", role=job["title"]))
        elif record.sheet == pipeline.ADDED:
            quick.set_status(conn, record.row, "APPLIED", note)
            ref = record.ref
        else:
            tracker.record_status(conn, record.legacy_row_id, "APPLIED", note)
            ref = record.ref
        record_decision(conn, job["id"], "added", f"{ref} · Applied (LinkedIn's confirmation e-mail)")
        done += 1
    return done


def read(conn: sqlite3.Connection, db_path: Path, imap_factory=None) -> list[dict]:
    """Check every mailbox in data/mail.env. Network first, then one transaction per mailbox; every check logged."""
    cfg = load_config(config_path(db_path))
    results = []
    for address, password in cfg["mailboxes"]:
        try:
            raw = fetch(address, password, cfg["days"], imap_factory)
            counts = store(conn, address, raw)
            # replies to your applications: a confirmation or a no changes the record's status (search/replies.py)
            from search import replies
            counts.update(replies.process(conn, address, fetch_query(address, password, replies.QUERY, cfg["days"], imap_factory)))
            ok, error = True, None
        except (imaplib.IMAP4.error, OSError) as e:
            counts, ok = {}, False
            text = str(e)
            error = ("Google refused the login: check the address and the app password (a normal password does not "
                     "work)" if "AUTHENTICATIONFAILED" in text.upper() or "credentials" in text.casefold()
                     else f"No connection to the mailbox: {text[:200]}")
        with db.transaction(conn):
            conn.execute("""INSERT INTO mail_reads(read_at_utc, mailbox, ok, messages, jobs, new_jobs, error)
                            VALUES (?,?,?,?,?,?,?)""", (_now(), address, int(ok), counts.get("messages", 0),
                                                        counts.get("jobs", 0), counts.get("new_jobs", 0), error))
        results.append({"mailbox": address, "ok": ok, "error": error, **counts})
    return results


def probe_boards(conn: sqlite3.Connection, get=None, sleep=None, limit: int = 40) -> list:
    """Each employer named in your alerts, once: a public Greenhouse / Lever / Ashby board with jobs in Israel joins
    the boards the search reads (so its own postings, with the official link, come in from then on). Staffing
    agencies are skipped: the employer behind their jobs is not named."""
    from search import discover, pipeline, sources
    _, prof = pipeline.current_profile(conn)
    known = {pipeline.company_name_key(r["company"]) for r in conn.execute("SELECT company FROM search_sources")}
    done = {r["company_key"] for r in conn.execute("SELECT company_key FROM mail_company_probes")}
    todo = []
    for r in conn.execute("SELECT DISTINCT company FROM mail_jobs WHERE company <> '' ORDER BY id DESC"):
        key = pipeline.company_name_key(r["company"])
        if key and key not in known | done and not agencies.agency_key(r["company"], prof["agency_companies"]) \
                and key not in {k for k, _ in todo}:
            todo.append((key, r["company"]))
    found_all = []
    for key, company in todo[:limit]:
        found = discover.probe(company, [], get or sources.http_get, sleep or time.sleep)
        with db.transaction(conn):
            if found.israel_jobs:
                conn.execute("""INSERT OR IGNORE INTO search_sources(provider, board, company, enabled, origin, added_at_utc)
                                VALUES (?, ?, ?, 1, 'found: a company in your alert e-mails', ?)""",
                             (found.provider, found.board, company, _now()))
            conn.execute("""INSERT OR REPLACE INTO mail_company_probes(company_key, company, probed_at_utc, provider, board,
                            israel_jobs, note) VALUES (?,?,?,?,?,?,?)""",
                         (key, company, _now(), found.provider, found.board, found.israel_jobs, found.note))
        found_all.append(found)
    return found_all


def last_reads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""SELECT r.* FROM mail_reads r JOIN (SELECT mailbox, max(id) AS id FROM mail_reads GROUP BY mailbox) l
                           ON l.id = r.id ORDER BY r.mailbox""").fetchall()


# ---- the jobs, through your rules ----------------------------------------------------------------------------

@dataclass
class MailView:
    job: sqlite3.Row
    held: list = field(default_factory=list)       # HELD keys that keep it off the list
    reasons: dict = field(default_factory=dict)    # key -> what exactly (the term, the tracker record)
    level: str = "ENGINEER"
    hits: list = field(default_factory=list)       # your tracker records for this job
    earlier: list = field(default_factory=list)    # your other records at this company
    lead_at_agency: list = field(default_factory=list)
    official: list = field(default_factory=list)   # the employer's own posting, when a board the console reads lists it
    narrow: bool = False                           # matched only on quality / validation / verification: check it
    also: list = field(default_factory=list)       # the same position from other alerts (another city, another ID)
    agency: str | None = None
    decision: str | None = None
    note: str = ""
    decided_at: str = ""


def decisions(conn: sqlite3.Connection) -> dict[int, tuple[str | None, str, str]]:
    out = {}
    for r in conn.execute("""SELECT subject_id, decision, note, created_at_utc FROM decision_events
                             WHERE subject_type='mail_job' ORDER BY id"""):
        out[r["subject_id"]] = (None if r["decision"] == "restored" else r["decision"], r["note"], r["created_at_utc"])
    return out


def record_decision(conn: sqlite3.Connection, job_id: int, decision: str, note: str = "") -> int:
    if decision not in DECISIONS:
        raise ValueError(f"Unknown decision {decision!r}")
    if not conn.execute("SELECT 1 FROM mail_jobs WHERE id=?", (job_id,)).fetchone():
        raise ValueError("Unknown job")
    with db.transaction(conn):
        return conn.execute("""INSERT INTO decision_events(created_at_utc, subject_type, subject_id, decision, note, origin)
                               VALUES (?, 'mail_job', ?, ?, ?, 'web')""", (_now(), job_id, decision, note.strip()[:2000])).lastrowid


def views(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """shown: new jobs worth a look · held: kept off by a rule (and why) · decided: you added, marked, skipped or hid them."""
    from search import pipeline   # late import: pipeline imports app, which imports this module's users
    _, prof = pipeline.current_profile(conn)
    today = today or date.fromisoformat(pipeline.local_day(pipeline._now()))
    history = pipeline.history_index(conn, today)
    index = official.OfficialIndex(conn, prof["agency_companies"])
    off = {pipeline.company_name_key(r["company"]) for r in conn.execute("SELECT company FROM search_sources WHERE enabled=0")}
    decided = decisions(conn)
    max_age = prof.get("max_age_days", 14)
    out = {"shown": [], "held": [], "decided": [], "held_counts": Counter()}
    for job in conn.execute("SELECT * FROM mail_jobs ORDER BY first_seen_at_utc DESC, id DESC"):
        v = MailView(job, level=gates.level(job["title"], prof))
        v.decision, v.note, v.decided_at = decided.get(job["id"], (None, "", ""))
        rel, matched, _, _ = gates.relevance(job["title"], prof)
        v.narrow = gates.narrow_match(matched)
        if rel.value == "NO_MATCH":
            v.held.append("not_qa")
        elif rel.value == "EXCLUDED":
            v.held.append("title")
            v.reasons["title"] = rel.reason
        geo = gates.geography(job["location"], [], None, "")
        if geo.value == "UNKNOWN" and job["source"] in ("AllJobs", "Drushim"):
            geo = gates.Verdict("ISRAEL", f"{job['source']} lists jobs in Israel")
        if geo.value == "OUTSIDE":
            v.held.append("outside")
        elif geo.value == "UNKNOWN":
            v.held.append("geo_unclear")
        if place := gates.excluded_place(job["location"], prof):
            v.held.append("place")
            v.reasons["place"] = ", ".join(place)
        v.agency = agencies.agency_key(job["company"], prof["agency_companies"])
        numbers = agencies.vacancy_numbers(job["title"])
        dedup, hits, note = history.classify(job["identity"], job["company"], job["title"], None, v.agency, numbers)
        if dedup in pipeline.KNOWN:
            v.held.append("known")
            v.reasons["known"] = note
        v.hits = hits
        if job["company"] and pipeline.company_name_key(job["company"]) in off:
            v.held.append("board_off")
        if (today - date.fromisoformat(pipeline.local_day(job["first_seen_at_utc"]))).days > max_age:
            v.held.append("older")
        v.earlier = [h for h in history.at_company(job["company"]) if h not in hits] if job["company"] else []
        v.lead_at_agency = history.applied_as_lead(job["company"], v.level)
        if v.decision in HIDING:
            out["decided"].append(v)
        elif v.held:
            out["held"].append(v)
            out["held_counts"][v.held[0]] += 1
        else:
            v.official = index.find(job["company"], job["title"]) if job["company"] else []
            out["shown"].append(v)
    level_rank = {"FOCUS": 0, "ENGINEER": 1, "JUNIOR": 2, "ABOVE": 3}
    # your level first, then titles that are clearly QA, then the newest
    out["shown"].sort(key=lambda v: (level_rank.get(v.level, 1), v.narrow,
                                     -datetime.fromisoformat(v.job["first_seen_at_utc"]).timestamp()))
    # one card per position: the same company and title from several alerts (another city, another LinkedIn ID)
    first = {}
    for v in list(out["shown"]):
        key = (pipeline.company_name_key(v.job["company"]), pipeline.core_title(v.job["title"]))
        if key in first and v.job["company"]:
            first[key].also.append(v.job)
            out["shown"].remove(v)
        else:
            first[key] = v
    return out


def tracker_text(job: sqlite3.Row) -> str:
    """What goes into the tracker record when you add a job from an alert."""
    from search import pipeline
    return (f"{job['company'] or 'Company not named'} — {job['title']}\n{job['location']}\n"
            f"From your {job['source']} alert e-mail ({pipeline.local_day(job['first_seen_at_utc'])}): {job['url']}")
