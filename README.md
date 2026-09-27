# munbyn-print

Driverless printing to a Munbyn RW403B thermal label printer over USB, no CUPS
involved -- **verified against the real printer** 2026-09-27 (see
`PLANS/PLAN.md` for the dated hardware facts and decisions behind everything
below).

## Why

Munbyn ships a macOS CUPS driver, but its filter (`rastertorw403b`) is
x86_64-only. On Apple Silicon without Rosetta it fails silently
(`com.apple.badarch-error`, 0 bytes sent). Rosetta is also being phased out by
Apple, so installing it is a dead end long-term.

The RW403B's own IEEE-1284 device ID reports `CMD:TSPL` -- it speaks the
TSC/TSPL label language over a plain USB printer-class bulk endpoint
(interface 0, bulk OUT `0x03` / IN `0x83`, confirmed on this Mac). This project
talks TSPL directly with `pyusb`, renders PDFs (`pypdfium2`) and images
(`Pillow`) to 1-bit label bitmaps at device resolution, and skips CUPS
entirely -- **verified working end-to-end against real 4x6 gap labels.**

**Important firmware quirk (verified on hardware, not just documented):** this
printer's USB path only implements a subset of TSPL -- `SIZE`, `GAP`/`BLINE`,
`REFERENCE`, `OFFSET`, `SETC AUTODOTTED OFF`, `DENSITY`, `SPEED`, `DIRECTION`,
`CLS`, `BITMAP` (mode 1), `PRINT`. Anything outside that set (native `TEXT`,
`BOX`, `BAR`, `GAPDETECT`, ...) is **silently dropped, not merely
unsupported-but-harmless** -- a job that used them printed nothing at all, not
even a feed. Everything this project sends -- including the self-test label --
is built entirely from that verified subset; see `munbyn.tspl.
SUPPORTED_COMMANDS` and `CLAUDE.md`.

## Setup

```
./setup.sh
```

Creates `.venv` (from `/usr/bin/python3`) and installs `requirements-dev.txt`.
`print_label.py` and `web.py` also auto re-exec into `.venv`'s Python if run
under the bare system Python and a dependency is missing.

## Three ways to print

1. **CLI** -- `python3 print_label.py somefile.pdf` (see Usage below).
2. **Web UI** -- `python3 web.py`, then open `http://127.0.0.1:5050` (port
   5050, not 5000 -- macOS AirPlay Receiver owns 5000). Drag a PDF or image
   in, adjust label/fit/printer options, Preview or Print. Includes a
   Self-test preview/print button (see Calibration below).
3. **The regular Preview "Print" button**, via the macOS print dialog, two
   ways:
   - **(a) The PDF menu's "Print to Munbyn RW403B" app** -- install with
     `scripts/install-pdf-service.sh`. This builds a small app (not an
     executable script -- see the caveat below) and links it into
     `~/Library/PDF Services`; it then appears at the bottom of the Print
     dialog's PDF dropdown menu. It sends the document through exactly
     `print_label.py --crop none --fit fit --rotate auto`, so it does **not**
     let you set paper size/scaling in the dialog itself -- for that, use (b).
   - **(b) The native CUPS queue "Munbyn RW403B (native)"** -- a real CUPS
     queue with a native arm64 raster-to-TSPL filter (built by a separate
     track/agent; see `cups/`). Install with `sudo
     ./scripts/install-cups-queue.sh`. This is the one that gives you
     Preview's normal paper-size/scaling controls in the dialog, since CUPS
     itself handles the rasterization contract. **This project's own
     `munbyn/`/CLI/web code never touches CUPS, `sudo`, or `/Library` --
     that queue is a separate, explicitly-privileged install path.**

   **Caveat on (a):** *executable scripts* placed directly in
   `~/Library/PDF Services` have been broken since Big Sur (the print
   dialog's host process is sandboxed and won't run scripts, binaries, or
   Automator plugins), confirmed both by a first-hand report (Ventura, Dec
   2022) and again on this Mac's macOS 26 (conductor, 2026-09-27).
   `install-pdf-service.sh` instead builds and links an *application* (an
   "app bundle wraps a script" pattern that pre-dates the sandboxing, since
   the dialog opens it via LaunchServices instead of executing the PDF
   Services entry itself) -- **confirmed working on this Mac's macOS 26**:
   applications placed in `~/Library/PDF Services` (or aliases to them) do
   run from the Print dialog's PDF menu. What's *not* yet confirmed is this
   specific app/symlink, end to end, against a real Print dialog -- **test
   the installed "Print to Munbyn RW403B" app once before relying on it.**
   `~/Library/PDF Services` can also hold a plain Finder alias to the app
   instead of the symlink the installer creates -- both work the same way to
   LaunchServices.

## Usage (CLI)

