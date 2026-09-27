# PLAN

## Status (2026-09-27)

Built, not yet hardware-verified. The CLI, web UI, config, and docs (this
file included) were written concurrently with `munbyn/labels.py`,
`munbyn/tspl.py`, `munbyn/usb_transport.py` and `munbyn/render.py` against a
shared architecture contract, in the same checkout, by separate builders. No
code in this repo has been run against the real printer yet -- the one real
test print is the conductor's job, not an agent's.

## Verified facts (2026-09-27, on this Mac, printer attached)

- USB `0d28:ccdd`, serial `MP-RHHN1UV2`, full speed, composite device.
  Interface 0 = printer class 7/1/2 (bidirectional), bulk OUT `0x03`, bulk IN
  `0x83`, 64-byte packets. Interface 1 = mass storage (kernel-owned, ignored).
- IEEE-1284 device ID (`ctrl_transfer(0xA1, 0, 0, 0, 1024)`, strip the 2-byte
  length prefix): `MFG:Munbyn;CMD:TSPL;MDL:RW403B;CMT:Label Printer;` -- the
  protocol is TSPL (TSC), not ESC/POS.
- Only `/usr/bin/python3` (3.9.6) is guaranteed to exist; all code targets
  3.9: `from __future__ import annotations`, no `match` statements, no
  runtime PEP 604 unions, no `tomllib`.
- `.venv` already has pyusb 1.3.1, libusb-package 1.0.30, pillow 11.3.0,
  pypdfium2 5.13.0, Flask 3.1.3, pytest 8.4.2, pytest-timeout 2.4.0.
  `libusb_package.get_libusb1_backend()` finds the device and reads the
  device ID without claiming it. Fallback dylib:
  `/opt/homebrew/lib/libusb-1.0.dylib`.
- `/opt/homebrew/bin` has `pdftoppm`, `pdftotext`, `timeout`; PDF rendering
  uses `pypdfium2` directly (device-resolution crop+render of the detected
  content bbox, never a low-res raster upscaled).
- Port 5000 is taken by macOS AirPlay Receiver (ControlCenter) -- the web UI
  defaults to `127.0.0.1:5050`.
