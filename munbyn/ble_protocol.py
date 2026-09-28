"""Pure encoder/decoder for the RW403B's Bluetooth LE print protocol.

No I/O and no ``bleak`` here -- this module only turns label bitmaps into the
exact frames the printer expects and decodes what it sends back.
``munbyn.ble_transport`` does the radio part.

The protocol comes from reading (and running) the JS of Munbyn's web editor,
which prints to the RW403B over Web Bluetooth. **None of it has been verified
on the printer yet.** The spec, with a source for every item, is
``PLANS/BLE-PROTOCOL.md``; section numbers below ("spec 7") point into it.
The byte-exact tests in ``tests/test_ble_protocol.py`` replay golden vectors
recorded from the editor's own code (``tests/fixtures/ble/``).

Pipeline for one page (spec 6-8)::

    1-bit page image (e.g. 812 x 1242 for 4x6 at feed_scale 0.981)
      -> compose_page: bake in x/y shift, pad the width to a multiple of 8 (812 -> 816)
      -> pack: rows top first, MSB = leftmost pixel, 1 = BLACK (tspl.pack_bitmap(black_is_one=True))
      -> sections: S = 1024*r - (1024*r % width_dots) bytes, r = 8 (4.5 if the whole
         page compresses to > 30720 bytes); each section heatshrink-compressed on its own
         (window_sz2=11, lookahead_sz2=4, no header)
      -> packets: <= per_size (400; 148 on BLE firmware 1.0.8) bytes of a compressed section
      -> MPPrintMsg (proto3) inside MPSendMsg{eventtype=DEVICEPRINT}.senddata
      -> enpack() framing -> written to characteristic 0xABF4

Control messages (DEVICEINFO, SELFTEST, CANCELPRINTING, PRINTINEND, density,
speed) are one frame each on 0xABF1. The printer answers on 0xABF3 with
framed ``MPRespondMsg``s (``NotificationDecoder``).

The protobuf encoding is hand-rolled (5 flat messages; ``sint32`` fields are
zigzag-encoded -- spec 4). ``heatshrink2`` (pinned in requirements.txt) is
imported lazily so this module stays importable without it.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image

from .labels import mm_to_dots, stretched_height_dots
from .tspl import pack_bitmap

# --------------------------------------------------------------------------- GATT (spec 1)


def uuid16(n: int) -> str:
    """Expand a 16-bit Bluetooth UUID alias onto the Bluetooth base UUID."""
    return "0000{:04x}-0000-1000-8000-00805f9b34fb".format(n)


SERVICE_UUID = uuid16(0xABF0)
#: Notifications from the printer (MPRespondMsg frames).
NOTIFY_UUID = uuid16(0xABF3)
#: DEVICEPRINT packets only.
DATA_UUID = uuid16(0xABF4)
#: Everything else: DEVICEINFO, SELFTEST, CANCELPRINTING, PRINTINEND, density, speed.
CONTROL_UUID = uuid16(0xABF1)

#: The editor's scan filter for this model (``namePrefix``).
NAME_PREFIX = "RW403B"


def short_uuid(uuid: str) -> str:
    """``"0000abf4-0000-..."`` -> ``"ABF4"`` (for logs); other UUIDs unchanged."""
    u = str(uuid).lower()
    if len(u) == 36 and u.startswith("0000") and u.endswith("-0000-1000-8000-00805f9b34fb"):
        return u[4:8].upper()
    return str(uuid)


# --------------------------------------------------------------------------- constants (spec 1, 5, 7-9)

#: Bytes of compressed section per DEVICEPRINT packet (``defaultWritrParamsObj.RW403B``).
PER_SIZE = 400
#: Used instead of ``PER_SIZE`` when DEVICEINFO reports BLE firmware "1.0.8".
PER_SIZE_BLE_1_0_8 = 148
#: Sleep after every DEVICEPRINT packet write (``perTime``), seconds.
PER_PACKET_DELAY_S = 0.003
#: Sleep after each page/copy loop (``perPageTime``), seconds.
PER_PAGE_DELAY_S = 0.002
#: Wait this long for a section's ack before aborting ("PrintTimeout").
SECTION_ACK_TIMEOUT_S = 10.0
#: Wait this long for a DEVICEINFO reply.
DEVICEINFO_TIMEOUT_S = 4.0
#: When DEVICEINFO says only "busy"/"calibrating", wait this long and ask again.
BUSY_RETRY_DELAY_S = 4.0
#: The editor's receiver drops frames with a longer payload (``_MAX_PACK_SIZE``).
MAX_PACK_SIZE = 1156
#: 14-bit length field.
MAX_PAYLOAD = 0x3FFF
#: Dots per mm the editor uses (``printRatio``); ``MPPrintMsg.width = dots / 8``.
DOTS_PER_MM = 8
#: The editor centre-crops anything wider than 110 mm * 8.
MAX_WIDTH_DOTS = 880
#: heatshrink parameters compiled into the editor's heatshrink.js (spec 8).
HS_WINDOW_SZ2 = 11
HS_LOOKAHEAD_SZ2 = 4
#: Whole-page compressed size above which sections shrink (r = 4.5 instead of 8).
BIG_PAGE_COMPRESSED = 30720
#: A 1-page job is sent once per copy (page=1) instead of once with page=copies
#: only when the printer supports sending while printing AND the compressed page
#: is at least this big (spec 7, "Copies").
SEND_PER_COPY_MIN_COMPRESSED = 163840
#: Labels taller than this are refused unless supportfunction bit 7 is set (spec 5).
MAX_LABEL_MM_WITHOUT_SEND_WHILE_PRINTING = 200.0
#: MPRespondMsg.code for "OK".
CODE_OK = 200
#: MPCodeMsg.code bit that marks a resend request (spec 9).
RESEND_BIT = 0x80000000

#: Density (PRINTINCONCENTRATION) and speed (PRINTINGSPEED) ranges in the editor's UI (spec 11).
DENSITY_RANGE = (1, 16)
SPEED_RANGE = (1, 8)


class EventType(enum.IntEnum):
    DEFAULT = 0
    DEVICEINFO = 1
    SELFTEST = 2
    CLOSETIME = 3
    DEVICEPRINT = 4
    CANCELPRINTING = 5
    DEVICEREPORT = 6
    FIRMWAREUPGRADE = 7
    PRINTINGSPEED = 8
    PRINTINCONCENTRATION = 9
    PRINTINEND = 10
    PAPERTYPESET = 11
    FACTORYCOMMAND = 12
    SNSET = 13
    PAPERINFOSET = 14


def event_name(eventtype: int) -> str:
    try:
        return EventType(eventtype).name
    except ValueError:
        return str(eventtype)


class ProtocolError(ValueError):
    """Malformed protobuf/frame data, or an out-of-range value to encode."""


# --------------------------------------------------------------------------- protobuf (spec 4)

_INT32_MIN, _INT32_MAX = -(1 << 31), (1 << 31) - 1


def _check_int32(name: str, v: int) -> int:
    v = int(v)
    if not _INT32_MIN <= v <= _INT32_MAX:
        raise ProtocolError("{} = {} does not fit in an int32".format(name, v))
    return v


def encode_varint(n: int) -> bytes:
    """Base-128 varint. Negative values (int32/enum) are sign-extended to 64
    bits, i.e. always 10 bytes, like every protobuf runtime does."""
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zigzag32(n: int) -> int:
    """sint32 zigzag: 0->0, -1->1, 1->2, -2->3, ..."""
    n = _check_int32("sint32", n)
    return ((n << 1) ^ (n >> 31)) & 0xFFFFFFFF


def unzigzag32(v: int) -> int:
    v &= 0xFFFFFFFF
    return (v >> 1) ^ -(v & 1)


def _as_int32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def _f_int(fn: int, v: int, name: str = "int32") -> bytes:
    v = _check_int32(name, v)
    return b"" if not v else encode_varint(fn << 3) + encode_varint(v)


def _f_sint(fn: int, v: int, name: str = "sint32") -> bytes:
    return b"" if not v else encode_varint(fn << 3) + encode_varint(zigzag32(v))


def _f_bytes(fn: int, v: bytes) -> bytes:
    return b"" if not v else encode_varint(fn << 3 | 2) + encode_varint(len(v)) + bytes(v)


def _f_str(fn: int, v: str) -> bytes:
    return _f_bytes(fn, (v or "").encode("utf-8"))


def parse_fields(buf: bytes) -> Dict[int, list]:
    """Generic proto3 wire parser: ``{field_number: [raw values]}``.

    Varints come back unsigned (up to 64 bits), length-delimited fields as
    ``bytes``, fixed32/fixed64 as their raw 4/8 bytes. Raises
    ``ProtocolError`` on truncated or unsupported input."""
    buf = bytes(buf)
    i, n, out = 0, len(buf), {}  # type: int, int, Dict[int, list]

    def read_varint() -> int:
        nonlocal i
        shift = v = 0
        while True:
            if i >= n:
                raise ProtocolError("truncated varint")
            b = buf[i]
            i += 1
            v |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                return v
            if shift > 63:
                raise ProtocolError("varint longer than 10 bytes")

    while i < n:
        key = read_varint()
        fn, wt = key >> 3, key & 7
        if fn == 0:
            raise ProtocolError("field number 0")
        if wt == 0:
            val = read_varint()
        elif wt == 2:
            ln = read_varint()
            if i + ln > n:
                raise ProtocolError("truncated length-delimited field {}".format(fn))
            val = buf[i : i + ln]
            i += ln
        elif wt == 5:
            if i + 4 > n:
                raise ProtocolError("truncated fixed32")
            val, i = buf[i : i + 4], i + 4
        elif wt == 1:
            if i + 8 > n:
                raise ProtocolError("truncated fixed64")
            val, i = buf[i : i + 8], i + 8
        else:
            raise ProtocolError("unsupported wire type {}".format(wt))
        out.setdefault(fn, []).append(val)
    return out


def _last(f: Dict[int, list], fn: int, default):
    return f[fn][-1] if fn in f else default


def _get_int(f: Dict[int, list], fn: int) -> int:
    v = _last(f, fn, 0)
    if isinstance(v, (bytes, bytearray)):
        raise ProtocolError("field {} is not a varint".format(fn))
    return _as_int32(v)


def _get_bytes(f: Dict[int, list], fn: int) -> bytes:
    v = _last(f, fn, b"")
    if not isinstance(v, (bytes, bytearray)):
        raise ProtocolError("field {} is not length-delimited".format(fn))
    return bytes(v)


def _get_str(f: Dict[int, list], fn: int) -> str:
    return _get_bytes(f, fn).decode("utf-8", "replace")


def js_number(text: str) -> Optional[int]:
    """Parse a decimal-number string the way the editor's loose ``==`` does
    (``Number(s)``): ``""`` -> 0, surrounding whitespace ignored; ``None`` for
    anything non-numeric or non-integral."""
    s = (text or "").strip()
    if not s:
        return 0
    try:
        return int(s, 10)
    except ValueError:
        pass
    try:
        f = float(s)
    except ValueError:
        return None
    if math.isfinite(f) and f == int(f):
        return int(f)
    return None


# -- MPSendMsg (host -> printer, every write)


def encode_send(
    eventtype: int, *, eventtag: str = "", sendint: int = 0, sendstr: str = "", senddata: bytes = b""
) -> bytes:
    """Serialize an ``MPSendMsg``. Field order is 1, 2, 4, 3, 5 -- the order the
    editor's serializer writes (``sendint`` is declared before ``sendstr``)."""
    return (
        _f_int(1, int(eventtype), "eventtype")
        + _f_str(2, eventtag)
        + _f_sint(4, sendint, "sendint")
        + _f_str(3, sendstr)
        + _f_bytes(5, senddata)
    )


