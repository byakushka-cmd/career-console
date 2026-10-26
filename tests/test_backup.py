import pytest

from app import backup, db


def company_count(path):
    conn = db.connect(path, readonly=True, create=False)
    try:
        return conn.execute("SELECT count(*) FROM companies").fetchone()[0]
    finally:
        conn.close()


def test_backup_and_restore_round_trip(tmp_path, imported_db):
    backups = tmp_path / "backups"
    original = company_count(imported_db)
    saved = backup.backup(imported_db, backups)
    assert saved.stat().st_mode & 0o077 == 0 and company_count(saved) == original
    assert sorted(p.name for p in backups.iterdir()) == [saved.name]  # one self-contained file

    conn = db.connect(imported_db)
    conn.execute("INSERT INTO companies(name, key) VALUES ('Later Co', 'later co')")
    conn.close()

    safety = backup.restore(saved, imported_db, backups)
    assert company_count(imported_db) == original
    conn = db.connect(imported_db, readonly=True)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    assert company_count(safety) == original + 1  # the state before restore was kept, not lost


def test_restore_rejects_a_bad_backup_and_keeps_the_database(tmp_path, imported_db):
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database" * 100)
    before = company_count(imported_db)
    with pytest.raises(ValueError):
        backup.restore(bad, imported_db, tmp_path / "backups")
    assert company_count(imported_db) == before
