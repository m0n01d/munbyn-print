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
should print for real. This applies to every agent working in this repo, not
just whichever one is touching `munbyn/`/CLI/web code that turn.

## The printer's command subset (hardware-verified 2026-09-27)

This firmware's USB path only implements a subset of TSPL -- anything outside
it is **silently dropped, not merely ignored**: a job using native
`TEXT`/`BOX`/`BAR` commands (with an otherwise-correct header) printed
**nothing at all**, not even a feed. `GAPDETECT` alone was also verified to do
nothing. Every job this repo builds is therefore restricted to
`munbyn.tspl.SUPPORTED_COMMANDS`: `SIZE, GAP, BLINE, REFERENCE, OFFSET, SETC
AUTODOTTED OFF, DENSITY, SPEED, DIRECTION, CLS, BITMAP (mode 1), PRINT` --
enforced by a test that scans every job builder's output. Anything that needs
text, a box, or a bar has to be drawn as pixels and sent as `BITMAP` instead
(see `selftest_image`/`selftest_job` in `munbyn/tspl.py`). Two more commands
exist in the vendor filter binary (`SETC PAUSEKEY OFF`, and a compressed
`BITMAP x,y,wb,h,3,len,<data>` mode that links libz) but are **untested** and
must not be used without a fresh hardware verification.

## Module map

- `print_label.py` -- CLI entry (argparse), thin
- `web.py` -- Flask entry, the same options over HTTP
- `munbyn/labels.py` -- label sizes, mm/dot conversion, size parsing
- `munbyn/tspl.py` -- TSPL command builder (header, bitmap, jobs, status decode,
  `SUPPORTED_COMMANDS`)
- `munbyn/usb_transport.py` -- pyusb transport (`Printer`, `find_printers`)
- `munbyn/render.py` -- PDF/image to 1-bit label bitmap
- `munbyn/config.py` -- `~/.config/munbyn-print/config.json` persistence
- `templates/`, `static/` -- web UI (plain HTML/JS, no build step)
- `scripts/install-pdf-service.sh` -- builds & links the "Print to Munbyn
  RW403B" macOS PDF Service app (an app bundle, not an executable script --
  see the README/PLAN for why)
- `scripts/print-from-dialog.sh` -- wrapper that app's droplet shells out to
- `cups/`, `scripts/install-cups-queue.sh` -- native arm64 CUPS queue; owned by
  a different agent/track, not by the files above

## Shared conventions

Team conventions live in the private repo `m0n01d/claude-conventions`. On this
Mac they don't auto-load (projects live under `~/Documents/web`, not
`~/code`) -- clone it to the scratchpad and read its `CLAUDE.md` at the start
of a session.