@dataclass
class SendMsg:
    eventtype: int = 0
    eventtag: str = ""
    sendint: int = 0
    sendstr: str = ""
    senddata: bytes = b""

    def encode(self) -> bytes:
        return encode_send(
            self.eventtype, eventtag=self.eventtag, sendint=self.sendint, sendstr=self.sendstr,
            senddata=self.senddata,
        )


def decode_send(payload: bytes) -> SendMsg:
    f = parse_fields(payload)
    return SendMsg(
        eventtype=_get_int(f, 1),
        eventtag=_get_str(f, 2),
        sendint=unzigzag32(_last(f, 4, 0)),
        sendstr=_get_str(f, 3),
        senddata=_get_bytes(f, 5),
    )


# -- MPPrintMsg (inside MPSendMsg.senddata for DEVICEPRINT)


@dataclass
class PrintMsg:
    page: int = 0  # copies for a 1-page job, else 1
    imgdata: bytes = b""  # <= per_size bytes of a compressed section
    datalength: int = 0  # sint32: uncompressed packed bytes of the whole page
    totalpackage: int = 0  # packets in this section
    indexpackage: int = 0  # 1-based packet index within the section
    width: int = 0  # bytes per row; only on packet 1 of each section
    totalsection: int = 0
    compression: int = 0  # always 1
    lastpage: int = 0  # never set
    sectionlength: int = 0  # sint32: COMPRESSED length of this section
    lowmemory: int = 0  # never set
    indexsection: int = 0  # 1-based
    printtype: int = 0
    colortype: int = 0

    def encode(self) -> bytes:
        return (
            _f_int(1, self.page, "page")
            + _f_bytes(2, self.imgdata)
            + _f_sint(3, self.datalength, "datalength")
            + _f_int(4, self.totalpackage, "totalpackage")
            + _f_int(5, self.indexpackage, "indexpackage")
            + _f_int(6, self.width, "width")
            + _f_int(7, self.totalsection, "totalsection")
            + _f_int(8, self.compression, "compression")
            + _f_int(9, self.lastpage, "lastpage")
            + _f_sint(10, self.sectionlength, "sectionlength")
            + _f_int(11, self.lowmemory, "lowmemory")
            + _f_int(12, self.indexsection, "indexsection")
            + _f_int(13, self.printtype, "printtype")
            + _f_int(14, self.colortype, "colortype")
        )


