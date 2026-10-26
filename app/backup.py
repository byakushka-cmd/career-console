"""Online backup and restore through SQLite's backup API (safe with WAL; stop the server first for restore)."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app import db
from app.config import DISPLAY_TZ


def _check(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        has_version = conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone()
    except sqlite3.DatabaseError as e:
        raise ValueError(f"{path.name} is not a readable SQLite database: {e}") from e
    finally:
        conn.close()
    if ok != "ok" or not has_version:
        raise ValueError(f"{path.name} failed checks (integrity={ok}, schema_version table={'yes' if has_version else 'no'})")


def backup(db_path: Path, backup_dir: Path, label: str = "") -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    stamp = datetime.now(ZoneInfo(DISPLAY_TZ)).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"career_console-{stamp}{'-' + label if label else ''}.sqlite"
    n = 1
    while target.exists():
        target = target.with_name(f"{target.stem}-{n}.sqlite")
        n += 1
    tmp = target.with_suffix(".partial")
    src = db.connect(db_path, readonly=True, create=False)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")  # a backup is one self-contained file, no -wal/-shm
    finally:
        dst.close()
        src.close()
    os.chmod(tmp, 0o600)
    _check(tmp)
    tmp.rename(target)
    return target


def restore(backup_file: Path, db_path: Path, backup_dir: Path) -> Path | None:
    """Restore backup_file into db_path. The current database is backed up first; returns that backup."""
    _check(backup_file)
    safety = backup(db_path, backup_dir, label="pre-restore") if db_path.exists() else None
    src = sqlite3.connect(f"file:{backup_file.resolve()}?mode=ro", uri=True)
    dst = sqlite3.connect(db_path)
    try:
        src.backup(dst)  # page-level copy through SQLite, so an existing WAL cannot be replayed onto it
        dst.execute("PRAGMA journal_mode=WAL")  # the live database stays in WAL mode
    finally:
        dst.close()
        src.close()
    os.chmod(db_path, 0o600)
    _check(db_path)
    return safety
