# BLE transport: index

**Status (2026-09-27): the protocol is fully specified from Munbyn's editor JS, and an
implementation plan is written. Nothing has run against real hardware, and no transport code
exists.**

- **Protocol spec:** `PLANS/BLE-PROTOCOL.md`. It covers:
  - the GATT layout, the framing and checksum, and the full `.proto` schema;
  - the handshake, bitmap packing, and sections/packets with a worked 4x6 (812x1242) example;
  - heatshrink compression, flow control and resend, and end of job;
  - defaults, the unknowns to settle on hardware, and a Chrome capture plan.

  Each item is tagged VERIFIED-IN-JS or INFERRED.
- **Implementation plan:** `PLANS/BLE-IMPLEMENTATION.md`.
  - P1 is a CLI `--ble` transport built on bleak.
  - P2 is hardware verification with Dwight, starting with a capture of the editor's own traffic.
  - P3 is Preview printing through a `socket://127.0.0.1:9100` CUPS queue and a user LaunchAgent
    bridge.

  The plan also lists files, pinned dependencies, tests, risks and an effort estimate (about 4-5
  agent-days plus 1.5-2 h of Dwight's time).
- **Fixtures and reference code (ours, not Munbyn's):** `tests/fixtures/ble/` (see its README).
  It holds golden vectors made by running the editor's own driver on this repo's self-test, a
  Python reference encoder that reproduces them byte for byte, the DevTools capture snippet, and
  the capture decoder.

## Why this exists

`editor.munbyn.com` prints to the RW403B from Chrome, but only over **Bluetooth**, using a
proprietary **protobuf** protocol. The bundle contains no `navigator.usb` or `navigator.serial`,
and no TSPL strings. The editor working therefore says nothing about this repo's TSPL-over-USB path,
which was verified on its own (`PLANS/PLAN.md`). In the other direction, **nothing in the BLE
protocol is evidence about TSPL.** For example, BLE's 1 = black bit polarity is not a TSPL fact.
Do not use either to reason about the other.

## Corrections to the first research notes (2026-09-27, earlier the same day)

The earlier version of this file was wrong or incomplete on these points. The details, with
sources, are in the spec's section 0.

- The RW403B runs the editor's `DeviceRW402B.js` driver (chunk 165), **not** `DeviceRW403B.js`
  (chunk 144).
- `perSize` is **400**, or 148 when the BLE firmware reports `"1.0.8"`. The "150" came from a test
  mode.
- `code 11 / info "2"` is the **section ack**, not "busy". A resend request names a **section**
  (`0x80000000 | section`), not a packet.
- All protobuf field numbers are recovered. Four fields are `sint32` (zigzag).
- Heatshrink (window 11, lookahead 4) **is** applied to every section of real print data.

Munbyn's JS bundles are kept only in the session scratchpad
(`/private/tmp/claude-501/-Users-dwightdoane-Documents-web/a62b4fdc-ce7e-4b73-8486-f65756e537cd/scratchpad/munbyn/`).
They are never committed.
