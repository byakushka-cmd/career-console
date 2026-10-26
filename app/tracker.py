"""Status of tracker records as you update it. The imported text is never changed: every update is an
append-only decision event, and a record's current status is its latest event (or the tracker's own
provisional reading when there is none)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app import db
from app.config import DISPLAY_TZ
from app.legacy_import import C_CHECKED, COMEET, M_APPLIED, M_UPDATED, converted_date

SUBJECT = "legacy_row"
# Applications you marked in the console ("Good find — applied" on a posting, code CC): your status updates on
# them (e.g. Rejected by them), kept apart from the posting's own decisions. Subject id = the posting id.
CONSOLE_SUBJECT = "console_application"
# Order = order of the buttons on a record page.
STATUS_CHOICES = {
    "CLOSED": "Gone / stale",
    "SKIPPED": "Not a fit (my decision)",
    "APPLIED": "Applied",
    "IN_PROCESS": "In process",
    "INTERVIEW": "Interview",
    "REJECTED": "Rejected by them",
    "HOLD": "Keep on hold",
    "DISCOVERED": "Interested — not applied yet",
    "DUPLICATE": "Duplicate",
}
STATUS_HINTS = {
    "CLOSED": "The job is gone or stale: removed, 404, filled, or no longer accepting applications",
    "SKIPPED": "You decided not to go on with it",
    "APPLIED": "You sent the application; no answer yet",
    "IN_PROCESS": "You are in contact with them: screening call, home task, talks",
    "INTERVIEW": "An interview is scheduled or done",
    "REJECTED": "They said no after you applied",
    "HOLD": "Parked; decide later",
    "DISCOVERED": "Still interesting, you have not applied",
    "DUPLICATE": "The same job as another of your records: kept, closed, never shown as new",
}


@dataclass(frozen=True)
class StatusChange:
    state: str
    note: str
    at_utc: str
    source: str            # "you" or "posting"
    posting_id: int | None = None


TRACKER = "TRACKER"  # "back to the tracker's own reading": ends your override without deleting history

# Your rule (2026-10-15): a job you did not apply to within two weeks is no longer relevant. Records still
# "not applied" or "on hold" with no movement for longer are closed automatically. Nothing is deleted or
# rewritten: it is a reading of today's date, and pressing any status brings the record back.
EXPIRE_DAYS = 14
EXPIRED = "EXPIRED"
EXPIRING_STATES = {"DISCOVERED", "HOLD", "CONSIDERING", "BLOCKED"}  # "unclear status" (REVIEW) may be an application


def today() -> date:
    return datetime.now(ZoneInfo(DISPLAY_TZ)).date()


def moved_on(sheet: str, values: list, change: StatusChange | None = None) -> str | None:
    """The record's last movement: the tracker's own last-update (or applied) date, or your latest status update."""
    if sheet == COMEET:
        dates = [converted_date(values[C_CHECKED]) if len(values) > C_CHECKED else None]
    else:
        dates = [converted_date(values[i]) if len(values) > i else None for i in (M_UPDATED, M_APPLIED)]
    if change is not None:
        dates.append(datetime.fromisoformat(change.at_utc).astimezone(ZoneInfo(DISPLAY_TZ)).date().isoformat())
    dates = [d for d in dates if d]
    return max(dates) if dates else None


def effective_state(state: str | None, moved: str | None, on: date | None = None) -> str | None:
    """EXPIRED for a not-applied record without movement for more than EXPIRE_DAYS; otherwise the state itself.
    Without any date the age is unknown, and the record is left as it is."""
    if state in EXPIRING_STATES and moved and ((on or today()) - date.fromisoformat(moved)).days > EXPIRE_DAYS:
        return EXPIRED
    return state


def record_status(conn: sqlite3.Connection, legacy_row_id: int, state: str, note: str = "", origin: str = "web") -> int:
    if state not in STATUS_CHOICES and state != TRACKER:
        raise ValueError(f"Unknown status {state!r}")
    row = conn.execute("SELECT sheet_name, source_row FROM legacy_rows WHERE id=?", (legacy_row_id,)).fetchone()
    if row is None or row["source_row"] == 1 or row["sheet_name"] not in ("Master Tracker", "Comeet Index"):
        raise ValueError("Not a tracker record")
    with db.transaction(conn):
        return conn.execute("""INSERT INTO decision_events(created_at_utc, subject_type, subject_id, decision, note, origin)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (datetime.now(timezone.utc).isoformat(timespec="seconds"), SUBJECT, legacy_row_id, state,
                             note.strip()[:2000], origin)).lastrowid


def record_console_status(conn: sqlite3.Connection, posting_id: int, state: str, note: str = "", origin: str = "web") -> int:
    from search import pipeline  # late import: search depends on app
    if state not in STATUS_CHOICES and state != TRACKER:
        raise ValueError(f"Unknown status {state!r}")
    if posting_id not in {p["id"] for p in pipeline.applied_postings(conn)}:
        raise ValueError("Not an application marked in the console")
    with db.transaction(conn):
        return conn.execute("""INSERT INTO decision_events(created_at_utc, subject_type, subject_id, decision, note, origin)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (datetime.now(timezone.utc).isoformat(timespec="seconds"), CONSOLE_SUBJECT, posting_id, state,
                             note.strip()[:2000], origin)).lastrowid


def manual_statuses(conn: sqlite3.Connection, subject: str = SUBJECT) -> dict[int, StatusChange]:
    out = {}
    for r in conn.execute("SELECT subject_id, decision, note, created_at_utc FROM decision_events WHERE subject_type=? ORDER BY id",
                          (subject,)):
        if r["decision"] == TRACKER:
            out.pop(r["subject_id"], None)
        else:
            out[r["subject_id"]] = StatusChange(r["decision"], r["note"], r["created_at_utc"], "you")
    return out


def status_history(conn: sqlite3.Connection, legacy_row_id: int, subject: str = SUBJECT) -> list[sqlite3.Row]:
    return conn.execute("""SELECT decision, note, created_at_utc FROM decision_events
                           WHERE subject_type=? AND subject_id=? ORDER BY id""", (subject, legacy_row_id)).fetchall()


def current_statuses(conn: sqlite3.Connection) -> dict[int, StatusChange]:
    """Your updates, plus 'Applied' carried over from postings you marked Already applied that have the same
    official job ID as a tracker record. The newest change wins."""
    from search import pipeline  # late import: search depends on app
    out = dict(manual_statuses(conn))
    for legacy_row_id, (posting_id, at_utc) in pipeline.tracker_rows_applied_via_postings(conn).items():
        current = out.get(legacy_row_id)
        if current is None or current.at_utc <= at_utc:
            out[legacy_row_id] = StatusChange("APPLIED", f"marked Already applied on posting CC {posting_id}", at_utc,
                                              "posting", posting_id)
    return out
