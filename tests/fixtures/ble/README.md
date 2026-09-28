# BLE protocol fixtures

These are the golden vectors and reference code for the RW403B Bluetooth protocol, specified in
`PLANS/BLE-PROTOCOL.md`. **Everything here is our own code or data.** None of it is Munbyn's JS,
which stays out of the repo. Nothing here has been run against a printer.

Since P1 (2026-09-27) the goldens are the tests of `munbyn/ble_protocol.py` and
`munbyn/ble_transport.py`: `tests/test_ble_protocol.py` checks the encoder byte for byte against
them, and `tests/test_ble_transport.py` replays the editor's logs through the real transport with a
fake BleakClient. `reference_*.py` are the research versions the module was ported from.

| File | What it is |
|---|---|
| `munbyn_ble.proto` | Our reconstruction of the protobuf schema. It compiles with `protoc` 31.1. |
| `reference_framing.py` | Frame codec (`enpack`, `Unpacker`). Run it to self-check against known frames. |
| `reference_job.py` | Hand-rolled proto3 encoder and decoder, the section/packet job builder, and the notification flow model. `--check` replays the golden files. |
| `golden_tiny16x2.json`, `golden_tiny16x2_copies3.json` | A 16x2 job: the full log of the **editor's own driver** run in node against a fake printer. |
| `golden_selftest_4x6.json`, `golden_selftest_4x6_resend.json` | The same logs for this repo's 4x6 self-test at `feed_scale=0.981` (812x1242, padded to 816), with no resend and with a section 2 resend. To keep the file small, a DEVICEPRINT write is stored as its SHA-256 plus length; full hex is kept for the first and last packets of sections 1 and 16. |
| `selftest_4x6_816x1242.bits.gz` | The packed input bitmap for those two files: 1 = black, MSB is the leftmost pixel, 102 B per row. It equals `munbyn.tspl.pack_bitmap(selftest_image(4x6, feed_scale=0.981), black_is_one=True)`. |
| `golden_control_frames.json` | Control frames (DEVICEINFO, SELFTEST, PRINTINEND, CANCELPRINTING, density 8, speed 4) and one DEVICEPRINT sample, produced by the editor's own protobuf and framing modules. |
| `golden_heatshrink.json` | Inputs and outputs of the editor's compiled `heatshrink.js` (window 11, lookahead 4). |
| `capture_snippet.js` | A DevTools snippet that logs the editor's real GATT writes and notifications (see the capture plan in the spec). |
| `decode_capture.py` | Decodes a capture, rebuilds the bitmap the editor sent, and diffs every frame against `reference_job.py`. `--make-png` writes the 816x1216 test image. |

The goldens were made by loading the editor's webpack modules into node: the driver
(`./DeviceRW402B.js`), the print worker, the protobuf classes, the framing and `heatshrink.js`. A
fake `writeValue` and a fake printer supplied the replies. The harness lives in the session
scratchpad and is not committed, because it loads Munbyn's code.

```sh
python3 -m venv /tmp/blevenv && /tmp/blevenv/bin/pip install heatshrink2==0.14.0 Pillow
python3 tests/fixtures/ble/reference_framing.py                      # ALL OK
/tmp/blevenv/bin/python tests/fixtures/ble/reference_job.py --check tests/fixtures/ble/golden_*.json
                                                                      # ALL GOLDEN CHECKS PASS
```

pytest does not collect the scripts here, because none is named `test_*.py`.
