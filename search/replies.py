"""Replies to your applications, read from your mailbox: an employer's "we got your application" or its no.

Rules, no AI. Most of these e-mails come from the employers' hiring systems (Comeet, Greenhouse, Lever, Workday,
Workable) and say the same things in the same words: "We Got It: Thanks for applying for <role>", "Thank you for
applying for the <role> position at <company>", "decided not to move forward". The console reads the company and
the role, finds the record in your tracker and sets its status:

- a confirmation: Applied, when the record is not an application yet; a new added record (AD) when your tracker has
  nothing at that company;
- a no: Rejected by them, unless you updated the record after the e-mail.

What the rules cannot place (no company, several records it could be) changes nothing and is listed for you.
The mailbox is opened read-only, as for the alerts; each e-mail is read once. LinkedIn's "your application was sent"
is handled with the alerts (search/mail.py).
"""
from __future__ import annotations

import email
import email.policy
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from hashlib import sha256

from app import db

# The Gmail search for replies: subjects these e-mails use, in English and Hebrew (alerts are left out).
QUERY = ('-from:jobalerts-noreply@linkedin.com -from:jobs-noreply@linkedin.com -from:alljob.co.il -from:drushim.co.il '
         'subject:("thank you for applying" OR "thanks for applying" OR "your application" OR "application received" '
         'OR "we got it" OR "thank you for your interest" OR "application for" OR "applying for" OR "applying to" '
         'OR "application status" OR "application at" OR candidacy OR מועמדות OR מועמדותך OR "קורות החיים" OR הגשת '
         'OR שהגשת)')

_REJECT = re.compile(
    r"not to move forward|not be moving forward|won'?t be moving forward|will not be moving forward|"
    r"move forward with (?:other|another)|other candidates|decided not to proceed|not be proceeding|"
    r"(?:were|was|have not been|has not been|weren't) (?:not )?selected|"
    r"did not find a match|not (?:a|the right) (?:fit|match) for|no longer (?:under consideration|considering)|"
    r"position has been filled|regret to inform|unable to offer|unfortunately,? (?:we|after|at this|this time|the)|"
    r"לצערנו|לא נמשיך|שלא להמשיך|מועמדים אחרים|מועמד אחר|החלטנו להמשיך עם|לא נוכל להתקדם|החלטנו שלא|לא נבחרת|"
    r"לא נמצאה התאמה", re.I)
_CONFIRM = re.compile(
    r"thanks? (?:you )?for (?:applying|your application|submitting)|thank you for your interest|"
    r"we(?:'ve| have)? (?:got|received|reviewed) your (?:job )?(?:application|cv|resume)|application (?:was )?received|"
    r"we got it|received your (?:application|cv|resume)|"
    r"קיבלנו את|התקבלו בהצלחה|תודה על הגשת|תודה שהגשת|הגשת מועמדות|קורות החיים שלך", re.I)

# A reply's subject says what it is about: "Thank you for applying for the <role> position at <company>",
# "We Got It: Thanks for applying for <role>", "Thanks for applying to <company>!", "Your application for <role> at
# <company>", "Application Status @ <company>". After the opening, "for/the" names the role, "to/in/with/at" the company.
_OPENING = re.compile(r"^(?:we got it:\s*)?(?:thanks?(?: you)?(?: so much)? for (?:applying|your application|your interest|"
                      r"submitting your application)|your (?:recent )?(?:job )?application|application status|thank you)"
                      r"\s*(?P<prep>for the|for|to the|to|in the|in|at|with|@|the)?\s+", re.I)
_ROLE_AT = re.compile(r"^(?P<title>.+?)(?: (?:position|role|job))?\s*(?:\bat\b|@|\bwith\b)\s*(?P<company>.+)$", re.I)
_ROLE_ONLY = re.compile(r"^(?P<title>.+?)(?: (?:position|role|job))?$", re.I)
_HEBREW_SUBJECT = re.compile(r"מועמדותך למשרת (?P<title>.+?) ב(?P<company>[^\s]+)$")
# "הגשת מועמדות בנק הפועלים" (sent by Adam Total, the hiring system of many Israeli employers)
_HEBREW_EMPLOYER = re.compile(r"^הגשת (?:ה)?מועמדות (?P<company>[^\d!.]{2,40}?)!?$")   # no ל/ב prefix: בנק starts with ב
# when the subject names no role, the e-mail often does
_BODY_ROLE = [re.compile(r"(?:for|to|in) (?:the|our) (?:role )?(?P<title>[^.\n<]{3,90}?) (?:position|role)\b", re.I),
              re.compile(r"job application for (?P<title>[^.\n<]{3,90}?)\.", re.I),
              re.compile(r"למעסיק (?P<company>[^\n<]{2,60}?) עבור משרה (?P<title>[^\n<]{3,90}?) בתאריך"),
              re.compile(r"(?:our|the) role (?P<title>[A-Z][^.\n<]{3,90}?) at ", re.I),
              re.compile(r"מועמדותך לתפקיד (?P<title>[^.\n<]{3,80}?)\."),
              re.compile(r"להגיש מועמדות ל(?P<company>[^.\n<]{2,40}?)\.(?P<title>)")]
