#!/usr/bin/env bash
# Create .venv (from /usr/bin/python3 if present, else whatever `python3` is)
# and install requirements-dev.txt. Safe to re-run.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  if [ -x /usr/bin/python3 ]; then
    PYTHON_BIN=/usr/bin/python3
  else
    PYTHON_BIN="$(command -v python3)"
  fi
fi

if [ ! -d .venv ]; then
  echo "Creating .venv with $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
  "$PYTHON_BIN" -m venv .venv
else
  echo ".venv already exists, reusing it"
fi

./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements-dev.txt

echo
echo "Done. Try:"
echo "  .venv/bin/python print_label.py --list"
echo "  .venv/bin/python print_label.py --selftest --test"
echo "  .venv/bin/pytest -q --timeout=60"
