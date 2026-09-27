# PLAN

## Status (2026-09-27, hardware-verified)

**The USB/TSPL path works.** The conductor printed a real job from this
repo's design to the real RW403B on real 4x6 gap labels: header + `CLS` +
one `BITMAP` + `PRINT`, written to bulk OUT `0x03` of interface 0 -- it
printed correctly, with the polarity this repo already defaulted to
(`bitmap_black_is_one=False`). Two other things were verified *not* to work
on this firmware and are now permanently ruled out (see "Struck-through
wrong assumptions" below): native `TEXT`/`BOX`/`BAR` commands, and
`GAPDETECT`. As a direct result, `munbyn/tspl.py`'s self-test was rewritten
from a native-command layout to a pure-`BITMAP` one (see
`selftest_image`/`selftest_job`), and a `SUPPORTED_COMMANDS` tuple + test now
enforce that every job this repo builds stays inside the verified subset.

The native CUPS queue path ("Munbyn RW403B (native)", `cups/`,
`scripts/install-cups-queue.sh`) is a separate, parallel track owned by a
different agent -- not covered by this file's authorship beyond linking to
it from the README.

**Update (2026-09-27, same day, conductor):** a second round of caliper
measurements found this printer mechanically short along the paper feed
(an 800-row bar printed at 98.1mm, not 100mm) -- see "Verified facts --
feed/x-axis calibration" below. `feed_scale=0.981` now corrects for it
end-to-end (render, self-test, `SIZE` line), **verified on paper**, and a
new `--scale-test` calibration label exists to re-derive it. A competing
hypothesis about fixing x-axis alignment via `SIZE` width was tested and
struck; x-alignment stays on `--x-shift` alone.

## Verified facts -- real hardware print (2026-09-27, conductor, real 4x6 gap labels)

1. **The BITMAP path prints correctly.** Exactly this job -- `SIZE 102
   mm,152 mm` / `GAP 3 mm,0 mm` / `REFERENCE 0,0` / `OFFSET 0 mm` / `SETC
   AUTODOTTED OFF` / `DENSITY 12` / `SPEED 4` / `DIRECTION 0,0` / `CLS` /
   `BITMAP 0,0,102,1218,1,<124236 raw bytes>` / `PRINT 1,1` -- printed with
   correct polarity. **`bitmap_black_is_one=False` (clear bit = black) is
   confirmed**, not just "medium confidence" from third-party TSPL
   implementations. Density 12 (the vendor PPD's own default) looked fine.
2. **Native `TEXT`/`BOX`/`BAR` print *nothing at all*, not even a feed.**
   The same verified header + `CLS` + native TSPL `TEXT`/`BOX`/`BAR`
   commands + a small `BITMAP` + `PRINT` produced no output (an earlier
   attempt with these commands possibly fed one blank label). Conclusion:
   this firmware's USB path implements only the vendor filter's own command
   subset -- anything outside it is **silently dropped**, not merely
   unsupported-but-harmless. `munbyn.tspl.SUPPORTED_COMMANDS` now codifies
   exactly that subset: `SIZE, GAP, BLINE, REFERENCE, OFFSET, SETC
   AUTODOTTED OFF, DENSITY, SPEED, DIRECTION, CLS, BITMAP (mode 1), PRINT`.
   Two more commands exist in the vendor filter binary --
   `SETC PAUSEKEY OFF`, and a compressed `BITMAP x,y,wb,h,3,len,<data>` mode
   that links libz -- but are **untested** and are not used anywhere in this
   repo.
3. **`GAPDETECT` alone does nothing** (sent by itself, nothing happened --
   no feed, no error). Status queries were also tried:
   `<ESC>!?`, `~!T`, `~!I`, `~!@`, `<ESC>!S` all got **no reply on bulk
   IN**. The IEEE-1284 device-ID control request (a separate, non-TSPL USB
   mechanism) does work, which is why `--status`/`--list` can still show a
   device id even though the TSPL status byte never comes back.
4. **Calibration is a manual, physical procedure**, per the RW403B manual:
   load at least 4 labels; closing the cover triggers automatic label
   identification. Fallback: hold the feed button until it beeps **once**
   (label identification). Single click = feed one label; double-click or
   hold to **two** beeps = printer's own self-test page; hold to **three**
   beeps (~6s) = reset. LED: green = ready, blue = Bluetooth connected, red
   = label not identified or cover open, flashing green+red = head
   overheated. There is no TSPL command for this (see fact 3) --
   `print_label.py --calibrate` sends nothing and just prints this
   procedure.
5. **The vendor's own CUPS queue** (`Munbyn_RW403B`,
   `usb://Munbyn/RW403B?serial=MP-RHHN1UV2`, PPD
   `/etc/cups/ppd/Munbyn_RW403B.ppd`, source PPD
   `/Library/Printers/Munbyn/RW403B.ppd`) still fails on this Mac: its
   filter `/Library/Printers/Munbyn/rastertorw403b` is x86_64-only and
   Rosetta is not installed (`com.apple.badarch-error`). For reference,
   CUPS's own `cgpdftoraster` step produces 8-bit gray
   (`cupsColorSpace 0`, `cupsBitsPerColor 8`), 812x1218 for 4x6 at 203 dpi,
   `PreferredRotation 90`. Printer sharing is off (see "Next steps").

## Verified facts -- device enumeration (2026-09-27, printer attached, no print yet)

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
- Vendor PPD: media Gap (default) / Continuous / Black line; gap height
  0-10mm (default 3); gap offset 0-10mm (default 0); speed 1-8 (default 4);
  darkness 1-16 (default 12); h/v offset +/-20mm; 203 dpi; default page 4x6in
  (812x1218 dots); max media width 4.25in; stock sizes (in): 1.60x1.20,
  1.96x1.20, 1.96x1.96, 2x1, 2x2, 2.25x1.25, 2.25x2.25, 2.30x2.30, 2.5x1.5,
  3x2, 3x3, 3x5, 4x6.
- `editor.munbyn.com/create` prints fine from Chrome but only imports
  PNG/JPEG, and does so over **Bluetooth + a custom protobuf protocol**, not
  TSPL/USB (confirmed by reading its JS bundles -- zero hits for TSPL
  command strings). Its own success therefore isn't evidence about the
  TSPL/USB path here; it's a different code path entirely. Research on that
  BLE protocol is kept separately in `PLANS/BLE.md` (unverified, future
  track), and was deliberately **not** used as evidence for this repo's
  `BITMAP` bit polarity -- see the note in `munbyn/tspl.py`'s docstring.

## Verified facts -- feed/x-axis calibration (2026-09-27, conductor, real
printer, 4x6 gap labels, caliper measurements by Dwight)

