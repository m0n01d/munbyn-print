# Bluetooth transport: implementation plan

Goal: print to the RW403B with the USB cable unplugged. First from the CLI (P1, P2), then from
Preview's normal Print dialog (P3). The protocol is specified in `PLANS/BLE-PROTOCOL.md`. Golden
vectors and a reference encoder are in `tests/fixtures/ble/`.

**Status (2026-09-27): plan only. No transport code exists yet.** The protocol is known from the
editor's JS and our encoder matches it byte for byte, but none of it has touched the printer. The
USB/TSPL path stays the default and is not changed by any phase here.

**Rule carried over from CLAUDE.md:** agents never write to the real printer over BLE without
Dwight's explicit OK for that print. Tests always use a fake BleakClient. `--test` builds the
frames and prints a summary without starting the radio. Even a scan or a connect happens only
when Dwight asks for it in P2.

## Architecture

```
                        render (existing)                    ble_protocol (new, pure)            ble_transport (new, bleak)
file/PDF/PNG ─► munbyn.render / tspl.selftest_image ─► 1-bit page (812xN) ─► pad to 816, pack 1=black ─► sections ─► heatshrink ─► frames ─► GATT ABF4/ABF1, notify ABF3
                                                   └► tspl.build_job (USB, unchanged)

P3:  Preview ─► CUPS queue "Munbyn RW403B (Bluetooth)" ─► existing cups/rastertotspl (TSPL) ─► socket://127.0.0.1:9100
                                                                                                 │
     user LaunchAgent: munbyn.ble_bridge  ◄──────────────────────────────────────────────────────┘
       parse TSPL subset ─► page bitmaps ─► ble_protocol ─► ble_transport ─► printer
```

Why the P3 bridge takes **TSPL** in: the native filter already produces a TSPL job that was checked
on paper, 812x1242 at 203x207dpi. Taking that as input means the filter and PPD stay untouched,
and only the queue's device URI changes. Bluetooth can't live inside CUPS at all. cupsd is a system
daemon, and Apple states that system daemons cannot get Bluetooth TCC access. So the radio has to
belong to a process in Dwight's login session.

## Phases

### P1: CLI `--ble` transport (agent work, no hardware)

1. **`munbyn/ble_protocol.py`** is pure, with no I/O and no bleak import. It is ported from
   `tests/fixtures/ble/reference_framing.py` and `reference_job.py`, and holds:
   - the framing (`enpack`, `Unpacker`);
   - the hand-rolled proto3 encoder and decoder (`MPSendMsg`, `MPPrintMsg`, `MPRespondMsg`,
     `MPCodeMsg`, `MPDeviceInfoMsg`);
   - `page_bytes(img)`: pad to a multiple of 8 dots, then `tspl.pack_bitmap(img, black_is_one=True)`;
   - `BleJob` (sections and packets) and the control frames;
   - `Flow`, the notification state machine (ack, resend with rewind, errors, printed count,
     DEVICEINFO decoding);
   - `compose_page(settings, img)`, which bakes `x_shift_mm`/`y_shift_mm` into the page bitmap.
     TSPL did this with `BITMAP x,y`; BLE has no offsets. It uses the same crop and stretch rules
     as `tspl._shift_and_bitmap` and caps the width at 880 dots (`maxW`).
2. **`munbyn/ble_transport.py`** does the I/O through bleak, imported lazily so the USB path and
   the tests don't need it.
   - `find_ble_printers(timeout)` scans with a service filter of `0000abf0-…` and a name prefix of
     `RW403B`.
   - `BlePrinter(address=None)` is an async core with a synchronous wrapper
     (`asyncio.run`), in the same shape as `usb_transport.Printer`. Its methods are
     `device_info()`, `print_pages(pages, copies)`, `cancel()`, `set_density(n)` and
     `set_speed(n)`.
   - Every write is `write_gatt_char(char, frame, response=True)`: DEVICEPRINT goes to ABF4,
     everything else to ABF1.
   - Timeouts follow the editor: DEVICEINFO 4 s, section ack 10 s, connect 2 x 4 s.
   - The ack waiter is registered before a section's last packet is written.
   - The job refuses to start unless every `printstatus` bit is 0 (busy or calibrating waits 4 s
     and retries once), and refuses a label over 200 mm unless `supportfunction` bit 7 is set.
   - `perSize` is 148 when `blever == "1.0.8"`.
   - It disconnects when the job is done, so the phone app or Chrome can still connect.
   - Errors map onto the existing `PrinterError` family.