def decode_print(buf: bytes) -> PrintMsg:
    f = parse_fields(buf)
    return PrintMsg(
        page=_get_int(f, 1),
        imgdata=_get_bytes(f, 2),
        datalength=unzigzag32(_last(f, 3, 0)),
        totalpackage=_get_int(f, 4),
        indexpackage=_get_int(f, 5),
        width=_get_int(f, 6),
        totalsection=_get_int(f, 7),
        compression=_get_int(f, 8),
        lastpage=_get_int(f, 9),
        sectionlength=unzigzag32(_last(f, 10, 0)),
        lowmemory=_get_int(f, 11),
        indexsection=_get_int(f, 12),
        printtype=_get_int(f, 13),
        colortype=_get_int(f, 14),
    )


# -- MPCodeMsg / MPRespondMsg (printer -> host)


@dataclass
class CodeMsg:
    code: int = 0
    info: str = ""  # a decimal number as a string

    @property
    def info_number(self) -> Optional[int]:
        return js_number(self.info)

    @property
    def unsigned_code(self) -> int:
        return self.code & 0xFFFFFFFF

    def encode(self) -> bytes:
        return _f_int(1, self.code, "code") + _f_str(2, self.info)


def decode_code(buf: bytes) -> CodeMsg:
    f = parse_fields(buf or b"")
    return CodeMsg(code=_get_int(f, 1), info=_get_str(f, 2))


@dataclass
class RespondMsg:
    eventtype: int = 0
    code: int = 0
    responddata: bytes = b""  # serialized MPDeviceInfoMsg (eventtype 1) or MPCodeMsg (4, 6)
    error: Optional[CodeMsg] = None

    @property
    def event(self) -> str:
        return event_name(self.eventtype)

    def encode(self) -> bytes:
        err = self.error.encode() if self.error is not None else b""
        return (
            _f_int(1, self.eventtype, "eventtype")
            + _f_int(2, self.code, "code")
            + _f_bytes(3, self.responddata)
            + (encode_varint(4 << 3 | 2) + encode_varint(len(err)) + err if self.error is not None else b"")
        )


def decode_respond(payload: bytes) -> RespondMsg:
    f = parse_fields(payload)
    err = None
    if 4 in f:
        err = decode_code(_get_bytes(f, 4))
    return RespondMsg(
        eventtype=_get_int(f, 1), code=_get_int(f, 2), responddata=_get_bytes(f, 3), error=err
    )


# -- MPDeviceInfoMsg (spec 4, 5)

#: ``printstatus`` bit k (LSB = bit 0) -> meaning (spec 5).
PRINTSTATUS_FLAGS: Tuple[str, ...] = (
    "busy",
    "out_of_paper",
    "print_cache_full",
    "hatch_open",
    "paper_jam",
    "head_overheated",
    "low_battery",
    "motor_overheated",
    "calibrating_paper",
)
HATCH_OPEN_BIT = 3


