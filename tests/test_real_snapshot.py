"""Acceptance checks against the private 2026-10-14 snapshot. Skipped when data/source is absent.

Rows are referred to by number only; no names from the private tracker appear in this file.
"""
import pytest

from app import db, legacy_import, validation

pytestmark = pytest.mark.real_data


@pytest.fixture
def real_db(tmp_path, real_snapshot):
    meta = legacy_import.SnapshotMeta.load(legacy_import.meta_path_for(real_snapshot))
    path = tmp_path / "real.sqlite"
    legacy_import.import_snapshot(real_snapshot, meta, path)
    return path


def test_fresh_import_passes_every_validation_check(real_db, real_snapshot, real_reference):
    if real_snapshot.name and legacy_import.file_sha256(real_snapshot) != validation.PHASE2_SNAPSHOT_SHA256:
        pytest.skip("not the Phase 2 baseline snapshot")
    checks = validation.validate(real_db, real_snapshot, real_reference)
    failed = [f"{c.name}: {c.detail}" for c in checks if not c.ok and c.severity == "FAIL"]
    assert failed == []
    if real_reference:
        assert sum(c.name.startswith("Matches Phase 2 dry-run") for c in checks) == len(validation.BASELINE_TABLES)


def test_known_requisitions_are_history_not_new(real_db):
    conn = db.connect(real_db, readonly=True, create=False)
    for a, b in validation.PHASE2_DUPLICATE_GROUPS:
        jobs = {r[0] for r in conn.execute("""SELECT j.job_id FROM job_legacy_links j JOIN legacy_rows l ON l.id=j.legacy_row_id
                                              WHERE l.sheet_name='Master Tracker' AND l.source_row IN (?, ?)""", (a, b))}
        assert len(jobs) == 1  # one requisition identity: it can never surface twice
        states = {r[0] for r in conn.execute("""SELECT s.provisional_state FROM legacy_row_states s JOIN legacy_rows l
                                                ON l.id=s.legacy_row_id WHERE l.sheet_name='Master Tracker' AND l.source_row IN (?, ?)""", (a, b))}
        if states & {"APPLIED", "REJECTED", "IN_PROCESS", "INTERVIEW", "OUTREACH"}:
            # the process history stays attached to that one job, so it is history, not a new opening
            assert conn.execute("SELECT count(*) FROM applications WHERE job_id=?", (jobs.pop(),)).fetchone()[0] >= 1


def test_other_requisitions_at_the_same_company_stay_distinct(real_db):
    conn = db.connect(real_db, readonly=True, create=False)
    company, job = conn.execute("""SELECT j.company_id, j.id FROM jobs j JOIN job_legacy_links x ON x.job_id=j.id
                                   JOIN legacy_rows l ON l.id=x.legacy_row_id
                                   WHERE l.sheet_name='Master Tracker' AND l.source_row=53""").fetchone()
    others = conn.execute("SELECT count(*) FROM jobs WHERE company_id=? AND id<>?", (company, job)).fetchone()[0]
    assert others >= 1


def test_contacts_survive_declined_or_rejected_jobs(real_db):
    conn = db.connect(real_db, readonly=True, create=False)
    missing = conn.execute("""SELECT count(*) FROM legacy_rows l JOIN legacy_row_states s ON s.legacy_row_id=l.id
                              WHERE l.sheet_name='Master Tracker' AND s.provisional_state IN ('SKIPPED','REJECTED','CLOSED')
                              AND trim(coalesce(json_extract(l.values_json, '$[8]'), '')) <> ''
                              AND l.id NOT IN (SELECT legacy_row_id FROM contact_mentions)""").fetchone()[0]
    assert missing == 0


def test_probable_comeet_duplicates_are_queued_not_merged(real_db):
    conn = db.connect(real_db, readonly=True, create=False)
    pairs = {tuple(r) for r in conn.execute("""
        SELECT l1.source_row, l2.source_row FROM duplicate_reviews d
        JOIN legacy_rows l1 ON l1.id=d.first_legacy_row JOIN legacy_rows l2 ON l2.id=d.second_legacy_row
        WHERE d.status='CANDIDATE' AND l1.sheet_name='Master Tracker' AND l2.sheet_name='Comeet Index'""")}
    assert pairs == {(64, 5), (66, 4)}
    assert conn.execute("SELECT count(*) FROM duplicate_reviews WHERE status='CANDIDATE'").fetchone()[0] == 2
