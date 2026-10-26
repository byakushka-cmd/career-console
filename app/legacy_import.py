"""Import the Google Sheets XLSX snapshot into a new SQLite database.

Port of the Phase 2 `career_console_phase2_migrate.py`. The identity, classification and
review-queue rules are unchanged (and verified against the Phase 2 dry-run database); what
changed is the plumbing:

- reads the snapshot with the stdlib reader in app.xlsx_snapshot (no proprietary dependency),
- never copies over, renames or deletes the snapshot, and checks its hash is unchanged afterwards,
- never deletes an existing database: it builds into a temporary file and links it into place
  only if the target does not exist yet,
- private snapshot metadata (sheet ID/URL, owner names) comes from a local JSON sidecar, not code,
- makes no network calls at all.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from app import db
from app.xlsx_snapshot import Sheet, read_workbook

TOOL = "career-console legacy_import v1 (port of career_console_phase2_migrate.py)"
CLASSIFIER = "phase2-classify-v1"

# Sheet order matters: it fixes legacy_rows ids, which the Phase 2 database also used.
SHEET_COLUMNS = {"Summary": 6, "Master Tracker": 17, "Screening Rules": 2, "Comeet Index": 11}
MASTER, COMEET, RULES = "Master Tracker", "Comeet Index", "Screening Rules"
# Phase 2 rule: Comeet Index rows from 8 on are roll-up/sweep notes, not requisitions.
COMEET_FIRST_ROLLUP_ROW = 8

# Master Tracker columns
M_COMPANY, M_ROLE, M_APPLIED, M_STATUS, M_UPDATED, M_ACTIVE, M_ACTIVE_CHECKED = range(7)
M_CHANNEL, M_CONTACT, M_HISTORY, M_URL, M_EVIDENCE = 7, 8, 13, 14, 15
# Comeet Index columns
C_COMPANY, C_ROLE, C_DISCOVERY, C_LIVE, C_CHECKED, C_IN_APPS, C_NOTE, C_URL = 0, 1, 4, 5, 6, 8, 9, 10

MASTER_DATE_COLUMNS = (M_APPLIED, M_UPDATED, M_ACTIVE_CHECKED)


@dataclass(frozen=True)
class SnapshotMeta:
    """Facts about the snapshot that are not inside the XLSX. Kept in a local, gitignored sidecar."""
    source_file_id: str
    source_url: str
    source_last_modified_utc: str
    source_timezone: str
    exported_at_utc: str | None = None
    owner_aliases: tuple[str, ...] = ()
    expected_sha256: str | None = None

    @classmethod
    def load(cls, path: Path) -> "SnapshotMeta":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        raw["owner_aliases"] = tuple(raw.get("owner_aliases", ()))
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


def meta_path_for(snapshot: Path) -> Path:
    return snapshot.with_name(snapshot.stem + ".meta.json")


@dataclass
class ImportResult:
    db_path: Path
    snapshot_sha256: str
    counts: dict[str, int]
    report: dict = field(default_factory=dict)


# ---- unchanged Phase 2 rules -------------------------------------------------------------

def cell_text(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def normalize(s) -> str:
    return re.sub(r"\s+", " ", cell_text(s).casefold()).strip()


def converted_date(value) -> str | None:
    """Excel serial or ISO date -> ISO date. Date-only: no timezone shift is ever applied."""
    if value in ("", None):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 40000 <= float(value) < 60000:
            return (datetime(1899, 12, 30) + timedelta(days=float(value))).date().isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    return None


def row_json(row: list) -> str:
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def raw_sha(row: list) -> str:
    return sha256(row_json(row).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    h = sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def url_identity(url):
    if not isinstance(url, str) or not url.lower().startswith(("https://", "http://")):
        return None
    from urllib.parse import urlsplit  # parsing only; no network
    p = urlsplit(url.strip())
    host = p.hostname.lower() if p.hostname else ""
    path = re.sub("/+", "/", p.path).rstrip("/")
    if not path or len(path) <= 1:
        return None
    # Generic careers pages / homepages are not requisition identity.
    if re.fullmatch(r"/(?:careers?|jobs?|positions?|apply|en-us/careers?)(?:/(?:apply|all))?", path, re.I):
        return None
    if "linkedin.com" in host:
        nums = re.search(r"/jobs/view/(?:[^/]*-)?(\d{8,})", path)
        return "linkedin:" + nums.group(1) if nums else None
    if "greenhouse.io" in host:
        m = re.search(r"/jobs/(\d+)", path)
        if m:
            return f"greenhouse:{host}:{m.group(1)}"
    if "ashbyhq.com" in host:
        m = re.search(r"/([0-9a-f]{8}-[0-9a-f-]{27,})$", path, re.I)
        if m:
            return f"ashby:{host}:{m.group(1).lower()}"
    # Exact path match is strong unless this looks like a broad index.
    if re.search(r"/(jobs?|positions?)/?$", path, re.I):
        return None
    return f"url:{host}{path.casefold()}"


def req_identity(company, role):
    r = cell_text(role)
    # Prefer genuinely explicit requisition IDs, never arbitrary numeric tokens.
    patterns = [r"\b(JR\d{5,})\b", r"\bReq(?:uisition)?\s*#?\s*(\d{4,})\b", r"\bjob\s*#\s*(\d{6,})\b"]
    for pattern in patterns:
        m = re.search(pattern, r, re.I)
        if m:
            return f"req:{normalize(company)}:{m.group(1).lower()}"
    return None


def classify(s, owner_aliases: tuple[str, ...] = ()) -> str:
    """Provisional status bucket from the raw Status text. Order of checks is significant."""
    t = normalize(s)
    if not t:
        return "REVIEW"
    if t.startswith(("hold", "hоld")):  # second form has a Cyrillic 'о', as typed in the source
        return "HOLD"
    if "duplicate" in t:
        return "DUPLICATE"
    if "not applied" in t or "not submitted" in t or "not confirmed applied" in t:
        if "closed" in t:
            return "CLOSED"
        if "excluded" in t or "not suitable" in t:
            return "SKIPPED"
        if "hold" in t:
            return "HOLD"
        return "DISCOVERED"
    if "blocked" in t or "attempted" in t:
        return "BLOCKED"
    if "interview" in t and "scheduled" in t:
        return "INTERVIEW"
    if "recruiter screen passed" in t or "application under review" in t:
        return "IN_PROCESS"
    if "rejected" in t:
        return "REJECTED"
    if "cv sent" in t or "outreach" in t or "direct email" in t:
        return "OUTREACH"
    if "applied" in t or "submitted" in t or "receipt confirmed" in t:
        return "APPLIED"
    if "candidate database" in t or "ats account" in t:
        return "PROFILE_ONLY"
    if "recruiter contact" in t or "recruiter conversation" in t:
        return "CONTACT_ONLY"
    if "closed" in t or "inactive" in t:
        return "CLOSED"
    if ("declined" in t or any(f"not for {a.casefold()}" in t for a in owner_aliases)
            or "not suitable" in t):
        return "SKIPPED"
    if "hold" in t:
        return "HOLD"
    if "lead" in t or "discovery" in t or "verified employer" in t:
        return "DISCOVERED"
    if "considering" in t:
        return "CONSIDERING"
    return "REVIEW"


def is_general(role) -> bool:
    s = normalize(role)
    return (s in ("general application", "role unspecified", "qa / automation leadership")
            or s.startswith("general application /") or "candidate database / role unspecified" in s)


def has_specific_person(raw) -> bool:
    t = cell_text(raw)
    if not t or normalize(t) in ("unknown", "503", "not identified", "owner not verified",
                                 "individual recruiter not verified", "recruiter not verified", "next point"):
        return False
    if "@" in t or "/" in t or "not verified" in t.casefold() or "not recorded" in t.casefold():
        return False
    name = t.split(" — ")[0].split(" (")[0].strip()
    return bool(re.fullmatch(r"[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)+", name))


def _nonempty(row: list) -> bool:
    return any(v is not None and v != "" for v in row)


# ---- import ------------------------------------------------------------------------------

def import_snapshot(snapshot: Path, meta: SnapshotMeta, db_path: Path) -> ImportResult:
    snapshot, db_path = Path(snapshot), Path(db_path)
    if db_path.exists():
        raise FileExistsError(f"{db_path} already exists; refusing to overwrite. Back it up and choose a new path.")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    digest = file_sha256(snapshot)
    if meta.expected_sha256 and digest != meta.expected_sha256:
        raise ValueError(f"Snapshot SHA-256 {digest} does not match expected {meta.expected_sha256}")
    book = read_workbook(snapshot, SHEET_COLUMNS)

    tmp = db_path.with_name(db_path.name + ".building")
    _remove_db_files(tmp)  # only ever our own leftover temp file
    try:
        conn = db.connect(tmp)
        db.migrate(conn, target=db.BASELINE_VERSION)
        with db.transaction(conn):
            report = _load(conn, book, meta, digest, snapshot)
        db.migrate(conn)
        with db.transaction(conn):
            _record_provenance(conn, book, meta, started)
            from app.upgrade import post_migrate  # late import: upgrade depends on this module
            report.update(post_migrate(conn))
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if ok != "ok" or fk:
            raise RuntimeError(f"Import integrity failed: {ok} {len(fk)} FK violations")
        conn.execute("PRAGMA journal_mode=WAL")
        counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in _COUNTED_TABLES}
        conn.close()
        if file_sha256(snapshot) != digest:
            raise RuntimeError("Snapshot changed during import")
        tmp.chmod(0o600)
        os.link(tmp, db_path)  # fails if db_path appeared meanwhile: never clobbers
        tmp.unlink()
    except BaseException:
        _remove_db_files(tmp)
        raise
    return ImportResult(db_path=db_path, snapshot_sha256=digest, counts=counts, report=report)


_COUNTED_TABLES = ["legacy_rows", "companies", "jobs", "job_legacy_links", "job_sources", "applications",
                   "history_events", "contacts", "contact_mentions", "discovery_records", "screening_rules",
                   "migration_issues", "duplicate_reviews"]


def _remove_db_files(path: Path) -> None:
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
        if p.exists():
            p.unlink()


def _load(conn, book: dict[str, Sheet], meta: SnapshotMeta, digest: str, snapshot: Path) -> dict:
    data = {name: sheet.rows for name, sheet in book.items()}
    exported = meta.exported_at_utc or datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("""INSERT INTO source_snapshots(file_name,sha256,size_bytes,source_file_id,source_url,
                    source_last_modified_utc,source_timezone,exported_at_utc,mode) VALUES (?,?,?,?,?,?,?,?,?)""",
                 (snapshot.name, digest, snapshot.stat().st_size, meta.source_file_id, meta.source_url,
                  meta.source_last_modified_utc, meta.source_timezone, exported, "READ_ONLY_DRY_RUN"))
    lr = {}
    for sheet in SHEET_COLUMNS:
        for n, row in enumerate(data[sheet], 1):
            if not _nonempty(row):
                continue
            cur = conn.execute(
                "INSERT INTO legacy_rows(snapshot_id,sheet_name,source_row,values_json,row_sha256) VALUES (1,?,?,?,?)",
                (sheet, n, row_json(row), raw_sha(row)))
            lr[(sheet, n)] = cur.lastrowid

    companies = {}

    def company_id(company):
        key = normalize(company)
        if key not in companies:
            companies[key] = conn.execute("INSERT INTO companies(name,key) VALUES (?,?)", (str(company), key)).lastrowid
        return companies[key]

    def issue(kind, msg, sheet, row, sev="REVIEW", action="Review manually; preserve original"):
        conn.execute("INSERT INTO migration_issues(legacy_row_id,severity,code,details,suggested_action) VALUES (?,?,?,?,?)",
                     (lr.get((sheet, row)), sev, kind, msg, action))

    master = {i + 1: row for i, row in enumerate(data[MASTER]) if i > 0 and _nonempty(row)}
    idx = {i + 1: row for i, row in enumerate(data[COMEET]) if i > 0 and _nonempty(row)}

    # Determine safely provable same-requisition clusters; no title-only auto merge.
    nodes = ([(MASTER, r, v) for r, v in master.items()]
             + [(COMEET, r, v) for r, v in idx.items() if r < COMEET_FIRST_ROLLUP_ROW])
    parent = list(range(len(nodes)))
    tags = []
    for sheet, row, v in nodes:
        url = v[M_URL] if sheet == MASTER else v[C_URL]
        t = set()
        if ui := url_identity(url):
            t.add(ui)
        if ri := req_identity(v[0], v[1]):
            t.add(ri)
        tags.append(t)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    seen = {}
    for i, tagset in enumerate(tags):
        for tag in sorted(tagset):
            if tag in seen:
                j = seen[tag]
                # Prevent accidental reuse of a provider ID across distinct employer entities.
                if normalize(nodes[i][2][0]) == normalize(nodes[j][2][0]):
                    join(i, j)
            else:
                seen[tag] = i
    # Only an explicit 'see canonical row N' permits an alias merge without an external ID.
    for i, (sheet, row, v) in enumerate(nodes):
        if sheet != MASTER:
            continue
        m = re.search(r"canonical\s+\w+\s+row\s+(\d+)", " ".join(cell_text(x) for x in v[3:]), re.I)
        if m:
            target = int(m.group(1))
            for j, (s2, r2, v2) in enumerate(nodes):
                if s2 == sheet and r2 == target and normalize(v2[0]) == normalize(v[0]):
                    join(i, j)

    by_group = defaultdict(list)
    for i in range(len(nodes)):
        by_group[find(i)].append(i)
    member_jobs, strong_groups = {}, []
    for member_ids in by_group.values():
        chosen = sorted(member_ids, key=lambda i: (nodes[i][0] != MASTER, nodes[i][1]))[0]
        sh, row, v = nodes[chosen]
        cid = company_id(v[0])
        jobid = conn.execute(
            "INSERT INTO jobs(company_id,title,status_hint,identity_policy,is_manual_review) VALUES (?,?,?,?,?)",
            (cid, v[1], cell_text(v[M_STATUS] if sh == MASTER else v[C_DISCOVERY]),
             "strong-identifiers-or-explicit-alias" if len(member_ids) > 1 else "source-row-identity", 0)).lastrowid
        for i in member_ids:
            shi, ri, vi = nodes[i]
            member_jobs[(shi, ri)] = jobid
            ref = lr[(shi, ri)]
            reason = "strong identifier / explicit alias" if len(member_ids) > 1 else "first observation"
            conn.execute("INSERT INTO job_legacy_links(job_id,legacy_row_id,reason) VALUES (?,?,?)", (jobid, ref, reason))
            url = vi[M_URL] if shi == MASTER else vi[C_URL]
            if cell_text(url).startswith(("http://", "https://")):
                conn.execute("INSERT INTO job_sources(job_id,legacy_row_id,url,identity) VALUES (?,?,?,?)",
                             (jobid, ref, url, url_identity(url)))
        if len(member_ids) > 1:
            strong_groups.append({"job_id": jobid, "rows": [(nodes[i][0], nodes[i][1]) for i in member_ids]})
            root = member_ids[0]
            for i in member_ids[1:]:
                conn.execute("""INSERT INTO duplicate_reviews(job_id,status,confidence,reason,first_legacy_row,second_legacy_row)
                                VALUES (?,?,?,?,?,?)""",
                             (jobid, "AUTO_LINKED", "HIGH", "Shared official/source ID or explicit canonical row pointer",
                              lr[(nodes[root][0], nodes[root][1])], lr[(nodes[i][0], nodes[i][1])]))

    # Include the Comeet roll-up row as a discovery record without inventing a requisition.
    for r, v in idx.items():
        conn.execute("INSERT INTO discovery_records(legacy_row_id,job_id,source_status,verified_raw,note_raw) VALUES (?,?,?,?,?)",
                     (lr[(COMEET, r)], member_jobs.get((COMEET, r)), cell_text(v[C_DISCOVERY]),
                      cell_text(v[C_LIVE]), cell_text(v[C_NOTE])))
    for r, v in enumerate(data[RULES][1:], 2):
        if cell_text(v[0]):
            conn.execute("INSERT INTO screening_rules(legacy_row_id,rule_name,guidance) VALUES (?,?,?)",
                         (lr[(RULES, r)], cell_text(v[0]), cell_text(v[1])))

    normalized, dates, staged_appl, contacts = Counter(), Counter(), [], {}
    for row, v in master.items():
        company, role, applied, status, updated, active, activechecked, channel, contact = v[:9]
        state = classify(status, meta.owner_aliases)
        normalized[state] += 1
        apdate, upd, actdate = converted_date(applied), converted_date(updated), converted_date(activechecked)
        if apdate:
            dates["applied_date_present"] += 1
        if upd:
            dates["last_update_valid"] += 1
        if cell_text(applied) and not apdate:
            issue("INVALID_APPLIED_DATE", f"Original Applied={applied!r}", MASTER, row)
        if cell_text(updated) and not upd:
            issue("INVALID_LAST_UPDATE", f"Last update={updated!r}; adjacent fields may be shifted", MASTER, row,
                  "ERROR", "Review source row position; do not shift cells automatically")
        if cell_text(activechecked) and not actdate:
            issue("INVALID_ACTIVE_CHECKED", f"Active checked={activechecked!r}", MASTER, row)
        if not cell_text(v[M_URL]):
            dates["missing_source_url"] += 1
        if cell_text(contact):
            co = company_id(company)
            person_id = None
            if has_specific_person(contact):
                display = cell_text(contact).split(" — ")[0].split(" (")[0]
                key = normalize(display)
                if key not in contacts:
                    contacts[key] = conn.execute("INSERT INTO contacts(display_name,key) VALUES (?,?)", (display, key)).lastrowid
                person_id = contacts[key]
            conn.execute("INSERT INTO contact_mentions(legacy_row_id,company_id,raw_contact,contact_id) VALUES (?,?,?,?)",
                         (lr[(MASTER, row)], co, cell_text(contact), person_id))
        if state == "REVIEW":
            issue("AMBIGUOUS_STATUS", f"Status={status!r}", MASTER, row, "REVIEW",
                  "Manually map to application / vacancy / decision state")
        if is_general(role):
            dates["general_app_or_unspecified"] += 1
        if state in ("APPLIED", "REJECTED", "INTERVIEW", "IN_PROCESS", "OUTREACH", "CONTACT_ONLY", "PROFILE_ONLY") or apdate:
            staged_appl.append((row, v, state, apdate))
        if state in ("APPLIED", "REJECTED", "INTERVIEW", "IN_PROCESS") and not apdate:
            issue("MISSING_APPLICATION_DATE", f"Application-process state {state}; applied date absent", MASTER, row,
                  "INFO", "Keep null date; do not infer from last-update")

    # One record per evidence-bearing legacy row preserves provenance; shared applications are a review task.
    for row, v, state, apdate in staged_appl:
        cid = company_id(v[M_COMPANY])
        jobid = None if is_general(v[M_ROLE]) else member_jobs[(MASTER, row)]
        typ = ("GENERAL" if is_general(v[M_ROLE]) else "OUTREACH" if state in ("OUTREACH", "CONTACT_ONLY")
               else "PROFILE" if state == "PROFILE_ONLY" else "ROLE")
        appid = conn.execute(
            """INSERT INTO applications(job_id,company_id,state,applied_on,current_status_raw,application_type,provenance,review_required)
               VALUES (?,?,?,?,?,?,?,?)""",
            (jobid, cid, state, apdate, cell_text(v[M_STATUS]), typ, f"Master Tracker row {row}",
             int(state not in ("APPLIED", "REJECTED", "INTERVIEW", "IN_PROCESS")))).lastrowid
        conn.execute("INSERT INTO application_sources(application_id,legacy_row_id) VALUES (?,?)", (appid, lr[(MASTER, row)]))
        conn.execute("""INSERT INTO history_events(application_id,legacy_row_id,event_type,event_text,event_time,provenance)
                        VALUES (?,?,?,?,?,?)""",
                     (appid, lr[(MASTER, row)], "IMPORTED_SNAPSHOT", cell_text(v[M_HISTORY]), converted_date(v[M_UPDATED]),
                      "Legacy tracker snapshot only; NOT a reconstructed event sequence"))

    # Exact matching normalized company/title is a candidate for review only, never an automatic merge.
    titlegroups = defaultdict(list)
    for row, v in master.items():
        titlegroups[(normalize(v[0]), normalize(v[1]))].append(row)
    title_only = []
    for key, rs in titlegroups.items():
        if len(rs) > 1 and len({member_jobs[(MASTER, r)] for r in rs}) > 1:
            title_only.append((key, rs))
            for r in rs:
                issue("SAME_TITLE_NEEDS_REVIEW", f"Same company/title across Master Tracker rows {rs}; not auto-merged",
                      MASTER, r, "REVIEW", "Compare requisition IDs and employer records")
    # Cross-sheet stale 'In Applications? No' flags, without modifying that sheet.
    stale_index = []
    for idxrow, v in idx.items():
        if idxrow >= COMEET_FIRST_ROLLUP_ROW:
            continue
        if normalize(v[C_IN_APPS]) == "no":
            hit = [r for r, m in master.items() if normalize(m[0]) == normalize(v[0]) and normalize(m[1]) == normalize(v[1])
                   and classify(m[M_STATUS], meta.owner_aliases) in ("APPLIED", "REJECTED", "INTERVIEW", "IN_PROCESS")]
            if hit:
                stale_index.append((idxrow, hit))
                issue("INDEX_APPLICATION_FLAG_STALE", f"Comeet Index says No, but Master Tracker row(s) {hit} indicate application",
                      COMEET, idxrow, "REVIEW", "Update derived console only; preserve original index")
    # Duplicate application evidence for the same strong identity: hold for manual reconciliation.
    applgroups = defaultdict(list)
    for row, v, state, _ in staged_appl:
        if state in ("APPLIED", "REJECTED", "INTERVIEW", "IN_PROCESS"):
            if jobid := member_jobs.get((MASTER, row)):
                applgroups[jobid].append(row)
    same_appl = {k: rs for k, rs in applgroups.items() if len(rs) > 1}
    for jobid, rs in same_appl.items():
        issue("APPLICATION_HISTORY_RECONCILIATION", f"Multiple process records {rs} map to one job; do not drop any record",
              MASTER, rs[0], "REVIEW", "Merge only with evidence after checking resubmissions/history")

    return {"normalized_statuses": dict(normalized.most_common()), "date_quality": dict(dates),
            "strong_identity_groups": strong_groups, "title_only_review_groups": title_only,
            "stale_index": stale_index, "application_overlap_review": same_appl,
            "empty_shared_string_cells": sum(len(s.empty_shared_string_cells) for s in book.values()),
            "formula_cells": sum(len(s.formula_cells) for s in book.values())}


def provisional_state(sheet: str, source_row: int, values: list, owner_aliases: tuple[str, ...]) -> str | None:
    if source_row == 1:
        return None
    if sheet == MASTER:
        return classify(values[M_STATUS], owner_aliases)
    if sheet == COMEET:
        return "NOTE" if source_row >= COMEET_FIRST_ROLLUP_ROW else "DISCOVERED"
    return None


def _record_provenance(conn, book: dict[str, Sheet], meta: SnapshotMeta, started: str) -> None:
    rows = conn.execute("SELECT id, sheet_name, source_row, values_json FROM legacy_rows").fetchall()
    for r in rows:
        state = provisional_state(r["sheet_name"], r["source_row"], json.loads(r["values_json"]), meta.owner_aliases)
        if state:
            conn.execute("INSERT INTO legacy_row_states(legacy_row_id, provisional_state, classifier) VALUES (?,?,?)",
                         (r["id"], state, CLASSIFIER))
    conn.execute("""INSERT INTO import_batches(snapshot_id, started_at_utc, finished_at_utc, tool, legacy_rows, notes)
                    VALUES (1, ?, ?, ?, ?, ?)""",
                 (started, datetime.now(timezone.utc).isoformat(timespec="seconds"), TOOL, len(rows),
                  "Read-only snapshot import; no write-back to the source spreadsheet."))