def status_bits(value: Optional[int], n: int = 9) -> List[int]:
    """The editor's ``Bp()``: an integer as ``n`` bits, LSB first. ``None``
    (a non-numeric status string) is treated as "every bit set" -- the editor
    would throw on it, so it must never read as ready."""
    if value is None or value < 0:
        return [1] * n
    return [(value >> k) & 1 for k in range(n)]


def status_flags(value: Optional[int]) -> List[str]:
    if value is None or value < 0:
        return ["unreadable_status"]
    return [name for k, name in enumerate(PRINTSTATUS_FLAGS) if (value >> k) & 1]


@dataclass
class DeviceInfo:
    mac: str = ""
    sn: str = ""
    firmwarever: str = ""
    paperstatus: int = 0
    elec: int = 0
    concentration: int = 0
    speed: int = 0
    papersize: int = 0
    printstatus: str = ""  # decimal string -> 9-bit mask
    papertype: int = 0
    closetime: int = 0
    protocol: int = 0
    blever: str = ""  # BLE firmware; "1.0.8" -> per_size 148
    mfr: str = ""
    eeid: int = 0
    supportfunction: int = 0  # bit 1 canResend, bit 7 supportSendWhenPrinting

    _STR = {1: "mac", 2: "sn", 3: "firmwarever", 9: "printstatus", 13: "blever", 14: "mfr"}
    _INT = {
        4: "paperstatus", 5: "elec", 6: "concentration", 7: "speed", 8: "papersize", 10: "papertype",
        11: "closetime", 12: "protocol", 15: "eeid", 16: "supportfunction",
    }

    @property
    def printstatus_value(self) -> Optional[int]:
        return js_number(self.printstatus)

    @property
    def printstatus_bits(self) -> List[int]:
        return status_bits(self.printstatus_value)

    @property
    def printstatus_flags(self) -> List[str]:
        return status_flags(self.printstatus_value)

    @property
    def ready(self) -> bool:
        """The editor's pre-print rule: every printstatus bit must be 0."""
        return not any(self.printstatus_bits)

    @property
    def only_busy_or_calibrating(self) -> bool:
        """Bit 0 (busy) or bit 8 (calibrating) set, bits 1-7 clear: the editor
        waits 4 s and asks again."""
        b = self.printstatus_bits
        return (b[0] == 1 or b[8] == 1) and not any(b[1:8])

    @property
    def can_resend(self) -> bool:
        return bool((self.supportfunction >> 1) & 1)

    @property
    def support_send_when_printing(self) -> bool:
        return bool((self.supportfunction >> 7) & 1)

    @property
    def per_size(self) -> int:
        """Packet size the editor uses for an RW403B with this BLE firmware."""
        return PER_SIZE_BLE_1_0_8 if self.blever == "1.0.8" else PER_SIZE

    def encode(self) -> bytes:
        parts = []
        for fn in range(1, 17):
            if fn in self._STR:
                parts.append(_f_str(fn, getattr(self, self._STR[fn])))
            else:
                parts.append(_f_int(fn, getattr(self, self._INT[fn]), self._INT[fn]))
        return b"".join(parts)


def decode_deviceinfo(buf: bytes) -> DeviceInfo:
    f = parse_fields(buf)
    d = DeviceInfo()
    for fn, name in DeviceInfo._STR.items():
        if fn in f:
            setattr(d, name, _get_str(f, fn))
    for fn, name in DeviceInfo._INT.items():
        if fn in f:
            setattr(d, name, _get_int(f, fn))
    return d


# --------------------------------------------------------------------------- framing (spec 3)

STX = 0x55


def enpack(payload: bytes) -> bytes:
    """Wrap a payload in the editor's frame: ``55 len_lo len_hi|chk chk|chk payload``.

    ``x = 0x55 ^ len_lo ^ len_hi``; x bits 2-3 go to byte 2 bits 6-7, x bits
    4-5 to byte 3 bits 0-1; ``X = x ^ XOR(payload)`` bits 2-7 fill byte 3
    bits 2-7."""
    payload = bytes(payload)
    n = len(payload)
    if n > MAX_PAYLOAD:
        raise ProtocolError("payload of {} bytes is longer than the 14-bit length field".format(n))
    b1, b2 = n & 0xFF, (n >> 8) & 0xFF
    x = STX ^ b1 ^ b2
    b2 |= (x & 0x0C) << 4
    b3 = (x >> 4) & 0x03
    for c in payload:
        x ^= c
    b3 |= x & 0xFC
    return bytes([STX, b1, b2 & 0xFF, b3]) + payload


class Unpacker:
    """Byte-at-a-time frame parser with the editor's states and checks.

    ``feed()`` takes any split of the incoming byte stream -- one BLE
    notification may hold part of a frame or several frames -- and keeps its
    state between calls. It returns the payloads of every complete, valid
    frame. Frames with bad check bits, or a length over ``MAX_PACK_SIZE``, are
    dropped (``dropped`` counts them). Unlike the JS, a zero-length frame
    yields ``b""`` instead of leaving the parser stuck."""

    def __init__(self) -> None:
        self.state = 0
        self.calc = 0
        self.check = 0
        self.flen = 0
        self.data = bytearray()
        self.dropped = 0

    def feed(self, chunk: bytes) -> List[bytes]:
        out: List[bytes] = []
        for b in bytes(chunk):
            s = self.state
            if s == 0:
                if b == STX:
                    self.state, self.calc = 1, STX
            elif s == 1:
                self.flen = b
                self.calc ^= b
                self.state = 2
            elif s == 2:
                self.flen |= b << 8
                self.calc ^= b & 0x3F
                if (self.flen & 0x3FFF) > MAX_PACK_SIZE:
                    self.state = 0
                    self.dropped += 1
                    continue
                self.data = bytearray()
                self.state = 3
            elif s == 3:
                t = ((b & 3) << 4) | ((self.flen & 0xC000) >> 12)
                self.check = b
                if t != (self.calc & 0x3C):
                    self.state = 0
                    self.dropped += 1
                    continue
                self.flen &= 0x3FFF
                self.state = 4
                if self.flen == 0:
                    self.state = 0
                    self._finish(out)
            else:  # s == 4
                self.data.append(b)
                self.calc ^= b
                if len(self.data) == self.flen:
                    self.state = 0
                    self._finish(out)
        return out

    def _finish(self, out: List[bytes]) -> None:
        if (self.calc & 0xFC) == (self.check & 0xFC):
            out.append(bytes(self.data))
        else:
            self.dropped += 1


