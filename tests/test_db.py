import sqlite3

import pytest

from app import db


def test_migrations_apply_in_order_and_only_once(tmp_path):
    conn = db.connect(tmp_path / "a.sqlite")
    assert db.migrate(conn, target=1) == [1]
    assert db.migrate(conn) == list(range(2, db.latest_version() + 1))
    assert db.migrate(conn) == []
    assert [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")] == \
        list(range(1, db.latest_version() + 1))


def test_failed_migration_rolls_back_completely(tmp_path):
    migrations = tmp_path / "m"
    migrations.mkdir()
    (migrations / "0001_ok.sql").write_text("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT, applied_at_utc TEXT);\nCREATE TABLE a (x);")
    (migrations / "0002_broken.sql").write_text("CREATE TABLE b (x);\nINSERT INTO nowhere VALUES (1);")
    conn = db.connect(tmp_path / "a.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn, directory=migrations)
    assert db.current_version(conn) == 1
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='b'").fetchone()
    assert not conn.in_transaction


def test_foreign_keys_are_enforced(imported_db):
    conn = db.connect(imported_db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs(company_id, title, identity_policy) VALUES (99999, 't', 'p')")


def test_readonly_connection_cannot_write(imported_db):
    conn = db.connect(imported_db, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM companies")


def test_missing_database_is_not_created_silently(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        db.connect(tmp_path / "typo.sqlite", create=False)
    assert not (tmp_path / "typo.sqlite").exists()


def test_init_creates_an_empty_database_once(tmp_path):
    import subprocess, sys
    target = tmp_path / "fresh" / "career_console.sqlite"
    run = lambda: subprocess.run([sys.executable, "-m", "app.cli", "init", "--db", str(target)], capture_output=True, text=True)
    first = run()
    assert first.returncode == 0 and target.exists() and "empty database" in first.stdout
    conn = db.connect(target)
    assert db.current_version(conn) == db.latest_version()
    assert conn.execute("SELECT count(*) FROM legacy_rows").fetchone()[0] == 0
    conn.close()
    second = run()
    assert second.returncode != 0 and "already exists" in (second.stderr + second.stdout)
