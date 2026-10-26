"""Review queue: migration issues and duplicate reviews, resolved through append-only decision events.

Decisions are recorded, never applied destructively: source rows, links and earlier decisions stay
as they are, and the latest event per item is its current resolution.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations

from app import db, legacy_import

ISSUE, DUPLICATE = "migration_issue", "duplicate_review"
TABLES = {ISSUE: "migration_issues", DUPLICATE: "duplicate_reviews"}
DECISIONS = {
    ISSUE: {"acknowledged": "Keep as is", "follow_up": "Needs follow-up", "reopened": "Reopen"},
    DUPLICATE: {"same_requisition": "Same requisition", "different_requisitions": "Different requisitions",
                "unsure": "Not sure", "reopened": "Reopen"},
}
RESOLVING = {"acknowledged", "same_requisition", "different_requisitions"}
CANDIDATE_REASON = "Same company and title, no shared ID; added by reconciliation, not merged"
MAX_NOTE = 2000


class DecisionError(ValueError):
    pass


@dataclass
class Event:
    id: int
    created_at_utc: str
    decision: str
    note: str
    label: str


@dataclass
class ReviewItem:
    kind: str
    id: int
    title: str
    detail: str
    hint: str
    severity: str  # ERROR / REVIEW / INFO for issues; CANDIDATE / AUTO_LINKED for duplicates
    row_ids: list[int]
    history: list[Event] = field(default_factory=list)

    @property
    def anchor(self) -> str:
        return f"item-{self.kind}-{self.id}"

    @property
    def latest(self) -> Event | None:
        return self.history[-1] if self.history else None

    @property
    def is_open(self) -> bool:
        return self.latest is None or self.latest.decision not in RESOLVING

    @property
    def needs_attention(self) -> bool:
        return self.severity in ("ERROR", "REVIEW", "CANDIDATE")

    @property
    def choices(self) -> list[tuple[str, str]]:
        options = DECISIONS[self.kind]
        if not self.is_open:
            return [("reopened", options["reopened"])]
        return [(k, v) for k, v in options.items() if k != "reopened" and (self.latest is None or k != self.latest.decision)]


def record_decision(conn: sqlite3.Connection, kind: str, item_id: int, decision: str, note: str = "",
                    origin: str = "web") -> int:
    if decision not in DECISIONS.get(kind, {}):
        raise DecisionError(f"{decision!r} is not a valid decision for {kind!r}")
    if not conn.execute(f"SELECT 1 FROM {TABLES[kind]} WHERE id=?", (item_id,)).fetchone():
        raise DecisionError(f"No {kind} with id {item_id}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.transaction(conn):
        cur = conn.execute("""INSERT INTO decision_events(created_at_utc, subject_type, subject_id, decision, note, origin)
                              VALUES (?, ?, ?, ?, ?, ?)""", (now, kind, item_id, decision, note.strip()[:MAX_NOTE], origin))
    return cur.lastrowid


def _humanize(code: str) -> str:
    return code.replace("_", " ").capitalize()


def review_items(conn: sqlite3.Connection) -> list[ReviewItem]:
    events = defaultdict(list)
    for e in conn.execute("SELECT * FROM decision_events ORDER BY id"):
        events[(e["subject_type"], e["subject_id"])].append(
            Event(e["id"], e["created_at_utc"], e["decision"], e["note"],
                  DECISIONS.get(e["subject_type"], {}).get(e["decision"], e["decision"])))
    items = []
    for r in conn.execute("SELECT * FROM migration_issues ORDER BY id"):
        items.append(ReviewItem(ISSUE, r["id"], _humanize(r["code"]), r["details"], r["suggested_action"], r["severity"],
                                [r["legacy_row_id"]] if r["legacy_row_id"] else [], events[(ISSUE, r["id"])]))
    for r in conn.execute("SELECT * FROM duplicate_reviews ORDER BY id"):
        candidate = r["status"] == "CANDIDATE"
        items.append(ReviewItem(
            DUPLICATE, r["id"], "Same requisition?" if candidate else "Auto-linked requisition", r["reason"],
            "Compare both records; decide whether they are one vacancy" if candidate
            else "Confirm the link, or mark them as different requisitions",
            "CANDIDATE" if candidate else r["status"],
            [x for x in (r["first_legacy_row"], r["second_legacy_row"]) if x], events[(DUPLICATE, r["id"])]))
    rank = {"ERROR": 0, "REVIEW": 1, "CANDIDATE": 2}
    items.sort(key=lambda i: (not i.is_open, not i.needs_attention, rank.get(i.severity, 3), i.kind, i.id))
    return items


def open_rows(items: list[ReviewItem]) -> Counter:
    """Legacy rows with open items that need a decision (drives the 'review' tag and filter)."""
    return Counter(row for i in items if i.is_open and i.needs_attention for row in i.row_ids)


def add_duplicate_candidates(conn: sqlite3.Connection) -> int:
    """Queue pairs of job identities that share company and title but no ID. Idempotent; never merges.

    Pairs inside the Master Tracker alone are already queued by the importer (SAME_TITLE_NEEDS_REVIEW).
    """
    reps: dict[tuple, dict[int, tuple[int, str]]] = defaultdict(dict)
    for r in conn.execute("""SELECT l.id, l.sheet_name, l.values_json, j.job_id FROM legacy_rows l
                             JOIN job_legacy_links j ON j.legacy_row_id = l.id ORDER BY l.id"""):
        values = json.loads(r["values_json"])
        key = (legacy_import.normalize(values[0]), legacy_import.normalize(values[1]))
        if all(key):
            reps[key].setdefault(r["job_id"], (r["id"], r["sheet_name"]))
    added = 0
    for jobs in reps.values():
        for (_, (a, sheet_a)), (_, (b, sheet_b)) in combinations(sorted(jobs.items(), key=lambda kv: kv[1][0]), 2):
            if sheet_a == sheet_b == legacy_import.MASTER:
                continue
            known = conn.execute("""SELECT 1 FROM duplicate_reviews WHERE (first_legacy_row=? AND second_legacy_row=?)
                                    OR (first_legacy_row=? AND second_legacy_row=?)""", (a, b, b, a)).fetchone()
            if not known:
                conn.execute("""INSERT INTO duplicate_reviews(job_id, status, confidence, reason, first_legacy_row, second_legacy_row)
                                VALUES (NULL, 'CANDIDATE', 'MEDIUM', ?, ?, ?)""", (CANDIDATE_REASON, a, b))
                added += 1
    return added
