"""The one-line installer (install.sh): valid shell, never touches data/, and the README and the web page
point to the same command."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install.sh"


def test_the_installer_is_valid_shell():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_an_update_never_touches_your_data():
    text = SCRIPT.read_text()
    [rsync] = [l for l in text.splitlines() if l.strip().startswith("rsync ")]
    assert "--delete" in rsync and "--exclude 'data/'" in rsync and "--exclude '.venv/'" in rsync
    assert "--delete-excluded" not in rsync
    assert re.search(r'if \[ "\$FIRST" = 1 \]; then\s+"\$UV" run career-console init', text)   # init only the first time


def test_readme_and_page_give_the_same_command():
    command = re.search(r"curl -fsSL https://raw\.githubusercontent\.com/\S+/install\.sh \| bash", SCRIPT.read_text()).group(0)
    assert command in (ROOT / "README.md").read_text()
    assert command in (ROOT / "docs" / "index.html").read_text()