```sh
# Dry run: build the job and print it, never touching USB
python3 print_label.py label.pdf --test

# Real print
python3 print_label.py label.pdf

# A 4x6 shipping label sitting somewhere on a Letter-size PDF page
python3 print_label.py invoice.pdf --size 4x6 --crop auto

# An image, scaled like Preview's "Scale: N%"
python3 print_label.py photo.jpg --scale 85

# Specific pages, multiple copies
python3 print_label.py doc.pdf --pages 1-3,5 --copies 2

# Custom size, media type, density
python3 print_label.py label.png --size 2.25x1.25 --media bline --gap 3.2 --density 10

# Built-in alignment/polarity test label (a single pixel-drawn BITMAP --
# see the firmware quirk above)
python3 print_label.py --selftest

# Manual gap/label calibration procedure; sends nothing to the printer
python3 print_label.py --calibrate

# Feed one (blank) label / check status / list attached printers
python3 print_label.py --feed
python3 print_label.py --status
python3 print_label.py --list

# Web UI (drag-and-drop, preview, print) at http://127.0.0.1:5050
python3 web.py

# Build/link the "Print to Munbyn RW403B" PDF Service app
scripts/install-pdf-service.sh
```

## Calibration

There is no TSPL command this firmware honours for gap/label calibration --
`GAPDETECT` was sent alone to real hardware and verified to do nothing.
`print_label.py --calibrate` (or the RW403B manual) instead:

1. Load at least 4 labels into the printer.
2. Close the cover -- this triggers automatic label identification.
3. If that doesn't work, hold the **feed button** until the printer beeps
   **once** (label identification).

Feed-button reference: single click feeds one label; double-click (or hold to
two beeps) prints the printer's own self-test page; hold to three beeps
(~6s) resets it. LED: green = ready, blue = Bluetooth connected, red = label
not identified or cover open, flashing green+red = print head overheated.

Separately, this project's own `--selftest` prints an **alignment/polarity**
test label (border, mm rulers, crosshair, and a "LEFT HALF SHOULD BE BLACK"
polarity swatch), useful after calibration or a stock change:

1. `python3 print_label.py --selftest` and look at the printed label.
2. If the swatch prints inverted (right half black), bitmap polarity is the
   other way round on this firmware: add `--black-is-one 1` (the default,
   `0`, is **confirmed correct on this Mac's printer** as of 2026-09-27 --
   see `PLANS/PLAN.md`). `--selftest --test --preview /tmp/selftest.png`
   shows the intended label without printing.
3. Nudge alignment with `--x-shift`/`--y-shift` (mm); adjust `--density` if
   text is too light or too dark.
4. Lock in what worked by adding `--save-defaults` to that same command line.

## Troubleshooting

- **Printer not found**: check the USB cable, then `--list`.
- **Busy / "another program may hold the printer"**: a stuck CUPS job likely
  has the device claimed -- `cancel -a Munbyn_RW403B_Native` (the native
  queue's destination name; `"Munbyn RW403B (native)"` is only its
  description, which `cancel` won't accept) or `cancel -a Munbyn_RW403B` for
  the vendor queue, then retry.
- **Blank or all-black labels**: usually a bitmap polarity mismatch -- see the
  calibration/self-test steps above (`--black-is-one`), or density set too
  low/high.
- **Nothing prints and no feed happens at all**: almost certainly a job that
  (accidentally) used a command outside `munbyn.tspl.SUPPORTED_COMMANDS` --
  this firmware drops such jobs entirely rather than erroring. `--test`
  describes exactly what would be sent.
- **`--status` says "no reply"**: expected, not a bug -- this firmware does
  not answer TSPL status queries (`<ESC>!?` and friends were tried against
  real hardware and got nothing back on bulk IN). Device *identification*
  (`--list`, and the `device:` line `--status` prints) still works, since
  that's a separate USB control request, not a TSPL command.
- **Skipped or misaligned labels**: re-run the manual calibration procedure
  above, especially after changing label stock.

## Layout

```
print_label.py             CLI entry point
web.py                      Flask web UI (127.0.0.1:5050)
munbyn/                     labels, tspl, render, usb_transport, config
templates/, static/         web UI assets (plain HTML/JS, no build step)
scripts/install-pdf-service.sh   builds/links the PDF-menu "app" print path
scripts/print-from-dialog.sh     wrapper that app shells out to
cups/, scripts/install-cups-queue.sh   native CUPS queue (separate track)
PLANS/PLAN.md               dated hardware facts, decisions, open items
PLANS/BLE.md                Bluetooth transport research (unverified, future)
tests/                      pytest -- USB is always mocked
```

See `CLAUDE.md` for the module map, agent rules, and the printer's verified
command subset, and `PLANS/PLAN.md` for the full research trail.