1. **Across the print head, the printer is accurate:** an 800-dot bar
   printed at 8 dots/mm (100mm), matching the vendor PPD's 203 dpi within
   caliper precision. No correction needed on this axis.
2. **Along the paper feed, the printer is mechanically SHORT:** an 800-row
   bar printed at **98.1mm**, not 100mm. `feed_scale = printed_length /
   intended_length = 0.981`. Munbyn's own phone app (Bluetooth, a
   completely different code path -- see the device-enumeration facts
   above) showed the same *kind* of shortfall (Dwight measured 97.23mm
   there) -- different magnitude, same direction, which is why this is
   treated as a **mechanical** printer/feed-roller property, not a bug in
   this repo's math or a quirk of the USB/TSPL path specifically.
3. **The fix is verified on paper, not just in theory.** Stretching the
   rendered image along the feed axis by `1/feed_scale` before sending it
   printed a bar at ~100mm. The exact job that did it, using the sample
   `ShedLab-labelsPLYWOOD.pdf`'s page 8 (a vertical 100mm bar with end
   caps, git-excluded, kept only as a local dry-run fixture):
   - page rendered at scale 203/72 -> 812x1218 gray (physical, 4x6 at
     203dpi)
   - resized in **grayscale**, height only, with **LANCZOS**, to
     `round(1218 / 0.981) = 1242` rows (812 wide, unchanged) -- *before*
     thresholding to 1-bit
   - thresholded at 160
   - sent as `SIZE 102 mm,155 mm` / `GAP 3 mm,0 mm` / `REFERENCE 0,0` /
     `OFFSET 0 mm` / `SETC AUTODOTTED OFF` / `DENSITY 12` / `SPEED 4` /
     `DIRECTION 0,0` / `CLS` / `BITMAP 0,0,102,1242,1,<data>` / `PRINT 1,1`
     (all lines CRLF) -- one label, no extra feed.
   - **General formulas** (now implemented as `munbyn.labels
     .stretched_height_dots`/`apply_feed_scale`/`validate_feed_scale` and
     used everywhere a job is built -- see `munbyn/tspl.py::header`,
     `munbyn/render.py::render_image`, `munbyn/tspl.py::selftest_image`):
     bitmap height = `round(label_height_dots / feed_scale)`; `SIZE`
     length = `round(label_height_mm / feed_scale)` whole mm (`152.4/0.981
     = 155.35 -> 155`); `SIZE` width and `GAP` are unchanged.