3. **CLI (`print_label.py`)**:
   - `--ble` (transport choice), `--ble-address UUID` and `--ble-scan` (list printers).
   - `--ble-info` sends DEVICEINFO only and shows status, versions, density and speed.
   - `--ble-density N` / `--ble-speed N` send the control message before the job. This is opt-in:
     by default nothing is sent and the printer's stored settings apply.
   - `_finish_job` gets a BLE branch that takes **page images**, not TSPL bytes. The page
     builders (`render_file`, `selftest_image`, `scale_test_image`) already return images, so
     `--selftest --ble` and `--scale-test --ble` come for free.
   - `--test --ble` prints a frame summary (section count, packets, bytes, first frame hex) and
     never imports bleak.
   - `--hex` writes the concatenated frames.
4. **Config (`munbyn/config.py`)** adds `transport` (`"usb"` by default), `ble_address`, and
   `ble_feed_scale`, which stays `None` until P2 measures it; until then it falls back to
   `feed_scale`.
5. **Docs**: add a BLE section to the README and a module-map line in CLAUDE.md.

### P2: hardware verification with Dwight (each step needs his OK)

0. **Capture first** (no new risk). Follow `PLANS/BLE-PROTOCOL.md` section 13: Dwight prints the
   816x1216 test PNG from the editor with `capture_snippet.js` installed and sends back
   `munbyn-ble-capture.json`. Then `decode_capture.py` must report `RESULT: MATCH`. This also
   answers unknowns 1, 2, 3 and 8: property flags, the real notification bytes, orientation and
   timing. If anything differs, fix `ble_protocol` before step 2.
1. **Terminal permission.** Grant Bluetooth to Terminal (or iTerm) once in System Settings >
   Privacy & Security > Bluetooth. CLI tools inherit the terminal's grant. Then
   `print_label.py --ble-scan`.
2. **`--ble-info`.** Record `firmwarever`, `blever`, `supportfunction`, `concentration`, `speed`
   and `printstatus` in `PLANS/BLE-PROTOCOL.md`.
3. **First print, with the cable unplugged:** `print_label.py --selftest --ble` on 4x6 gap stock.
   Check polarity, orientation and whether it stops at the gap (unknowns 3 and 4).
4. **Calibrate:** `--scale-test --ble` gives `ble_feed_scale` and the x-offset over BLE.
   `feed_scale` 0.981 was measured over USB and may differ.
5. Copies = 2, a real PDF label, and a dense dithered image (the r = 4.5 branch, and many packets).
6. Optional, only if Dwight wants it: cancel mid-job, open the hatch mid-job, set density once and
   power-cycle to see whether it persists (unknown 5).
7. Record the results. Move items from INFERRED to "VERIFIED ON HARDWARE (date)" in the protocol
   spec, and update `PLANS/PLAN.md`'s status.

### P3: Preview over BLE (CUPS socket queue + user LaunchAgent bridge)

0. **TCC spike (about 30 min, done first because it decides the packaging).** Can a LaunchAgent
   that runs `.venv/bin/python -m munbyn.ble_bridge` get the Bluetooth permission prompt, and keep
   the grant? A launchd job has no terminal to inherit a grant from. `/usr/bin/python3` resolves to
   the CLT `Python.app`, and we don't know whether it declares `NSBluetoothAlwaysUsageDescription`.
   If it can't, wrap the bridge in a minimal `Munbyn BLE Bridge.app`: an `Info.plist` with
   `NSBluetoothAlwaysUsageDescription` and `LSUIElement`, whose executable `exec`s the venv python.
   The LaunchAgent then points at that bundle's executable, with `AssociatedBundleIdentifiers`.
   The repo already builds an app bundle for the PDF Service, so that pattern exists.
1. **`munbyn/tspl_parse.py`** parses the TSPL subset `rastertotspl` emits: `SIZE`, `GAP`, `DENSITY`,
   `SPEED`, `DIRECTION`, `CLS`, `BITMAP x,y,wb,h,mode,<data>` and `PRINT m,n`.
   - It inverts the TSPL polarity (0 = black) to BLE's (1 = black).
   - It rejects anything outside `SUPPORTED_COMMANDS`.
   - It returns the page images and the copy count.
