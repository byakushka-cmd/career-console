#!/usr/bin/env bash
# Career Console installer for macOS. Run it again to update: your data (the data/ folder) is never touched.
#
#   curl -fsSL https://raw.githubusercontent.com/byakushka-cmd/career-console/main/install.sh | bash
#
# It installs uv (which brings Python and the libraries), puts the console in ~/CareerConsole (or
# $CAREER_CONSOLE_DIR), creates an empty database on the first install, adds the official job boards of
# Israeli tech companies, puts three icons on your desktop and opens the console in your browser.
set -euo pipefail

REPO="${CAREER_CONSOLE_REPO:-byakushka-cmd/career-console}"
DIR="${CAREER_CONSOLE_DIR:-$HOME/CareerConsole}"
TARBALL="${CAREER_CONSOLE_TARBALL:-https://github.com/$REPO/archive/refs/heads/main.tar.gz}"
OPEN="${CAREER_CONSOLE_OPEN:-1}"      # 0: no desktop icons, no browser (for testing the installer)
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if [ "$(uname)" != "Darwin" ]; then
  echo "This installer is for macOS. On Linux or Windows follow the manual steps in the README."
  exit 1
fi

if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  say "Installing uv (it brings Python and the console's libraries)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv 2>/dev/null || echo "$HOME/.local/bin/uv")"

FIRST=1
[ -f "$DIR/data/career_console.sqlite" ] && FIRST=0
if [ "$FIRST" = 0 ]; then
  say "Updating Career Console in $DIR (your data stays)…"
  "$UV" run --project "$DIR" career-console stop >/dev/null 2>&1 || true
else
  say "Installing Career Console in $DIR…"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "$TARBALL" | tar -xz -C "$TMP"
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
mkdir -p "$DIR"
# the new code replaces the old; data/ (database, settings, mailbox password) and .venv/ are left alone
rsync -a --delete --exclude 'data/' --exclude '.venv/' "$SRC"/ "$DIR"/

cd "$DIR"
"$UV" sync --quiet
if [ "$FIRST" = 1 ]; then
  "$UV" run career-console init
  say "Finding the official job boards of Israeli tech companies (a few minutes)…"
  "$UV" run career-console boards discover || echo "(No connection? Run it later: Search settings → Official job boards.)"
else
  "$UV" run career-console migrate
fi

if [ "$OPEN" = 1 ]; then
  say "Putting the icons on your desktop…"
  "$UV" run career-console desktop
  "$UV" run career-console start
fi

say "Done. From now on: double-click “Career Console” on your desktop."
echo "Next: Search settings (what to look for, your regions) and Job-alert e-mails (your Gmail, with an app password)."
