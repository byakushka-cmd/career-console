"""Fixtures. Everything here is synthetic: fictional companies and people, generated at test time."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app import legacy_import
from app.config import REPO_ROOT
from tests.xlsx_factory import build_xlsx

MASTER_HEADER = ["Company", "Role", "Applied", "Status", "Last update", "Still active?", "Active checked", "Channel",
                 "Recruiter / contact", "Next action", "Priority", "Auto-search rule", "Decision / screening note",
                 "History / notes", "Active source URL", "Evidence", "Funnel signal"]
COMEET_HEADER = ["Company", "Role", "Location", "ATS", "Discovery status", "Live verified", "Checked", "Fit",
                 "In Applications?", "Decision / note", "Source URL"]


def master_row(company, role, applied=None, status=None, updated=None, contact=None, history=None, url=None,
               note=None, active_checked=None, **overrides):
    row = [company, role, applied, status, updated, None, active_checked, None, contact, None, None, None, note,
           history, url, None, None]
    for index, value in overrides.items():
        row[int(index.lstrip("c"))] = value
    return row


def synthetic_sheets() -> dict[str, list[list]]:
    master = [
        MASTER_HEADER,
        # 2 + 3: same requisition (explicit req ID); 3 has a blank applied cell (empty shared string)
        master_row("Acme Cloud", "QA Automation Lead (Req 12345)", 46265, "Applied / confirmation received", 46270,
                   contact="Dana Levi — recruiter", history="Applied via careers site",
                   url="https://boards.greenhouse.io/acme/jobs/111"),
        master_row("Acme Cloud", "QA Automation Lead (Req 12345)", "", "Rejected — employer email", 46290,
                   history="Rejection email", url="https://www.linkedin.com/jobs/view/1234567890"),
        # 4: discovered, never applied
        master_row("Globex", "Senior QA Engineer", None, "Discovery — NOT APPLIED", 46300,
                   url="https://jobs.lever.co/globex/abc-123"),
        # 5: malformed row: text where the Last update date belongs
        master_row("Initech", "QA Lead", None, "Closed / stale", "No — absent from official careers listing",
                   active_checked="Stale mirror / aggregator", c5=46298),
        # 6: declined; uses the owner's name, which comes from local metadata only
        master_row("Hooli", "QA Manager", None, "Not for Testowner", 46280, contact="Avi Cohen — hiring manager"),
        [None] * 17,  # 7: empty row inside the data
        # 8: general application without a concrete job
        master_row("Umbrella", "General application", 46250, "Applied", 46250),
        # 9: hold
        master_row("Stark Labs", "Senior SDET", None, "HOLD — not listed on official employer careers; NOT APPLIED", 46295),
        # 10: status the rules cannot read
        master_row("Wayne Tech", "Head of QA", None, "Something ambiguous", 46296),
        # 11: explicit pointer to row 4 makes it the same job without an external ID
        master_row("Globex", "Senior QA Engineer (repost)", None, "Duplicate observation", 46302,
                   note="Same posting — see canonical tracker row 4"),
    ]
    comeet = [
        COMEET_HEADER,
        ["Acme Cloud", "QA Automation Lead (Req 12345)", "Tel Aviv", "Comeet", "Found on current company board", "Yes",
         "2026-09-27", "Strong", "No", "Board lists the role", "https://www.comeet.com/jobs/acme/AA.001"],
        ["Soylent", "QA Engineer", "Haifa", "Comeet", "Discovery only", "Not yet", "2026-09-27", "Pending", "No",
         "Do not treat as active until verified", None],
        # same company and title as tracker row 4 but no shared ID: a duplicate *candidate*, never auto-merged
        ["Globex", "Senior QA Engineer", "Tel Aviv", "Comeet", "Discovery only", "Not yet", "2026-09-28", "Pending", "No",
         "Not verified yet", None],
    ]
    rules = [["Rule", "How to use it"], ["Already applied", "The exact role is not new again."],
             ["Rejected / process ended", "Do not resurface the exact req."], [None, None]]
    summary = [["Tracker · refreshed 2026-09-01", None, None, None, None, None], [None] * 6,
               ["Metric", "Value", None, None, None, None], ["Tracked rows", 3, None, None, None, None]]
    return {"Summary": summary, "Master Tracker": master, "Screening Rules": rules, "Comeet Index": comeet}


SYNTHETIC_META = {
    "source_file_id": "synthetic-file-id",
    "source_url": "https://example.invalid/sheet",
    "source_last_modified_utc": "2026-09-30T08:00:00Z",
    "source_timezone": "America/Los_Angeles",
    "exported_at_utc": "2026-09-30T09:00:00+00:00",
    "owner_aliases": ["Testowner"],
}


# One fixed "today" for every test: ages (fresh, 14-day window, expired records) must not drift with the calendar.
TODAY = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr("search.pipeline._now", lambda: TODAY)
    monkeypatch.setattr("app.tracker.today", lambda: date(2026, 10, 14))
    return TODAY


@pytest.fixture
def snapshot(tmp_path) -> Path:
    path = build_xlsx(tmp_path / "snapshot.xlsx", synthetic_sheets(),
                      hyperlinks={"Master Tracker": {"O2": "https://boards.greenhouse.io/acme/jobs/111",
                                                     "A4": "http://globex.example"}})
    path.chmod(0o444)
    (tmp_path / "snapshot.meta.json").write_text(json.dumps(SYNTHETIC_META))
    return path


@pytest.fixture
def meta(snapshot) -> legacy_import.SnapshotMeta:
    return legacy_import.SnapshotMeta.load(legacy_import.meta_path_for(snapshot))


@pytest.fixture
def imported_db(tmp_path, snapshot, meta) -> Path:
    db_path = tmp_path / "db" / "career_console.sqlite"
    db_path.parent.mkdir()
    legacy_import.import_snapshot(snapshot, meta, db_path)
    return db_path


# ---- private real data (local only) --------------------------------------------------------

REAL_SOURCE = REPO_ROOT / "data" / "source"
REAL_REFERENCE = REPO_ROOT / "data" / "reference"


@pytest.fixture
def real_snapshot() -> Path:
    found = sorted(REAL_SOURCE.glob("*.xlsx"))
    if not found or not legacy_import.meta_path_for(found[-1]).exists():
        pytest.skip("private snapshot not present in data/source")
    return found[-1]


@pytest.fixture
def real_reference() -> Path | None:
    found = sorted(REAL_REFERENCE.glob("*.sqlite"))
    return found[-1] if found else None
