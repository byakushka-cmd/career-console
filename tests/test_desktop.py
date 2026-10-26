import shutil
import sys

import pytest

from app import desktop


def test_shortcuts_call_the_console_commands():
    scripts = desktop.shortcuts()
    assert set(scripts) == {"Career Console", "Search Jobs Now", "Stop Career Console"}
    assert "career-console start" in scripts["Career Console"][1]
    assert "career-console search" in scripts["Search Jobs Now"][1]
    assert "career-console stop" in scripts["Stop Career Console"][1]
    assert all((desktop.ICONS / icon).exists() for icon, _ in scripts.values())


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("osacompile"), reason="macOS only")
def test_shortcuts_build_into_apps_with_icons(tmp_path):
    made = desktop.install(tmp_path)
    assert {a.name for a in made} == {"Career Console.app", "Search Jobs Now.app", "Stop Career Console.app"}
    for app in made:
        icon = app / "Contents" / "Resources" / "applet.icns"
        assert icon.exists() and icon.stat().st_size > 10_000
    assert len(desktop.uninstall(tmp_path)) == 3 and not list(tmp_path.iterdir())