_APPLICATION_SUBJECT = re.compile(r"appl(?:y|ied|ying|ication)|candidacy|interest|position|role|status|"
                                  r"מועמד|משרה|קורות|הגשת|בקשה", re.I)
_NOT_A_ROLE = {"a", "an", "the", "position", "a position", "our", "us", "this position", "job", "role"}
_GENERIC_NAMES = re.compile(r"\b(?:hr|people|talent|team|recruiting|recruitment|careers?|hiring|jobs?|no-?reply|"
                            r"notifications?|workday|workable|greenhouse|lever|comeet|linkedin|mailer|info|marketing)\b|&",
                            re.I)
_GENERIC_LABELS = {"no-reply", "noreply", "careers", "career", "mail", "hire", "eu", "us", "notifications", "notification",
                   "workflow", "email", "candidates", "recruitment", "jobs", "info", "marketing", "www", "people", "talent",
                   "ocs", "oraclecloud", "sender", "fa", "adamtotal"}
_FORWARDED_FROM = re.compile(r"^From:\s*(.+)$", re.M)


@dataclass
class Reply:
    message_id: str
    received: str
    sender: str
    subject: str
    kind: str          # applied | rejected
    company: str
    title: str


def _clean(text: str) -> str:
    text = re.sub(r"[​-‏‪-‮⁦-⁩️]|[^\w\s&@'’().,/+#:–—-]", " ", text or "")
    return re.sub(r"\s+", " ", text).strip(" !.,-–—:")


def _company_from(name: str, address: str) -> str:
    """The employer from the sender: its name unless it is a person or a hiring system, else its domain."""
    name = _clean(name.split(" - ")[-1])
    if name and not _GENERIC_NAMES.fullmatch(name) and len(name.split()) <= 4 and not re.search(r"[\u0590-\u05ff]", name):
        rest = _clean(_GENERIC_NAMES.sub(" ", name))
        flat_address = re.sub(r"\W", "", address.casefold())
        # "Jane Smith <notifications@acme…>" is a recruiter; "Acme Cloud <no-reply@acmecloud…>" the company
        person = re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", rest or "") and not any(w.casefold() in flat_address for w in rest.split())
        if rest and not person:
            return rest
    labels = [l for l in address.split("@")[-1].split(".")[:-1] if l.casefold() not in _GENERIC_LABELS]
    labels = [l for l in labels if not re.search(r"comeet|greenhouse|lever|workday|workable|^(?:us|eu|ap)-", l, re.I)]
    return labels[0].capitalize() if labels else ""


def subject_parts(subject: str) -> tuple[str, str]:
    """(company, role) a reply's subject names, either may be empty."""
    if m := _HEBREW_SUBJECT.search(subject):
        return _clean(m.group("company")), _clean(m.group("title"))
    if m := _HEBREW_EMPLOYER.match(subject.strip()):
        return _clean(m.group("company")), ""
    m = _OPENING.match(subject)
    if not m:
        return "", ""
    rest = re.sub(r"\s+(?:was|has been|is)\s.*$|[!.]+$", "", subject[m.end():].strip(), flags=re.I).strip()
    prep = (m.group("prep") or "").casefold()
    if (at := _ROLE_AT.match(rest)) and prep not in ("to", "in", "at", "with", "@"):
        return _clean(at.group("company")), _clean(at.group("title"))
    if prep in ("to", "in", "at", "with", "@") and not re.search(r"\b(?:position|role)\b", rest, re.I):
        return _clean(rest), ""
    if prep in ("to the", "in the") and (at := _ROLE_AT.match(rest)):
        return _clean(at.group("company")), _clean(at.group("title"))
    return "", _clean(_ROLE_ONLY.match(rest).group("title")) if rest else ""


