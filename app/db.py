"""SQLite connection and transactional schema migrations."""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.config import REPO_ROOT

MIGRATIONS_DIR = REPO_ROOT / "migrations"
BASELINE_VERSION = 1
_MIGRATION_FILE = re.compile(r"(\d{4})_(\w+)\.sql")


def connect(path: Path | str, *, readonly: bool = False, create: bool = True) -> sqlite3.Connection:
    """Autocommit connection; callers use transaction() or migrate() for atomic changes.

    create=False refuses to silently create an empty database at a mistyped path.
    """
    uri = f"file:{Path(path).resolve()}?mode={'rwc' if create else 'rw'}"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def available_migrations(directory: Path | None = None) -> list[tuple[int, str, Path]]:
    directory = directory or MIGRATIONS_DIR
    found = []
    for path in sorted(directory.glob("*.sql")):
        m = _MIGRATION_FILE.fullmatch(path.name)
        if m:
            found.append((int(m.group(1)), m.group(2), path))
    versions = [v for v, _, _ in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError(f"Duplicate migration versions in {directory}")
    return found


def current_version(conn: sqlite3.Connection) -> int:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone():
        return 0
    return conn.execute("SELECT coalesce(max(version), 0) FROM schema_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, target: int | None = None, directory: Path | None = None) -> list[int]:
    """Apply pending migrations, each in its own transaction together with its version row."""
    applied = []
    for version, name, path in available_migrations(directory):
        if version <= current_version(conn) or (target is not None and version > target):
            continue
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        script = (f"BEGIN IMMEDIATE;\n{path.read_text(encoding='utf-8')}\n"
                  f"INSERT INTO schema_version(version, name, applied_at_utc) VALUES ({version}, '{name}', '{now}');\n"
                  "COMMIT;")
        try:
            conn.executescript(script)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


def latest_version(directory: Path | None = None) -> int:
    return max(v for v, _, _ in available_migrations(directory))
