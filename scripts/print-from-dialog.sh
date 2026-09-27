#!/usr/bin/env bash
# Wrapper shelled out to by the "Print to Munbyn RW403B" PDF Service app
# (see scripts/install-pdf-service.sh): prints one PDF via print_label.py,
# logs everything, and shows a macOS notification with the result.
#
# Usage:
#   scripts/print-from-dialog.sh "<pdf>" [--test]
#
# --test forwards print_label.py's own --test (dry run, never touches USB) --
# used to verify this wrapper without printing for real.
set -uo pipefail

# The .app droplet always calls this wrapper by its absolute path *inside
# the repo checkout it was built from* (see install-pdf-service.sh) -- the
# app itself can live far away (~/Applications, a --dest test directory),
# but this script never moves relative to the repo, so its own location is
# a reliable way to find REPO_DIR, unlike a path hardcoded to one checkout.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
PRINT_SCRIPT="$REPO_DIR/print_label.py"
LOG_PATH="$HOME/Library/Logs/munbyn-print.log"

pdf="${1:-}"
mode="${2:-}"

mkdir -p "$(dirname "$LOG_PATH")"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_PATH"
}

notify() {
  # Pass subtitle/message as argv, never interpolated into AppleScript
  # source -- a PDF filename (the message, here) can contain a double quote
  # or backslash, which would otherwise either break the notification or run
  # as AppleScript code (e.g. via `do shell script`).
  osascript -e 'on run argv' \
    -e 'display notification (item 2 of argv) with title "Munbyn RW403B" subtitle (item 1 of argv)' \
    -e 'end run' "$1" "$2" >/dev/null 2>&1 || true
}

log "invoked with: $* "

if [ -z "$pdf" ] || [ ! -f "$pdf" ]; then
  log "error: no readable PDF argument found (got: '${pdf}')"
  notify "Print failed" "No PDF file was received."
  exit 1
fi

args=(--crop none --fit fit --rotate auto)
if [ "$mode" = "--test" ]; then
  args+=(--test)
fi
args+=("$pdf")

if "$VENV_PYTHON" "$PRINT_SCRIPT" "${args[@]}" >> "$LOG_PATH" 2>&1; then
  log "printed: $pdf"
  if [ "$mode" = "--test" ]; then
    notify "Dry run OK" "$(basename "$pdf") -- --test, nothing was sent to the printer."
  else
    notify "Print sent" "Sent to the Munbyn RW403B."
  fi
  exit 0
else
  status=$?
  log "print_label.py exited $status for: $pdf"
  notify "Print failed" "print_label.py failed -- see ~/Library/Logs/munbyn-print.log"
  exit "$status"
fi
