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
   - **(c) Unplugged: "Munbyn RW403B (Bluetooth)"** -- the same queue
     pointed at the MunbynBLE bridge, which prints over Bluetooth. Three
     steps; see "Bluetooth (unplugged) setup" below.

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

# Feed/x-alignment calibration label (100mm bar across the head, 100mm bar
# along the feed, both with 10mm ticks); see Calibration below
python3 print_label.py --scale-test

# Compensate for this printer's mechanical feed shortfall (see Calibration);
# 1.0 disables it -- this printer's measured value is already the default
python3 print_label.py label.pdf --feed-scale 0.981

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

There are three, mostly independent things to calibrate: **gap/label
identification** (a manual, physical procedure -- no TSPL command for it),
**bitmap polarity/alignment** (`--selftest`), and **feed-axis length /
x-alignment** (`--scale-test`, this printer's own mechanical quirks).

### Gap/label identification (cover-close or feed button)

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

### Bitmap polarity / rough alignment (`--selftest`)

`--selftest` prints an **alignment/polarity** test label (border, mm rulers,
crosshair, and a "LEFT HALF SHOULD BE BLACK" polarity swatch), useful after
calibration or a stock change:

1. `python3 print_label.py --selftest` and look at the printed label.
2. If the swatch prints inverted (right half black), bitmap polarity is the
   other way round on this firmware: add `--black-is-one 1` (the default,
   `0`, is **confirmed correct on this Mac's printer** as of 2026-09-27 --
   see `PLANS/PLAN.md`). `--selftest --test --preview /tmp/selftest.png`
   shows the intended label without printing -- **un-stretched, physical
   size**, i.e. what it will look like on paper, not the feed-scale-stretched
   bitmap actually sent (see below); every `--preview`, including the web
   UI's, follows this same convention.
3. Nudge alignment with `--x-shift`/`--y-shift` (mm); adjust `--density` if
   text is too light or too dark.
4. Lock in what worked by adding `--save-defaults` to that same command line.

### Feed-axis length and x-alignment (`--feed-scale`, `--scale-test`)

**This printer is mechanically short along the paper feed**, hardware-
verified 2026-09-27: an 800-row bar printed at 98.1mm, not 100mm (Munbyn's
own phone app shows the same *kind* of error over a completely different,
Bluetooth code path, so it's the printer, not this repo's math). `feed_scale
= printed_length / intended_length`; this repo stretches every job's bitmap
height and `SIZE` length by `1/feed_scale` before sending to compensate --
**verified on paper**. The default, `0.981`, is this printer's own measured
value (`munbyn.config.DEFAULTS`); `--feed-scale 1.0` disables the
correction entirely. **The native CUPS queue's feed correction is separate**
-- see "The CUPS queue's own feed scale" below.

Across the print head this printer is accurate (no scale correction needed
there). The image itself lands about 3.1mm right of the label's left edge
regardless of `SIZE` width -- tested and confirmed not to be fixable by
changing `SIZE` (see PLANS/PLAN.md's struck hypothesis); Dwight attributes
this to how the label physically sits in the printer, not to the firmware or
`SIZE`. It's left entirely to `--x-shift`.

`--scale-test` prints (or `--test`: dry-runs/`--preview`s, un-stretched like
`--selftest`) a calibration label with two bars (target lengths 90mm across
the head, 100mm along the feed), each with 10mm ticks plus a final tick at
its true end, using the label size and *current* `feed_scale`. On stock too
small to fit a bar's full target length, that bar is clipped and the label
says so (a "(SHORT ...)" note) -- the caption always prints the bar's
*actual* drawn length, not the target, so the numbers below always mean the
printed label in front of you:

- **"A across"** -- across the print head, starting at a printed nominal
  left gap (5.00mm -- comfortably above this printer's own ~3.1mm offset, so
  a correction below doesn't crop the gap to nothing). Measure the *actual*
  left gap on the printed label with calipers and compare it to that printed
  nominal value:

  ```
  x_shift = nominal_left_mm - measured_left_mm
  ```

  A negative result nudges the image left (and crops it); re-print with
  `--x-shift <x_shift>` and re-measure -- the gap should now read close to
  the nominal value again.
- **"B along feed"** -- along the paper feed. Measure its printed length
  with calipers (`measured_B_mm`) and compute, using the length the label's
  "B along feed" line says it actually drew (`drawn_B_mm` -- not always
  100mm; shorter on stock too short for the full bar):

  ```
  new_feed_scale = old_feed_scale * measured_B_mm / drawn_B_mm
  ```

  Save it and re-run to confirm:

  ```sh
  python3 print_label.py --feed-scale <new_feed_scale> --save-defaults
  python3 print_label.py --scale-test   # re-measure B; should now read ~drawn_B_mm
  ```

Re-run `--scale-test` after any stock change or on a different printer unit
-- `feed_scale`/`--x-shift` are mechanical properties of a specific
printer/roller/stock combination, not universal constants.

### The CUPS queue's own feed scale

The native CUPS queue (below) does **not** read `munbyn.config`/
`--feed-scale`/`--scale-test` at all -- it gets its own feed correction
baked into the installed PPD's default `Resolution` (`203x<N>dpi`, quantised
to about 0.5% steps), set once at install time by `sudo
scripts/install-cups-queue.sh --feed-scale <F>` and left untouched by
recalibrating the CLI/web UI's `feed_scale`. Re-run that install command
with a new `--feed-scale` to recalibrate the CUPS queue. See
`cups/README.md`.

## Bluetooth (`--ble`) -- unplugged printing

**Status (2026-09-28): verified on the printer.** From macOS Terminal on the
Mac mini, `--status --ble` and `--selftest --ble` worked: the self-test came
out upright, not mirrored, with the right polarity, one label, stopped at the
gap (16 sections acked, 0 resends, 4.1 s). DEVICEINFO: firmware 1.1.16, BLE
firmware 1.2.1, density 8, speed 4, supportfunction 0. The protocol comes from
Munbyn's web editor (`PLANS/BLE-PROTOCOL.md`). USB stays the default.

macOS only lets a process use Bluetooth if the **app responsible for it**
declares why (`NSBluetoothAlwaysUsageDescription`) and the user allowed it.
Terminal has that, so the CLI works there. cupsd (Preview's print path) never
can, and anything started from an app without the key -- the Claude app, a
web server started from it -- is **killed by macOS** at the first Bluetooth
call. So Bluetooth lives in one small app, **MunbynBLE.app**: it runs a
bridge on `127.0.0.1:9100` that takes ordinary TSPL jobs and prints them over
Bluetooth. The CUPS Bluetooth queue, `print_label.py --ble` and the web UI all
hand their job to it.

```
Preview -> CUPS "Munbyn RW403B (Bluetooth)" -> rastertotspl -> socket://127.0.0.1:9100 -\
print_label.py --ble / web UI (Bluetooth) --------- TSPL job over loopback TCP ---------+-> MunbynBLE bridge -> Bluetooth -> printer
```

### Bluetooth (unplugged) setup

On the Mac next to the printer (the Mac mini), from this repo:

1. **Install the bridge** (as you, no sudo):
   `scripts/install-ble-bridge.sh`
   It builds `~/Applications/MunbynBLE.app` (no Dock icon), copies the Python
   runtime to `~/Library/Application Support/MunbynBLE`, and starts it with a
   LaunchAgent (`~/Library/LaunchAgents/com.m0n01d.munbyn-ble-bridge.plist`)
   that also starts it at login and restarts it if it stops. Re-run it after
   changing anything in `munbyn/`.
2. **Click Allow** when macOS asks *"MunbynBLE would like to use
   Bluetooth"* (it asks as the bridge starts). Missed it? System Settings >
   Privacy & Security > Bluetooth > MunbynBLE on.
3. **Add the Preview queue:** `sudo scripts/install-cups-queue.sh --ble`
   (same filter and PPD as the USB queue, device URI
   `socket://127.0.0.1:9100`; the USB queue is left alone). Then File > Print
   > "Munbyn RW403B (Bluetooth)".

Unplug the USB cable, and close the Munbyn phone app / the web editor tab
first: the printer takes one Bluetooth connection at a time (the bridge
connects per job and disconnects after).

```sh
# Is the bridge up, and can it reach the printer? (DEVICEINFO over Bluetooth)
python3 print_label.py --status --ble

# Print through the bridge (files, --selftest, --scale-test, --feed, --copies)
python3 print_label.py label.pdf --ble --copies 2
python3 print_label.py --ble --save-defaults     # make Bluetooth the default; --usb overrides it

# Frame dump: builds the whole Bluetooth job and shows every write -- no radio, no bridge
python3 print_label.py --selftest --ble --test
python3 print_label.py label.pdf --ble --test --hex frames.bin

# Terminal only: Bluetooth in this process, no bridge
python3 print_label.py --ble-scan
python3 print_label.py --selftest --ble-direct --debug   # --debug logs every frame in hex
```

- **Which printer.** The bridge prints to `ble_address` in
  `~/.config/munbyn-print/config.json` (the Mac mini has
  `5903F05B-AA01-DD0A-C87B-BAC1438801DF`, re-read for every job); without
  one it scans for a name starting `RW403B`. The address is a per-Mac
  CoreBluetooth UUID from `--ble-scan` (Terminal). Save a new one with
  `--ble-address <UUID> --save-defaults`. The Mac has to be in Bluetooth range
  (about 10 m).
- **What the bridge does with a job.** It accepts only the verified TSPL
  subset (anything else: logged, notification, nothing printed), turns each
  `CLS`/`BITMAP`/`PRINT m,n` page into a Bluetooth page (TSPL's clear bit =
  black becomes Bluetooth's 1 = black; rows are printed as received, already
  feed-corrected by whoever built the job) and sends `m x n` copies. `SIZE`,
  `GAP`, `DENSITY` and `SPEED` are logged and ignored: over Bluetooth the
  printer uses its stored settings and feeds by its own gap sensor. Jobs run
  one at a time, in order.
- **Failures.** A Bluetooth failure is retried once on a fresh connection,
  unless a label may already have printed (then it is not, to avoid a
  duplicate). After that the bridge logs it and posts a macOS notification
  ("Munbyn BLE: Print failed: ..."). The CLI and the web UI show the error;
  CUPS can't (its socket backend only knows the bridge took the job).
- **What gets sent** (spec in `PLANS/BLE-PROTOCOL.md`): DEVICEINFO (the job
  refuses to start unless the printer reports every status bit clear;
  busy/calibrating waits 4 s and asks once more), the page as
  heatshrink-compressed 400-byte packets section by section with an ack per
  section (a resend request rewinds to that section), PRINTINEND, then one
  "printed" report per page x copy, then disconnect. `--x-shift`/`--y-shift`
  are baked into the bitmap (Bluetooth has no offsets). TSPL-only flags
  (`--density`, `--speed`, `--media`, `--gap`, `--offset`, `--direction`,
  `--black-is-one`) are ignored with a note.
- **Density/speed are opt-in and Terminal-only.** `--ble-direct
  --ble-density 1-16` / `--ble-speed 1-8` send the editor's
  PRINTINCONCENTRATION / PRINTINGSPEED before the job. The printer stores
  them; their scale is the editor's, not TSPL's `DENSITY 0-15`.
  `--status --ble` shows the current values. They don't go through the
  bridge (the CLI refuses and says so).
- **Feed scale.** Bluetooth jobs use their own `ble_feed_scale` (config key;
  `--ble-feed-scale F`, or `--feed-scale F` for one run with `--ble`). It
  defaults to the USB-measured 0.981 and is **still unverified over
  Bluetooth** (the phone app measured 97.23 mm for 100 mm): print
  `--scale-test --ble`, measure bar B, and save
  `--ble-feed-scale <old*B/100> --save-defaults`. The CUPS Bluetooth queue
  uses its PPD's Resolution instead (`--feed-scale` of
  `install-cups-queue.sh --ble`).
- **Web UI.** Printer > Transport: *USB* or *Bluetooth (via MunbynBLE
  bridge)*. The Feed scale field switches to the transport's saved value.
- **Ctrl-C** during a `--ble-direct` job sends CANCELPRINTING, waits up to
  2 s for the printer's OK and disconnects; a second Ctrl-C stops waiting.
  Stopping the bridge (`launchctl bootout`) cancels its running job the same
  way.
- **"frame ... larger than this connection allows"**: retry from Terminal with
  `--ble-direct --ble-packet-size 148`. `--ble-write response|no-response`
  forces the GATT write type (default `auto`; on this printer 0xABF4 is
  write-without-response, max 509 B, and 0xABF1 is write, 512 B).

### Bluetooth troubleshooting

- **Bridge log:** `~/Library/Logs/munbyn-ble-bridge.log` (every job, header
  values, retries, errors; start the bridge with `--debug` for frame hex).
  Startup crashes (Python tracebacks) land in
  `~/Library/Logs/munbyn-ble-bridge.launchd.log`.
- **Is it running?** `python3 print_label.py --status --ble`, or
  `launchctl print gui/$UID/com.m0n01d.munbyn-ble-bridge`.
- **Restart it (default, `--launch-mode direct`):**
  `launchctl kickstart -k gui/$UID/com.m0n01d.munbyn-ble-bridge` (cancels a
  job in progress). **With `--launch-mode open`** the LaunchAgent's job is
  `open`, not the app, so `kickstart -k` only restarts `open` -- it finds
  MunbynBLE still running and just waits on it again. Restart the bridge
  itself with `pkill -TERM -f ~/Applications/MunbynBLE.app/Contents/MacOS/MunbynBLE`
  instead; KeepAlive relaunches it (through `open`, since that's still how
  the LaunchAgent is configured).
- **Never asked / clicked Don't Allow:** turn MunbynBLE on under System
  Settings > Privacy & Security > Bluetooth, or reset and restart so macOS
  asks again:
  `tccutil reset BluetoothAlways com.m0n01d.munbyn-ble-bridge` then restart
  it (the bullet above -- kickstart in direct mode, pkill in open mode).
  Re-running the installer after the launcher itself changed rebuilds
  (re-signs) the app, and macOS asks once more.
- **Prompt names something other than MunbynBLE** (or MunbynBLE never gets a
  Bluetooth switch): reinstall with `scripts/install-ble-bridge.sh
  --launch-mode open` (the LaunchAgent then starts the app through
  LaunchServices, `open -W -g -a`).
- **"The MunbynBLE bridge is not running"** from the CLI/web UI: run
  `scripts/install-ble-bridge.sh`. A Preview job sent while the bridge is down
  waits in the CUPS queue (the socket backend keeps retrying) and prints when
  it's back.
- **Port 9100 taken** (the log says "Cannot listen"): see who has it with
  `lsof -nP -iTCP:9100 -sTCP:LISTEN`, or move the bridge: set
  `"ble_bridge_port"` in the config, restart it (see above), and re-run
  `sudo scripts/install-cups-queue.sh --ble --ble-port <N>`.
- **Remove it:** `scripts/install-ble-bridge.sh --uninstall` and
  `sudo scripts/install-cups-queue.sh --uninstall --ble`.

### Bluetooth from Terminal without the bridge (`--ble-direct`)

For a command-line tool, the permission belongs to the app it runs in --
Terminal, iTerm2 or VS Code, not `python3` -- so the first `--ble-scan` or
`--ble-direct` run from, say, Terminal pops the prompt for *Terminal*. Allow
it; the grant covers everything later run from that app. To reset:

```sh
tccutil reset BluetoothAlways com.apple.Terminal      # Terminal
tccutil reset BluetoothAlways com.googlecode.iterm2   # iTerm2
```

Over SSH there is no app to show the prompt to; use the bridge. Bluetooth
must also be switched on.

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
munbyn/                     labels, tspl, render, usb_transport, config,
                            ble_protocol (pure BLE frames), ble_transport (bleak),
                            tspl_parse + ble_bridge (TSPL -> Bluetooth bridge),
                            ble_bridge_client (CLI/web side of the bridge)
templates/, static/         web UI assets (plain HTML/JS, no build step)
scripts/install-pdf-service.sh   builds/links the PDF-menu "app" print path
scripts/print-from-dialog.sh     wrapper that app shells out to
cups/, scripts/install-cups-queue.sh   native CUPS queues: USB, and --ble (socket -> bridge)
scripts/install-ble-bridge.sh    builds MunbynBLE.app + its LaunchAgent (the Bluetooth bridge)
macos/munbyn-ble-launcher.c      MunbynBLE.app's executable (runs the bridge as a child)
PLANS/PLAN.md               dated hardware facts, decisions, open items
PLANS/BLE-PROTOCOL.md       Bluetooth protocol spec (from Munbyn's web editor)
PLANS/BLE-IMPLEMENTATION.md Bluetooth plan and status
PLANS/BLE.md                first Bluetooth notes (superseded by BLE-PROTOCOL.md)
tests/                      pytest -- USB and Bluetooth are always mocked
tests/fixtures/ble/         Bluetooth golden vectors + capture tools
```

See `CLAUDE.md` for the module map, agent rules, and the printer's verified
command subset, and `PLANS/PLAN.md` for the full research trail.