def parse(msg: email.message.EmailMessage, text: str) -> Reply | None:
    """A reply to an application, or None. `text` is the e-mail's text (from search.mail.message_text)."""
    subject = re.sub(r"^(?:\s*(?:fwd?|fw|re)\s*:\s*)+", "", str(msg.get("Subject", "")), flags=re.I).strip()
    name, address = parseaddr(str(msg.get("From", "")))
    if subject != str(msg.get("Subject", "")).strip() and (m := _FORWARDED_FROM.search(text or "")):
        name, address = parseaddr(m.group(1))     # your forward: the employer is the original sender
    body = (text or "")[:4000]
    if not _APPLICATION_SUBJECT.search(subject):
        return None     # a newsletter that happens to say "unfortunately" or "thank you"
    kind = "rejected" if _REJECT.search(body) else "applied" if _CONFIRM.search(subject + " " + body) else None
    if kind is None:
        return None
    company, title = subject_parts(subject)
    if not title:
        for pattern in _BODY_ROLE:
            if m := pattern.search(body):
                title = _clean(m.group("title"))
                company = company or _clean(m.groupdict().get("company") or "")
                break
    title = "" if title.casefold() in _NOT_A_ROLE else title
    company = company or _company_from(name, address)
    try:
        received = parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        received = ""
    message_id = (msg.get("Message-ID") or "").strip() or f"sha256:{sha256(msg.as_bytes()).hexdigest()}"
    return Reply(message_id, received, address.casefold(), subject[:300], kind, company[:200], title[:300])


def _words(text: str) -> set[str]:
    from search import pipeline
    return set(re.findall(r"\w+", pipeline.core_title(text))) - {"the", "and", "of", "for", "a", "at", "position", "role"}


def candidates(history, company: str) -> list:
    """Your records at this employer: the same name, or one name the start of the other ("Anchor" / "Anchor Fintech")."""
    from search import pipeline
    key = pipeline.company_name_key(company)
    if not key:
        return []
    flat = lambda k: re.sub(r"\W", "", k)
    out = []
    for other, hits in history.by_company.items():
        if other and (flat(other).startswith(flat(key)) or flat(key).startswith(flat(other))) and min(len(flat(key)), len(flat(other))) >= 3:
            out += hits
    seen = set()
    return [h for h in out if not (h.ref in seen or seen.add(h.ref))]


def same_role(hits: list, title: str) -> list:
    """The records whose role matches the title best (at least half the words), one per record."""
    want = _words(title)
    if not want or not hits:
        return []
    scored = sorted(((len(want & _words(h.role)) / max(1, len(want | _words(h.role))), h) for h in hits), key=lambda s: -s[0])
    if scored[0][0] < 0.5:
        return []
    return list({h.ref: h for score, h in scored if score == scored[0][0]}.values())


def best_record(hits: list, title: str):
    """The record a reply is about: the one whose role matches the title best (an application among equals)."""
    from search import pipeline
    top = same_role(hits, title)
    if len(top) == 1:
        return top[0]
    applied = [h for h in top if h.state in pipeline.APPLIED_STATES]
    return applied[0] if len(applied) == 1 else None


def _set_status(conn: sqlite3.Connection, hit, state: str, note: str) -> bool:
    from app import added as quick, tracker
    from search import pipeline
    try:
        if hit.sheet == pipeline.ADDED:
            quick.set_status(conn, hit.row, state, note)
        elif hit.legacy_row_id is not None:
            tracker.record_status(conn, hit.legacy_row_id, state, note)
        elif hit.sheet == pipeline.CONSOLE:
            tracker.record_console_status(conn, hit.row, state, note)
        else:
            return False
    except ValueError:
        return False   # e.g. a posting you marked Duplicate: not an application of its own
    return True


def apply(conn: sqlite3.Connection, reply: Reply) -> tuple[str | None, str | None, str]:
    """(the record it is about, its page, what the console did) — the change itself is made here."""
    ref, action = _apply(conn, reply)
    href = None
    if ref:
        from search import pipeline
        hits = {h.ref: h for hs in pipeline.history_index(conn).by_company.values() for h in hs}
        href = hits[ref].href if ref in hits else None
    return ref, href, action


