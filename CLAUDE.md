# CLAUDE.md

munbyn-print talks directly to a Munbyn RW403B thermal label printer over USB
using its native TSPL command language (via `pyusb`), because the vendor's
macOS CUPS driver is x86_64-only and fails on Apple Silicon without Rosetta.
It renders PDFs (`pypdfium2`) and images (`Pillow`) to 1-bit label bitmaps and
sends them raw -- no CUPS involved. A CLI (`print_label.py`) and a small Flask
web UI (`web.py`, `http://127.0.0.1:5050`) share the `munbyn/` package.

## Commands

- Setup: `./setup.sh` (creates `.venv` from `/usr/bin/python3`, installs
  `requirements-dev.txt`)
- Tests: `.venv/bin/pytest -q --timeout=60`
- Dry run (never touches USB): `python3 print_label.py <file> --test`

## Never print without asking

**Agents must never invoke `print_label.py` or `web.py` against real hardware
without `--test`, and must never call a transport `write()` on the real
device.** Tests always mock USB (`munbyn.usb_transport.Printer`) -- they never
open the real device. Only a human, or an explicit human-approved real print,
should print for real.

## Module map

- `print_label.py` -- CLI entry (argparse), thin
- `web.py` -- Flask entry, the same options over HTTP
- `munbyn/labels.py` -- label sizes, mm/dot conversion, size parsing
- `munbyn/tspl.py` -- TSPL command builder (header, bitmap, jobs, status decode)
- `munbyn/usb_transport.py` -- pyusb transport (`Printer`, `find_printers`)
- `munbyn/render.py` -- PDF/image to 1-bit label bitmap
- `munbyn/config.py` -- `~/.config/munbyn-print/config.json` persistence
- `templates/`, `static/` -- web UI (plain HTML/JS, no build step)
- `scripts/install-pdf-service.sh` -- macOS "Print to Munbyn RW403B" PDF Service

## Shared conventions

Team conventions live in the private repo `m0n01d/claude-conventions`. On this
Mac they don't auto-load (projects live under `~/Documents/web`, not
`~/code`) -- clone it to the scratchpad and read its `CLAUDE.md` at the start
of a session.