def unpack_all(data: bytes) -> List[bytes]:
    """Every valid payload in ``data`` (one-shot ``Unpacker``)."""
    return Unpacker().feed(data)


# --------------------------------------------------------------------------- control frames (spec 2, 11)


def control_frame(eventtype: int, sendint: int = 0) -> bytes:
    return enpack(encode_send(int(eventtype), sendint=sendint))


DEVICEINFO_FRAME = control_frame(EventType.DEVICEINFO)  # 55 02 40 5d 08 01
SELFTEST_FRAME = control_frame(EventType.SELFTEST)  # 55 02 40 5d 08 02
CANCELPRINTING_FRAME = control_frame(EventType.CANCELPRINTING)  # 55 02 40 59 08 05
PRINTINEND_FRAME = control_frame(EventType.PRINTINEND)  # 55 02 40 55 08 0a


def density_frame(level: int) -> bytes:
    """PRINTINCONCENTRATION; the editor's UI range is 1..16. INFERRED to be
    stored by the printer -- see spec 11."""
    lo, hi = DENSITY_RANGE
    if not lo <= int(level) <= hi:
        raise ValueError("Bluetooth density must be {}..{}, not {!r}".format(lo, hi, level))
    return control_frame(EventType.PRINTINCONCENTRATION, int(level))


def speed_frame(level: int) -> bytes:
    """PRINTINGSPEED; the editor's UI range is 1..8."""
    lo, hi = SPEED_RANGE
    if not lo <= int(level) <= hi:
        raise ValueError("Bluetooth speed must be {}..{}, not {!r}".format(lo, hi, level))
    return control_frame(EventType.PRINTINGSPEED, int(level))


# --------------------------------------------------------------------------- compression (spec 8)


def _heatshrink():
    try:
        import heatshrink2
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "Bluetooth printing needs the heatshrink2 package: "
            "`.venv/bin/pip install -r requirements.txt` (or re-run ./setup.sh)"
        ) from exc
    return heatshrink2


def compress(data: bytes) -> bytes:
    """heatshrink, window_sz2=11, lookahead_sz2=4, no header, zero-filled window."""
    return _heatshrink().compress(bytes(data), window_sz2=HS_WINDOW_SZ2, lookahead_sz2=HS_LOOKAHEAD_SZ2)


def decompress(data: bytes) -> bytes:
    return _heatshrink().decompress(bytes(data), window_sz2=HS_WINDOW_SZ2, lookahead_sz2=HS_LOOKAHEAD_SZ2)


# --------------------------------------------------------------------------- page bitmap (spec 6)


def compose_page(
    img: Image.Image, *, x_shift_mm: float = 0.0, y_shift_mm: float = 0.0, feed_scale: float = 1.0
) -> Image.Image:
    """The page exactly as it goes over Bluetooth: a mode "1" image whose width
    is a multiple of 8 dots.

    BLE has no ``BITMAP x,y`` offsets, so ``x_shift_mm``/``y_shift_mm`` are baked
    into the bitmap with the same rules as ``munbyn.tspl._shift_and_bitmap``:
    positive moves the image right/down (the canvas keeps its size, so what
    moves off the right/bottom edge is clipped), negative crops the left/top
    side. ``y_shift_mm`` is stretched by ``1/feed_scale`` like every feed-axis
    length. Then the width is padded with white columns on the right up to a
    multiple of 8 (812 -> 816): the editor sends ``width = dots / 8`` and its
    section math needs whole bytes per row. Raises ``ValueError`` over
    ``MAX_WIDTH_DOTS``.
    """
    if img.mode != "1":
        img = img.convert("1", dither=Image.Dither.NONE)
    w, h = img.size
    x = mm_to_dots(x_shift_mm)
    y = stretched_height_dots(mm_to_dots(y_shift_mm), feed_scale) if y_shift_mm else 0
    if x or y:
        shifted = Image.new("1", (w, h), 255)
        src = img.crop((max(0, -x), max(0, -y), w, h))
        shifted.paste(src, (max(0, x), max(0, y)))
        img = shifted
    padded_w = (w + 7) // 8 * 8
    if padded_w > MAX_WIDTH_DOTS:
        raise ValueError(
            "page is {} dots wide; the Bluetooth protocol takes at most {} dots".format(w, MAX_WIDTH_DOTS)
        )
    if padded_w != w:
        canvas = Image.new("1", (padded_w, h), 255)
        canvas.paste(img, (0, 0))
        img = canvas
    return img


def pack_page(img: Image.Image) -> Tuple[int, int, bytes]:
    """``(width_dots, height, data)``: rows top first, MSB = leftmost pixel,
    **1 = black** (the editor's ``applyThresholdAndPack``). ``img`` must
    already be a multiple of 8 dots wide (see ``compose_page``)."""
    if img.width % 8:
        raise ValueError("page width must be a multiple of 8 dots (pad it with compose_page first)")
    width_bytes, height, data = pack_bitmap(img, black_is_one=True)
    return width_bytes * 8, height, data


# --------------------------------------------------------------------------- sections and packets (spec 7)