2. **`munbyn/ble_bridge.py`** is an asyncio TCP server bound to **127.0.0.1:9100 only**.
   - One job per connection: read to EOF, print over BLE, then close. CUPS' socket backend waits
     for that close, so the CUPS job finishes when the label is out.
   - Jobs run one at a time.
   - It connects per job and disconnects when idle.
   - It logs to `~/Library/Logs/munbyn-ble-bridge.log` and posts a macOS notification on failure.
     The socket backend can't report errors back to CUPS.
3. **`scripts/install-ble-bridge.sh`** installs the LaunchAgent. It writes
   `~/Library/LaunchAgents/com.m0n01d.munbyn-ble-bridge.plist` (`RunAtLoad`, `KeepAlive`,
   `ProcessType=Interactive` so App Nap doesn't stretch BLE timers, and log paths). It runs
   `launchctl bootstrap gui/$UID`, has an `--uninstall` option, and needs no sudo.
4. **The queue.** `lpadmin -p Munbyn_RW403B_BLE -v socket://127.0.0.1:9100` with the existing PPD
   and filter. Dwight runs it with sudo, as before. The `cups/` track is owned by another agent, so
   coordinate: either add a `--uri`/`--name` option to `scripts/install-cups-queue.sh`, or write a
   small separate script.
5. Dwight prints the ShedLab page from Preview through the BLE queue with the cable unplugged.

### P4 (optional, later)

- A BLE option in the web UI.
- Printer sharing from the Mac that owns the BLE link (see Risks: range).

## Files

| File | Phase | New/changed |
|---|---|---|
| `munbyn/ble_protocol.py` | P1 | new (pure) |
| `munbyn/ble_transport.py` | P1 | new (bleak, lazy import) |
| `print_label.py` | P1 | `--ble`, `--ble-address`, `--ble-scan`, `--ble-info`, `--ble-density`, `--ble-speed`; BLE branch in `_finish_job` |
| `munbyn/config.py` | P1 | `transport`, `ble_address`, `ble_feed_scale` |
| `requirements.txt` | P1 | BLE deps (below) |
| `tests/test_ble_protocol.py` | P1 | new (goldens from `tests/fixtures/ble/`) |
| `tests/test_ble_transport.py` | P1 | new (fake BleakClient) |
| `tests/test_cli.py` | P1 | `--ble --test` paths |
| `munbyn/tspl_parse.py`, `tests/test_tspl_parse.py` | P3 | new |
| `munbyn/ble_bridge.py`, `tests/test_ble_bridge.py` | P3 | new |
| `scripts/install-ble-bridge.sh` | P3 | new (LaunchAgent, maybe the .app wrapper) |
| `scripts/install-cups-queue.sh` or `scripts/install-ble-queue.sh` | P3 | coordinate with the cups track |
| README, CLAUDE.md, PLANS/PLAN.md, PLANS/BLE-PROTOCOL.md | P1-P3 | docs |

## Dependencies (checked 2026-09-27 against PyPI's JSON API)

The repo targets `/usr/bin/python3`, which is 3.9.6 (PLANS/PLAN.md), so every pin below has to
support 3.9.

| Package | Pin | Why this version |
|---|---|---|
| `bleak` | `==1.1.1` | Released 2025-09-07. It is the last release that supports Python 3.9 (`>=3.9`). 2.0 and later need 3.10 or newer; the latest is 3.0.2 (2026-05-02). |
| `pyobjc-core`, `pyobjc-framework-CoreBluetooth`, `pyobjc-framework-libdispatch` | `==11.1; sys_platform == "darwin"` | bleak asks only for `>=10.3`, which resolves to 12.x. 12.x has no cp39 wheels, and its source build fails with this Mac's clang (tried in a scratch venv). 11.1 (2025-06-14) is the last line with cp39 universal2 wheels. |
| `heatshrink2` | `==0.14.0` | Released 2026-02-09 under the ISC licence, with a cp39 macOS arm64 wheel. Its defaults are `window_sz2=11, lookahead_sz2=4`, the same values Munbyn compiled in. Byte-identical to Munbyn's `heatshrink.js`. |
| (transitive) `async-timeout`, `typing-extensions` | unpinned | bleak needs them on Python 3.9 |
| **not** `protobuf` | – | There are 5 flat messages. A hand-rolled encoder of about 60 lines is proven byte-identical by the goldens, and avoids a codegen step plus a multi-MB dependency. |

That exact set installed and imported cleanly in a scratch Python 3.9 venv on this Mac on
2026-09-27. Moving the venv to Python 3.10 or newer would allow the latest bleak (3.x) and pyobjc
12. That is a separate decision.

## Test strategy

- **Protocol (P1, no hardware):**
  - The framing vectors, and `Unpacker` with split and joined notifications.
  - Protobuf round-trips, including zigzag edge cases and the negative int32 in a resend code.
  - The job builder must match `golden_tiny16x2*.json` and `golden_selftest_4x6*.json` byte for
    byte.
  - A dense page must take the r = 4.5 branch.
  - Width guards (812 is padded to 816) and the 148-byte `perSize` path.
- **Flow:** fake notification sequences for ack, resend (rewind and continue), a second resend of
  the same section (abort), ack timeout (with an injected short timeout), printer-error bits,
  `{13,"1"}` stop, and the busy/calibrating DEVICEINFO retry.
- **Transport:** a fake `BleakClient` that records `(char, bytes, response)` and fires notify
  callbacks. It checks:
  - that each message goes to the right characteristic;
  - `response=True`;
  - that the ack waiter is registered before the last packet;
  - the disconnect path;
  - that `--test` never imports bleak.
- **Bridge (P3):** feed the TSPL fixtures from `tests/test_cups_filter.py` or `cups/` output into
  `tspl_parse`. The resulting pages must equal the images `munbyn.render` produced. Also test the
  TCP server end to end on an ephemeral port with a fake transport.
- **Capture diff (P2 step 0):** `decode_capture.py` on a real editor capture must report MATCH.
- **Hardware:** manual, one approved print at a time (P2).

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| The firmware behaves differently from the editor's model: ack format, split notifications, write type | medium | Capture before writing a single byte ourselves (P2 step 0). The decoder already handles split frames. |
| A 433-byte with-response write fails from macOS (long write/MTU) | low | Chrome on macOS uses the same CoreBluetooth write, and bleak allows up to 512 B with response. If it fails, drop to `perSize` 148, which the editor uses for BLE firmware 1.0.8. |
| The LaunchAgent can't get Bluetooth TCC | medium | P3 step 0 spike. Fall back to the `.app` wrapper. The last resort is a login-item app. |
| No page size is sent, so a job runs over the gap or feed length is off over BLE | medium | P2 steps 3-4: measure `ble_feed_scale` and keep pages at or below the label length. |
| Density and speed scales differ from TSPL, or settings persist unexpectedly | medium | Send nothing by default. Opt-in flags only. Record the DEVICEINFO values. |
| Python 3.9 pins freeze bleak at 1.1.1 | low | Pinned and checked. A venv upgrade is a separate, deliberate step. |
| Only one central can connect: the phone app or Chrome blocks the Mac, or the bridge blocks them | medium | Connect per job and disconnect when idle. Give a clear error when the connect fails. |
| BLE range (about 10 m): the Mac running the bridge must be near the printer. Sessions run on the Mac mini, but Dwight works from the MacBook. | decision | **Ask Dwight which Mac owns the BLE link.** The other Mac can print through printer sharing (needs his OK, per PLAN.md). |
| The socket backend reports every job to CUPS as a success, even when the BLE print fails | certain | The bridge log plus a macOS notification. A custom backend isn't practical, because `/usr/libexec/cups/backend` is SIP-protected. |
| Relying on a reverse-engineered protocol: a firmware update could change it | low-medium | Goldens plus the capture tool make any drift quick to diagnose. USB/TSPL stays as the fallback. |

## Effort (rough)

| Phase | Agent work | Dwight's time |
|---|---|---|
| P1 | 1.5-2 days: protocol port and tests (0.5), transport and fake-client tests (0.5-1), CLI, config and docs (0.5) | none |
| P2 | 0.5-1 day: capture analysis and fixes | about 10 min for the capture, plus a 45-60 min print session |
| P3 | about 2 days: TCC spike (0.5), TSPL parser and bridge with tests (1), LaunchAgent, queue and docs (0.5) | about 20 min: sudo queue install, the TCC prompt, one Preview print |
| **Total** | **about 4-5 agent-days** | **about 1.5-2 h over 3 sittings** |

The first useful milestone is P1 plus P2 step 0. At that point the encoder is proven against a real
editor capture, still with no writes of our own.
