"""Byte-exact tests for munbyn/ble_protocol.py against golden vectors.

The goldens in tests/fixtures/ble/ were recorded from Munbyn's web editor's own
code (run in node against a fake printer) -- see tests/fixtures/ble/README.md
and PLANS/BLE-PROTOCOL.md. Nothing here touches Bluetooth.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import random

import pytest
from PIL import Image, ImageDraw

from munbyn import ble_protocol as bp
from munbyn import tspl

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ble")


def _load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@pytest.fixture(scope="module")
def selftest_bits():
    g = _load("golden_selftest_4x6.json")
    with gzip.open(os.path.join(FIX, g["input"]["bits_file"])) as f:
        data = f.read()
    assert _sha(data) == g["input"]["byte_array_sha256"]
    return data


@pytest.fixture(scope="module")
def selftest_page(selftest_bits):
    return bp.BlePage.from_bytes(selftest_bits, 816)


# --------------------------------------------------------------------------- framing

KNOWN_FRAMES = {
    "0801": "5502405d0801",  # DEVICEINFO
    "080a": "55024055080a",  # PRINTINEND
    "0805": "550240590805",  # CANCELPRINTING
    "0802": "5502405d0802",  # SELFTEST
    "08092010": "5504006108092010",  # density 8
    "08082008": "5504007908082008",  # speed 4
    "08061a05080b120132": "5509c06d08061a05080b120132",  # ack
}


@pytest.mark.parametrize("payload,frame", sorted(KNOWN_FRAMES.items()))
def test_enpack_matches_editor_frames(payload, frame):
    assert bp.enpack(bytes.fromhex(payload)).hex() == frame
    assert bp.unpack_all(bytes.fromhex(frame)) == [bytes.fromhex(payload)]


def test_enpack_worked_example_2_header(selftest_page):
    # spec 3, worked example 2: first image packet of the 4x6 job, payload 429 bytes.
    frame = selftest_page.section_frames(1, page_field=1)[0]
    assert len(frame) == 433
    assert frame[:4].hex() == "55ad8167"


def test_enpack_rejects_payload_over_14_bits():
    with pytest.raises(bp.ProtocolError):
        bp.enpack(b"\x00" * 0x4000)


def test_unpacker_rejects_corrupted_frame():
    bad = bytearray(bytes.fromhex(KNOWN_FRAMES["0801"]))
    bad[-1] ^= 0x10
    u = bp.Unpacker()
    assert u.feed(bytes(bad)) == []
    assert u.dropped == 1


def test_unpacker_rejects_bad_header_check():
    bad = bytearray(bytes.fromhex(KNOWN_FRAMES["0801"]))
    bad[3] ^= 0x01  # header check bits live in byte 3 bits 0-1
    assert bp.unpack_all(bytes(bad)) == []


def test_unpacker_two_frames_in_one_chunk_and_split_across_chunks():
    stream = bytes.fromhex(KNOWN_FRAMES["0801"] + KNOWN_FRAMES["080a"] + KNOWN_FRAMES["08061a05080b120132"])
    assert bp.unpack_all(stream) == [b"\x08\x01", b"\x08\x0a", bytes.fromhex("08061a05080b120132")]
    # every possible split into 1-byte notifications gives the same payloads
    u = bp.Unpacker()
    out = []
    for b in stream:
        out.extend(u.feed(bytes([b])))
    assert out == bp.unpack_all(stream)
    # and garbage before a frame is skipped
    assert bp.unpack_all(b"\x00\x13\x37" + stream[:6]) == [b"\x08\x01"]


def test_unpacker_zero_length_frame_yields_empty_payload():
    frame = bp.enpack(b"")
    assert bp.unpack_all(frame + bytes.fromhex(KNOWN_FRAMES["0801"])) == [b"", b"\x08\x01"]


def test_unpacker_drops_frames_longer_than_max_pack_size():
    big = bp.enpack(b"\x01" * (bp.MAX_PACK_SIZE + 1))
    ok = bp.enpack(b"\x01" * bp.MAX_PACK_SIZE)
    assert bp.unpack_all(big) == []
    assert bp.unpack_all(ok) == [b"\x01" * bp.MAX_PACK_SIZE]


def test_enpack_unpack_roundtrip_random_payloads():
    rng = random.Random(1234)
    for n in (1, 2, 3, 255, 256, 257, 433, 1000, 1156):
        p = bytes(rng.randrange(256) for _ in range(n))
        assert bp.unpack_all(bp.enpack(p)) == [p]


# --------------------------------------------------------------------------- protobuf


@pytest.mark.parametrize(
    "n,zz", [(0, 0), (-1, 1), (1, 2), (-2, 3), (2, 4), (126684, 253368), (1528, 3056),
             (2**31 - 1, 2**32 - 2), (-(2**31), 2**32 - 1)],
)
def test_zigzag32_edge_cases(n, zz):
    assert bp.zigzag32(n) == zz
    assert bp.unzigzag32(zz) == n


def test_zigzag32_rejects_out_of_range():
    with pytest.raises(bp.ProtocolError):
        bp.zigzag32(2**31)


def test_varint_negative_int32_is_ten_bytes():
    assert bp.encode_varint(-1).hex() == "ffffffffffffffffff01"
    assert len(bp.encode_varint(-(2**31))) == 10
    assert bp.encode_varint(300).hex() == "ac02"


def test_encode_send_field_order_is_1_2_4_3_5():
    got = bp.encode_send(9, eventtag="t", sendint=8, sendstr="s", senddata=b"\x01")
    assert got.hex() == "0809" + "120174" + "2010" + "1a0173" + "2a0101"
    m = bp.decode_send(got)
    assert (m.eventtype, m.eventtag, m.sendint, m.sendstr, m.senddata) == (9, "t", 8, "s", b"\x01")


def test_sendint_is_zigzag_not_int32():
    # density=8 must travel as 0x10, not 0x08 (spec 4: sint32)
    assert bp.encode_send(bp.EventType.PRINTINCONCENTRATION, sendint=8).hex() == "08092010"
    assert bp.encode_send(1, sendint=-3).hex() == "080120" + "05"


def test_parse_fields_rejects_truncation():
    with pytest.raises(bp.ProtocolError):
        bp.parse_fields(b"\x12\x05ab")
    with pytest.raises(bp.ProtocolError):
        bp.parse_fields(b"\x08\x80")


def test_golden_print_sample_roundtrips():
    g = _load("golden_control_frames.json")["print_sample"]
    msg = bp.decode_print(bytes.fromhex(g["mpprintmsg"]))
    assert msg.compression == 1
    assert msg.encode().hex() == g["mpprintmsg"]
    send = bp.decode_send(bytes.fromhex(g["mpsendmsg"]))
    assert send.eventtype == bp.EventType.DEVICEPRINT
    assert send.senddata.hex() == g["mpprintmsg"]
    assert bp.encode_send(bp.EventType.DEVICEPRINT, senddata=msg.encode()).hex() == g["mpsendmsg"]
    assert bp.enpack(bytes.fromhex(g["mpsendmsg"])).hex() == g["frame"]


def test_first_packet_fields_match_spec_worked_example(selftest_page):
    frame = selftest_page.section_frames(1, page_field=1)[0]
    (payload,) = bp.unpack_all(frame)
    assert payload[:5].hex() == "08042aa803"  # eventtype 4, senddata 424 bytes
    p = bp.decode_print(bp.decode_send(payload).senddata)
    assert (p.page, len(p.imgdata), p.datalength, p.totalpackage, p.indexpackage, p.width,
            p.totalsection, p.compression, p.sectionlength, p.indexsection) == (
        1, 400, 126684, 4, 1, 102, 16, 1, 1528, 1)
    assert p.imgdata[:4].hex() == "000f000f"
    assert payload.endswith(bytes.fromhex("18b8bb0f200428013066381040015" "0f0176001"))
    # packet 2 of the same section has no width field (proto3 default 0)
    payload2 = bp.unpack_all(selftest_page.section_frames(1, page_field=1)[1])[0]
    assert payload2.endswith(bytes.fromhex("18b8bb0f200428023810400150f0176001"))


# --------------------------------------------------------------------------- control frames


def test_control_frames_match_golden():
    g = _load("golden_control_frames.json")["control"]
    assert bp.DEVICEINFO_FRAME.hex() == g["DEVICEINFO (x())"]["frame"]
    assert bp.SELFTEST_FRAME.hex() == g["SELFTEST (V())"]["frame"]
    assert bp.PRINTINEND_FRAME.hex() == g["PRINTINEND (W())"]["frame"]
    assert bp.CANCELPRINTING_FRAME.hex() == g["CANCELPRINTING (Q())"]["frame"]
    assert bp.density_frame(8).hex() == g["PRINTINCONCENTRATION=8"]["frame"]
    assert bp.speed_frame(4).hex() == g["PRINTINGSPEED=4"]["frame"]
    for entry in g.values():
        assert entry["characteristic"].startswith("0xABF1")


@pytest.mark.parametrize("bad", [0, 17, -1])
def test_density_range(bad):
    with pytest.raises(ValueError):
        bp.density_frame(bad)


@pytest.mark.parametrize("bad", [0, 9])
def test_speed_range(bad):
    with pytest.raises(ValueError):
        bp.speed_frame(bad)


def test_uuids():
    assert bp.SERVICE_UUID == "0000abf0-0000-1000-8000-00805f9b34fb"
    assert bp.short_uuid(bp.DATA_UUID) == "ABF4"
    assert bp.short_uuid(bp.CONTROL_UUID) == "ABF1"
    assert bp.short_uuid(bp.NOTIFY_UUID) == "ABF3"


# --------------------------------------------------------------------------- heatshrink


def test_heatshrink_params_are_11_4():
    g = _load("golden_heatshrink.json")
    assert (g["window_sz2"], g["lookahead_sz2"]) == (bp.HS_WINDOW_SZ2, bp.HS_LOOKAHEAD_SZ2) == (11, 4)


@pytest.mark.parametrize("name", sorted(_load("golden_heatshrink.json")["vectors"]))
def test_heatshrink_matches_editor_js(name):
    v = _load("golden_heatshrink.json")["vectors"][name]
    data, want = bytes.fromhex(v["in"]), bytes.fromhex(v["out"])
    assert bp.compress(data) == want
    assert bp.decompress(want) == data


# --------------------------------------------------------------------------- bitmap


def test_compose_page_pads_812_to_816_without_changing_bytes():
    img = Image.new("1", (812, 20), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 811, 3], fill=0)  # a full-width black bar, right to the last column
    d.point((811, 10), fill=0)
    page = bp.compose_page(img)
    assert page.size == (816, 20)
    assert all(page.getpixel((x, y)) for x in range(812, 816) for y in range(20))  # pad is white
    w, h, data = bp.pack_page(page)
    assert (w, h) == (816, 20)
    assert data == tspl.pack_bitmap(page, black_is_one=True)[2]
    assert data == tspl.pack_bitmap(img, black_is_one=True)[2]  # pad bits are 0 either way
    assert data[:102] == b"\xff" * 101 + b"\xf0"  # 1 = black, MSB leftmost, pad bits 0


def test_compose_page_shift_moves_and_crops_like_tspl():
    img = Image.new("1", (16, 16), 255)
    img.putpixel((8, 8), 0)
    img.putpixel((4, 4), 0)
    right = bp.compose_page(img, x_shift_mm=1.0)  # mm_to_dots(1) == 8
    assert right.size == (16, 16)
    assert right.getpixel((4, 4)) == 255 and right.getpixel((12, 4)) == 0
    assert all(right.getpixel((x, 8)) == 255 for x in range(16))  # (8,8) moved off the right edge
    left = bp.compose_page(img, x_shift_mm=-1.0)
    assert left.getpixel((0, 8)) == 0
    down = bp.compose_page(img, y_shift_mm=-1.0)
    assert down.getpixel((8, 0)) == 0
    # y shift is a feed-axis length: stretched by 1/feed_scale like everything else
    tall = Image.new("1", (8, 400), 255)
    tall.putpixel((0, 0), 0)
    moved = bp.compose_page(tall, y_shift_mm=25.0, feed_scale=0.981)
    ys = [y for y in range(400) if moved.getpixel((0, y)) == 0]
    assert ys == [round(200 / 0.981)]


def test_compose_page_rejects_too_wide():
    with pytest.raises(ValueError):
        bp.compose_page(Image.new("1", (881, 8), 1))
    assert bp.compose_page(Image.new("1", (880, 8), 1)).width == 880


def test_pack_page_requires_multiple_of_8():
    with pytest.raises(ValueError):
        bp.pack_page(Image.new("1", (812, 8), 1))
    with pytest.raises(ValueError):
        bp.BlePage.from_bytes(b"\x00" * 10, 812)


# --------------------------------------------------------------------------- sections / packets vs goldens


def test_selftest_sections_match_golden(selftest_page):
    g = _load("golden_selftest_4x6.json")["expect"]
    assert selftest_page.section_len == g["section_size"] == 8160
    assert selftest_page.rows_per_section == 80
    assert len(selftest_page.sections) == g["sections"] == 16
    assert [len(s) for s in selftest_page.sections] == g["section_compressed_lens"]
    assert [_sha(s) for s in selftest_page.sections] == g["section_sha256"]
    assert selftest_page.whole_compressed_len == 17747
    assert not selftest_page.small_sections
    assert sum(len(selftest_page.packets(s)) for s in range(1, 17)) == 50
    # every section decompresses back to its slice of the page
    s = selftest_page.section_len
    for i, sec in enumerate(selftest_page.sections):
        assert bp.decompress(sec) == selftest_page.data[i * s:(i + 1) * s]


def _golden_writes(name):
    return [e for e in _load(name)["log"] if e["dir"] == "write"]


def _same_write(ours: bp.Write, js: dict) -> bool:
    char_ok = bp.short_uuid(ours.char) == js["char"][2:]
    if "sha256" in js:
        ok = _sha(ours.frame) == js["sha256"]
    else:
        ok = True
    if "hex" in js:
        ok = ok and ours.frame.hex() == js["hex"]
    return char_ok and ok


def test_selftest_job_writes_match_editor_byte_for_byte(selftest_page):
    js = _golden_writes("golden_selftest_4x6.json")
    ours = bp.planned_writes([selftest_page], 1)
    assert len(ours) == len(js) == 52
    assert [w.label.split()[0] for w in ours[:2]] == ["DEVICEINFO", "DEVICEPRINT"]
    assert ours[-1].label == "PRINTINEND"
    for i, (o, j) in enumerate(zip(ours, js)):
        assert _same_write(o, j), "write {} differs: {}".format(i, o.label)
    assert max(len(w.frame) for w in ours) == 433


@pytest.mark.parametrize("name,copies", [("golden_tiny16x2.json", 1), ("golden_tiny16x2_copies3.json", 3)])
def test_tiny_jobs_match_editor(name, copies):
    g = _load(name)
    pi = g["pageInputs"][0]
    page = bp.BlePage.from_bytes(bytes.fromhex(pi["byteArrayHex"]), pi["widthDots"])
    assert page.section_len == pi["sectionSize"]
    assert [s.hex() for s in page.sections] == pi["sections"]
    ours = bp.planned_writes([page], copies)
    js = _golden_writes(name)
    assert [w.frame.hex() for w in ours] == [j["hex"] for j in js]
    assert [bp.short_uuid(w.char) for w in ours] == [j["char"][2:] for j in js]


def test_dense_page_takes_small_section_branch():
    rng = random.Random(7)
    data = bytes(rng.randrange(256) for _ in range(102 * 1242))  # incompressible "dither"
    page = bp.BlePage.from_bytes(data, 816)
    assert page.small_sections
    assert page.section_len == 4608 - 4608 % 816 == 4080  # 40 rows
    assert len(page.sections) == 32
    assert len(page.data) - 31 * 4080 == 2 * 102  # the last section is 2 rows


def test_per_size_148_packets(selftest_page):
    frames = selftest_page.section_frames(1, page_field=1, per_size=148)
    assert len(frames) == -(-1528 // 148) == 11
    assert max(len(f) for f in frames) <= 181
    p = bp.decode_print(bp.decode_send(bp.unpack_all(frames[-1])[0]).senddata)
    assert (p.indexpackage, p.totalpackage, len(p.imgdata)) == (11, 11, 1528 - 10 * 148)


def test_section_size_uses_width_in_dots():
    # the spec's pitfall: 812 instead of 816 would give 8120-byte sections (79.6 rows)
    with pytest.raises(ValueError):
        bp.section_size(b"\x00" * 1000, 812, 10)
    assert bp.section_size(b"", 816, 0) == 8160


def test_ack_delay_matches_editor():
    assert bp.ack_delay_s(1528) == 0.005
    assert bp.ack_delay_s(553) == 0.002
    assert bp.ack_delay_s(5) == 0.0
    assert bp.ack_delay_s(10**6) == 0.005
    # JS Math.round rounds .5 up: 0.5 ms -> 1
    assert bp.ack_delay_s(163.84) == 0.001


# --------------------------------------------------------------------------- copies / plan


def _tiny_page():
    return bp.BlePage.from_bytes(bytes.fromhex("f00faa55"), 16)


def test_plan_single_page_sends_once_with_page_equal_copies():
    assert bp.plan_sends([_tiny_page()], 3) == ([(0, 3)], 3)


def test_plan_multi_page_sends_each_page_per_copy():
    pages = [_tiny_page(), _tiny_page()]
    assert bp.plan_sends(pages, 2) == ([(0, 1), (1, 1), (0, 1), (1, 1)], 4)


def test_plan_huge_page_with_send_while_printing_goes_per_copy():
    page = _tiny_page()
    page.sections = [b"x" * bp.SEND_PER_COPY_MIN_COMPRESSED]
    assert bp.plan_sends([page], 2, support_send_when_printing=True) == ([(0, 1), (0, 1)], 2)
    assert bp.plan_sends([page], 2, support_send_when_printing=False) == ([(0, 2)], 2)


def test_plan_rejects_bad_input():
    with pytest.raises(ValueError):
        bp.plan_sends([], 1)
    with pytest.raises(ValueError):
        bp.plan_sends([_tiny_page()], 0)


def test_planned_writes_optional_density_speed_come_after_deviceinfo():
    w = bp.planned_writes([_tiny_page()], 1, density=8, speed=4)
    assert [x.frame.hex() for x in w[:3]] == ["5502405d0801", "5504006108092010", "5504007908082008"]
    assert all(x.char == bp.CONTROL_UUID for x in w[:3])
    assert w[3].char == bp.DATA_UUID


# --------------------------------------------------------------------------- notifications


def test_decode_golden_notifications():
    g = _load("golden_tiny16x2.json")
    notes = [bytes.fromhex(e["hex"]) for e in g["log"] if e["dir"] == "notify"]
    dec = bp.NotificationDecoder()
    evs = [e for n in notes for e in dec.feed(n)]
    assert [e.kind for e in evs] == ["deviceinfo", "ack", "printed"]
    info = evs[0].info
    assert (info.firmwarever, info.blever, info.printstatus, info.concentration, info.speed, info.elec) == (
        "SIM", "1.0.9", "0", 8, 4, 100)
    assert info.ready and info.can_resend and not info.support_send_when_printing
    assert info.per_size == 400


def test_decode_resend_golden_frame():
    frame = bytes.fromhex("5512404c080410f4031a0b0882808080f8ffffffff01")
    (ev,) = bp.NotificationDecoder().feed(frame)
    assert (ev.kind, ev.section, ev.code) == ("resend", 2, 500)
    assert bp.resend_frame(2) == frame


def test_ack_and_printed_frames_match_spec():
    assert bp.ACK_FRAME.hex() == "5509c06d08061a05080b120132"
    assert bp.PRINTED_FRAME.hex() == "5509c06d08061a05080a120130"


def test_decode_printer_error_stop_print_error_cancel():
    k = lambda f: bp.NotificationDecoder().feed(f)[0]  # noqa: E731
    e = k(bp.report_frame(10, "10"))  # bits 1 and 3: out of paper + hatch open
    assert e.kind == "printer_error" and e.flags == ("out_of_paper", "hatch_open") and e.status == 10
    assert k(bp.report_frame(13, "1")).kind == "stopped"
    assert k(bp.report_frame(11, " 2 ")).kind == "ack"  # loose == like the editor
    assert k(bp.report_frame(11, "3")).kind == "other"
    pe = k(bp.respond_frame(bp.EventType.DEVICEPRINT, code=500, responddata=bp.CodeMsg(7, "x").encode()))
    assert pe.kind == "print_error" and pe.code == 500
    assert k(bp.respond_frame(bp.EventType.DEVICEPRINT, code=200)).kind == "ok"
    assert k(bp.respond_frame(bp.EventType.CANCELPRINTING, code=200)).kind == "cancel_ok"
    ce = k(bp.respond_frame(bp.EventType.CANCELPRINTING, code=1, error=bp.CodeMsg(3, "busy")))
    assert ce.kind == "cancel_error" and "busy" in ce.detail
    assert k(bp.respond_frame(bp.EventType.SELFTEST, code=200)).kind == "ok"
    assert k(bp.enpack(b"\x08")).kind == "malformed"
    # resend code exactly 0x80000000 (section 0) is not a resend in the editor (a > i)
    zero = bp.respond_frame(bp.EventType.DEVICEPRINT, code=500, responddata=bp.CodeMsg(-(2**31)).encode())
    assert k(zero).kind == "print_error"


def test_deviceinfo_roundtrip_and_status_rules():
    info = bp.DeviceInfo(mac="AA", sn="SN1", firmwarever="2.0", printstatus="257", blever="1.0.8",
                         supportfunction=0x82, concentration=12, speed=4, elec=90)
    got = bp.decode_deviceinfo(info.encode())
    assert got == info
    assert got.printstatus_flags == ["busy", "calibrating_paper"]
    assert got.only_busy_or_calibrating and not got.ready
    assert got.per_size == 148
    assert got.can_resend and got.support_send_when_printing
    assert not bp.DeviceInfo(printstatus="3").only_busy_or_calibrating
    assert bp.DeviceInfo(printstatus="").ready
    assert not bp.DeviceInfo(printstatus="junk").ready
    assert bp.DeviceInfo(printstatus="junk").printstatus_flags == ["unreadable_status"]


def test_describe_frame_and_job(selftest_page):
    assert bp.describe_frame(bp.DEVICEINFO_FRAME) == "DEVICEINFO"
    assert bp.describe_frame(bp.density_frame(8)) == "PRINTINCONCENTRATION sendint=8"
    writes = bp.planned_writes([selftest_page], 1)
    text = bp.describe_job([selftest_page], 1, writes)
    assert "816x1242" in text
    assert "16 section(s)" in text and "50 packet(s)" in text
    assert "52 write(s): 50 DEVICEPRINT" in text
    assert "55ad8167" in text
    short = bp.describe_job([selftest_page], 1, writes, max_write_lines=10)
    assert "writes not shown" in short