def section_size(data: bytes, width_dots: int, whole_compressed_len: Optional[int] = None) -> int:
    """Uncompressed bytes per section: ``1024*r - (1024*r % width_dots)`` with
    r = 8, or 4.5 when the whole page compresses to more than 30720 bytes.
    Uses the width in DOTS on a length in BYTES -- the editor's formula, which
    always yields a whole multiple of 8 rows."""
    if width_dots <= 0 or width_dots % 8:
        raise ValueError("width must be a positive multiple of 8 dots")
    if whole_compressed_len is None:
        whole_compressed_len = len(compress(data))
    r = 4.5 if whole_compressed_len > BIG_PAGE_COMPRESSED else 8
    s = int(1024 * r)
    return s - s % width_dots


def ack_delay_s(section_compressed_len: int) -> float:
    """The editor's pause after a section's ack/resend (``oZ()``):
    ``min(5, round(len/1024/160*1000/2))`` ms. Acks or resend requests that
    arrive during this pause are ignored, as in the editor."""
    ms = math.floor(section_compressed_len / 1024 / 160 * 1000 / 2 + 0.5)
    return min(5, ms) / 1000.0


@dataclass
class BlePage:
    """One packed, sectioned, compressed page."""

    width_dots: int
    height: int
    data: bytes  # packed, uncompressed (datalength = len(data))
    section_len: int  # uncompressed bytes per section (the last one may be shorter)
    sections: List[bytes] = field(repr=False)  # each compressed on its own
    whole_compressed_len: int = 0

    @classmethod
    def from_bytes(cls, data: bytes, width_dots: int) -> "BlePage":
        if width_dots <= 0 or width_dots % 8:
            raise ValueError("width must be a positive multiple of 8 dots: pad with white columns first")
        stride = width_dots // 8
        if not data or len(data) % stride:
            raise ValueError("page data is not a whole, non-zero number of {}-byte rows".format(stride))
        whole = len(compress(data))
        s = section_size(data, width_dots, whole)
        sections = [compress(data[i : i + s]) for i in range(0, len(data), s)]
        return cls(width_dots, len(data) // stride, bytes(data), s, sections, whole)

    @classmethod
    def from_image(cls, img: Image.Image) -> "BlePage":
        """From an already-composed page (see ``compose_page``)."""
        width_dots, _height, data = pack_page(img)
        return cls.from_bytes(data, width_dots)

    @property
    def stride(self) -> int:
        return self.width_dots // 8

    @property
    def width_field(self) -> int:
        """``MPPrintMsg.width``: bytes per row (= dots / 8 = mm at 8 dots/mm)."""
        return self.width_dots // DOTS_PER_MM

    @property
    def rows_per_section(self) -> int:
        return self.section_len // self.stride

    @property
    def compressed_len(self) -> int:
        return sum(len(s) for s in self.sections)

    @property
    def small_sections(self) -> bool:
        """True when the busy-page branch (r = 4.5) was taken."""
        return self.whole_compressed_len > BIG_PAGE_COMPRESSED

    def packets(self, idx1: int, per_size: int = PER_SIZE) -> List[bytes]:
        if per_size < 1:
            raise ValueError("per_size must be at least 1")
        sec = self.sections[idx1 - 1]
        return [sec[i : i + per_size] for i in range(0, len(sec), per_size)] or [b""]

    def section_frames(self, idx1: int, *, page_field: int, per_size: int = PER_SIZE) -> List[bytes]:
        """Every DEVICEPRINT frame (for characteristic 0xABF4) of 1-based section
        ``idx1``, in send order. ``width`` is only set on packet 1 -- the editor
        reuses one MPPrintMsg and zeroes it for the rest."""
        if not 1 <= idx1 <= len(self.sections):
            raise ValueError("section {} out of range 1..{}".format(idx1, len(self.sections)))
        pk = self.packets(idx1, per_size)
        sec_len = len(self.sections[idx1 - 1])
        frames = []
        for n, chunk in enumerate(pk, 1):
            msg = PrintMsg(
                page=page_field,
                imgdata=chunk,
                datalength=len(self.data),
                totalpackage=len(pk),
                indexpackage=n,
                width=self.width_field if n == 1 else 0,
                totalsection=len(self.sections),
                compression=1,
                sectionlength=sec_len,
                indexsection=idx1,
            )
            frames.append(enpack(encode_send(EventType.DEVICEPRINT, senddata=msg.encode())))
        return frames


def plan_sends(
    pages: Sequence[BlePage], copies: int, *, support_send_when_printing: bool = False
) -> Tuple[List[Tuple[int, int]], int]:
    """Which page goes out when, and with what ``page`` field (spec 7, "Copies").

    Returns ``(sends, expected_printed)``: ``sends`` is ``[(page_index,
    page_field), ...]`` in order, and the job is done when the printer has
    reported ``expected_printed`` pages. A 1-page job is sent once with
    ``page = copies`` (unless the printer supports sending while printing and
    the page is huge); a multi-page job is sent copies x pages times with
    ``page = 1``."""
    if copies < 1:
        raise ValueError("copies must be at least 1")
    if not pages:
        raise ValueError("nothing to print")
    if len(pages) == 1:
        per_copy = support_send_when_printing and pages[0].compressed_len >= SEND_PER_COPY_MIN_COMPRESSED
        sends = [(0, 1)] * copies if per_copy else [(0, copies)]
        return sends, copies
    sends = [(t, 1) for _copy in range(copies) for t in range(len(pages))]
    return sends, copies * len(pages)


@dataclass
class Write:
    """One planned GATT write."""

    char: str  # DATA_UUID or CONTROL_UUID
    frame: bytes = field(repr=False)
    label: str = ""


def planned_writes(
    pages: Sequence[BlePage],
    copies: int = 1,
    *,
    per_size: int = PER_SIZE,
    support_send_when_printing: bool = False,
    density: Optional[int] = None,
    speed: Optional[int] = None,
) -> List[Write]:
    """Every write of a job in order, assuming each section is acked the first
    time: DEVICEINFO, [density], [speed], the DEVICEPRINT packets, PRINTINEND.
    This is exactly what ``--test --ble`` shows and ``--hex`` saves; the live
    transport makes the same writes plus any resends the printer asks for."""
    writes = [Write(CONTROL_UUID, DEVICEINFO_FRAME, "DEVICEINFO")]
    if density is not None:
        writes.append(Write(CONTROL_UUID, density_frame(density), "PRINTINCONCENTRATION={}".format(density)))
    if speed is not None:
        writes.append(Write(CONTROL_UUID, speed_frame(speed), "PRINTINGSPEED={}".format(speed)))
    sends, _expected = plan_sends(pages, copies, support_send_when_printing=support_send_when_printing)
    for send_no, (pi, page_field) in enumerate(sends, 1):
        page = pages[pi]
        n = len(page.sections)
        for s in range(1, n + 1):
            frames = page.section_frames(s, page_field=page_field, per_size=per_size)
            for k, fr in enumerate(frames, 1):
                label = "DEVICEPRINT send {} page {} sec {}/{} pkt {}/{}".format(
                    send_no, pi + 1, s, n, k, len(frames)
                )
                writes.append(Write(DATA_UUID, fr, label))
    writes.append(Write(CONTROL_UUID, PRINTINEND_FRAME, "PRINTINEND"))
    return writes


# --------------------------------------------------------------------------- notifications (spec 2, 9, 10)


@dataclass(frozen=True)
class Event:
    """One decoded notification.

    ``kind`` is one of:

    * ``deviceinfo`` -- DEVICEINFO reply (``info``)
    * ``ack`` -- section accepted, send the next ({ev 6, {11, "2"}})
    * ``resend`` -- go back to 1-based ``section`` ({ev 4, code != 200, {0x80000000|k}})
    * ``printed`` -- one page/copy came out ({ev 6, {10, "0"}})
    * ``printer_error`` -- {ev 6, {10, info != 0}}; ``flags``/``status`` from the info bits
    * ``stopped`` -- {ev 6, {13, "1"}}
    * ``print_error`` -- {ev 4, code != 200} without the resend bit (``code``, ``detail``)
    * ``cancel_ok`` / ``cancel_error`` -- reply to CANCELPRINTING
    * ``ok`` -- SELFTEST/PAPERTYPESET/DEVICEPRINT accepted (code 200)
    * ``other`` -- anything else (``detail``); ``malformed`` -- undecodable payload
    """

    kind: str
    section: int = 0
    info: Optional[DeviceInfo] = None
    flags: Tuple[str, ...] = ()
    status: Optional[int] = None
    code: int = 0
    detail: str = ""
    payload: bytes = b""

    def __str__(self) -> str:
        bits = [self.kind]
        if self.kind == "resend":
            bits.append("section={}".format(self.section))
        if self.flags:
            bits.append("flags={}".format(",".join(self.flags)))
        if self.kind in ("print_error", "cancel_error"):
            bits.append("code={}".format(self.code))
        if self.detail:
            bits.append(self.detail)
        if self.info is not None:
            bits.append(
                "printstatus={!r} blever={!r} firmware={!r} supportfunction={}".format(
                    self.info.printstatus, self.info.blever, self.info.firmwarever, self.info.supportfunction
                )
            )
        return " ".join(bits)


def decode_payload(payload: bytes) -> Event:
    """Classify one ``MPRespondMsg`` payload the way the editor's ``V()`` does."""
    try:
        r = decode_respond(payload)
        et, data = r.eventtype, r.responddata
        if et == EventType.DEVICEINFO:
            if data:
                return Event("deviceinfo", info=decode_deviceinfo(data), payload=payload)
            return Event("other", detail="empty DEVICEINFO reply", payload=payload)
        if et == EventType.DEVICEREPORT:
            c = decode_code(data)
            num = c.info_number
            if c.code == 11 and num == 2:
                return Event("ack", payload=payload)
            if c.code == 10:
                if num == 0:
                    return Event("printed", status=0, payload=payload)
                return Event(
                    "printer_error", status=num, flags=tuple(status_flags(num)), code=c.code,
                    detail="info={!r}".format(c.info), payload=payload,
                )
            if c.code == 13 and num == 1:
                return Event("stopped", code=13, payload=payload)
            return Event("other", code=c.code, detail="DEVICEREPORT {}/{!r}".format(c.code, c.info), payload=payload)
        if et == EventType.DEVICEPRINT:
            if r.code == CODE_OK:
                return Event("ok", code=r.code, detail="DEVICEPRINT", payload=payload)
            c = decode_code(data)
            u = c.unsigned_code
            if u > RESEND_BIT:
                return Event("resend", section=u - RESEND_BIT, code=r.code, payload=payload)
            return Event(
                "print_error", code=r.code, detail="MPCodeMsg code={} info={!r}".format(c.code, c.info),
                payload=payload,
            )
        if et == EventType.CANCELPRINTING:
            if r.code == CODE_OK:
                return Event("cancel_ok", code=r.code, payload=payload)
            e = r.error
            return Event(
                "cancel_error", code=r.code,
                detail="error code={} info={!r}".format(e.code, e.info) if e else "",
                payload=payload,
            )
        if et in (EventType.SELFTEST, EventType.PAPERTYPESET) and r.code == CODE_OK:
            return Event("ok", code=r.code, detail=event_name(et), payload=payload)
        return Event("other", code=r.code, detail="{} code={}".format(event_name(et), r.code), payload=payload)
    except ProtocolError as exc:
        return Event("malformed", detail=str(exc), payload=payload)


class NotificationDecoder:
    """Feed raw 0xABF3 notification bytes in, get ``Event``s out. Keeps the
    framing state across notifications (a frame may be split)."""

    def __init__(self) -> None:
        self.unpacker = Unpacker()

    def feed(self, chunk: bytes) -> List[Event]:
        return [decode_payload(p) for p in self.unpacker.feed(chunk)]


def respond_frame(
    eventtype: int, *, code: int = 0, responddata: bytes = b"", error: Optional[CodeMsg] = None
) -> bytes:
    """Build a printer->host notification frame (for fakes and tests)."""
    return enpack(RespondMsg(int(eventtype), code, responddata, error).encode())


def report_frame(code: int, info: str) -> bytes:
    """{ev 6 DEVICEREPORT, MPCodeMsg{code, info}}: ack is (11, "2"), printed (10, "0")."""
    return respond_frame(EventType.DEVICEREPORT, responddata=CodeMsg(code, info).encode())


ACK_FRAME = report_frame(11, "2")  # 55 09 c0 6d 08 06 1a 05 08 0b 12 01 32
PRINTED_FRAME = report_frame(10, "0")  # 55 09 c0 6d 08 06 1a 05 08 0a 12 01 30


def resend_frame(section: int, code: int = 500) -> bytes:
    return respond_frame(
        EventType.DEVICEPRINT, code=code, responddata=CodeMsg(_as_int32(RESEND_BIT | section)).encode()
    )


def deviceinfo_frame(info: DeviceInfo, code: int = CODE_OK) -> bytes:
    return respond_frame(EventType.DEVICEINFO, code=code, responddata=info.encode())


# --------------------------------------------------------------------------- describing (--test)


def describe_frame(frame: bytes) -> str:
    """One-line decode of a host->printer frame."""
    payloads = unpack_all(frame)
    if len(payloads) != 1:
        return "<not a single valid frame: {} bytes>".format(len(frame))
    try:
        m = decode_send(payloads[0])
    except ProtocolError as exc:
        return "<undecodable MPSendMsg: {}>".format(exc)
    name = event_name(m.eventtype)
    if m.eventtype == EventType.DEVICEPRINT:
        p = decode_print(m.senddata)
        return (
            "DEVICEPRINT page={} sec {}/{} pkt {}/{} width={} datalength={} sectionlength={} "
            "compression={} imgdata={}B".format(
                p.page, p.indexsection, p.totalsection, p.indexpackage, p.totalpackage, p.width,
                p.datalength, p.sectionlength, p.compression, len(p.imgdata),
            )
        )
    if m.sendint:
        return "{} sendint={}".format(name, m.sendint)
    return name


def describe_job(
    pages: Sequence[BlePage],
    copies: int,
    writes: Sequence[Write],
    *,
    per_size: int = PER_SIZE,
    preview_bytes: int = 24,
    max_write_lines: int = 200,
) -> str:
    """Human-readable dump of a Bluetooth job for ``--test``: page geometry,
    sections, packets, then every planned write (elided in the middle past
    ``max_write_lines``)."""
    sends, expected = plan_sends(pages, copies)
    lines = [
        "Bluetooth (BLE) job -- dry run, nothing sent. Service {} (0x{}), data -> 0x{}, "
        "control -> 0x{}, notify <- 0x{}".format(
            SERVICE_UUID, short_uuid(SERVICE_UUID), short_uuid(DATA_UUID), short_uuid(CONTROL_UUID),
            short_uuid(NOTIFY_UUID),
        ),
        "{} page(s), copies={}, {} page send(s), expecting {} 'printed' report(s); packet size {} B "
        "(the live job uses {} B if the printer reports BLE firmware 1.0.8)".format(
            len(pages), copies, len(sends), expected, per_size, PER_SIZE_BLE_1_0_8
        ),
    ]
    for i, p in enumerate(pages, 1):
        pk = [len(p.packets(s, per_size)) for s in range(1, len(p.sections) + 1)]
        lines.append(
            "page {}: {}x{} dots, {} B/row, datalength {} B, whole page heatshrinks to {} B -> r={} "
            "-> section {} B = {} rows; {} section(s), {} B compressed, {} packet(s)".format(
                i, p.width_dots, p.height, p.stride, len(p.data), p.whole_compressed_len,
                4.5 if p.small_sections else 8, p.section_len, p.rows_per_section, len(p.sections),
                p.compressed_len, sum(pk),
            )
        )
        lines.append(
            "  compressed section sizes (packets): "
            + ", ".join("{} ({})".format(len(s), n) for s, n in zip(p.sections, pk))
        )
    data_writes = sum(1 for w in writes if w.char == DATA_UUID)
    lines.append(
        "{} write(s): {} DEVICEPRINT on 0x{}, {} control on 0x{}; {} B total; largest frame {} B".format(
            len(writes), data_writes, short_uuid(DATA_UUID), len(writes) - data_writes,
            short_uuid(CONTROL_UUID), sum(len(w.frame) for w in writes),
            max((len(w.frame) for w in writes), default=0),
        )
    )
    lines.append("  #    char  len  first {} bytes hex  | decoded".format(preview_bytes))
    shown = list(enumerate(writes, 1))
    if len(shown) > max_write_lines:
        half = max_write_lines // 2
        shown = shown[:half] + [None] + shown[-half:]  # type: ignore[list-item]
    for item in shown:
        if item is None:
            lines.append("  ... ({} writes not shown) ...".format(len(writes) - 2 * (max_write_lines // 2)))
            continue
        n, w = item
        lines.append(
            "  {:<4} {:<5} {:>4}  {:<{hw}} | {}".format(
                n, short_uuid(w.char), len(w.frame), w.frame[:preview_bytes].hex(), describe_frame(w.frame),
                hw=preview_bytes * 2,
            )
        )
    return "\n".join(lines)
