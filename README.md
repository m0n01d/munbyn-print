# munbyn-print

Driverless printing to a Munbyn RW403B thermal label printer over USB, no CUPS
involved.

## Why

Munbyn ships a macOS CUPS driver, but its filter (`rastertorw403b`) is
x86_64-only. On Apple Silicon without Rosetta it fails silently
(`com.apple.badarch-error`, 0 bytes sent) -- see `PLANS/PLAN.md` for the full
research trail. Rosetta is also being phased out by Apple, so installing it is
a dead end long-term.

The RW403B's own IEEE-1284 device ID reports `CMD:TSPL` -- it speaks the
TSC/TSPL label language over a plain USB printer-class bulk endpoint
(interface 0, bulk OUT `0x03` / IN `0x83`, confirmed on this Mac). This project
talks TSPL directly with `pyusb`, renders PDFs (`pypdfium2`) and images
(`Pillow`) to 1-bit label bitmaps at device resolution, and skips CUPS
entirely.

## Setup

```
./setup.sh
```

Creates `.venv` (from `/usr/bin/python3`) and installs `requirements-dev.txt`.
`print_label.py` and `web.py` also auto re-exec into `.venv`'s Python if run
under the bare system Python and a dependency is missing.

## Usage

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

# Built-in alignment/polarity test label
python3 print_label.py --selftest

# Gap/label auto-calibration (feeds a few labels)
python3 print_label.py --calibrate

# Feed one label / check status / list attached printers
python3 print_label.py --feed
python3 print_label.py --status
python3 print_label.py --list

# Web UI (drag-and-drop, preview, print) at http://127.0.0.1:5050
python3 web.py

# "Print from Preview" via the macOS print dialog's PDF menu (written, not
# installed automatically -- see the caveat in PLANS/PLAN.md)
scripts/install-pdf-service.sh
```

## Calibration workflow

1. `python3 print_label.py --selftest` and look at the printed label.
2. If the "LEFT HALF SHOULD BE BLACK" box prints inverted (right half
   black), bitmap polarity is the other way round on this firmware: add
   `--black-is-one 1` (the default is `0`, clear bit = black dot).
   `--selftest --test --preview /tmp/selftest.png` shows the intended label.
3. Nudge alignment with `--x-shift`/`--y-shift` (mm); adjust `--density` if
   text is too light or too dark.
4. Lock in what worked by adding `--save-defaults` to that same command line.

## Troubleshooting

- **Printer not found**: check the USB cable, then `--list`.
- **Busy / "another program may hold the printer"**: a stuck CUPS job likely
  has the device claimed -- `cancel -a Munbyn_RW403B`, then retry.
- **Blank or all-black labels**: usually a bitmap polarity mismatch -- see the
  selftest step above (`--black-is-one`), or density set too low/high.
- **Skipped or misaligned labels**: run `--calibrate` to re-detect gap/label
  size, especially after changing label stock.

## Layout

```
print_label.py       CLI entry point
web.py                Flask web UI (127.0.0.1:5050)
munbyn/               labels, tspl, render, usb_transport, config
templates/, static/    web UI assets (plain HTML/JS, no build step)
scripts/               install-pdf-service.sh
tests/                 pytest -- USB is always mocked
```

See `CLAUDE.md` for the module map and `PLANS/PLAN.md` for verified hardware
facts, decisions, and open items.