4. **A follow-up dry run of the same page 8 confirms the math end-to-end**
   (CLI defaults, and `--scale 100 --crop none` which reproduces the
   verified job's own rendering path): both produce the identical header
   above (`SIZE 102 mm,155 mm`, `BITMAP 0,0,102,1242,...`); the
   `--scale 100 --crop none` run's bar measures **822 rows** outer-cap-to-
   outer-cap in the stretched bitmap, matching this fact's "~822" estimate
   for the verified job almost exactly (806 unstretched rows x
   `1242/1218` stretch factor = 822.1). The CLI-defaults run (auto-crop +
   `fit`, which scales the trimmed bar+caption-text bounding box to fill
   the label rather than rendering at 1:1 physical scale) measures a
   different, larger bar (~1020 rows) -- expected, not a discrepancy: it's
   a different fit mode, not a different feed_scale bug.
5. **X-axis alignment is a separate, already-handled concern.** The
   image lands ~3.1mm right of the label's left edge regardless of `SIZE`
   width (not a `SIZE`/firmware effect -- tested and struck, see below;
   Dwight attributes it to how the label physically sits in the printer).
   This repo
   does not change any default to compensate; it's left to the existing
   `--x-shift`, now with `--scale-test`'s "A across" bar (a nominal,
   printed left-gap value to compare against a caliper measurement) to
   help calibrate it per label stock.

## Decisions

- **TSPL, not ESC/POS.** The device's own IEEE-1284 ID says `CMD:TSPL`; the
  vendor CUPS filter's embedded strings are TSPL commands; **confirmed** by
  a real print. High confidence.
- **`pypdfium2` for PDF rendering**, cropping to the detected content bbox
  and rendering only that region at device resolution -- never low-res then
  upscaled -- so text and barcodes stay sharp.
- **Web UI on port 5050**, not 5000 (AirPlay Receiver owns 5000 on macOS).
- **Python 3.9 compatibility everywhere** -- `/usr/bin/python3` is 3.9.6 and
  is the required fallback interpreter; both entry points re-exec into
  `.venv` if a dependency import fails.
- **Bitmap polarity: `bitmap_black_is_one = False`** (clear bit = black
  dot) -- **confirmed against real hardware** 2026-09-27, not merely
  inferred from third-party TSPL implementations. Still configurable
  (`--black-is-one`) since a different printer/firmware could differ; the
  self-test's polarity swatch is what to re-check it against.
- **Every job this repo builds is restricted to
  `munbyn.tspl.SUPPORTED_COMMANDS`** (`SIZE, GAP, BLINE, REFERENCE, OFFSET,
  SETC AUTODOTTED OFF, DENSITY, SPEED, DIRECTION, CLS, BITMAP, PRINT`),
  enforced by a test. This firmware silently drops anything else rather than
  erroring on it, which makes an out-of-subset command a *silent* failure
  mode -- worth over-enforcing against.
- **The self-test label is one `BITMAP`, not native `TEXT`/`BOX`/`BAR`.**
  Direct consequence of fact 2 above. `selftest_image()` draws the whole
  label (border, mm rulers, crosshair, identifying text, polarity swatch)
  with Pillow and a real TrueType font (`ImageFont.load_default(size=N)`,
  Pillow >= 10.1, falling back to `/System/Library/Fonts/Helvetica.ttc`),
  and it's exposed separately from `selftest_job()` so `--preview`/the web
  UI can show the *exact* image, not an approximation.
- **PDFs are rasterised at the final scale.** Rotation and fit/fill/scale
  are decided from the content bbox first, then pdfium renders exactly that
  region at exactly the dots-per-point the label needs; `--scale 100` and
  `--fit actual` are true physical size for PDFs. Images honour real DPI
  metadata (a 72-dpi stamp is treated as "none": pixel = dot).
- **`SETC AUTODOTTED OFF` sent unconditionally**, matching both Munbyn's own
  filter and an unrelated clone vendor's default. The command isn't in
  either official TSC manual and its exact effect is unconfirmed, but it's
  part of the verified-working job, so it stays exactly where the vendor
  filter put it.