- Munbyn's own macOS CUPS driver (queue `Munbyn_RW403B`, filter
  `/Library/Printers/Munbyn/rastertorw403b`) fails on this Mac: the filter is
  x86_64-only and Rosetta isn't installed
  (`printer-state-reasons=com.apple.badarch-error`, 0 bytes sent). Its own
  TSPL job, in order (from the filter's embedded strings): `SIZE` ->
  `GAP`/`BLINE` (or `GAP 0,0` for continuous) -> `REFERENCE 0,0` -> `OFFSET`
  -> `SETC AUTODOTTED OFF` -> `DENSITY` -> `SPEED` -> `DIRECTION 0,0` ->
  `CLS` -> `BITMAP` (mode 1 raw, or vendor mode 3 compressed) -> `PRINT 1,n`.
- Vendor PPD: media Gap (default) / Continuous / Black line; gap height
  0-10mm (default 3); gap offset 0-10mm (default 0); speed 1-8 (default 4);
  darkness 1-16 (default 12); h/v offset +/-20mm; 203 dpi; default page 4x6in
  (812x1218 dots); max media width 4.25in; stock sizes (in): 1.60x1.20,
  1.96x1.20, 1.96x1.96, 2x1, 2x2, 2.25x1.25, 2.25x2.25, 2.30x2.30, 2.5x1.5,
  3x2, 3x3, 3x5, 4x6.
- `editor.munbyn.com/create` prints fine from Chrome but only imports
  PNG/JPEG -- and, per the JS-bundle research below, does so over
  **Bluetooth + a custom protobuf protocol**, not TSPL/USB. Its own success
  therefore isn't direct evidence that TSPL-over-USB works; it's simply a
  different code path (the tool itself labels non-Bluetooth models "System
  Printing, USB driver required").

## Decisions

- **TSPL, not ESC/POS.** The device's own IEEE-1284 ID says `CMD:TSPL`; the
  vendor CUPS filter's embedded strings are TSPL commands. High confidence.
- **`pypdfium2` for PDF rendering**, cropping to the detected content bbox
  and rendering only that region at device resolution -- never low-res then
  upscaled -- so text and barcodes stay sharp.
- **Web UI on port 5050**, not 5000 (AirPlay Receiver owns 5000 on macOS).
- **Python 3.9 compatibility everywhere** -- `/usr/bin/python3` is 3.9.6 and
  is the required fallback interpreter; both entry points re-exec into
  `.venv` if a dependency import fails.
- **Bitmap polarity is configurable** (`--black-is-one`), not hardcoded:
  the official TSC manual never states polarity in prose (only an
  uncaptioned hex/binary example); two independent third-party TSPL
  implementations agree (`0`=black, `1`=white, MSB=leftmost) but neither is
  confirmed against Munbyn's own firmware (medium confidence) -- the
  selftest label exists specifically to settle this empirically, per device.
  **Default: `bitmap_black_is_one = False`** (clear bit = black dot), the
  TSC/EPL convention. Munbyn's web editor uses 1 = black, but that is its
  Bluetooth protobuf image path, not TSPL, so it was not taken as evidence
  for the TSPL `BITMAP` interpreter. `BITMAP` mode is always 1 (OR), the
  vendor filter's raw-bitmap template; mode 3 (compressed) is undocumented.
- **PDFs are rasterised at the final scale.** Rotation and fit/fill/scale
  are decided from the content bbox first, then pdfium renders exactly that
  region at exactly the dots-per-point the label needs; `--scale 100` and
  `--fit actual` are true physical size for PDFs. Images honour real DPI
  metadata (a 72-dpi stamp is treated as "none": pixel = dot).
- **`SETC AUTODOTTED OFF` sent unconditionally**, matching both Munbyn's own
  filter and an unrelated clone vendor's default. The command isn't in
  either official TSC manual and its exact effect is unconfirmed.

## Open items

- [ ] **Bitmap polarity + alignment** -- run `print_label.py --selftest`,
  read the printed label, set `--black-is-one` if the "LEFT HALF SHOULD BE
  BLACK" box prints inverted, nudge `--x-shift`/`--y-shift`, then
  `--save-defaults`. Not yet done -- no real print has been sent from this
  repo.
- [ ] **Gap calibration** -- run `--calibrate` once real stock is loaded.
  Unconfirmed by the TSC manual: how many labels it physically feeds, and
  whether the detected gap/label length persists across power-off (unlike
  several other `SET ...` commands, whose sections explicitly say they
  persist). Treat it as needing a re-run after any stock change, matching
  the RW403B manual's own guidance.
- [ ] **The regular Preview "Print" button** -- three options, none chosen
  or implemented yet:
  - **(A) Install Rosetta** so the vendor's x86_64 filter runs. Quick, but
    Apple's own Sept 2026 developer notice says Rosetta support ends for
    good after macOS 27 (narrow gaming-only carve-out beyond that) -- a dead
    end long-term. *Unreviewed.*
  - **(B) A native arm64 CUPS filter** (raster -> TSPL) replacing the
    vendor one. Durable, but touches `/Library` and CUPS, which this
    project's build rules disallow doing unsupervised. *Unreviewed.*
  - **(C) The PDF Services menu item** in
    `scripts/install-pdf-service.sh` (written, not installed automatically).
    Caveat: a first-hand report (Ventura, Dec 2022) found executable PDF
    Services broken since Big Sur -- the print dialog's host process is
    sandboxed and won't run scripts, compiled binaries, or Automator
    plugins. No first-hand confirmation either way exists yet for macOS
    15/26, so whether this even works on this Mac is unconfirmed -- test it
    before relying on it. *Unreviewed.*
- [ ] The web UI has only been exercised against pytest's Flask test client
  with USB mocked -- not against a real browser or a real printer.