def _apply(conn: sqlite3.Connection, reply: Reply) -> tuple[str | None, str]:
    from app import added as quick
    from search import pipeline
    history = pipeline.history_index(conn)
    day = pipeline.local_day(reply.received) if reply.received else ""
    # a reply is never about an application you made after it (records are often added a day or two later)
    late = (date.fromisoformat(day) + timedelta(days=3)).isoformat() if day else ""
    hits = [h for h in candidates(history, reply.company) if not (late and h.applied and h.applied > late)]
    hit = best_record(hits, reply.title) if reply.title else None
    top = same_role(hits, reply.title) if reply.title else hits
    what = "confirmation" if reply.kind == "applied" else "rejection"
    note = f"{what.capitalize()} e-mail from {reply.company or reply.sender} on {day}: “{reply.subject[:90]}”"
    later = lambda h: bool(h.acted_on and day and h.acted_on > day)
    if reply.kind == "applied":
        # a role named precisely ("QA Engineer - 23547", four words or more) in your tracker under another company name
        precise = reply.title and (re.search(r"\d{4,}", reply.title) or len(_words(reply.title)) >= 4)
        elsewhere = [h for h in {h.ref: h for hs in history.by_title.values() for h in hs}.values()
                     if precise and pipeline.core_title(h.role) == pipeline.core_title(reply.title)]
        if top and all(h.state in pipeline.APPLIED_STATES for h in top):
            return top[0].ref, f"already {top[0].state.lower().replace('_', ' ')} in your tracker"
        if hit is None and not hits and elsewhere:
            return elsewhere[0].ref, f"probably {elsewhere[0].ref} (the same role under another company name): left as it is"
        if hit is None and not hits and reply.company and reply.title:
            prof = pipeline.current_profile(conn)[1]
            added_id = quick.add(conn, "", f"{reply.company} — {reply.title or 'role not named'}\n{note}", "APPLIED",
                                 company=reply.company, role=reply.title or f"Role not named ({reply.company}'s confirmation)",
                                 note=note, profile=prof)
            return f"{quick.CODE} {added_id}", "added to your tracker as Applied"
        if hit is None:
            return None, (f"not matched: {len(hits)} records at {reply.company}" if hits
                          else f"not matched: nothing at {reply.company or 'an unnamed company'} in your tracker"
                               + ("" if reply.title else ", and no role named"))
        if hit.state in pipeline.APPLIED_STATES:
            return hit.ref, f"already {hit.state.lower().replace('_', ' ')} in your tracker"
        if hit.state == "DUPLICATE":
            return hit.ref, "a duplicate record: left as it is"
        if later(hit):
            return hit.ref, f"your later update ({hit.acted_on}) kept"
        return hit.ref, "set to Applied" if _set_status(conn, hit, "APPLIED", note) else "left as it is"
    if top and all(h.state == "REJECTED" for h in top):
        return top[0].ref, "already rejected in your tracker"
    if hit is None and not reply.title:
        applied = [h for h in hits if h.state in pipeline.APPLIED_STATES - {"REJECTED"}]
        hit = applied[0] if len({h.ref for h in applied}) == 1 else None
    if hit is None:
        return None, (f"not matched: {len(hits)} records at {reply.company}" if hits
                      else f"not matched: nothing at {reply.company or 'an unnamed company'} in your tracker")
    if hit.state == "REJECTED":
        return hit.ref, "already rejected in your tracker"
    if later(hit):
        return hit.ref, f"your later update ({hit.acted_on}) kept"
    return hit.ref, "set to Rejected by them" if _set_status(conn, hit, "REJECTED", note) else "left as it is"


def process(conn: sqlite3.Connection, mailbox: str, raw_messages: list[bytes]) -> dict:
    """Each reply e-mail not read before, oldest first: its change in your tracker, and a line in mail_replies."""
    from search import mail
    found = []
    for raw in raw_messages:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        if mail.sender_source(parseaddr(str(msg.get("From", "")))[1]):
            continue   # an alert, read by search/mail.py
        try:
            reply = parse(msg, mail.message_text(msg))
        except (LookupError, ValueError):
            reply = None
        if reply and not conn.execute("SELECT 1 FROM mail_replies WHERE message_id=?", (reply.message_id,)).fetchone():
            found.append(reply)
    counts = {"replies": 0, "changed": 0}
    for reply in sorted(found, key=lambda r: r.received):
        ref, href, action = apply(conn, reply)
        with db.transaction(conn):
            conn.execute("""INSERT OR IGNORE INTO mail_replies(message_id, mailbox, received_at_utc, sender, subject, kind,
                            company, title, record_ref, record_href, action, read_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (reply.message_id, mailbox, reply.received, reply.sender, reply.subject, reply.kind,
                          reply.company, reply.title, ref, href, action, mail._now()))
        counts["replies"] += 1
        counts["changed"] += action.startswith(("set to", "added to"))
    return counts


def recent(conn: sqlite3.Connection, limit: int = 40) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM mail_replies ORDER BY received_at_utc DESC LIMIT ?", (limit,)).fetchall()