- **The PDF-menu print path is an app, not an executable script**
  (`scripts/install-pdf-service.sh` now builds "Print to Munbyn RW403B.app"
  via `osacompile` and links it into `~/Library/PDF Services`, instead of
  writing a shell script directly into that folder). Direct consequence of
  the struck-through "shell-script PDF service" assumption below.
- **`feed_scale` corrects the printer's mechanical feed shortfall, applied
  in exactly one place per concern.** `munbyn.config.DEFAULTS["feed_scale"]
  = 0.981` (this printer, measured 2026-09-27 -- see the feed/x-axis
  calibration facts above). `munbyn.labels.apply_feed_scale`/
  `stretched_height_dots`/`validate_feed_scale` are the single shared
  implementation, called from `munbyn.render.render_image` (PDFs and
  images), `munbyn.tspl.selftest_image` and `munbyn.tspl.scale_test_image`
  (still grayscale, before thresholding), and `munbyn.tspl.header` (the
  `SIZE` length). `munbyn.tspl.build_job` validates every page's height
  against the same stretched value, so a page rendered with a different
  `feed_scale` than the `JobSettings` it's built with fails loudly instead
  of silently shipping a bitmap that disagrees with its own `SIZE` line.
  `feed_scale=1.0` is an exact no-op everywhere (byte-identical output to
  before this correction existed) -- confirmed by a regression test against
  the verified 4x6 job header and 1218 rows. `--preview`/the web UI's
  preview always show the label **un-stretched** (physical size, as it
  will look on paper), not the stretched bitmap actually sent -- see
  `print_label.py`'s `--preview` help text.
- **`--scale-test` (CLI) / `/api/scale-test` (web) print a two-bar
  calibration label** ("A across" the head, target 90mm; "B along the
  feed", target 100mm; both with 10mm ticks plus a final tick at the true
  end, `feed_scale`'s current value printed on the label) using the
  *current* `feed_scale`. Either bar is clipped (and the label says so) on
  stock too small for its target length, so the label always prints each
  bar's *actual* drawn length, not the target -- the recalibration formula
  uses that printed length: measure B, `new_feed_scale = old_feed_scale *
  measured_B_mm / drawn_B_mm`, `--feed-scale <new> --save-defaults`, re-run
  `--scale-test` to confirm. See the README's calibration section.

## Struck-through wrong assumptions

- ~~Status queries would behave like typical ESC/POS-style query/reply
  commands and get *some* reply.~~ **Killed by:** real hardware, fact 3
  above -- five different status-query byte sequences (`<ESC>!?`, `~!T`,
  `~!I`, `~!@`, `<ESC>!S`) all got no reply at all on bulk IN. `--status`
  still sends `<ESC>!?` once (cheap, harmless, and would show a reply if one
  ever came), but reports "no reply" as the expected, correct outcome, not
  an error.
- ~~`GAPDETECT` would trigger the printer's own gap/label auto-calibration
  (feeding a few labels while it learns spacing).~~ **Killed by:** real
  hardware, fact 3 above -- sent alone, it was verified to do nothing at
  all. Calibration is now documented as the manual, physical procedure from
  fact 4 (`print_label.py --calibrate` sends nothing).
- ~~The self-test label could mix native `TEXT`/`BOX`/`BAR` (drawn by the
  printer itself) with one `BITMAP` for the polarity swatch, the way the
  vendor's own working job is structured for the image portion.~~ **Killed
  by:** real hardware, fact 2 above -- a job with native `TEXT`/`BOX`/`BAR`
  commands (plus an otherwise-correct header and a small `BITMAP`) printed
  **nothing**, not even a feed. This firmware's USB path only honours
  `SUPPORTED_COMMANDS`; the self-test is now one pure `BITMAP` image built
  entirely with Pillow (`selftest_image`).
- ~~An executable shell script placed directly in `~/Library/PDF Services`
  (scanning all argv for a readable file, since the exact argv contract is
  undocumented) could work as the PDF-menu print path.~~ **Killed by:** a
  first-hand report (Ventura, Dec 2022) that the print dialog's host process
  has been sandboxed since Big Sur and refuses to run scripts, compiled
  binaries, or Automator plugins placed there -- **confirmed again on this
  Mac's macOS 26** (conductor, hardware result 6, 2026-09-27).
  `install-pdf-service.sh` now builds an *application* (`osacompile` from an
  AppleScript droplet) and links that into `~/Library/PDF Services` instead
  -- LaunchServices opens an app there rather than the sandboxed host trying
  to execute it, and the conductor confirmed **applications placed there (or
  aliases to them) do still work**. What's not yet confirmed is this
  specific built app/symlink, end to end, against a real Print dialog -- test
  it before relying on it (see the README).
- ~~Firmware centers the `SIZE` width on a 108mm head, so `SIZE 108 mm`
  would move the image 3mm left, fixing the x-alignment offset.~~ **Killed
  by:** real hardware, 2026-09-27 -- tested with `SIZE 108 mm` and the left
  gap did not change at all. The image lands ~3.1mm right of the label's
  left edge regardless (a bar with a nominal 0.75mm left gap measured
  3.87mm; the label itself measures 101.39mm wide, not 108mm). Dwight
  attributes the offset to how the label physically sits in the printer,
  not to `SIZE`. X-alignment is left entirely to the existing `--x-shift`
  (default unchanged); `--scale-test`'s "A across" bar exists to make that
  offset measurable per label stock, not to eliminate the need for it.

## Open items

- [x] **Bitmap polarity + alignment** -- confirmed via a real print
  2026-09-27 (`bitmap_black_is_one=False`). Still worth re-checking with
  `--selftest` after any stock change or on a different unit.
- [x] **Feed-axis length (`feed_scale`)** -- confirmed via a real print
  2026-09-27 (`feed_scale=0.981`, see the feed/x-axis calibration facts
  above); `--scale-test` exists to re-derive it after a stock change or on
  a different unit. X-axis alignment remains a separate, un-automated
  concern (`--x-shift`, no default change).
- [ ] **Gap calibration** -- no TSPL command works (see fact 3); the manual
  procedure (fact 4) hasn't yet been exercised end-to-end by an agent (only
  documented) -- a human should run through cover-close vs. feed-button
  calibration at least once and note here whether the detected gap/label
  length persists across power-off.
