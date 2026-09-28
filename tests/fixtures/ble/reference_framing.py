"""Reference frame codec for the RW403B Bluetooth protocol (research artifact, NOT wired into munbyn/).

Our own re-implementation of the framing the Munbyn web editor uses (editor.munbyn.com,
static/js/app.e3efa380.js, webpack module 4476, enpack()/unpack()). Checked byte-for-byte against
that JS on 7 payloads (lengths 2..1156) on 2026-09-27; see PLANS/BLE-PROTOCOL.md section 3.
Nothing here has been run against a real printer.

Frame = 0x55 | len_lo | len_hi(6 bits) + 2 header-check bits | 2 header-check bits + 6 payload-check bits | payload

   x  = 0x55 ^ len_lo ^ len_hi            header XOR, before the check bits are folded in
   b2 = len_hi | ((x & 0x0C) << 4)         x bits 2,3 -> b2 bits 6,7
   b3 = ((x >> 4) & 0x03) | (X & 0xFC)     x bits 4,5 -> b3 bits 0,1;
                                           X = x ^ XOR(all payload bytes), bits 2..7 -> b3 bits 2..7
Payload length is 14 bits. The editor's receiver rejects frames longer than 1156 bytes.

    python3 tests/fixtures/ble/reference_framing.py     # self-check against known frames
"""
from __future__ import annotations

STX = 0x55
MAX_PACK_SIZE = 1156  # receiver-side limit in the editor's parser (_MAX_PACK_SIZE)


def enpack(payload: bytes) -> bytes:
    n = len(payload)
    if n > 0x3FFF:
        raise ValueError("payload longer than the 14-bit length field")
    b1, b2 = n & 0xFF, (n >> 8) & 0xFF
    x = STX ^ b1 ^ b2
    b2 |= (x & 0x0C) << 4
    b3 = (x >> 4) & 0x03
    for c in payload:
        x ^= c
    b3 |= x & 0xFC
    return bytes([STX, b1, b2 & 0xFF, b3]) + bytes(payload)


class Unpacker:
    """Byte-at-a-time state machine with the same states and decisions as the editor's _decode().

    feed() may be given any split of the incoming byte stream (one BLE notification may hold part
    of a frame or several frames). Unlike the JS, a zero-length frame yields b"" instead of leaving
    the parser stuck in state 4.
    """

    def __init__(self):
        self.state = 0
        self.calc = 0
        self.check = 0
        self.flen = 0
        self.cnt = 0
        self.data = bytearray()

    def feed(self, chunk: bytes):
        for b in chunk:
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
                    continue
                self.data = bytearray()
                self.cnt = 0
                self.state = 3
            elif s == 3:
                t = ((b & 3) << 4) | ((self.flen & 0xC000) >> 12)
                self.check = b
                if t != (self.calc & 0x3C):
                    self.state = 0
                    continue
                self.flen &= 0x3FFF
                self.state = 4
                if self.flen == 0:
                    self.state = 0
                    if (self.calc & 0xFC) == (self.check & 0xFC):
                        yield b""
            elif s == 4:
                self.data.append(b)
                self.cnt += 1
                self.calc ^= b
                if self.cnt == self.flen:
                    self.state = 0
                    if (self.calc & 0xFC) == (self.check & 0xFC):
                        yield bytes(self.data)
            else:
                self.state = 0


def unpack_one(frame: bytes):
    out = list(Unpacker().feed(frame))
    return out[0] if out else None


# Frames produced by the editor's own enpack() (see golden_*.json and PLANS/BLE-PROTOCOL.md).
KNOWN = {
    "0801": "5502405d0801",  # DEVICEINFO
    "080a": "55024055080a",  # PRINTINEND
    "0805": "550240590805",  # CANCELPRINTING
    "0802": "5502405d0802",  # SELFTEST
    "08092010": "5504006108092010",  # PRINTINCONCENTRATION sendint=8
    "08082008": "5504007908082008",  # PRINTINGSPEED sendint=4
    "08061a05080b120132": "5509c06d08061a05080b120132",  # notify: section ack {6, {11,"2"}}
}

if __name__ == "__main__":
    ok = True
    for payload, frame in KNOWN.items():
        got = enpack(bytes.fromhex(payload)).hex()
        rt = unpack_one(bytes.fromhex(frame)) == bytes.fromhex(payload)
        ok &= got == frame and rt
        print(f"{payload:20s} -> {got:28s} {'ok' if got == frame and rt else 'MISMATCH'}")
    bad = bytearray(bytes.fromhex(KNOWN["0801"]))
    bad[-1] ^= 0x10
    rejected = unpack_one(bytes(bad)) is None
    ok &= rejected
    print("corrupted frame rejected:", rejected)
    split = list(Unpacker().feed(bytes.fromhex(KNOWN["0801"] + KNOWN["080a"])))
    ok &= split == [b"\x08\x01", b"\x08\x0a"]
    print("two frames in one chunk:", split)
    print("ALL OK" if ok else "FAILED")
    raise SystemExit(0 if ok else 1)
