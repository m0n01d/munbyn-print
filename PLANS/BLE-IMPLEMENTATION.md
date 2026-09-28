# Bluetooth transport: implementation plan

Goal: print to the RW403B with the USB cable unplugged. First from the CLI (P1, P2), then from
Preview's normal Print dialog (P3). The protocol is specified in `PLANS/BLE-PROTOCOL.md`. Golden
vectors and a reference encoder are in `tests/fixtures/ble/`.

**Status (2026-09-28): P1 is hardware-verified; P3 is built (via the MunbynBLE bridge app) and
waiting for its first install.**

- **P1 verified on hardware 2026-09-28** (conductor + Dwight, Mac mini, printer RW403B-D0C6,
  CoreBluetooth address `5903F05B-AA01-DD0A-C87B-BAC1438801DF`, saved as `ble_address`). From macOS
  Terminal, `--status --ble` and `--selftest --ble` worked: upright, not mirrored, correct polarity,
  one label, stopped at the gap; 16 sections acked, 0 resends, "printed" report received, 4.1 s.
  DEVICEINFO: firmware 1.1.16, BLE firmware 1.2.1, density 8, speed 4, supportfunction 0 (so no
  "send while printing": labels over 200 mm are refused). GATT: 0xABF4 write-without-response
  (max 509 B), 0xABF1 write (512 B), 0xABF3 notify.
- **The TCC finding that shaped P3.** Run from the Claude desktop app (responsible process
  `com.anthropic.claude-code`, no `NSBluetoothAlwaysUsageDescription`), Python is *killed* by TCC
  (SIGABRT, "attempted to access privacy-sensitive data without a usage description"). The same
  applies to anything launched by cupsd, a web server started from the Claude app, or a bare launchd
  job. So the P3 spike's fallback is the plan: Bluetooth lives only in `MunbynBLE.app`.
- **P3 as built** (see "P3 as built" below): `munbyn/tspl_parse.py`, `munbyn/ble_bridge.py`,
  `munbyn/ble_bridge_client.py`, `macos/munbyn-ble-launcher.c`, `scripts/install-ble-bridge.sh`,
  `scripts/install-cups-queue.sh --ble`; the CLI's `--ble` and the web UI's Bluetooth transport go
  through the bridge (`--ble-direct` = the old in-process path, Terminal only). Unit-tested with a
  fake transport; the installer is checked with a `--dest` dry run. **Not yet done on hardware:**
  the first real install, the TCC prompt naming MunbynBLE, a Preview print through the Bluetooth
  queue, and `ble_feed_scale` (still the USB 0.981).

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

#### P1 as built (2026-09-27)

Done, with these differences from the plan above:

