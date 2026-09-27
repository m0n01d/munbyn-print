# Native CUPS filter for the Munbyn RW403B

Munbyn's macOS driver fails on Apple Silicon. Its filter
(`/Library/Printers/Munbyn/rastertorw403b`) is x86_64-only, and Rosetta isn't
installed. This directory replaces that filter with our own **universal
(arm64 + x86_64)** CUPS filter, so Preview's normal **File > Print** works.
The Print dialog keeps its paper size, scaling, copies and "Printer Features"
options.

| File | What it is |
| --- | --- |
| `rastertotspl.c` | The CUPS raster -> TSPL filter |
| `Makefile` | `make` (universal build), `make test-local`, `make ppd` (the PPD to install, for a feed scale), `make clean` |
| `munbyn-rw403b-native.ppd` | Munbyn's RW403B PPD with the filter swapped, a feed-corrected default Resolution (203x207dpi), plus 4x4/4x3/4x2/4x1 in sizes, a custom size and a Threshold option. It is the template for feed scale 0.981 |
| `../scripts/install-cups-queue.sh` | Generates the PPD for `--feed-scale`, installs the filter + PPD and adds the queue (run with `sudo`) |
| `../tests/test_cups_filter.py` | Builds the filter and tests it on synthetic rasters and through `cupsfilter` |

## What it sends to the printer

The filter sends only the TSPL subset verified on this printer on
2026-09-27. When a job also contained `TEXT`, `BOX` or `BAR`, the printer
printed nothing. Per job it sends:

```
SIZE <w> mm,<h> mm            the bitmap in whole mm of printer steps (see below)
GAP <g> mm,<o> mm             or BLINE <g> mm,<o> mm, or GAP 0,0 (continuous)
REFERENCE 0,0
OFFSET 0 mm
SETC AUTODOTTED OFF
DENSITY <0..15>
SPEED <1..8>
DIRECTION 0,0
```

Then, for each page, it sends `CLS`, `BITMAP 0,0,<bytes/row>,<rows>,1,<raw data>` and `PRINT 1,<copies>`.
Every line ends in CR LF. In the bitmap data a clear bit prints a **black** dot,
the most significant bit is the leftmost dot, and row padding is white. The
bitmap always covers the whole page, so the label advances correctly.

The filter sends the header once per job, and again only if the page size
changes.

`SIZE` comes from the raster, not from the PPD's page size. Both numbers
count raster dots/rows as printer steps of 1/203 in (8 per mm):
`round(cupsWidth / (203 / 25.4))` and `round(cupsHeight / (203 / 25.4))`,
using `HWResolution[0]` for the 203. Rows are always steps of the feed,
whatever the vertical resolution the page was rendered at.

For a 4x6 page, the header and `BITMAP` line are byte-identical to jobs
verified on paper:

| Resolution | Raster | Header and `BITMAP` line |
| --- | --- | --- |
| `203x207dpi` (default, feed-corrected) | 812 x 1242 | `SIZE 102 mm,155 mm` / `GAP 3 mm,0 mm` / ... / `BITMAP 0,0,102,1242,1,` |
| `203x203dpi` (uncorrected) | 812 x 1218 | `SIZE 102 mm,152 mm` / `GAP 3 mm,0 mm` / ... / `BITMAP 0,0,102,1218,1,` |

