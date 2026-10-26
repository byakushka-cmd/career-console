"""Bring an existing database to the latest schema safely: backup first, migrate, reconcile, verify."""
from __future__ import annotations

from pathlib import Path

from app import backup, db, review
from app.validation import BASELINE_TABLES


class UpgradeError(RuntimeError):
    pass


def post_migrate(conn) -> dict:
    """Idempotent data steps that follow schema migrations (call inside a transaction)."""
    from search import pipeline  # imported here: search depends on app, not the other way round
    return {"candidates_added": review.add_duplicate_candidates(conn), **pipeline.ensure_defaults(conn)}


def _counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in BASELINE_TABLES}


def upgrade(db_path: Path, backup_dir: Path) -> dict:
    conn = db.connect(db_path, create=False)
    try:
        start = db.current_version(conn)
        before = _counts(conn)
    finally:
        conn.close()
    saved = backup.backup(db_path, backup_dir, label=f"pre-migrate-v{start}") if start < db.latest_version() else None

    conn = db.connect(db_path, create=False)
    try:
        applied = db.migrate(conn)
        with db.transaction(conn):
            steps = post_migrate(conn)
        added = steps["candidates_added"]
        after = _counts(conn)
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        end = db.current_version(conn)
    finally:
        conn.close()
    # Baseline rows may only grow by the reconciliation candidates; anything else means data changed.
    expected = dict(before, duplicate_reviews=before["duplicate_reviews"] + added)
    changed = {t: (expected[t], after[t]) for t in after if after[t] != expected[t]}
    if ok != "ok" or fk or changed:
        hint = f" Restore with: career-console restore {saved}" if saved else ""
        raise UpgradeError(f"Post-migration check failed (integrity={ok}, fk={len(fk)}, changed={changed}).{hint}")
    return {"from": start, "to": end, "applied": applied, "backup": saved, "candidates_added": added,
            "sources_added": steps["sources"], "profile_added": steps["profile"]}
