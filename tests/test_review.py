import shutil
import sqlite3

import pytest

from app import db, legacy_import, review, upgrade, validation


@pytest.fixture
def conn(imported_db):
    c = db.connect(imported_db)
    yield c
    c.close()


def item(conn, kind, **match):
    return next(i for i in review.review_items(conn) if i.kind == kind and all(getattr(i, k) == v for k, v in match.items()))


def test_candidate_queued_for_same_company_and_title_without_shared_id(conn):
    candidates = conn.execute("""SELECT l1.sheet_name, l1.source_row, l2.sheet_name, l2.source_row
                                 FROM duplicate_reviews d JOIN legacy_rows l1 ON l1.id=d.first_legacy_row
                                 JOIN legacy_rows l2 ON l2.id=d.second_legacy_row WHERE d.status='CANDIDATE'""").fetchall()
    assert [tuple(c) for c in candidates] == [("Master Tracker", 4, "Comeet Index", 4)]
    # nothing was merged: the two rows keep separate job identities
    jobs = conn.execute("""SELECT count(DISTINCT j.job_id) FROM job_legacy_links j JOIN legacy_rows l ON l.id=j.legacy_row_id
                           WHERE l.source_row=4""").fetchone()[0]
    assert jobs == 2


def test_reconciliation_is_idempotent(conn):
    with db.transaction(conn):
        assert review.add_duplicate_candidates(conn) == 0


def test_decisions_resolve_items_and_keep_full_history(conn):
    issue = item(conn, review.ISSUE, title="Invalid last update")
    assert issue.is_open and issue.needs_attention
    review.record_decision(conn, review.ISSUE, issue.id, "acknowledged", "Row is shifted; original kept")
    issue = item(conn, review.ISSUE, id=issue.id)
    assert not issue.is_open and issue.latest.label == "Keep as is"
    assert issue.choices == [("reopened", "Reopen")]
    review.record_decision(conn, review.ISSUE, issue.id, "reopened")
    issue = item(conn, review.ISSUE, id=issue.id)
    assert issue.is_open and [e.decision for e in issue.history] == ["acknowledged", "reopened"]
    assert issue.history[0].note == "Row is shifted; original kept"


def test_follow_up_and_unsure_keep_items_open(conn):
    candidate = item(conn, review.DUPLICATE, severity="CANDIDATE")
    review.record_decision(conn, review.DUPLICATE, candidate.id, "unsure")
    assert item(conn, review.DUPLICATE, id=candidate.id).is_open
    review.record_decision(conn, review.DUPLICATE, candidate.id, "different_requisitions")
    assert not item(conn, review.DUPLICATE, id=candidate.id).is_open
    assert review.open_rows(review.review_items(conn)).get(candidate.row_ids[1], 0) == 0


@pytest.mark.parametrize("kind, item_id, decision", [
    ("migration_issue", 1, "same_requisition"),  # wrong vocabulary for the kind
    ("duplicate_review", 1, "acknowledged"),
    ("migration_issue", 99999, "acknowledged"),  # unknown item
    ("legacy_row", 1, "acknowledged"),           # unknown kind
])
def test_invalid_decisions_are_rejected(conn, kind, item_id, decision):
    with pytest.raises(review.DecisionError):
        review.record_decision(conn, kind, item_id, decision)
    assert conn.execute("SELECT count(*) FROM decision_events").fetchone()[0] == 0


def test_decision_log_is_append_only(conn):
    review.record_decision(conn, review.ISSUE, 1, "acknowledged")
    for sql in ("UPDATE decision_events SET decision='x'", "DELETE FROM decision_events"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(sql)


def test_upgrade_backs_up_then_migrates_without_losing_rows(tmp_path, snapshot, meta, monkeypatch):
    # build a database at schema v2 (the Milestone 1 state), then upgrade it
    old_migrations = tmp_path / "m2"
    old_migrations.mkdir()
    for f in sorted(db.MIGRATIONS_DIR.glob("000[12]_*.sql")):
        shutil.copy(f, old_migrations)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", old_migrations)
    monkeypatch.setattr(upgrade, "post_migrate", lambda conn: {})  # the M1 importer had no post-migration steps
    path = tmp_path / "old.sqlite"
    legacy_import.import_snapshot(snapshot, meta, path)
    monkeypatch.undo()  # back to the real migrations and reconciliation

    result = upgrade.upgrade(path, tmp_path / "backups")
    assert (result["from"], result["to"]) == (2, db.latest_version())
    assert result["backup"].exists() and "pre-migrate-v2" in result["backup"].name
    assert result["candidates_added"] == 1
    assert result["sources_added"] == 3 and result["profile_added"] == 1  # boards linked in the synthetic tracker
    assert all(c.ok for c in validation.validate(path, snapshot) if c.severity == "FAIL")
    # running it again is a no-op without a new backup
    again = upgrade.upgrade(path, tmp_path / "backups")
    assert again["applied"] == [] and again["backup"] is None and again["candidates_added"] == 0
