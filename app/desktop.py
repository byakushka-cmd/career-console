"""Desktop shortcuts for macOS: open the console, search now, stop the console.

Each shortcut is a tiny AppleScript app (built with osacompile) that calls the `career-console` command and
shows a macOS notification. Icons are rendered from app/static/icons/*.svg with the system's own tools.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import REPO_ROOT

ICONS = REPO_ROOT / "app" / "static" / "icons"
URL = "http://127.0.0.1:45627/opportunities"


def _uv() -> str:
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")


def _cc(command: str) -> str:
    return f"'{_uv()}' run --project '{REPO_ROOT}' career-console {command}"


def shortcuts() -> dict[str, tuple[str, str]]:
    """name -> (icon svg, AppleScript source)."""
    note = 'display notification {msg} with title "Career Console"'
    return {
        "Career Console": ("console.svg", f'''
try
    do shell script "{_cc('start')} 2>&1"
on error message
    display dialog "Career Console could not start:" & return & message buttons {{"OK"}} with icon caution
end try'''),
        "Search Jobs Now": ("search.svg", f'''
{note.format(msg='"Checking the official job boards…"')}
try
    set output to do shell script "{_cc('search')} 2>&1"
    set summary to paragraph 2 of output
    {note.format(msg='summary')}
    do shell script "{_cc('start')} 2>&1"
on error message
    {note.format(msg='message')}
end try'''),
        "Stop Career Console": ("stop.svg", f'''
try
    set output to do shell script "{_cc('stop')} 2>&1"
    {note.format(msg='output')}
on error message
    {note.format(msg='message')}
end try'''),
    }


def build_icns(svg: Path, out: Path) -> Path:
    """SVG -> .icns with QuickLook (render), sips (sizes) and iconutil; all built into macOS."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        subprocess.run(["qlmanage", "-t", "-s", "1024", "-o", str(tmp), str(svg)], check=True, capture_output=True)
        png = tmp / f"{svg.name}.png"
        iconset = tmp / "icon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                subprocess.run(["sips", "-z", str(size * scale), str(size * scale), str(png), "--out", str(iconset / name)],
                               check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True, capture_output=True)
    return out


def install(desktop: Path | None = None) -> list[Path]:
    desktop = desktop or Path.home() / "Desktop"
    made = []
    for name, (icon, script) in shortcuts().items():
        app = desktop / f"{name}.app"
        if app.exists():
            shutil.rmtree(app)
        subprocess.run(["osacompile", "-o", str(app), "-e", script.strip()], check=True, capture_output=True)
        build_icns(ICONS / icon, app / "Contents" / "Resources" / "applet.icns")
        subprocess.run(["touch", str(app)], check=True)  # make Finder pick up the new icon
        made.append(app)
    return made


def uninstall(desktop: Path | None = None) -> list[Path]:
    desktop = desktop or Path.home() / "Desktop"
    removed = []
    for name in shortcuts():
        app = desktop / f"{name}.app"
        if app.exists():
            shutil.rmtree(app)
            removed.append(app)
    return removed
