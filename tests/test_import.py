import json
import socket
import sqlite3

import pytest

from app import db, legacy_import, validation
from app.legacy_import import SnapshotMeta, classify, file_sha256, import_snapshot


def rows_by_source(conn, sheet="Master Tracker"):
    return {r["source_row"]: r for r in conn.execute("SELECT * FROM legacy_rows WHERE sheet_name=?", (sheet,))}


def job_of(conn, sheet, row):
    return conn.execute("""SELECT j.job_id FROM job_legacy_links j JOIN legacy_rows l ON l.id=j.legacy_row_id
                           WHERE l.sheet_name=? AND l.source_row=?""", (sheet, row)).fetchone()[0]


@pytest.fixture
def conn(imported_db):
    c = db.connect(imported_db, readonly=True, create=False)
    yield c
    c.close()


def test_every_nonempty_row_imported_verbatim_with_provenance(conn, snapshot):
    assert conn.execute("SELECT count(*) FROM legacy_rows").fetchone()[0] == 20
    master = rows_by_source(conn)
    assert sorted(master) == [1, 2, 3, 4, 5, 6, 8, 9, 10, 11]  # empty row 7 skipped, numbering kept
    snap = conn.execute("SELECT * FROM source_snapshots").fetchone()
    assert snap["sha256"] == file_sha256(snapshot) and snap["source_timezone"] == "America/Los_Angeles"
    for r in conn.execute("SELECT * FROM legacy_rows"):
        assert legacy_import.raw_sha(json.loads(r["values_json"])) == r["row_sha256"]
        assert r["snapshot_id"] == snap["id"]


def test_malformed_row_is_flagged_and_left_unshifted(conn):
    values = json.loads(rows_by_source(conn)[5]["values_json"])
    assert values[4] == "No — absent from official careers listing"  # original text stays where it was
    codes = {r["code"]: r["severity"] for r in conn.execute(
        "SELECT m.code, m.severity FROM migration_issues m JOIN legacy_rows l ON l.id=m.legacy_row_id WHERE l.source_row=5 AND l.sheet_name='Master Tracker'")}
    assert codes == {"INVALID_LAST_UPDATE": "ERROR", "INVALID_ACTIVE_CHECKED": "REVIEW"}


def test_blank_shared_string_never_becomes_a_date(conn):
    assert json.loads(rows_by_source(conn)[3]["values_json"])[2] is None
    app = conn.execute("SELECT * FROM applications WHERE provenance='Master Tracker row 3'").fetchone()
    assert app["state"] == "REJECTED" and app["applied_on"] is None
    assert conn.execute("SELECT count(*) FROM migration_issues WHERE code='INVALID_APPLIED_DATE'").fetchone()[0] == 0


def test_same_requisition_is_linked_without_dropping_observations(conn):
    job = job_of(conn, "Master Tracker", 2)
    assert job_of(conn, "Master Tracker", 3) == job == job_of(conn, "Comeet Index", 2)
    assert conn.execute("SELECT count(*) FROM job_legacy_links WHERE job_id=?", (job,)).fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM duplicate_reviews WHERE job_id=? AND status='AUTO_LINKED'", (job,)).fetchone()[0] == 2
    # both application observations survive, and are queued for manual reconciliation
    assert conn.execute("SELECT count(*) FROM applications WHERE job_id=?", (job,)).fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM migration_issues WHERE code='APPLICATION_HISTORY_RECONCILIATION'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM migration_issues WHERE code='INDEX_APPLICATION_FLAG_STALE'").fetchone()[0] == 1


def test_different_requisitions_at_same_company_stay_separate(conn):
    assert job_of(conn, "Master Tracker", 4) != job_of(conn, "Master Tracker", 2)
    assert job_of(conn, "Master Tracker", 4) == job_of(conn, "Master Tracker", 11)  # explicit canonical pointer
    assert job_of(conn, "Comeet Index", 3) not in {job_of(conn, "Master Tracker", r) for r in (2, 4)}


def test_contacts_survive_a_declined_job(conn):
    row6 = conn.execute("""SELECT s.provisional_state, m.raw_contact, c.display_name FROM legacy_rows l
                           JOIN legacy_row_states s ON s.legacy_row_id=l.id
                           JOIN contact_mentions m ON m.legacy_row_id=l.id LEFT JOIN contacts c ON c.id=m.contact_id
                           WHERE l.sheet_name='Master Tracker' AND l.source_row=6""").fetchone()
    assert tuple(row6) == ("SKIPPED", "Avi Cohen — hiring manager", "Avi Cohen")


def test_owner_names_come_from_local_metadata():
    assert classify("Not for Testowner", ("Testowner",)) == "SKIPPED"
    assert classify("Not for Testowner") == "REVIEW"
    assert classify("hоld — Cyrillic о as typed") == "HOLD"


