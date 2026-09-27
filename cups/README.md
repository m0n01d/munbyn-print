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
| `Makefile` | `make` (universal build), `make test-local`, `make clean` |
| `munbyn-rw403b-native.ppd` | Munbyn's RW403B PPD with the filter swapped, plus 4x4/4x3/4x2/4x1 in sizes, a custom size and a Threshold option |
| `../scripts/install-cups-queue.sh` | Installs the filter + PPD and adds the queue (run with `sudo`) |
| `../tests/test_cups_filter.py` | Builds the filter and tests it on synthetic rasters and through `cupsfilter` |

## What it sends to the printer

The filter sends only the TSPL subset verified on this printer on
2026-09-27. When a job also contained `TEXT`, `BOX` or `BAR`, the printer
printed nothing. Per job it sends:

```
SIZE <w> mm,<h> mm            page size in whole mm (rounded like munbyn/tspl.py)
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
changes. For a 4x6 page at the PPD defaults, the header and `BITMAP` line are
byte-identical to the job verified on paper:
`SIZE 102 mm,152 mm` / `GAP 3 mm,0 mm` / ... / `BITMAP 0,0,102,1218,1,`.

## Options (Print dialog > Printer Features, or `lp -o Name=value`)

| PPD option | Default | Emitted as |
| --- | --- | --- |
| MediaType | 1 Gap | 1 -> `GAP`, 2 Black Line -> `BLINE`, 0 Continuous -> `GAP 0,0` |
| GapHeight / GapOffset | 3 / 0 mm | the two numbers of `GAP` / `BLINE` |
| Darkness | 12 | `DENSITY` (same number; 16 is clamped to 15) |
| PrintSpeed | 40 | `SPEED` = value / 10 (1..8) |
| Horizontal / Vertical | 0 mm | Moves the image right/down. A negative value moves it left/up, and whatever goes past the edge is cut off. |
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
100% from its bottom-left corner.

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

To see the image, decode it with `tspl.simulate(job, 812, 1218)`.

Validate the PPD with `cupstestppd -W translations -I filters
cups/munbyn-rw403b-native.ppd`, which should print PASS. The only WARNs are
about Munbyn's non-Adobe size names. Without `-I filters` it FAILs until the
filter is installed as a root-owned file, which is expected.

## Install (Dwight, with sudo)

```sh
sudo scripts/install-cups-queue.sh                 # or add --make-default
sudo scripts/install-cups-queue.sh --uninstall     # removes only what it added
```

The script:

1. Builds the filter as you, if needed.
2. Installs `/Library/Printers/Munbyn/rastertotspl` (root:wheel 0755) and
   `/Library/Printers/PPDs/Contents/Resources/munbyn-rw403b-native.ppd`
   (root:wheel 0644).
3. Finds the USB URI with `lpinfo -v`, and falls back to the known
   `usb://Munbyn/RW403B?serial=MP-RHHN1UV2`.
4. Creates the queue **Munbyn_RW403B_Native** ("Munbyn RW403B (native)"),
   enables it and sets it to accept jobs.

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
  job, but the macOS `usb` backend writes to the printer-class interface,
  while the verified print used pyusb on interface 0, bulk OUT 0x03. These are
  expected to be the same endpoint.
- These have only been checked by decoding the output, not on paper:
  - the dither modes
  - rotation
  - offsets
  - multi-page jobs
  - `BLINE` / continuous media
  - the non-default `DENSITY` and `SPEED` values
