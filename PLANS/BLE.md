# BLE transport -- research notes (UNVERIFIED)

**Status: research only, nothing in this repo implements it, nothing here
has been run against real hardware.** Everything below comes from reading
Munbyn's own web editor (`editor.munbyn.com/create`) JS bundles, curled
2026-09-27 and kept at
`/private/tmp/claude-501/-Users-dwightdoane-Documents-web/a62b4fdc-ce7e-4b73-8486-f65756e537cd/scratchpad/ble-findings.md`
(a subagent's evidence, not verified) and its source bundles under
`scratchpad/munbyn/` in that same scratchpad (`create.html`,
`static_js_app.e3efa380.js`, `chunk_144_RW403B.js` i.e.
`./DeviceRW403B.js`, `chunk_920_devices.js`, `heatshrink.js`). This is a
**separate future track** from the USB/TSPL path this repo implements today
-- USB covers printing whenever the printer is plugged in; BLE would only
matter for a cable-free setup.

## Why this exists

`editor.munbyn.com` prints fine from Chrome, and it was briefly considered
as evidence that some TSPL/USB path works -- it doesn't. Per the JS bundle,
the editor:

- Uses **BLE only** -- no `navigator.usb`, no `navigator.serial` anywhere in
  the bundle.
- Sends **no TSPL at all** -- zero hits for the strings `"SIZE"`, `"GAP"`,
  `"CLS"`, `"BITMAP"`, `"TSPL"` in the whole bundle.
- Sends a proprietary **protobuf** protocol instead (below).

So the editor's own success prints nothing about whether this repo's
TSPL-over-USB approach is right; it's simply a different code path. (This
repo's TSPL/USB approach was verified independently and directly against
the real printer -- see `PLANS/PLAN.md`.)

## BLE transport shape

- **Scan**: name prefix `RW403B`.
- **GATT service** `0xABF0` (44016), with:
  - notify characteristic `0xABF3` (44019)
  - bulk image-data write characteristic `0xABF4` (44020)
  - control write characteristic `0xABF1` (44017)
  - (same service/characteristic set is shared across RW402B, RW405B,
    ST425, FM226 -- this is a device-family protocol, not RW403B-specific)
- **Writes**: `writeValue` (with response, i.e. acknowledged writes), sent
  as this device's chunk parameters: `{perSize: 400, perTime: 3}` -- 400
  byte packets, 3 ms between writes, plus an extra per-section delay of
  `min(5, round(len/1024/160*1000/2))` ms.
- **Retry protocol** via notifications: `MPCodeMsg{code, info}` --
  `code: 11, info: 2` means busy/back off; `code: 4` carries a packet index
  the host must resend.

## Payload framing and encoding

- Payload is **protobuf** (the `google-protobuf` JS runtime), messages
  `MPSendMsg` / `MPPrintMsg` built via `.serializeBinary()`.
- Framed by an `enpack()` function (bundle module `4476`): byte 0 is `0x55`
  (STX), bytes 1-2 are a little-endian length with XOR-checksum bits folded
  in, byte 3 is more checksum bits, then the payload.
- `MPPrintMsg` fields (names only -- **field numbers were not recovered**,
  see below): `page`, `compression` (a flag, value `1` seen), `datalength`,
  `totalsection`, `sectionlength`, `width`, `imgdata`, `indexpackage`,
  `totalpackage`.
- **Pixel packing** (bundle module `58861`): MSB-first (`n |= 1 << (7-o)`),
  and **1 bit = BLACK by default** -- a `setWhite` flag inverts this for
  some models, but RW403B is *not* in the bundle's `hasSetWhiteDevices`
  list, so RW403B keeps the default (1=black). This is the opposite
  convention from this repo's default TSPL `BITMAP` polarity
  (`bitmap_black_is_one=False`, i.e. 0=black) -- expected, since it's a
  wholly different wire protocol, not evidence about TSPL's own convention
  (see `PLANS/PLAN.md`'s note on why the editor's BLE polarity was
  deliberately *not* used as evidence for the TSPL `BITMAP` interpreter).
- **heatshrink** (an LZSS variant) is loaded and used at least to *size*
  sections (over 30720 compressed bytes bumps a section multiplier from 8
  to 4.5). **Whether real print image data is actually heatshrink-compressed
  is unconfirmed** -- the likely call site is in the template/customize
  chunks (bundle modules 624/904), which were not fetched.
- **Control message enum** (separate small protobuf messages, not part of
  the image payload): `DEFAULT=0, DEVICEINFO=1, SELFTEST=2, CLOSETIME=3,
  DEVICEPRINT=4, CANCELPRINTING=5, DEVICEREPORT=6, FIRMWAREUPGRADE=7,
  PRINTINGSPEED=8, PRINTINCONCENTRATION=9, PRINTINEND=10, PAPERTYPESET=11,
  FACTORYCOMMAND=12, SNSET=13, PAPERINFOSET=14`. Density/speed are sent as
  separate control messages (`setSendint(value)`), not fields on the print
  job itself.
- **Status**: the notify characteristic delivers bytes that `unpack()`
  hands to `MPRespondMsg.deserializeBinary`; the interesting payload is
  `MPDeviceInfoMsg`, with fields `printstatus, paperstatus, elec,
  concentration, speed, papersize, firmwarever, mac, sn, papertype,
  protocol`.
- `maxW` for RW403B in the bundle's device table: **110 mm**.

## What's still missing (would block an implementation)

- The actual image -> packet builder (chunking `imgdata` into
  `indexpackage`/`totalpackage` pieces against `sectionlength`).
- Confirmation of whether/when heatshrink compression is applied to real
  print data.
- Numeric density/speed defaults and valid ranges over this control channel.
- The protobuf **field numbers** for `MPPrintMsg`/`MPSendMsg`/`MPCodeMsg`/
  `MPDeviceInfoMsg` -- the bundle only gave field *names*; the numbers live
  in the generated protobuf message classes inside `app.e3efa380.js`, which
  were not fully decompiled.

## Implication for a future implementation

A BLE transport (likely `bleak` on macOS, matching this project's existing
`pyusb` style) would need all of the above reverse-engineered into a
concrete protobuf schema plus the `enpack()` checksum/framing and the
packet/section/retry state machine, before it could be tried against real
hardware. That is real, nontrivial protocol work, not a small addition --
treat it as its own project phase, and verify every assumption above
against a real device before trusting it (the same way the USB/TSPL path in
`PLANS/PLAN.md` was verified, not assumed).

**Do not use any of the above for the USB/TSPL path in this repo.** It is
recorded here purely so the research is not lost, per this project's own
rule of writing decisions down in `PLANS/` before a session ends.