- [ ] **The regular Preview "Print" button** -- two candidate paths, both
  still unverified on paper, instead of the three-option open question this
  used to be:
  - **(A) Install Rosetta** -- still not chosen; Apple's Sept 2026 developer
    notice says Rosetta support ends for good after macOS 27, so this
    remains a dead end long-term and is not being pursued.
  - **(B) A native arm64 CUPS filter** (raster -> TSPL) -- built and
    byte-verified against the hardware-confirmed job (see `cups/README.md`),
    as a separate track/agent (`cups/`, `scripts/install-cups-queue.sh`,
    queue name "Munbyn RW403B (native)"). **Not yet printed on paper.** Not
    owned by this file's authors beyond linking to it from the README.
  - **(C) The PDF Services app** (`scripts/install-pdf-service.sh`) --
    rewritten as an app bundle rather than a raw script (see the struck-through
    assumption above). Builds and decompiles correctly
    (`osadecompile`-verified against a `--dest /tmp` test build), and the
    conductor confirmed apps placed in `~/Library/PDF Services` do run from
    the Print dialog's PDF menu (hardware result 6) -- but **this specific
    app/symlink is not yet tested against a real macOS Print dialog** -- do
    that before relying on it for real prints.
- [ ] The web UI has only been exercised against pytest's Flask test client
  with USB mocked, plus a manual dry-run smoke test of `/api/selftest` -- not
  against a real browser end-to-end, and not against a real printer.

## Next steps

- [ ] **BLE transport** -- separate future track; see `PLANS/BLE.md`
  (unverified research: GATT service/characteristics, protobuf message
  shapes, `enpack()` framing, chunking/retry protocol). Real protocol work,
  not a small addition -- would need its own implementation phase and its
  own hardware verification before trusting any of it.
- [ ] **Install the native CUPS queue** (`sudo ./scripts/install-cups-queue.sh`,
  a separate track/agent's work) and verify Preview's normal paper-size/
  scaling controls actually reach the printer correctly through it.
- [ ] **Optional: printer sharing**, so the MacBook (`dwight`) could print
  through the Mac mini (`dwightdoane`) over the network instead of needing
  the printer plugged into whichever machine is in front of Dwight --
  **needs Dwight's own OK first**: this is a macOS System Settings /
  sharing change (printer sharing), which is outside what an agent should
  flip on its own (see CLAUDE.md's rules on system/CUPS settings).