- **Flags.** `--status --ble` is the DEVICEINFO query (no separate `--ble-info`). Added `--usb`
  (overrides a saved `"transport": "ble"`), `--ble-scan-timeout`, `--ble-feed-scale`,
  `--ble-packet-size N` (the documented fallback for a too-small MTU is 148), `--ble-write
  auto|response|no-response`, and `--ble-printer-selftest` (the printer's own SELFTEST page).
  `--list --ble` points at `--ble-scan`, the only command that lists devices.
- **`ble_feed_scale` defaults to 0.981** (the USB value), not `None`. It is a separate config key and
  is unverified over BLE. With `--ble`, a plain `--feed-scale` also works for one run.
- **Write type.** `auto` by default: with-response when the characteristic has the Write property,
  else without-response, which is what Chrome's `writeValue()` does. A frame bigger than the connection
  allows (512 B with response, `max_write_without_response_size` without) is refused *before* the first
  DEVICEPRINT, with a message naming `--ble-packet-size 148`. Each write has a 5 s timeout, which the
  editor doesn't have.
- **Scan** is by name prefix only, with no `0xABF0` service filter. Whether the printer advertises the
  service UUID is unknown, and the editor filters by name too. With `--ble-address`, bleak looks the
  device up by address first (`--ble-scan-timeout`), then connects (2 x 4 s).
- **One DEVICEINFO per job.** The editor sends one at connect time and one at print time. We send one
  after connecting (it does both jobs), so the writes are exactly the golden 52 for the 4x6 self-test.
- **Duplicate replies.** Acks or resend requests that arrive during the post-section pause
  (`min(5, len/1024/160*1000/2)` ms) are dropped, as in the editor. The golden-replay tests pin this.
- **Timeouts.** After PRINTINEND the transport waits 30 s + 15 s per expected page for the "printed"
  reports. Past that it errors, saying every section was acked. That limit is ours; the editor has none.
- **Ctrl-C.** A loop signal handler, not KeyboardInterrupt. The first press during a job sends
  CANCELPRINTING, waits up to 2 s for `{ev 5, 200}`, and disconnects (exit 130). A second press, or any
  press outside a job, cancels at once.
- **Density/speed** are opt-in only (`--ble-density`, `--ble-speed`). TSPL `--density`/`--speed` are
  never mapped, and a note says so. `--status --ble --ble-density N` sets the value and then shows it.
- **Not done:** the web UI (still USB-only; P4). `--black-is-one` is ignored over BLE, which always
  sends 1 = black; `--invert` is the escape hatch if hardware shows otherwise.
- **Guesses to check in P2:** the 200 ms pause after a density/speed write, the 1 s post-connect and
  200 ms post-notify sleeps (copied from the editor, which has Web Bluetooth's connect sequence), and
  the "printed" timeout above.

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

#### P3 as built (2026-09-28)

- **Packaging.** `scripts/install-ble-bridge.sh` (no sudo; `--uninstall`; idempotent) builds
  `~/Applications/MunbynBLE.app`: Info.plist with `CFBundleIdentifier com.m0n01d.munbyn-ble-bridge`,
  `CFBundleName MunbynBLE`, `LSUIElement`, `LSMinimumSystemVersion 11.0` and
  `NSBluetoothAlwaysUsageDescription`, ad-hoc signed (`codesign --force --deep -s -`). Its
  executable is a compiled C launcher (`macos/munbyn-ble-launcher.c`) that `posix_spawn`s
  `python -m munbyn.ble_bridge` as a **child** and waits, forwarding SIGTERM/SIGINT/SIGHUP. Not
  `exec`: the process would become Python and lose the bundle identity TCC checks. Not a bash
  launcher either: the process would be `/bin/bash`, whose identity is Apple's, not the bundle's.
- **Runtime copy.** By default the installer copies `.venv` and `munbyn/` to
  `~/Library/Application Support/MunbynBLE` and the launcher runs that. The repo is under
  `~/Documents`, which TCC also protects, so running in place would add a second prompt ("access
  files in your Documents folder") and break the bridge if it's missed. `--in-place` runs from the
  repo anyway (and adds `NSDocumentsFolderUsageDescription`). Re-run the installer after changing
  `munbyn/`. The app bundle is only rebuilt when the launcher, its baked paths or the Info.plist
  change (a build stamp), because a rebuild changes the ad-hoc signature and macOS asks again.
- **LaunchAgent** `~/Library/LaunchAgents/com.m0n01d.munbyn-ble-bridge.plist`: `RunAtLoad`,
  `KeepAlive`, `ThrottleInterval 10`, `ProcessType Interactive`, `LimitLoadToSessionType Aqua`,
  `AssociatedBundleIdentifiers` = the bundle id (so System Settings > Login Items shows MunbynBLE),
  stdout/stderr to `~/Library/Logs/munbyn-ble-bridge.launchd.log`; `launchctl bootstrap gui/$UID`
  (bootout on uninstall/update). **ProgramArguments = the app's own executable** (default,
  `--launch-mode direct`), not `open -W -g -a`: launchd then owns the real process (KeepAlive,
  `bootout`, `kickstart -k` all act on it and SIGTERM reaches the bridge), and TCC attributes a
  process to the signed bundle whose `Contents/MacOS` executable it runs, which is what the Claude
  app case shows (TCC checked the responsible app's Info.plist). With `open`, the app would be a
  separate LaunchServices job, `bootout` would only kill `open`, and stdout would be lost.
  `--launch-mode open` is kept as the fallback if the prompt doesn't name MunbynBLE (then uninstall
  also `pkill`s the app). One consequence: in open mode `kickstart -k` only restarts the `open` job,
  which just re-waits on the still-running app, so it does **not** restart the bridge -- use
  `pkill -TERM -f .../MunbynBLE.app/Contents/MacOS/MunbynBLE` instead (KeepAlive relaunches it via
  `open`). The installer prints the right restart command for whichever mode it just installed.
- **Permission prompt at install time.** On start, if the authorization is "not determined", the
  bridge creates a `CBCentralManager` (no scan, no connect), so "MunbynBLE would like to use
  Bluetooth" appears right away instead of during the first print. It logs the state and posts a
  notification if it's denied. Status replies include `bluetooth_authorization`.
- **Bridge** (`munbyn/ble_bridge.py`): asyncio server on 127.0.0.1:`ble_bridge_port` (9100). Per
  connection: first line `MUNBYN-STATUS` [`DEVICEINFO`] -> one JSON line; otherwise a job read to EOF
  (64 MB cap, 60 s idle timeout), queued, printed one at a time, answered with one JSON line
  (`ok`, `job`, `pages`, `labels`, `printed`, `attempts`, `seconds` or `error`) and closed after
  printing, so CUPS's socket backend waits for the label. Address re-read from the config per job.
  Failures: parse error -> log + notification; Bluetooth error -> one retry on a fresh connection
  unless it can't help (printer status, frame too large, label too tall, cancelled) or a label may
  already be out (`BlePrinter.end_sent`/`printed`, new), then log + notification. SIGTERM cancels a
  running job (CANCELPRINTING) and exits.
- **Parser** (`munbyn/tspl_parse.py`): exactly `SUPPORTED_COMMANDS`, `BITMAP` mode 1 only, CRLF or
  LF, case-insensitive; the printer's buffer semantics (BITMAP ORs in, PRINT m,n = m*n copies of the
  buffer, CLS clears); page = origin to the furthest bitmap edge, clipped at 880 dots; a `PRINT`
  with nothing drawn feeds a blank page of `SIZE`. Clear bit = black in, mode "1" images out
  (`pack_page` makes Bluetooth's 1 = black). Rows are not rescaled.
- **CLI/web.** `--ble` (or `"transport": "ble"`) probes the bridge's status (so a job is never sent
  to a stranger on the port), builds the TSPL job with `ble_feed_scale` and the x/y shift baked into
  the bitmap (`ble_bridge_client.build_bridge_job`: byte-for-byte the pages `--ble-direct` sends --
  tested), sends it and prints the bridge's verdict. `--status --ble` asks the bridge for DEVICEINFO.
  Not running -> "run scripts/install-ble-bridge.sh". `--ble-density/--ble-speed` need
  `--ble-direct`; `--ble-packet-size/--ble-write/--ble-scan-timeout` are noted as ignored; a
  differing `--ble-address` is noted (the bridge uses the config's). `--ble-scan` and
  `--ble-printer-selftest` stay in-process (Terminal). The web UI has Printer > Transport (USB /
  Bluetooth via bridge); its Feed scale field follows the transport.
- **CUPS.** `sudo scripts/install-cups-queue.sh --ble [--feed-scale F] [--ble-port N]` adds
  `Munbyn_RW403B_BLE` ("Munbyn RW403B (Bluetooth)"), `socket://127.0.0.1:9100`, same filter and
  generated PPD; `--uninstall --ble` removes only that queue, and the USB `--uninstall` keeps the
  shared filter/PPD while the Bluetooth queue exists.
- **Tests.** `tests/test_tspl_parse.py`, `tests/test_ble_bridge.py` (fake transport, real server on an
  ephemeral port, real `cups/rastertotspl` output), `tests/test_cli_bridge.py` (CLI + web against the
  bridge), `tests/test_install_ble_bridge.py` (`--dest` dry run with launchctl/pkill shadowed; the
  launcher is run with a stub module to check the child relationship and SIGTERM forwarding).
  `tests/conftest.py` refuses every bridge connection unless a test opts in with its own fake.

### P3 hardware checklist (Dwight / conductor)

1. `scripts/install-ble-bridge.sh` -> the prompt must say **"MunbynBLE** would like to use
   Bluetooth" -> Allow. The log should then say `Bluetooth permission: allowed` after a
   `launchctl kickstart -k gui/$UID/com.m0n01d.munbyn-ble-bridge`.
2. `print_label.py --status --ble` (from the Claude app is fine: only the bridge touches Bluetooth).
3. `print_label.py --selftest --ble` (bridge path) -> same label as the Terminal run.
4. `sudo scripts/install-cups-queue.sh --ble`, then the ShedLab page from Preview with the cable
   unplugged.
5. `--scale-test --ble` to measure `ble_feed_scale`.

### P4 (optional, later)

- ~~A BLE option in the web UI.~~ Done in P3 (via the bridge).
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

## Hardware verification (2026-09-28, conductor + Dwight, Mac mini, RW403B-D0C6)

- P1 direct (from Terminal.app): `--status --ble` read DEVICEINFO (fw 1.1.16, BLE fw 1.2.1, density 8, speed 4); `--selftest --ble` printed upright, not mirrored, correct polarity, one label, 16 sections acked, 0 resends, 4.1 s.
- Feed scale over BLE: ShedLab page-8 100 mm bar printed at ~100 mm with `ble_feed_scale` 0.981 (same as USB). Verified.
- From the Claude desktop app, in-process BLE is killed by TCC (responsible process com.anthropic.claude-code has no NSBluetoothAlwaysUsageDescription). Always go through the bridge from there.
- P3 bridge: `scripts/install-ble-bridge.sh` installed MunbynBLE.app + LaunchAgent; the prompt read "MunbynBLE would like to use Bluetooth"; after Allow, `--status --ble` and `--selftest --ble` via the bridge worked (job #2, 7.6 s).
- Config on the Mac mini now defaults to `transport: ble`.
- Preview over Bluetooth VERIFIED: Dwight ran `sudo ./scripts/install-cups-queue.sh --ble` (queue `Munbyn_RW403B_BLE`, socket://127.0.0.1:9100) and printed from Preview; bridge job #3 printed, printer reported 1 of 1, 7.1 s.