def test_general_application_has_no_job_and_states_are_recorded(conn):
    app = conn.execute("SELECT * FROM applications WHERE provenance='Master Tracker row 8'").fetchone()
    assert app["application_type"] == "GENERAL" and app["job_id"] is None
    states = dict(conn.execute("""SELECT l.source_row, s.provisional_state FROM legacy_row_states s
                                  JOIN legacy_rows l ON l.id=s.legacy_row_id WHERE l.sheet_name='Master Tracker'"""))
    assert states == {2: "APPLIED", 3: "REJECTED", 4: "DISCOVERED", 5: "CLOSED", 6: "SKIPPED", 8: "APPLIED",
                      9: "HOLD", 10: "REVIEW", 11: "DUPLICATE"}
    assert conn.execute("SELECT count(*) FROM migration_issues WHERE code='AMBIGUOUS_STATUS'").fetchone()[0] == 1


def test_stale_summary_is_stored_but_never_used(conn):
    assert conn.execute("SELECT count(*) FROM legacy_rows WHERE sheet_name='Summary'").fetchone()[0] == 3
    assert conn.execute("""SELECT count(*) FROM legacy_row_states s JOIN legacy_rows l ON l.id=s.legacy_row_id
                           WHERE l.sheet_name='Summary'""").fetchone()[0] == 0


def test_database_is_wal_private_and_current(imported_db, conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.current_version(conn) == db.latest_version()
    assert imported_db.stat().st_mode & 0o077 == 0


def test_refuses_to_overwrite_an_existing_database(tmp_path, snapshot, meta):
    target = tmp_path / "existing.sqlite"
    target.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        import_snapshot(snapshot, meta, target)
    assert target.read_bytes() == b"precious"


def test_import_never_touches_snapshot_or_network(tmp_path, snapshot, meta, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    before = (file_sha256(snapshot), snapshot.stat().st_mtime_ns)
    import_snapshot(snapshot, meta, tmp_path / "x.sqlite")
    assert (file_sha256(snapshot), snapshot.stat().st_mtime_ns) == before


def test_failed_import_leaves_nothing_behind(tmp_path, snapshot, meta, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("simulated failure")
    monkeypatch.setattr(legacy_import, "_record_provenance", boom)
    target = tmp_path / "x.sqlite"
    with pytest.raises(RuntimeError, match="simulated"):
        import_snapshot(snapshot, meta, target)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("x.")) == []


def test_expected_checksum_is_enforced(tmp_path, snapshot, meta):
    wrong = SnapshotMeta(**{**meta.__dict__, "expected_sha256": "0" * 64})
    with pytest.raises(ValueError, match="does not match"):
        import_snapshot(snapshot, wrong, tmp_path / "x.sqlite")
    assert not (tmp_path / "x.sqlite").exists()


def test_validation_passes_on_a_clean_import(imported_db, snapshot):
    checks = validation.validate(imported_db, snapshot)
    assert [c.name for c in checks if not c.ok] == []
    names = {c.name for c in checks}
    assert "Round-trip: every snapshot row stored verbatim" in names
    report = validation.render_report(checks, imported_db, snapshot, None)
    assert "**PASS**" in report


def test_source_evidence_cannot_be_edited(imported_db):
    c = db.connect(imported_db)
    for sql in ("UPDATE legacy_rows SET values_json='[]'", "DELETE FROM legacy_rows",
                "UPDATE source_snapshots SET sha256='x'", "DELETE FROM source_snapshots"):
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            c.execute(sql)
    c.close()


def test_validation_detects_a_tampered_row(imported_db, snapshot):
    c = db.connect(imported_db)
    c.execute("DROP TRIGGER legacy_rows_no_update")  # simulate an edit made outside the app
    c.execute("UPDATE legacy_rows SET values_json=replace(values_json, 'Globex', 'Globax') WHERE source_row=4 AND sheet_name='Master Tracker'")
    c.close()
    failed = {c.name for c in validation.validate(imported_db, snapshot) if not c.ok}
    assert {"Every legacy row matches its row SHA-256", "Round-trip: every snapshot row stored verbatim",
            "Evidence immutable, decision log append-only"} <= failed


def test_validation_compares_against_a_reference_database(tmp_path, imported_db, snapshot, meta):
    other = tmp_path / "again.sqlite"
    import_snapshot(snapshot, meta, other)
    assert all(c.ok for c in validation.validate(imported_db, reference_db=other))
    c = db.connect(other)
    c.execute("DELETE FROM duplicate_reviews WHERE id=(SELECT min(id) FROM duplicate_reviews)")
    c.close()
    failed = [c.name for c in validation.validate(imported_db, reference_db=other) if not c.ok]
    assert failed == ["Matches Phase 2 dry-run: duplicate_reviews"]
