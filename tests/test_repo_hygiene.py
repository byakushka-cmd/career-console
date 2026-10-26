"""Private data must never be committable."""
import json
import subprocess

import pytest

from app.config import REPO_ROOT
from tests.conftest import REAL_SOURCE

PRIVATE_SUFFIXES = (".sqlite", ".sqlite-wal", ".sqlite-shm", ".db", ".xlsx", ".xls", ".csv", ".env")


def committable_files():
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO_ROOT,
                             capture_output=True, text=True, check=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [REPO_ROOT / line for line in out.splitlines() if line]


def test_gitignore_covers_private_data():
    ignore = (REPO_ROOT / ".gitignore").read_text()
    for pattern in ("data/", "*.sqlite", "*.xlsx", ".env"):
        assert pattern in ignore


def test_no_private_file_types_are_committable():
    assert [p for p in committable_files() if p.name.endswith(PRIVATE_SUFFIXES)] == []
    assert [p for p in committable_files() if "data" in p.relative_to(REPO_ROOT).parts[:1]] == []


@pytest.mark.real_data
def test_private_identifiers_do_not_appear_in_committable_files():
    metas = sorted(REAL_SOURCE.glob("*.meta.json"))
    if not metas:
        pytest.skip("no private metadata present")
    meta = json.loads(metas[-1].read_text())
    secrets = [meta["source_file_id"], *meta.get("owner_aliases", [])]
    leaks = []
    for path in committable_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        leaks += [(path.name, s) for s in secrets if s and s.casefold() in text.casefold()]
    assert leaks == []
