"""Storage paths. Everything private lives under one data directory (gitignored)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
DISPLAY_TZ = "Asia/Jerusalem"  # for times we generate; source date-only values are never shifted


@dataclass(frozen=True)
class Paths:
    data: Path

    @property
    def source(self) -> Path:  # immutable snapshots + their metadata sidecars
        return self.data / "source"

    @property
    def reference(self) -> Path:  # read-only copies of earlier artifacts used for validation
        return self.data / "reference"

    @property
    def db(self) -> Path:
        return self.data / "career_console.sqlite"

    @property
    def backups(self) -> Path:
        return self.data / "backups"

    @property
    def reports(self) -> Path:
        return self.data / "reports"


def local_time(iso_utc: str | None) -> str:
    """Times we record are UTC; show them in Israel time. Source date-only values never go through this."""
    if not iso_utc:
        return ""
    return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(ZoneInfo(DISPLAY_TZ)).strftime("%Y-%m-%d %H:%M")


def paths() -> Paths:
    return Paths(Path(os.environ.get("CAREER_CONSOLE_DATA_DIR", REPO_ROOT / "data")).resolve())


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path
