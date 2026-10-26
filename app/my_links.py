"""Your own links for application forms (LinkedIn profile; CV, portfolio or GitHub), one click to copy.

Kept in my_links.json next to the database (data/): private, never in git, never sent anywhere.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

FIELDS = {"linkedin": "LinkedIn profile", "other": "CV, portfolio or GitHub"}


def path_for(db_path: Path) -> Path:
    return Path(db_path).parent / "my_links.json"


def load(path: Path) -> dict[str, str]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in FIELDS and isinstance(v, str) and v} if isinstance(data, dict) else {}


def clean(url: str, field: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if len(url) > 500:
        raise ValueError(f"{FIELDS[field]}: the link is too long")
    parts = urlsplit(url if "://" in url else "https://" + url)
    host = (parts.hostname or "").casefold()
    if parts.scheme not in ("http", "https") or "." not in host:
        raise ValueError(f"{FIELDS[field]}: not a web link")
    if field == "linkedin":
        if not (host == "linkedin.com" or host.endswith(".linkedin.com")) or not parts.path.startswith("/in/"):
            raise ValueError("A LinkedIn profile link looks like https://www.linkedin.com/in/your-name/")
        return urlunsplit(("https", "www.linkedin.com", parts.path, "", ""))  # without ?isSelfProfile=… and the like
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def save(path: Path, raw: dict) -> dict[str, str]:
    data = {k: v for k in FIELDS if (v := clean(raw.get(k, ""), k))}
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return data