**`SIZE` can differ by up to ~1 mm from `munbyn/tspl.py`'s `header()` on page
sizes other than 4x6.** Both round to whole mm, but from different starting
numbers: this filter rounds the *raster's* dot/row count (which itself came
from rounding a physical mm size to pixels, then, for the feed axis, to
whatever integer dpi the PPD's `Resolution` choice quantizes `feed_scale`
to -- e.g. `207` for `0.981`, versus the true `0.981`), while
`munbyn/tspl.py` rounds the physical mm size directly. A 1.20in-tall label
at `203x203dpi` (uncorrected) gets `31 mm` here vs `30 mm` from
`munbyn/tspl.py` (`244` dots is `30.53mm`, but `round(30.48mm) = 30`); a 3x5
label at the feed-corrected default gets `130 mm` here vs
`round(127 / 0.981) = 129 mm`. Fixing this exactly would mean plumbing the
real `feed_scale` (not just the quantized `HWResolution` ratio) into the
filter, e.g. as a PPD attribute read with `ppdFindAttr` -- not done, since
every size this filter is actually used with (see the table above and the
PPD's page sizes) is within 1 mm either way and the two paths are not
expected to be byte-identical outside the verified 4x6 case.

## Feed correction (Resolution)

Caliper measurements on real 4x6 gap labels (2026-09-27):

- **Across the head the printer is exact.** 800 dots = 100 mm.
- **Along the feed it is short.** An 800-row bar printed 98.1 mm long, so
  the feed scale (printed length / intended length) is 0.981. Munbyn's own
  phone app shows the same kind of error (97.23 mm), so it is mechanical.
- **Stretching the image along the feed by 1 / 0.981 fixed it on paper.**
  The ShedLab page-8 bar (100 mm) printed at about 100 mm with
  `SIZE 102 mm,155 mm` and a 1242-row `BITMAP`. `GAP` and the width were
  unchanged.

The PPD gets the same result without resampling. Its default Resolution is
`HWResolution [203 207]` (207 = round(203 / 0.981); the residual error is
0.03%). Apple's `cgpdftoraster` honours the non-square resolution: it
renders the page at 207 rows per inch, so a 4x6 page is 812 x 1242 and
vector PDFs stay sharp. That makes Preview's "Scale: 100%" physically true
along the feed. Checked with `cupsfilter`: the ShedLab page-8 bar comes out
822 rows outer-to-outer (806 at 203x203), the same rows as the verified
resample-by-hand job, and the filter's header bytes equal that job's.

The filter never resamples, with one exception. `Rotate` 90/270 on a
non-square raster turns the page's 203-dpi axis into the feed axis and its
207-dpi axis into the head axis. The filter then resamples the gray image
(bilinear, before thresholding) so the rotated page keeps its physical
aspect ratio. `Vertical` offsets are converted with the vertical resolution,
so 10 mm is 81 rows at 207 (80 at 203).

The uncorrected choice, `203x203dpi` ("203 dpi, uncorrected"), is still in
the PPD. Use it to re-measure: `lp -o Resolution=203x203dpi`, print a 100 mm
bar along the feed, and take feed scale = measured mm / 100. When measuring
with the correction on, use the current feed scale x measured mm / 100
instead. Then re-run the installer with `--feed-scale` (see Install).

`make -C cups ppd FEED_SCALE=0.975 PPD_OUT=/some/dir/x.ppd` writes the PPD
for another feed scale: the default choice becomes `203x<round(203 / F)>dpi`,
labelled with F. The uncorrected choice is dropped when F rounds to 203. The
target accepts 0.90..1.10 and fails if the template no longer has exactly one
`203x207dpi` choice to rewrite.

## Horizontal alignment

The image lands about 3.1 mm right of the label's left edge. A bar drawn
with a nominal 0.75 mm left gap measured 3.87 mm from the edge, and the
label itself measures 101.39 mm wide. Dwight attributes this to how the label
sits in the printer. It is not corrected by default.

~~The firmware centers the `SIZE` width on its 108 mm head, so
`SIZE 108 mm` would move the image 3 mm left.~~ **Struck:** tested on paper
with `SIZE 108 mm`, and the left gap did not change. The filter keeps
`SIZE` at the page width.

To shift the image, use the `Horizontal` option (`lp -o Horizontal=-3`, or
Printer Features > Horizontal Offset). It is the CUPS counterpart of the
CLI's `--x-shift`. A negative value moves the image left, and whatever goes
past the left edge is cut off.

## Options (Print dialog > Printer Features, or `lp -o Name=value`)

| PPD option | Default | Emitted as |
| --- | --- | --- |
| MediaType | 1 Gap | 1 -> `GAP`, 2 Black Line -> `BLINE`, 0 Continuous -> `GAP 0,0` |
| GapHeight / GapOffset | 3 / 0 mm | the two numbers of `GAP` / `BLINE` |
| Darkness | 12 | `DENSITY` (same number; 16 is clamped to 15) |
| PrintSpeed | 40 | `SPEED` = value / 10 (1..8) |
| Resolution | 203x207dpi | Not a TSPL command: it sets the raster `cgpdftoraster` renders. `203x207dpi` = feed-corrected (default), `203x203dpi` = uncorrected. See "Feed correction". |
| Horizontal / Vertical | 0 mm | Moves the image right/down. A negative value moves it left/up, and whatever goes past the edge is cut off. Horizontal is converted with the horizontal resolution, Vertical with the vertical one. |
| Rotate | 0 | 0/90/180/270 degrees clockwise. The filter rotates the image itself and always sends `DIRECTION 0,0`, the only verified value. 90/270 swap the label width and height, so a 6x4 landscape page prints on 4x6 stock. |
| PrintMode | Default | Default = plain threshold. ErrorDiffusion = Floyd-Steinberg. Diffusion = 8x8 ordered dither. Gathering = clustered ordered dither. |
| Threshold | 160 | In the Default mode, pixels darker than this print black. Pick a higher value for darker output. |

Copies come from the raster header's `NumCopies`, and fall back to `argv[4]`
when that is 0. `cgpdftoraster` sets `NumCopies` to 1 when it has already
rendered collated copies itself (P1 P2 P1 P2), while `argv[4]` still holds the
full count. Using `argv[4]` would therefore double collated jobs.

Scaling is done by the Print dialog / `cgpdftoraster`, not by the filter.
Choose the label size as the Paper Size and use "Scale to fit" (or `-o
fit-to-page`) for Letter-size documents. Without it, a Letter page is drawn at
100% from its bottom-left corner. At the default Resolution, 100% is true
size along the feed too.

## Build and verify (no install, no printing)

```sh
make -C cups                  # -> cups/rastertotspl (universal)
make -C cups test-local       # builds, then runs tests/test_cups_filter.py
```

To check the whole chain by hand, run the same filters cupsd would. This only
writes TSPL to a file:

```sh
mkdir -p /tmp/munbyn-cups
sed "s|/Library/Printers/Munbyn/rastertotspl|$PWD/cups/rastertotspl|" \
  cups/munbyn-rw403b-native.ppd > /tmp/munbyn-cups/local.ppd
cupsfilter -p /tmp/munbyn-cups/local.ppd -m printer/foo -e label.pdf > /tmp/munbyn-cups/job.bin
```

Or do it in two steps, raster first and then the filter alone:

```sh
grep -v '^\*cupsFilter' cups/munbyn-rw403b-native.ppd > /tmp/munbyn-cups/nofilter.ppd
cupsfilter -p /tmp/munbyn-cups/nofilter.ppd -m application/vnd.cups-raster label.pdf > /tmp/munbyn-cups/page.ras
PPD=cups/munbyn-rw403b-native.ppd ./cups/rastertotspl 1 dwight test 1 '' /tmp/munbyn-cups/page.ras > /tmp/munbyn-cups/job.bin
```

To inspect the result:

```sh
.venv/bin/python -c "import sys; from munbyn import tspl; print(tspl.describe(open(sys.argv[1],'rb').read()))" /tmp/munbyn-cups/job.bin
```

To see the image, decode it with `tspl.simulate(job, 812, 1242)` (1218 rows
for `-o Resolution=203x203dpi`).

Validate the PPD with `cupstestppd -W translations -I filters
cups/munbyn-rw403b-native.ppd`, which should print PASS. The only WARNs are
about Munbyn's non-Adobe size names. Without `-I filters` it FAILs until the
filter is installed as a root-owned file, which is expected.

## Install (Dwight, with sudo)

```sh
sudo scripts/install-cups-queue.sh                     # feed scale 0.981; or add --make-default
sudo scripts/install-cups-queue.sh --feed-scale 0.981  # the same, explicitly; re-run with a new value to update
sudo scripts/install-cups-queue.sh --uninstall         # removes only what it added
```

The script:

1. Generates the PPD for `--feed-scale` (default 0.981, allowed
   0.90..1.10) with `make -C cups ppd` into a temp dir, and checks it with
   `cupstestppd` before touching anything.
2. Builds the filter as you, if needed.
3. Installs `/Library/Printers/Munbyn/rastertotspl` (root:wheel 0755) and
   the generated PPD as
   `/Library/Printers/PPDs/Contents/Resources/munbyn-rw403b-native.ppd`
   (root:wheel 0644).
4. Finds the USB URI with `lpinfo -v`, and falls back to the known
   `usb://Munbyn/RW403B?serial=MP-RHHN1UV2`.
5. Creates the queue **Munbyn_RW403B_Native** ("Munbyn RW403B (native)"),
   or updates it in place with `lpadmin -P` (which also resets its option
   defaults to the PPD's). It enables the queue and sets it to accept jobs.
6. Checks that the queue's PPD now defaults to the expected Resolution, and
   prints a warning with the fix if it doesn't.

Munbyn's own files and the `Munbyn_RW403B` queue are left alone. The default
printer only changes with `--make-default`.

## Troubleshooting

- Check the queue with `lpstat -p Munbyn_RW403B_Native -l`. Clear stuck jobs
  with `cancel -a Munbyn_RW403B_Native`. Look for errors in
  `/var/log/cups/error_log`.
- For labels that feed wrongly or a red LED: load at least 4 labels and close
  the cover (this runs auto label identification). Or hold the feed button
  until **one** beep. Two beeps prints a self-test page; three beeps resets
  the printer.

## Not yet verified

- The CUPS path has not printed on paper yet. The job bytes match the verified
  jobs (header and `BITMAP` line exactly; the ShedLab page-8 bitmap differs
  from the hand-resampled one only in 438 anti-aliased edge pixels, with the
  same bar rows). But the macOS `usb` backend writes to the printer-class
  interface, while the verified prints used pyusb on interface 0, bulk OUT
  0x03. These are expected to be the same endpoint.
- The installer's `--feed-scale` flow has not been run. It is only checked
  statically (`bash -n`, and tests of `make ppd` + `cupstestppd`).
- `Rotate` 90/270 resampling on the 203x207 raster.
- These have only been checked by decoding the output, not on paper:
  - the dither modes
  - rotation
  - offsets
  - multi-page jobs
  - `BLINE` / continuous media
  - the non-default `DENSITY` and `SPEED` values
