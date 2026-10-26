"""Migration validation: integrity, provenance, XLSX round-trip, baseline expectations, and an
optional table-by-table comparison against the Phase 2 dry-run database.

Checks report row numbers and counts only, but the Markdown report is still written to the
private data/reports directory.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from app import db
from app.config import DISPLAY_TZ
from app.legacy_import import (C_URL, COMEET, MASTER, M_APPLIED, M_UPDATED, M_URL, SHEET_COLUMNS,
                               converted_date, file_sha256, row_json)
from app.xlsx_snapshot import read_workbook, split_ref

# Expectations for the 2026-10-14 snapshot, from the Phase 2 audit (row numbers only, no names).
PHASE2_SNAPSHOT_SHA256 = "27824f9e944ea47b89bf7aa7074b3fe5a908622d6da02fff967fe2b6932ebfe2"
PHASE2_BASELINE = {
    "master_records": 203, "comeet_records": 7, "screening_rules": 15, "legacy_rows": 272,
    "jobs": 205, "companies": 156, "applications": 112, "contact_mentions": 105, "contacts": 25,
    "migration_issues": 9, "empty_shared_string_cells": 174,
}
PHASE2_DUPLICATE_GROUPS = {(35, 79), (53, 92), (66, 158), (140, 184)}
PHASE2_SHIFTED_ROW = 98

# Rows that later milestones may append to baseline tables without changing the baseline itself.
APPENDABLE = {"duplicate_reviews": "status = 'CANDIDATE'"}

BASELINE_TABLES = ["source_snapshots", "legacy_rows", "companies", "jobs", "job_legacy_links", "job_sources",
                   "applications", "application_sources", "history_events", "contacts", "contact_mentions",
                   "discovery_records", "screening_rules", "migration_issues", "duplicate_reviews"]


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "FAIL"  # what a failure means: FAIL blocks, WARN is advisory


def _count(conn, sql, *args) -> int:
    return conn.execute(sql, args).fetchone()[0]


def validate(db_path: Path, snapshot: Path | None = None, reference_db: Path | None = None) -> list[Check]:
    checks: list[Check] = []
    add = lambda *a, **k: checks.append(Check(*a, **k))
    conn = db.connect(db_path, readonly=True, create=False)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        add("SQLite integrity_check", ok == "ok", ok)
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        add("Foreign-key violations", not fk, f"{len(fk)} violations")
        version, latest = db.current_version(conn), db.latest_version()
        add("Schema version", version == latest, f"database v{version}, latest v{latest}")
        add("Journal mode WAL", conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal",
            conn.execute("PRAGMA journal_mode").fetchone()[0], severity="WARN")
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        guards = {"decision_events_no_update", "decision_events_no_delete", "legacy_rows_no_update",
                  "legacy_rows_no_delete", "source_snapshots_no_update", "source_snapshots_no_delete"}
        add("Evidence immutable, decision log append-only", guards <= triggers,
            f"{len(guards & triggers)} of {len(guards)} guard triggers present")
        mode = db_path.stat().st_mode & 0o777
        add("Database file permissions", mode & 0o077 == 0, oct(mode), severity="WARN")

        snaps = conn.execute("SELECT * FROM source_snapshots").fetchall()
        add("Exactly one source snapshot recorded", len(snaps) == 1, f"{len(snaps)} recorded")
        snap_row = snaps[0] if snaps else None

        bad_sha = [r["id"] for r in conn.execute("SELECT id, values_json, row_sha256 FROM legacy_rows")
                   if sha256(r["values_json"].encode()).hexdigest() != r["row_sha256"]]
        add("Every legacy row matches its row SHA-256", not bad_sha, f"{len(bad_sha)} mismatches")
        orphan = _count(conn, "SELECT count(*) FROM legacy_rows WHERE snapshot_id NOT IN (SELECT id FROM source_snapshots)")
        add("Every legacy row traces to the snapshot", orphan == 0, f"{orphan} orphans")
        summary_refs = _count(conn, """SELECT count(*) FROM legacy_rows l WHERE l.sheet_name='Summary' AND (
            l.id IN (SELECT legacy_row_id FROM job_legacy_links) OR l.id IN (SELECT legacy_row_id FROM application_sources)
            OR l.id IN (SELECT legacy_row_id FROM history_events) OR l.id IN (SELECT legacy_row_id FROM legacy_row_states))""")
        add("Stale Summary sheet is not used as current state", summary_refs == 0,
            f"{summary_refs} derived records reference Summary rows")

        if snapshot is not None:
            checks += _snapshot_checks(conn, Path(snapshot), snap_row)
        if reference_db is not None:
            checks += _reference_checks(conn, Path(reference_db))
    finally:
        conn.close()
    return checks


def _snapshot_checks(conn, snapshot: Path, snap_row) -> list[Check]:
    out = []
    add = lambda *a, **k: out.append(Check(*a, **k))
    digest = file_sha256(snapshot)
    add("Snapshot file matches recorded SHA-256", snap_row is not None and digest == snap_row["sha256"], digest)
    add("Snapshot file is read-only on disk", not (snapshot.stat().st_mode & 0o222), oct(snapshot.stat().st_mode & 0o777),
        severity="WARN")
    book = read_workbook(snapshot, SHEET_COLUMNS)

    stored = {(r["sheet_name"], r["source_row"]): r["values_json"]
              for r in conn.execute("SELECT sheet_name, source_row, values_json FROM legacy_rows")}
    parsed = {(name, n): row_json(row) for name, sheet in book.items()
              for n, row in enumerate(sheet.rows, 1) if any(v not in (None, "") for v in row)}
    missing = sorted(set(parsed) - set(stored))
    extra = sorted(set(stored) - set(parsed))
    changed = sorted(k for k in set(parsed) & set(stored) if parsed[k] != stored[k])
    add("Round-trip: every snapshot row stored verbatim", not (missing or extra or changed),
        f"{len(parsed)} rows compared; missing {len(missing)}, extra {len(extra)}, changed {len(changed)}")

    # Blank shared-string cells must stay blank, and must never become dates.
    blanks = [(name, ref) for name, sheet in book.items() for ref in sheet.empty_shared_string_cells]
    leaked = []
    for name, ref in blanks:
        row, col = split_ref(ref)
        values = stored.get((name, row + 1))
        if values is not None and json.loads(values)[col] is not None:
            leaked.append(f"{name}!{ref}")
    add("Blank shared-string cells imported as empty", not leaked,
        f"{len(blanks)} blank shared-string cells; {len(leaked)} imported as non-empty")

    bad_dates = []
    for r in conn.execute("""SELECT a.applied_on, h.event_time, l.source_row, l.values_json FROM applications a
                             JOIN application_sources s ON s.application_id=a.id JOIN legacy_rows l ON l.id=s.legacy_row_id
                             LEFT JOIN history_events h ON h.application_id=a.id"""):
        v = json.loads(r["values_json"])
        if r["applied_on"] != converted_date(v[M_APPLIED]) or r["event_time"] != converted_date(v[M_UPDATED]):
            bad_dates.append(r["source_row"])
    add("Imported dates come only from real source date cells", not bad_dates, f"{len(bad_dates)} rows disagree")

    # A link in a URL column whose target differs from the cell text would be a URL the import lost.
    url_columns = {MASTER: M_URL, COMEET: C_URL}
    links = [(name, ref, target) for name, sheet in book.items() for ref, target in sheet.hyperlinks.items()]
    differing, lost = [], []
    for name, ref, target in links:
        row, col = split_ref(ref)
        text = book[name].rows[row][col] if row < len(book[name].rows) and col < len(book[name].rows[row]) else None
        if str(text or "").strip() != target.strip():
            differing.append(f"{name}!{ref}")
            if url_columns.get(name) == col:
                lost.append(f"{name}!{ref}")
    add("No source URL hidden behind hyperlink display text", not lost,
        f"{len(links)} native hyperlinks; {len(lost)} in URL columns differ from cell text; "
        f"elsewhere: {', '.join(d for d in differing if d not in lost) or 'none'}", severity="WARN")

    if digest == PHASE2_SNAPSHOT_SHA256:
        out += _baseline_checks(conn, book)
    return out


def _baseline_checks(conn, book) -> list[Check]:
    out = []
    add = lambda *a, **k: out.append(Check(*a, **k))
    actual = {
        "master_records": _count(conn, "SELECT count(*) FROM legacy_rows WHERE sheet_name=? AND source_row>1", MASTER),
        "comeet_records": _count(conn, "SELECT count(*) FROM legacy_rows WHERE sheet_name=? AND source_row>1", COMEET),
        "screening_rules": _count(conn, "SELECT count(*) FROM screening_rules"),
        "legacy_rows": _count(conn, "SELECT count(*) FROM legacy_rows"),
        "jobs": _count(conn, "SELECT count(*) FROM jobs"),
        "companies": _count(conn, "SELECT count(*) FROM companies"),
        "applications": _count(conn, "SELECT count(*) FROM applications"),
        "contact_mentions": _count(conn, "SELECT count(*) FROM contact_mentions"),
        "contacts": _count(conn, "SELECT count(*) FROM contacts"),
        "migration_issues": _count(conn, "SELECT count(*) FROM migration_issues"),
        "empty_shared_string_cells": sum(len(s.empty_shared_string_cells) for s in book.values()),
    }
    for key, expected in PHASE2_BASELINE.items():
        add(f"Baseline count: {key}", actual[key] == expected, f"expected {expected}, got {actual[key]}")
    groups = {tuple(sorted(r[0] for r in conn.execute(
        "SELECT l.source_row FROM job_legacy_links j JOIN legacy_rows l ON l.id=j.legacy_row_id WHERE j.job_id=?", (job,))))
        for (job,) in conn.execute("SELECT job_id FROM job_legacy_links GROUP BY job_id HAVING count(*)>1")}
    add("Same-requisition groups linked (rows 35/79, 53/92, 66/158, 140/184)", groups == PHASE2_DUPLICATE_GROUPS,
        f"found {sorted(groups)}")
    flagged = _count(conn, """SELECT count(*) FROM migration_issues m JOIN legacy_rows l ON l.id=m.legacy_row_id
                              WHERE l.sheet_name=? AND l.source_row=? AND m.code='INVALID_LAST_UPDATE'""",
                     MASTER, PHASE2_SHIFTED_ROW)
    add(f"Malformed Master Tracker row {PHASE2_SHIFTED_ROW} flagged, not shifted", flagged == 1, f"{flagged} flags")
    return out


def _reference_checks(conn, reference: Path) -> list[Check]:
    out = []
    ref = sqlite3.connect(f"file:{reference.resolve()}?mode=ro&immutable=1", uri=True)
    try:
        for table in BASELINE_TABLES:
            cols = [r[1] for r in ref.execute(f"PRAGMA table_info({table})")]
            if table == "source_snapshots":
                cols = [c for c in cols if c != "exported_at_utc"]  # time of the Phase 2 run
            select = f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid"
            b = [tuple(r) for r in ref.execute(select)]
            last = ref.execute(f"SELECT coalesce(max(rowid), 0) FROM {table}").fetchone()[0]
            a = [tuple(r) for r in conn.execute(select.replace("ORDER BY", f"WHERE rowid <= {last} ORDER BY"))]
            extra = conn.execute(f"SELECT count(*) FROM {table} WHERE rowid > ?", (last,)).fetchone()[0]
            allowed = conn.execute(f"SELECT count(*) FROM {table} WHERE rowid > ? AND {APPENDABLE[table]}",
                                   (last,)).fetchone()[0] if table in APPENDABLE else 0
            detail = f"{len(b)} baseline rows identical" if a == b else f"{len(a)} baseline rows here, {len(b)} in dry-run"
            if extra:
                detail += f"; {extra} added since ({allowed} allowed: {APPENDABLE.get(table, 'none')})"
            out.append(Check(f"Matches Phase 2 dry-run: {table}", a == b and extra == allowed, detail))
        ddl = lambda c: {n: " ".join(s.split()) for n, s in c.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name IN ({})".format(
                ",".join("?" * len(BASELINE_TABLES))), BASELINE_TABLES)}
        out.append(Check("Baseline table DDL identical to dry-run", ddl(conn) == ddl(ref), "normalized whitespace"))
    finally:
        ref.close()
    return out


def render_report(checks: list[Check], db_path: Path, snapshot: Path | None, reference: Path | None) -> str:
    now = datetime.now(ZoneInfo(DISPLAY_TZ)).strftime("%Y-%m-%d %H:%M %Z")
    failed = [c for c in checks if not c.ok and c.severity == "FAIL"]
    warned = [c for c in checks if not c.ok and c.severity == "WARN"]
    lines = [
        "# Career Console — migration validation report", "",
        f"- Generated: {now}",
        f"- Database: `{db_path.name}`",
        f"- Snapshot: `{snapshot.name if snapshot else 'not checked'}`",
        f"- Reference dry-run: `{reference.name if reference else 'not compared'}`",
        f"- Result: **{'PASS' if not failed else 'FAIL'}** — {len(checks)} checks, {len(failed)} failed, {len(warned)} warnings",
        "", "| Check | Result | Detail |", "|---|---|---|",
    ]
    for c in checks:
        mark = "ok" if c.ok else c.severity
        lines.append(f"| {c.name} | {mark} | {c.detail.replace('|', '/')} |")
    lines += ["", "Validation reads the database and snapshot only; nothing is modified, and no network is used."]
    return "\n".join(lines) + "\n"


def write_report(text: str, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo(DISPLAY_TZ)).strftime("%Y%m%d-%H%M%S")
    path = reports_dir / f"validation-{stamp}.md"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path
