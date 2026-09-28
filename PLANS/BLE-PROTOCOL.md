# RW403B Bluetooth print protocol -- spec (from the editor's JS, not yet from hardware)

**Status (2026-09-27): no part of this has been tested on the printer.** This spec comes from
reading the public JS of Munbyn's web editor (`editor.munbyn.com/create`), which prints to the
RW403B over Web Bluetooth. Where it says "(ran)", we went further and ran the editor's own code in
node against a fake GATT layer and a fake printer. Our Python encoder
(`tests/fixtures/ble/reference_job.py`) matches the editor's output byte for byte on 4 jobs. One of
those jobs is this repo's own 4x6 self-test, 812x1242 at `feed_scale` 0.981.

Every item has one of two tags:

- **VERIFIED-IN-JS** means we read it in the editor's code, and the tag names the function or module.
  "(ran)" means we also ran that code.
- **INFERRED** means the item is our reasoning, or comes from the Web Bluetooth spec or CoreBluetooth
  docs. It is **not** in Munbyn's code.

Neither tag means the printer behaves that way. See [Unknowns](#12-unknowns-to-settle-on-hardware)
and [Capture plan](#13-capture-plan).

**Sources.** The bundles were curled 2026-09-27 and kept in the session scratchpad. They are **not**
committed, because they are Munbyn's code.

| File | What it holds |
|---|---|
| `static/js/app.e3efa380.js` | Main app. Module 5626 holds the device tables. Module 4476 is the frame codec. Module 53689 holds the protobuf classes. Module 58861 holds helpers (`wb` split, `oZ` delay, `Bp` bits, `ad` = `>>>0`). Module 90530 loads the driver and connects (`u()`, `S()`). Module 37539 is the `blueDevice` store. Module 43103 holds the print button `Tn()` and the page processor `pn()`. |
| `static/js/165.1bd064fb.js` | Module 61165, `./DeviceRW402B.js`. **This is the driver that runs for the RW403B** (see section 0). |
| `/e41d95b3303d45b3.js` | Print Web Worker: `applyThresholdAndPack`, `getSectionSize`, `splitPackets`. |
| `/heatshrink.js` | Emscripten build of heatshrink (LZSS). |
| `static/js/chunk-vendors.9c2e6645.js` | Module 65339, the google-protobuf runtime. |

## 0. Corrections to PLANS/BLE.md's first notes

- **The RW403B driver is `DeviceRW402B.js` (chunk 165), not `DeviceRW403B.js` (chunk 144).**
  VERIFIED-IN-JS, loader `u()` in module 90530:
  `let{selfFourInDevices:a,...}=r.A;if(a.includes(e)&&(e="RW402B"),...` with
  `selfFourInDevices=["RW402B","RW403B","RW405B","ST425"]` in module 5626. Nothing calls chunk 144's
  `bluePrint`. The print button calls chunk 165's `bluePrintForTest`.
- **`perSize` is 400.** In module 5626, `defaultWritrParamsObj.RW403B = {perSize:400, perTime:3}`.
  The driver swaps in 148 only when DEVICEINFO reports BLE firmware `"1.0.8"`. VERIFIED-IN-JS,
  61165 `ae()`:
  `"RW403B"==r&&"1.0.8"==M.value?.bleVer?148:v[r]?.perSize||148`. The "150" seen earlier belongs to
  the hidden `openTest` mode, which is off by default, and to the unused chunk 144.
- **`code 11 / info "2"` acknowledges a section. It does not mean "busy".** The resend request names a
  **section**, not a packet (section 9).
- **Every protobuf field number has been recovered** (section 4). **Heatshrink is applied** to every
  section of real print data (section 8).

## 1. GATT layout and connection

| Item | Value | Tag |
|---|---|---|
| Scan filter | `namePrefix: "RW403B"`. The editor sets `deviceType` to the matched prefix. | VERIFIED-IN-JS 5626 `blueDeviceNames`; 90530 connect (`deviceType:e?.namePrefix`) |
| Service | `0xABF0` (44016) | VERIFIED-IN-JS 5626 `characteristicList.RW403B=[44016,44019,44020,44017]` |
| Notify | `0xABF3` (44019): index [1], `notifyApi` | VERIFIED-IN-JS 90530 `S()` |
| Data write | `0xABF4` (44020): index [2], `writeApi`. Carries **DEVICEPRINT packets only**. | VERIFIED-IN-JS 90530 `S()`; 61165 `se()` `d.writeApi` ... `i.writeValue(d)` |
| Control write | `0xABF1` (44017): index [3], `writeApi2`. Carries DEVICEINFO, PRINTINEND, SELFTEST, CANCELPRINTING, density, speed. | VERIFIED-IN-JS 61165 `ve()`: `await(d.writeApi2?.writeValue(a))` |
| 128-bit UUIDs | `0000abf0-0000-1000-8000-00805f9b34fb` (and the same pattern for abf1, abf3, abf4) | INFERRED (Web Bluetooth expands 16-bit aliases on the Bluetooth base UUID) |
| Write type | Both characteristics use plain `writeValue()`, and every call is awaited. The spec's "response optional" rule makes Chrome use **with-response** when the characteristic has the Write property. | VERIFIED-IN-JS (awaited `writeValue`); the resulting write type is INFERRED. The property flags are unknown and the capture will show them. |
| Write size | Up to 433 bytes per frame: 400 image bytes + 29 bytes of protobuf + 4 bytes of framing. | VERIFIED-IN-JS (ran) |

Connection sequence. VERIFIED-IN-JS, 90530 connect code and `S()`, 37539 store:

1. `navigator.bluetooth.requestDevice({filters: [{namePrefix: ...}, ...], optionalServices: [...]})`.
2. Sleep 500 ms. Then `connectDevice(dev, 2)`: up to 2 tries of `gatt.connect()`, each with a 4 s
   timeout. Sleep 1000 ms.
3. `getPrimaryService(0xABF0)`, with a 6 s timeout. Then fetch `0xABF3`, `0xABF4` and `0xABF1`.
4. RW403B is in `notifyDevices`, so the editor calls `startNotifications()` on `0xABF3` and routes
   `characteristicvaluechanged` to `dealwithNotify` (61165 `V()`).
5. Sleep 200 ms. RW403B is in `hasDetailDevices`, so the editor sends DEVICEINFO and waits up to
   4 s for the reply. If none comes, the connect fails.
6. When the printer disconnects, the editor **does not reconnect on its own**. The code says so
   (Chinese comment: "no auto-reconnect").

## 2. The messages, and where each one goes

Every write is **one frame** (section 3) whose payload is a serialized `MPSendMsg`. Every
notification carries frames whose payload is an `MPRespondMsg`. VERIFIED-IN-JS, 61165 `se()`, `ve()`
and `V()`.

| Host -> printer | Char | MPSendMsg | Frame (ran) |
|---|---|---|---|
| DEVICEINFO | ABF1 | `eventtype=1` | `55 02 40 5d 08 01` |
| SELFTEST | ABF1 | `eventtype=2` | `55 02 40 5d 08 02` |
| CANCELPRINTING | ABF1 | `eventtype=5` | `55 02 40 59 08 05` |
| PRINTINEND | ABF1 | `eventtype=10` | `55 02 40 55 08 0a` |
| density = 8 | ABF1 | `eventtype=9` (PRINTINCONCENTRATION), `sendint=8` (zigzag) | `55 04 00 61 08 09 20 10` |
| speed = 4 | ABF1 | `eventtype=8` (PRINTINGSPEED), `sendint=4` | `55 04 00 79 08 08 20 08` |
| image packet | **ABF4** | `eventtype=4` (DEVICEPRINT), `senddata` = serialized `MPPrintMsg` | section 7 |

| Printer -> host (ABF3) | MPRespondMsg | Meaning (VERIFIED-IN-JS 61165 `V()`) |
|---|---|---|
| DEVICEINFO reply | `eventtype=1`, `responddata` = MPDeviceInfoMsg | status, settings, versions (section 5) |
| Section ack | `eventtype=6`, `responddata` = MPCodeMsg{`code=11`, `info="2"`} | Send the next section. Frame (ran): `55 09 c0 6d 08 06 1a 05 08 0b 12 01 32` |
| Page printed | `eventtype=6`, MPCodeMsg{`code=10`, `info="0"`} | One page or copy came out. Frame (ran): `55 09 c0 6d 08 06 1a 05 08 0a 12 01 30` |
| Printer error | `eventtype=6`, MPCodeMsg{`code=10`, `info=<bitmask != 0>`} | Abort. Bits as in `printstatus` (section 5). |
| Print stopped | `eventtype=6`, MPCodeMsg{`code=13`, `info="1"`} | Abort, unless the hatch-open bit is set |
| Resend section | `eventtype=4`, `code != 200`, `responddata` = MPCodeMsg{`code = 0x80000000 \| section`} | Go back to that 1-based section (section 9). Example (ran): `55 12 40 4c 08 04 10 f4 03 1a 0b 08 82 80 80 80 f8 ff ff ff ff 01` |
| Print error | `eventtype=4`, `code != 200`, MPCodeMsg.code without the high bit | Abort after 1.5 s (`isIndexError`) |
| OK replies | `eventtype` 2, 5 or 11 with `code=200` | SELFTEST, CANCELPRINTING or PAPERTYPESET accepted |

`info` is a **string**. The editor compares it loosely (`2==i`), so our decoder should parse it as
an integer.

## 3. Framing (VERIFIED-IN-JS, module 4476 `enpack()`/`_decode()`; ran on 7 payloads)

```
byte 0   0x55 (STX)
byte 1   len & 0xFF
byte 2   (len >> 8) & 0x3F  |  (x & 0x0C) << 4           x  = 0x55 ^ byte1 ^ (len >> 8)
byte 3   (x >> 4) & 0x03    |  (X & 0xFC)                X  = x ^ XOR(every payload byte)
byte 4+  payload (len bytes)
```

- The length field is 14 bits. The editor's receiver drops frames longer than **1156** bytes
  (`_MAX_PACK_SIZE`), and drops frames whose check bits don't match.
- The header check is 4 bits (bits 2-5 of `x`). The whole-frame check is 6 bits (bits 2-7 of `X`).
  Bits 0 and 1 are never sent.
- The receiver is a byte-at-a-time state machine that **keeps its state across calls**. One
  notification may hold part of a frame or several frames. VERIFIED-IN-JS for the parser; whether
  the printer ever splits a frame is unknown.
- Quirk: in the JS, a zero-length frame leaves the parser stuck. Ours emits `b""`.

**Worked example 1: DEVICEINFO.** The payload is `08 01`, so len = 2.
`x = 0x55 ^ 0x02 ^ 0x00 = 0x57`.
`byte2 = 0x00 | (0x57 & 0x0C) << 4 = 0x40`.
`byte3 = (0x57 >> 4) & 3 = 0x01`.
`X = 0x57 ^ 0x08 ^ 0x01 = 0x5E`, so `byte3 |= 0x5E & 0xFC`, giving `0x5D`.
Frame: `55 02 40 5d 08 01`.

**Worked example 2: first image packet of the 4x6 job** (section 7). The payload is 429 bytes = `0x1AD`.
`x = 0x55 ^ 0xAD ^ 0x01 = 0xF9`.
`byte2 = 0x01 | (0xF9 & 0x0C) << 4 = 0x81`.
`byte3 = (0xF9 >> 4) & 3 = 0x03`.
XOR over the payload gives `X = 0x66`, so `byte3 = 0x03 | 0x64 = 0x67`.
Header: `55 ad 81 67`.

Our code: `tests/fixtures/ble/reference_framing.py`. Running it checks both examples.

## 4. Protobuf schema (VERIFIED-IN-JS, module 53689; checked 3 ways)

This is proto3 with no package. There are no repeated, oneof or map fields; every constructor is
`Message.initialize(this,e,0,-1,null,null)`.

**Four fields are `sint32` (zigzag)**, shown in the writers as `writeSint32(3,..)` and
`writeSint32(10,..)`. Encoding them as `int32` sends wrong values.

**The editor writes `MPSendMsg` fields in the order 1, 2, 4, 3, 5.** Its serializer is literally
`writeEnum(1) writeString(2) writeSint32(4) writeString(3) writeBytes(5)`. Byte-identical output
needs that order. Since `sendstr` is never set, the order never shows in practice.

How the schema was checked:

1. Each field was set on its own through the editor's own setter and serialized by the editor. All
   80 fields plus the enum matched.
2. `protoc` 31.1 decoded the editor's bytes.
3. python-protobuf output built from this schema was byte-identical to the editor's.

```proto
syntax = "proto3";

enum EventType {
  DEFAULT = 0; DEVICEINFO = 1; SELFTEST = 2; CLOSETIME = 3; DEVICEPRINT = 4; CANCELPRINTING = 5;
  DEVICEREPORT = 6; FIRMWAREUPGRADE = 7; PRINTINGSPEED = 8; PRINTINCONCENTRATION = 9;
  PRINTINEND = 10; PAPERTYPESET = 11; FACTORYCOMMAND = 12; SNSET = 13; PAPERINFOSET = 14;
}

message MPSendMsg {            // host -> printer, every write
  EventType eventtype = 1;
  string eventtag = 2;         // never set for RW403B
  sint32 sendint = 4;          // ZIGZAG; density / speed value
  string sendstr = 3;          // never set for RW403B
  bytes senddata = 5;          // DEVICEPRINT: a *serialized* MPPrintMsg (bytes, not an embedded message)
}

message MPPrintMsg {           // inside MPSendMsg.senddata
  int32 page = 1;              // copies (single-page job), else 1
  bytes imgdata = 2;           // <= perSize bytes of a heatshrink-compressed section
  sint32 datalength = 3;       // ZIGZAG; uncompressed packed-bitmap bytes of the whole page
  int32 totalpackage = 4;      // packets in the current section
  int32 indexpackage = 5;      // 1-based packet index within the section
  int32 width = 6;             // bytes per row (= dots/8 = mm); ONLY on packet 1 of each section, else 0
  int32 totalsection = 7;      // sections in the page
  int32 compression = 8;       // always 1
  int32 lastpage = 9;          // never set
  sint32 sectionlength = 10;   // ZIGZAG; COMPRESSED length of the current section
  int32 lowmemory = 11;        // never set
  int32 indexsection = 12;     // 1-based (set because RW403B is in hasResendDevices)
  int32 printtype = 13;        // 0 unless two-colour mode (never for RW403B)
  int32 colortype = 14;        // 0 unless two-colour mode
}

message MPRespondMsg {         // printer -> host, every notification frame
  EventType eventtype = 1;
  int32 code = 2;              // 200 = OK for SELFTEST / CANCELPRINTING / PAPERTYPESET / DEVICEPRINT
  bytes responddata = 3;       // serialized MPDeviceInfoMsg (eventtype 1) or MPCodeMsg (4, 6)
  MPCodeMsg error = 4;
}

message MPCodeMsg { int32 code = 1; string info = 2; }   // info is a decimal number as a string

message MPDeviceInfoMsg {
  string mac = 1; string sn = 2; string firmwarever = 3; int32 paperstatus = 4; int32 elec = 5;
  int32 concentration = 6; int32 speed = 7; int32 papersize = 8;
  string printstatus = 9;      // decimal string -> 9-bit status mask (section 5)
  int32 papertype = 10; int32 closetime = 11; int32 protocol = 12;
  string blever = 13;          // BLE firmware; "1.0.8" -> perSize 148
  string mfr = 14; int32 eeid = 15;
  int32 supportfunction = 16;  // bit 1 canResend, bit 7 supportSendWhenPrinting
}

// Present in the bundle, unused by the RW403B print path:
message MPPaperTypeMsg { int32 height = 1; int32 offset = 2; }
message MPFirmwareMsg { int32 crccode = 1; sint32 datalength = 2; bytes bindata = 3;
                        int32 totalpackage = 4; int32 indexpackage = 5; int32 firmwaretype = 6; }
message MPTestMsg { int32 sensor1length = 1; bytes sensor1data = 2; int32 sensor2length = 3;
                    bytes sensor2data = 4; int32 sensor3length = 5; bytes sensor3data = 6;
                    int32 voltlength = 7; bytes voltdata = 8; int32 tempurelength = 9;
                    bytes tempuredata = 10; int32 capdata = 11; }
message MPSetParaMsg { int32 agingcount = 1; int32 aginginterval = 2; int32 set_sn = 3; string sn_char = 4; }
message MPAgingAckMsg { int32 agingcount = 1; int32 agingdistance = 2; }
message MPPaperInfoMsg { int32 heat_num_perline = 1; int32 heattime_ratio = 2; int32 avg_method = 3;
  int32 avg_num = 4; int32 heat_max_pot = 5; int32 heat_min_pot = 6; int32 heat_max_time = 7;
  int32 heat_min_time = 8; int32 size_y = 9; int32 size_w = 10; int32 paper_type = 11;
  int32 print_offset_point = 12; int32 support_back = 13; int32 info_type = 14; }
```

The field names are the lowercased names jspb generates. The original `.proto` may have used
camelCase, which has no effect on the wire. The same schema is also in
`tests/fixtures/ble/munbyn_ble.proto`.

## 5. Handshake

- **At connect time**, the editor sends DEVICEINFO and waits up to 4 s for the reply. VERIFIED-IN-JS,
  90530 `S()`.
- **Before every print**, the print button `Tn()` (module 43103) does the following. VERIFIED-IN-JS.
  1. It refuses a label taller than 200 mm unless `supportfunction` bit 7 is set.
  2. It sends DEVICEINFO and waits for the reply, 4 s timeout.
  3. It requires **every `printstatus` bit to be 0**. Otherwise it throws ("设备信息异常", "device
     info abnormal").
  4. Then it calls `bluePrintForTest`.
- **`printstatus`** is `Number(string)` split into 9 bits, LSB first. VERIFIED-IN-JS, 58861 `Bp()` and
  the 61165 `printTips` order.

  | Bit | Meaning |
  |---|---|
  | 0 | busy |
  | 1 | out of paper |
  | 2 | print cache full |
  | 3 | hatch open |
  | 4 | paper jam |
  | 5 | head overheated |
  | 6 | low battery |
  | 7 | motor overheated |
  | 8 | calibrating paper |

  If bit 0 or bit 8 (busy, calibrating) is set and bits 1-7 are all clear, the editor waits 4 s and
  asks again (61165 `V()`).
- **`supportfunction`**: bit 1 means `canResend` and bit 7 means `supportSendWhenPrinting`.
  VERIFIED-IN-JS, 61165 `V()`. The RW403B is in `hasResendDevices`, so it uses the resend path
  whatever bit 1 says.
- **No paper, size, gap, density or speed message is part of a job.** VERIFIED-IN-JS: `checkPaperType`
  runs only for `hasPaperDevices=["FM226"]`, and `setPrinter` runs only when the user changes a
  dropdown (section 11).

## 6. Bitmap (VERIFIED-IN-JS, worker `applyThresholdAndPack`, app `pn()`)

- **Canvas.** The canvas is `width_mm*8 x height_mm*8` dots (`printRatio` = 8). The 4x6 preset is
  102 x 152 mm = **816 x 1216**. The editor fills it white, then fits the image and centres it.
  Anything wider than `maxW = 110*8 = 880` dots is centre-cropped.
- **Threshold.** `gray = .299R + .587G + .114B`. A pixel is black when `gray <= 162`, or `<= 160` for
  PDFs. Dither mode uses 170 or 180.
- **Packing.** Rows are sent top canvas row first. Within a byte the **MSB is the leftmost pixel**,
  and **1 = BLACK**. Each row takes `ceil(w/8)` bytes, and padding bits are 0 (white). The RW403B is
  not in `hasSetWhiteDevices`, so there is no inversion. No rotation is applied beyond the user's
  output direction, which defaults to 0°.
- **This equals `munbyn.tspl.pack_bitmap(img, black_is_one=True)`.** VERIFIED-IN-JS (ran): the
  worker's `applyThresholdAndPack`, run on the RGBA of this repo's 812x1242 self-test padded to 816,
  gave exactly the bytes `pack_bitmap` gives. Padding 812 to 816 with white changes no bytes,
  because the pad bits are 0 in both.
- **The width must be a multiple of 8 dots.** The editor always meets this, since its canvas is
  mm*8. `width` is sent as `dots/8`. A fractional value makes the protobuf runtime throw, which we
  reproduced. **Pad 812 to 816 before sending.**
- **Which label edge prints first, and whether the image comes out mirrored**, cannot be read from
  the code. Unknown.

## 7. Sections and packets, with the 4x6 worked example

VERIFIED-IN-JS (ran): worker `getSectionSize` and `splitPackets(..., true)`; 61165 `le()`, `se()` and
58861 `wb()`.

```
page      = packed bitmap, datalength = stride * rows            stride = width_dots / 8
r         = 8, or 4.5 if len(heatshrink(page)) > 30720
S         = 1024*r - (1024*r % width_dots)      # bytes; a multiple of width_dots = 8 whole rows
sections  = [heatshrink(page[i:i+S]) for i in range(0, datalength, S)]   # each compressed on its own
packets   = compressed_section[j:j+perSize]      # perSize = 400 (148 if blever == "1.0.8")
```

`S` uses the width in **dots** but slices **bytes**, so every section is a whole multiple of 8 rows.
**Pitfall:** passing 812 instead of 816 gives `S = 8120`, which is 79.6 rows. The sections then no
longer line up with rows.

**Worked example.** The input is this repo's 4x6 self-test at `feed_scale=0.981`:
`selftest_image(JobSettings(size=4x6, feed_scale=0.981))` is **812 x 1242**, padded to **816 x 1242**.
This is also the raster the CUPS queue produces at 203x207dpi. Every number below came from running
the **editor's own driver and worker** and was reproduced exactly by `reference_job.py`, in
`tests/fixtures/ble/golden_selftest_4x6.json`.

- stride = 102 B, so datalength = 102 x 1242 = **126 684** B.
- heatshrink of the whole page = 17 747 B. That is at most 30 720, so r = 8 and
  S = 8192 - 8192 % 816 = **8160 B = 80 rows**.
- 126 684 / 8160 gives **16 sections**: 15 of 80 rows and a last one of 42 rows (4284 B).
- Compressed sections, in bytes, with the packet counts at 400 B:
  1528 (4), 1980 (5), 1191 (3), 1034 x 4 (3 each), 1038 (3), 1034 x 5 (3 each), 1197 (3),
  1153 (3), 553 (2). That makes **50 packets** and 17 946 B compressed.
- Writes: DEVICEINFO, then 50 DEVICEPRINT, then PRINTINEND, for **52 writes** in total. There are
  16 ack notifications and one "printed".
- A busy page whose whole-page compression is over 30 720 B takes the r = 4.5 branch instead:
  S = 4608 - 4608 % 816 = 4080 B = 40 rows, so the same page would be 32 sections, the last one
  2 rows.

First packet, section 1, packet 1/4. The header is `55 ad 81 67` (section 3). Then:

```
08 04                      MPSendMsg.eventtype = 4 (DEVICEPRINT)
2a a8 03                   MPSendMsg.senddata, 424 bytes:
  08 01                      page = 1
  12 90 03 <400 bytes>       imgdata (starts 00 0f 00 0f ...)
  18 b8 bb 0f                datalength = zigzag(126684) = 253368
  20 04                      totalpackage = 4
  28 01                      indexpackage = 1
  30 66                      width = 102            <- packet 1 of each section only
  38 10                      totalsection = 16
  40 01                      compression = 1
  50 f0 17                   sectionlength = zigzag(1528) = 3056
  60 01                      indexsection = 1
```

Packet 2 of the same section ends in `18b8bb0f 2004 2802 3810 4001 50f017 6001`. `width` is
missing, because 0 is the proto3 default and so is not written. The editor reuses one
`MPPrintMsg` object for the whole page. Between packets of a section it changes only `imgdata`,
`indexpackage` and `width`. Between sections it also changes `totalpackage`, `sectionlength` and
`indexsection`. VERIFIED-IN-JS, 61165 `le()` and `se()`.

**Copies.** For a one-page job the page is sent once with `page = copies`. VERIFIED-IN-JS, 61165
`ie()`. There is one exception: when `supportSendWhenPrinting` is set **and** the compressed page is
at least 163 840 B, the page is sent once per copy with `page = 1`.

## 8. Compression (VERIFIED-IN-JS + ran)

- **Heatshrink with window_sz2 = 11 and lookahead_sz2 = 4.** There is no header and the window
  starts zero-filled. Evidence:
  - The compiled `heatshrink.js` exports `_print_config()`. When we called it, it printed
    `HEATSHRINK_STATIC_WINDOW_BITS=11` and `HEATSHRINK_STATIC_LOOKAHEAD_BITS=4`.
  - Of all (W, L) pairs, only (11, 4) decodes every vector the JS produced.
  - `heatshrink2==0.14.0` (PyPI, ISC licence, cp39 arm64 wheel), called as
    `compress(data, window_sz2=11, lookahead_sz2=4)`, is byte-identical to the JS on 12 vectors and
    on all 16 sections of the worked example.
- Each **section** is compressed on its own. It is never the whole page, and never a packet.
- `compression = 1` is always set. We don't know whether the firmware accepts `compression = 0`
  with raw sections. Don't depend on it.
- Lookahead 4 caps the ratio at 8:1, so a blank 4x6 page is still about 15.5 KB.

## 9. Flow control, acks and resends (VERIFIED-IN-JS, 61165 `le()`, `se()`, `ue()`, `V()`)

```
for each section s = 1..N:
    Q = min(5, round(len(section)/1024/160*1000/2))   # ms, 58861 oZ()
    for each packet p:  write(ABF4, frame); await it; sleep(perTime = 3 ms)
    wait for ack  (10 s timeout -> abort "PrintTimeout")
       ack      = {ev 6, MPCodeMsg{11, "2"}}   -> sleep Q; s += 1
       resend   = {ev 4, code != 200, MPCodeMsg{code: 0x80000000 | k}}
                    -> if section k already resent once: abort ("段 k 重发失败", "section k resend failed")
                    -> else sleep Q; continue from section k (k, k+1, ... are all sent again)
       ev 4, code != 200, no high bit -> abort after 1.5 s
sleep 2 ms (per page / per copy loop)
write(ABF1, PRINTINEND)
count {ev 6, MPCodeMsg{10, "0"}} until it equals copies x pages -> finished
```

- `code` is an int32 on the wire. `0x80000000 | k` travels as a negative 10-byte varint, and the
  editor takes `(code >>> 0) - 2**31`.
- An ack is only acted on once the packet loop for the section has finished (`while(U)`). With
  `canResend` true, a second ack within `Q` ms is ignored.
  **INFERRED design rule for us:** register the ack waiter *before* writing a section's last packet.
- There is **no per-packet ack**. The only per-packet pacing is the awaited GATT write plus 3 ms.
- **Cancel.** Send CANCELPRINTING on ABF1. The reply `{ev 5, code 200}` means OK, and the editor
  then stops its loops.

## 10. End of job

- After the last section is acked, the editor writes **PRINTINEND** on ABF1 and does not wait for
  any reply. VERIFIED-IN-JS, 61165 `te()` then `j()`.
- The job is finished when the number of `{ev 6, {10, "0"}}` reports equals `copies x pages`.
  VERIFIED-IN-JS, 61165 `V()`. Any `{10, info != "0"}` or `{13, "1"}` while printing aborts the job.
- The editor counts these reports from the start of the job. **INFERRED**: they may arrive before
  PRINTINEND. Accept them at any time.

## 11. Defaults: density, speed, paper

- **Density** (`PRINTINCONCENTRATION`, `sendint` 1..16) and **speed** (`PRINTINGSPEED`, `sendint`
  1..8). VERIFIED-IN-JS, module 5626 `concentrationList`/`speedList`, and 61165 `ge()`. The editor
  sends one only when the user changes the dropdown (app `N()` calls `setPrinter`), not as part of a
  job, and does not wait for a reply.
- The dropdowns show the printer's own `concentration`/`speed` from DEVICEINFO. Their placeholders
  before connecting are 5 and 4. **INFERRED**: the printer stores these settings.
- The vendor PPD (PLANS/PLAN.md) lists darkness 1-16 (default 12) and speed 1-8 (default 4), the same
  ranges. **INFERRED**: these are the same settings. How they map onto TSPL `DENSITY 0..15` is
  unknown.
- **Paper**: nothing is sent. The printer presumably prints `datalength/width` rows and uses its own
  gap sensor to feed to the next label. That is **INFERRED** and is the main hardware question
  (section 12).
- **Feed length.** `feed_scale = 0.981` was measured over USB/TSPL. Munbyn's phone app, which uses
  Bluetooth, measured 97.23 mm for 100 mm. That app renders differently, so over BLE the scale is
  **unknown**. Measure it with `--scale-test` over BLE.

## 12. Unknowns to settle on hardware

1. The **property flags** of ABF1 and ABF4 (Write versus WriteWithoutResponse) and the negotiated
   MTU. Does a 433-byte with-response write go through from macOS (ATT long write)? *(The capture
   shows the flags. Chrome on macOS uses the same CoreBluetooth write that bleak uses.)*
2. The **real notification bytes**: does the ack carry `code = 200`? Are frames split across
   notifications? Are there unsolicited `{10, ...}` status reports? What are `blever` and
   `supportfunction` on this unit?
3. **Which edge prints first**, and whether the image is **mirrored**.
4. **Label length and gap.** With no size sent, does a 1242-row page stop at the gap? Is `feed_scale`
   0.981 right over BLE?
5. How **density and speed** map to the editor's scale, the current stored values, and whether a
   PRINTINCONCENTRATION write persists across power cycles.
6. Whether **PRINTINEND** is required, and whether a job works without the print-time DEVICEINFO.
7. **USB and BLE at the same time.** Does the printer take BLE jobs while the USB cable is plugged
   in? Does a connected phone or Chrome session block the Mac?
8. Timing: the write round trip, the time to print a 4x6 page, and the ack latency.
9. Whether the firmware accepts `MPSendMsg` fields in number order (1, 2, 3, 4, 5). This doesn't
   matter here, because `sendstr` is never set.

## 13. Capture plan

Goal: log the editor's real writes and notifications to one real RW403B print, then diff them
against our encoder. This carries no new risk, because it is the path that already prints for
Dwight.

1. **Make the test image.** In the repo, run:
   `.venv/bin/python tests/fixtures/ble/decode_capture.py --make-png ~/Desktop/munbyn_816x1216.png`
   It writes an exact 816x1216 black-and-white self-test, so the editor's 4x6 canvas draws it at
   scale 1. It is asymmetric (text and corner marks), which shows orientation and mirroring.
2. **Install the logger.** In Chrome, open `https://editor.munbyn.com/create`. Pick 4x6 in (102 x
   152 mm), import the PNG, and **before connecting** open DevTools (Cmd+Opt+J). Paste the contents
   of `tests/fixtures/ble/capture_snippet.js` and press Enter.
   - The snippet wraps `BluetoothRemoteGATTCharacteristic.prototype.writeValue`,
     `writeValueWithResponse` and `writeValueWithoutResponse`. It logs time, characteristic UUID,
     method, hex and the time the write promise resolved.
   - It also wraps `startNotifications` to add a `characteristicvaluechanged` logger, and records
     each characteristic's `properties`.
   - If the editor was already connected, disconnect and reconnect.
3. **Print.** Connect to the RW403B; the LED turns blue. Print **one** label and leave density and
   speed alone. Wait for it to come out.
4. **Save.** In the console, run `__munbynCap.save()`. That downloads `munbyn-ble-capture.json`.
   Note the label's orientation and measure the printed bars.
5. **Diff.** Run
   `python3 tests/fixtures/ble/decode_capture.py munbyn-ble-capture.json --png editor_bitmap.png`,
   using a venv that has `heatshrink2`. It:
   - lists the characteristics and their property flags;
   - decodes every frame;
   - rebuilds the bitmap the editor actually sent by decompressing the sections, and saves it as a
     PNG;
   - re-encodes that bitmap with our encoder and compares every DEVICEPRINT frame byte for byte;
   - prints write gaps and round-trip times.

   Because the diff starts from the editor's own sent bitmap, it doesn't depend on copying the
   editor's canvas scaling. The tool was checked on a synthetic capture: it gives MATCH on the
   editor's own output and pinpoints a single altered field.
6. Optional repeats: 2 copies, a PNG that forces the r = 4.5 branch (a dense dither), and changing
   the density dropdown once, which logs the PRINTINCONCENTRATION write.

`chrome://bluetooth-internals` can also show the service and characteristic properties. It connects
to the printer, so only Dwight should use it.
