"""Reference encoder and flow model for an RW403B Bluetooth print job (research artifact, NOT wired
into munbyn/, never run against a real printer).

Our own Python version of what editor.munbyn.com sends over Web Bluetooth. It matches, byte for byte,
the writes the editor's own JS makes when driven by a fake printer. Those writes are recorded in
golden_*.json. The spec, with sources, is PLANS/BLE-PROTOCOL.md. The P1 implementation in
PLANS/BLE-IMPLEMENTATION.md starts from this file.

    # needs heatshrink2==0.14.0 (the repo .venv does not have it yet)
    python3 tests/fixtures/ble/reference_job.py --check tests/fixtures/ble/golden_*.json

Pipeline:
  1-bit pack   row-major, top row first, MSB = leftmost pixel, 1 = BLACK, stride ceil(w/8), pad bits 0
               (== munbyn.tspl.pack_bitmap(img, black_is_one=True))
  sections     r = 8, or 4.5 if heatshrink(whole page) > 30720 bytes;
               S = 1024*r - (1024*r % width_dots)   bytes of the UNcompressed page, width_dots % 8 == 0
  compression  heatshrink each S-byte slice on its own, window_sz2=11, lookahead_sz2=4, no header
  packets      400-byte slices of each COMPRESSED section (148 if DEVICEINFO.blever == "1.0.8")
  message      MPSendMsg{eventtype=DEVICEPRINT(4), senddata=serialized MPPrintMsg{...}} (proto3)
  framing      reference_framing.enpack()
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field

import heatshrink2  # pip install heatshrink2==0.14.0

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reference_framing import Unpacker, enpack  # noqa: E402

EVENT = dict(DEFAULT=0, DEVICEINFO=1, SELFTEST=2, CLOSETIME=3, DEVICEPRINT=4, CANCELPRINTING=5,
             DEVICEREPORT=6, FIRMWAREUPGRADE=7, PRINTINGSPEED=8, PRINTINCONCENTRATION=9, PRINTINEND=10,
             PAPERTYPESET=11, FACTORYCOMMAND=12, SNSET=13, PAPERINFOSET=14)
EVENT_NAME = {v: k for k, v in EVENT.items()}
PER_SIZE = 400                # defaultWritrParamsObj.RW403B.perSize
PER_SIZE_BLE_1_0_8 = 148      # used instead when DEVICEINFO.blever == "1.0.8"
PER_TIME_MS = 3               # sleep after every DEVICEPRINT packet write
PER_PAGE_MS = 2               # sleep after each page/copy loop
SECTION_ACK_TIMEOUT_S = 10.0  # wait for the section ack, then abort ("PrintTimeout")
DEVICEINFO_TIMEOUT_S = 4.0
HS_WINDOW, HS_LOOKAHEAD = 11, 4
DOTS_PER_MM = 8               # printRatio

SERVICE = 0xABF0
CHAR_NOTIFY = 0xABF3          # notifyApi
CHAR_DATA = 0xABF4            # writeApi:  DEVICEPRINT packets
CHAR_CTRL = 0xABF1            # writeApi2: DEVICEINFO, PRINTINEND, SELFTEST, CANCELPRINTING, density, speed


def uuid16(n: int) -> str:
    return f"0000{n:04x}-0000-1000-8000-00805f9b34fb"


# ----------------------------------------------------------------------------- protobuf (proto3, by hand)
def _varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1  # int32/enum negatives are sign-extended to 10 bytes
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _zigzag32(n: int) -> int:
    return ((n << 1) ^ (n >> 31)) & 0xFFFFFFFF


def _f_varint(fn, v):
    return b"" if not v else _varint(fn << 3) + _varint(v)


def _f_sint(fn, v):
    return b"" if not v else _varint(fn << 3) + _varint(_zigzag32(v))


def _f_bytes(fn, v):
    return b"" if not v else _varint(fn << 3 | 2) + _varint(len(v)) + bytes(v)


def mp_send(eventtype: int, *, eventtag: str = "", sendint: int = 0, sendstr: str = "",
            senddata: bytes = b"") -> bytes:
    # The editor writes fields in the order 1, 2, 4, 3, 5 (sendint is declared before sendstr).
    return (_f_varint(1, eventtype) + _f_bytes(2, eventtag.encode()) + _f_sint(4, sendint)
            + _f_bytes(3, sendstr.encode()) + _f_bytes(5, senddata))


def mp_print(*, page=0, imgdata=b"", datalength=0, totalpackage=0, indexpackage=0, width=0,
             totalsection=0, compression=0, lastpage=0, sectionlength=0, lowmemory=0, indexsection=0,
             printtype=0, colortype=0) -> bytes:
    return (_f_varint(1, page) + _f_bytes(2, imgdata) + _f_sint(3, datalength)
            + _f_varint(4, totalpackage) + _f_varint(5, indexpackage) + _f_varint(6, width)
            + _f_varint(7, totalsection) + _f_varint(8, compression) + _f_varint(9, lastpage)
            + _f_sint(10, sectionlength) + _f_varint(11, lowmemory) + _f_varint(12, indexsection)
            + _f_varint(13, printtype) + _f_varint(14, colortype))


def parse_pb(buf: bytes) -> dict:
    """Generic proto3 wire parser -> {field: [raw values]} (varints unsigned, LEN as bytes)."""
    i, out = 0, {}

    def rv():
        nonlocal i
        shift = v = 0
        while True:
            b = buf[i]
            i += 1
            v |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                return v

    while i < len(buf):
        key = rv()
        fn, wt = key >> 3, key & 7
        if wt == 0:
            val = rv()
        elif wt == 2:
            n = rv()
            val = buf[i:i + n]
            i += n
        elif wt == 5:
            val, i = buf[i:i + 4], i + 4
        elif wt == 1:
            val, i = buf[i:i + 8], i + 8
        else:
            raise ValueError(f"wire type {wt}")
        out.setdefault(fn, []).append(val)
    return out


def _i32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def _unzig(v):
    return (v >> 1) ^ -(v & 1)


def parse_send(payload: bytes) -> dict:
    f = parse_pb(payload)
    d = {"eventtype": f.get(1, [0])[-1], "sendint": _unzig(f.get(4, [0])[-1]),
         "senddata": f.get(5, [b""])[-1]}
    d["event"] = EVENT_NAME.get(d["eventtype"], d["eventtype"])
    return d


PRINT_FIELDS = {1: "page", 2: "imgdata", 3: "datalength", 4: "totalpackage", 5: "indexpackage",
                6: "width", 7: "totalsection", 8: "compression", 9: "lastpage", 10: "sectionlength",
                11: "lowmemory", 12: "indexsection", 13: "printtype", 14: "colortype"}


def parse_print(buf: bytes) -> dict:
    f = parse_pb(buf)
    d = {}
    for fn, name in PRINT_FIELDS.items():
        v = f.get(fn, [b"" if fn == 2 else 0])[-1]
        d[name] = _unzig(v) if fn in (3, 10) else v
    return d


def parse_respond(payload: bytes) -> dict:
    """MPRespondMsg{1 eventtype enum, 2 code int32, 3 responddata bytes, 4 error MPCodeMsg}"""
    f = parse_pb(payload)
    r = {"eventtype": f.get(1, [0])[-1], "code": _i32(f.get(2, [0])[-1]),
         "responddata": f.get(3, [b""])[-1], "error": f.get(4, [None])[-1]}
    r["event"] = EVENT_NAME.get(r["eventtype"], r["eventtype"])
    return r


def parse_code(buf: bytes) -> dict:
    """MPCodeMsg{1 code int32, 2 info string}"""
    f = parse_pb(buf or b"")
    return {"code": _i32(f.get(1, [0])[-1]), "info": f.get(2, [b""])[-1].decode()}


DEVICEINFO_FIELDS = {1: ("mac", str), 2: ("sn", str), 3: ("firmwarever", str), 4: ("paperstatus", int),
                     5: ("elec", int), 6: ("concentration", int), 7: ("speed", int),
                     8: ("papersize", int), 9: ("printstatus", str), 10: ("papertype", int),
                     11: ("closetime", int), 12: ("protocol", int), 13: ("blever", str),
                     14: ("mfr", str), 15: ("eeid", int), 16: ("supportfunction", int)}
PRINTSTATUS_BITS = ["busy", "out_of_paper", "cache_full", "hatch_open", "paper_jam", "head_overheat",
                    "low_battery", "motor_overheat", "calibrating_paper"]  # bit k, LSB = bit 0


def parse_deviceinfo(buf: bytes) -> dict:
    f = parse_pb(buf)
    d = {}
    for fn, (name, typ) in DEVICEINFO_FIELDS.items():
        if fn in f:
            v = f[fn][-1]
            d[name] = v.decode() if typ is str else _i32(v)
    st = int(d.get("printstatus") or 0)
    d["printstatus_flags"] = [n for k, n in enumerate(PRINTSTATUS_BITS) if st >> k & 1]
    sf = d.get("supportfunction", 0)
    d["canResend"] = bool(sf >> 1 & 1)
    d["supportSendWhenPrinting"] = bool(sf >> 7 & 1)
    return d


# ----------------------------------------------------------------------------- control frames (-> 0xABF1)
def ctrl_frame(event: str, sendint: int = 0) -> bytes:
    return enpack(mp_send(EVENT[event], sendint=sendint))


DEVICEINFO_FRAME = ctrl_frame("DEVICEINFO")   # 55 02 40 5d 08 01
PRINTINEND_FRAME = ctrl_frame("PRINTINEND")   # 55 02 40 55 08 0a


def density_frame(level: int) -> bytes:
    return ctrl_frame("PRINTINCONCENTRATION", level)  # editor UI range 1..16


def speed_frame(level: int) -> bytes:
    return ctrl_frame("PRINTINGSPEED", level)  # editor UI range 1..8


# ----------------------------------------------------------------------------- image -> job
def hs(data: bytes) -> bytes:
    return heatshrink2.compress(bytes(data), window_sz2=HS_WINDOW, lookahead_sz2=HS_LOOKAHEAD)


def unhs(data: bytes) -> bytes:
    return heatshrink2.decompress(bytes(data), window_sz2=HS_WINDOW, lookahead_sz2=HS_LOOKAHEAD)


def section_size(byte_array: bytes, width_dots: int) -> int:
    r = 4.5 if len(hs(byte_array)) > 30720 else 8
    s = int(1024 * r)
    return s - s % width_dots


@dataclass
class Job:
    byte_array: bytes
    width_dots: int
    copies: int = 1
    per_size: int = PER_SIZE
    sections: list = field(default_factory=list)

    def __post_init__(self):
        if self.width_dots % 8:
            raise ValueError("width must be a multiple of 8 dots: pad with white columns first")
        if len(self.byte_array) % (self.width_dots // 8):
            raise ValueError("byte_array is not a whole number of rows")
        s = section_size(self.byte_array, self.width_dots)
        self.section_len = s
        self.sections = [hs(self.byte_array[i:i + s]) for i in range(0, len(self.byte_array), s)]

    def section_frames(self, idx1: int) -> list:
        """All packet frames for 1-based section idx1 (-> 0xABF4), in send order."""
        sec = self.sections[idx1 - 1]
        pk = [sec[i:i + self.per_size] for i in range(0, len(sec), self.per_size)]
        frames = []
        for n, chunk in enumerate(pk, 1):
            msg = mp_print(page=self.copies, imgdata=chunk, datalength=len(self.byte_array),
                           totalpackage=len(pk), indexpackage=n,
                           width=self.width_dots // DOTS_PER_MM if n == 1 else 0,
                           totalsection=len(self.sections), compression=1, sectionlength=len(sec),
                           indexsection=idx1)
            frames.append(enpack(mp_send(EVENT["DEVICEPRINT"], senddata=msg)))
        return frames


# ----------------------------------------------------------------------------- flow model (no I/O)
class Flow:
    """What the editor's driver does with each notification while a job is in flight.

    feed(notification_bytes) -> list of actions:
      ('ack',)                   DEVICEREPORT MPCodeMsg{code 11, info "2"}: section accepted, send next
      ('resend', section)        DEVICEPRINT reply, code != 200, MPCodeMsg.code = 0x80000000 | section:
                                 go back to that section and carry on from there
      ('fail', reason)           DEVICEPRINT reply code != 200 without the resend bit, or a 2nd
                                 resend request for the same section
      ('printed', n, expected)   DEVICEREPORT {code 10, info "0"}: one page/copy came out
      ('done',)                  printed count reached expected
      ('printer_error', flags)   DEVICEREPORT {code 10, info != "0"} while printing
      ('stopped',)               DEVICEREPORT {code 13, info "1"} while printing (unless hatch open)
      ('deviceinfo', dict)       DEVICEINFO reply
    """

    def __init__(self, expected_pages: int = 1):
        self.unpacker = Unpacker()
        self.expected = expected_pages
        self.printed = 0
        self.resent = set()

    def feed(self, chunk: bytes) -> list:
        acts = []
        for payload in self.unpacker.feed(chunk):
            r = parse_respond(payload)
            et, data = r["eventtype"], r["responddata"]
            if et == EVENT["DEVICEINFO"] and data:
                acts.append(("deviceinfo", parse_deviceinfo(data)))
            elif et == EVENT["DEVICEREPORT"]:
                c = parse_code(data)
                code, info = c["code"], c["info"]
                if code == 11 and info == "2":
                    acts.append(("ack",))
                elif code == 10:
                    st = int(info or 0)
                    if st:
                        acts.append(("printer_error",
                                     [n for k, n in enumerate(PRINTSTATUS_BITS) if st >> k & 1]))
                    else:
                        self.printed += 1
                        acts.append(("printed", self.printed, self.expected))
                        if self.printed == self.expected:
                            acts.append(("done",))
                elif code == 13 and info == "1":
                    acts.append(("stopped",))
            elif et == EVENT["DEVICEPRINT"] and r["code"] != 200:
                c = parse_code(data)
                u = c["code"] & 0xFFFFFFFF
                if u > 0x80000000:
                    sec = u - 0x80000000
                    if sec in self.resent:
                        acts.append(("fail", f"section {sec} resend requested twice"))
                    else:
                        self.resent.add(sec)
                        acts.append(("resend", sec))
                else:
                    acts.append(("fail", f"DEVICEPRINT error code {r['code']} / {c}"))
            elif et == EVENT["CANCELPRINTING"] and r["code"] != 200:
                acts.append(("fail", "cancel reported an error"))
        return acts


def replay(job: Job, notifications: list, copies: int) -> tuple:
    """Drive Flow with recorded notifications; return (writes we would make, flow actions)."""
    flow = Flow(expected_pages=copies)
    writes = [DEVICEINFO_FRAME] + job.section_frames(1)
    sec, acts = 1, []
    for n in notifications:
        for a in flow.feed(n):
            acts.append(a[0])
            if a[0] == "ack":
                sec += 1
                writes += job.section_frames(sec) if sec <= len(job.sections) else [PRINTINEND_FRAME]
            elif a[0] == "resend":
                sec = a[1]
                writes += job.section_frames(sec)
    return writes, acts


# ----------------------------------------------------------------------------- golden checks
HERE = os.path.dirname(os.path.abspath(__file__))


def check(path: str) -> bool:
    g = json.load(open(path))
    notes = [bytes.fromhex(e["hex"]) for e in g["log"] if e["dir"] == "notify"]
    js_writes = [e for e in g["log"] if e["dir"] == "write"]
    if "pageInputs" in g:  # full golden (tiny16x2*)
        pi = g["pageInputs"][0]
        ba, width = bytes.fromhex(pi["byteArrayHex"]), pi["widthDots"]
        copies = 3 if g["summary"]["scenario"].endswith("copies3") else 1
        exp_sections = pi["sections"]
        sec_ok = lambda job: [s.hex() for s in job.sections] == exp_sections  # noqa: E731
        exp_size = pi["sectionSize"]
        same_write = lambda ours, js: ours.hex() == js["hex"]  # noqa: E731
    else:  # compact golden (selftest_4x6*)
        with gzip.open(os.path.join(HERE, g["input"]["bits_file"])) as f:
            ba = f.read()
        width, copies = g["input"]["width_dots"], 1
        assert hashlib.sha256(ba).hexdigest() == g["input"]["byte_array_sha256"]
        sec_ok = lambda job: [hashlib.sha256(s).hexdigest() for s in job.sections] == g["expect"]["section_sha256"]  # noqa: E731
        exp_size = g["expect"]["section_size"]
        same_write = lambda ours, js: hashlib.sha256(ours).hexdigest() == js["sha256"] and (  # noqa: E731
            "hex" not in js or ours.hex() == js["hex"])
    job = Job(ba, width, copies=copies)
    ours, acts = replay(job, notes, copies)
    ok_sec = job.section_len == exp_size and sec_ok(job)
    ok_w = len(ours) == len(js_writes) and all(same_write(o, j) for o, j in zip(ours, js_writes))
    ok = ok_sec and ok_w and acts[-1] == "done"
    print(f"{os.path.basename(path)}: section {job.section_len} B x {len(job.sections)}, sections match: {ok_sec}; "
          f"{len(js_writes)} editor writes vs {len(ours)} ours, identical: {ok_w}; ends 'done': {acts[-1] == 'done'}")
    return ok


if __name__ == "__main__":
    if sys.argv[1:2] != ["--check"] or len(sys.argv) < 3:
        sys.exit("usage: reference_job.py --check golden_*.json")
    res = [check(p) for p in sys.argv[2:]]
    print("ALL GOLDEN CHECKS PASS" if all(res) else "SOME CHECKS FAILED")
    sys.exit(0 if all(res) else 1)
