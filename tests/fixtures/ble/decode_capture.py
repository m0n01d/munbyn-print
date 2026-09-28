"""Decode a capture from capture_snippet.js and diff it against our reference encoder.

Research tool (see PLANS/BLE-PROTOCOL.md, "Capture plan"). It never touches Bluetooth or a printer.

    # needs heatshrink2==0.14.0 (+ Pillow for --png / --make-png)
    python3 tests/fixtures/ble/decode_capture.py munbyn-ble-capture.json [--png out.png]
    python3 tests/fixtures/ble/decode_capture.py --make-png test_816x1216.png   # test image to print

What it does:
  1. lists each characteristic the editor used, with its GATT property flags;
  2. decodes every write and notification (framing -> MPSendMsg/MPPrintMsg/MPRespondMsg);
  3. rebuilds the page bitmap the editor sent: joins each section's imgdata, heatshrink-decompresses it
     and saves it as a PNG with --png;
  4. re-encodes that bitmap with reference_job.Job and compares every DEVICEPRINT frame byte for byte.
     This checks our encoder without needing to copy the editor's canvas scaling.
  5. prints timings: gaps between writes, write-with-response round trips, section ack latency.
"""
from __future__ import annotations

import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
rj = None  # reference_job, imported in main() so --make-png works without heatshrink2


def short(u: str) -> str:
    return "0x" + u[4:8].upper() if u.startswith("0000") and u.endswith("-0000-1000-8000-00805f9b34fb") else u


def decode(cap: dict):
    events = []
    unp = {}
    for e in cap["log"]:
        raw = bytes.fromhex(e["hex"])
        # Writes are one frame each. Notifications may split or join frames, so use one parser per char.
        payloads = list(rj.Unpacker().feed(raw)) if e["dir"] == "write" else \
            list(unp.setdefault(e["char"], rj.Unpacker()).feed(raw))
        for p in payloads:
            d = {"t": e["t"], "dir": e["dir"], "char": short(e["char"]), "frame": raw, "method": e.get("method"),
                 "doneT": e.get("doneT")}
            if e["dir"] == "write":
                s = rj.parse_send(p)
                d["event"] = s["event"]
                d["sendint"] = s["sendint"]
                if s["event"] == "DEVICEPRINT":
                    d["print"] = rj.parse_print(s["senddata"])
            else:
                r = rj.parse_respond(p)
                d["event"], d["code"] = r["event"], r["code"]
                if r["eventtype"] == 1 and r["responddata"]:
                    d["deviceinfo"] = rj.parse_deviceinfo(r["responddata"])
                elif r["responddata"]:
                    try:
                        d["codemsg"] = rj.parse_code(r["responddata"])
                    except Exception as ex:  # unknown payload: keep the bytes
                        d["raw"] = r["responddata"].hex() + f" ({ex})"
            events.append(d)
        if not payloads:
            events.append({"t": e["t"], "dir": e["dir"], "char": short(e["char"]), "frame": raw,
                           "event": "(partial frame, buffered)"})
    return events


def pages(events):
    """Split DEVICEPRINT writes into page transmissions; keep the last copy of each section."""
    runs, cur, last = [], None, None
    for d in events:
        p = d.get("print")
        if not p:
            continue
        if cur is None or (p["indexsection"] == 1 and p["indexpackage"] == 1 and last
                           and last["indexsection"] == last["totalsection"]
                           and last["indexpackage"] == last["totalpackage"]):
            cur = {"sections": {}, "frames": {}, "meta": p}
            runs.append(cur)
        s = p["indexsection"]
        if p["indexpackage"] == 1:
            cur["sections"][s], cur["frames"][s] = [], []
        cur["sections"][s].append(p["imgdata"])
        cur["frames"][s].append(d["frame"])
        last = p
    return runs


def main(argv):
    if argv[:1] == ["--make-png"]:
        return make_png(argv[1])
    global rj
    import reference_job as rj
    cap = json.load(open(argv[0]))
    png = argv[argv.index("--png") + 1] if "--png" in argv else None
    print("characteristics:")
    for u, c in cap.get("chars", {}).items():
        on = [k for k, v in c["properties"].items() if v]
        print(f"  {short(u)} service {short(c.get('service') or '?')} device {c.get('device')!r}: {', '.join(on)}")
    ev = decode(cap)
    print(f"\n{len(ev)} messages")
    for d in ev:
        if d.get("print"):
            p = d["print"]
            if p["indexpackage"] not in (1, p["totalpackage"]):
                continue
            info = (f"sec {p['indexsection']}/{p['totalsection']} pkg {p['indexpackage']}/{p['totalpackage']} "
                    f"secLen {p['sectionlength']} dataLen {p['datalength']} width {p['width']} page {p['page']} "
                    f"img {len(p['imgdata'])}" + (f" lastpage {p['lastpage']}" if p["lastpage"] else ""))
        else:
            info = " ".join(f"{k}={d[k]}" for k in ("sendint", "code", "codemsg", "deviceinfo", "raw") if d.get(k))
        rtt = f" rtt {d['doneT'] - d['t']:.1f}ms" if d.get("doneT") else ""
        print(f"{d['t']:9.1f} {d['dir']:6s} {d['char']} {d.get('method') or '':26s} {d['event']} {info}{rtt}")

    ok = True
    for i, run in enumerate(pages(ev), 1):
        m = run["meta"]
        stride = m["width"]
        comp = [b"".join(run["sections"][s]) for s in sorted(run["sections"])]
        ba = b"".join(rj.unhs(c) for c in comp)
        rows = len(ba) // stride if stride else 0
        print(f"\npage {i}: width {stride} B/row = {stride * 8} dots, {rows} rows, datalength {m['datalength']} "
              f"(rebuilt {len(ba)}), {len(comp)} sections, packet size {max(len(x) for s in run['sections'].values() for x in s)}")
        if len(ba) != m["datalength"]:
            print("  !! rebuilt length != datalength")
            ok = False
        if png:
            save_png(ba, stride, rows, png if i == 1 else png.replace(".png", f"_{i}.png"))
        per = max(len(x) for s in run["sections"].values() for x in s)
        job = rj.Job(ba, stride * 8, copies=m["page"], per_size=rj.PER_SIZE if per > rj.PER_SIZE_BLE_1_0_8 else per)
        for s in sorted(run["frames"]):
            ours, theirs = job.section_frames(s), run["frames"][s]
            same = ours == theirs
            ok &= same
            if not same:
                print(f"  section {s}: DIFFERENT ({len(theirs)} editor frames, {len(ours)} ours)")
                for a, b in zip(ours, theirs):
                    if a != b:
                        pa, pb = (rj.parse_print(rj.parse_send(list(rj.Unpacker().feed(x))[0])["senddata"]) for x in (a, b))
                        diff = {k: (pa[k] if k != "imgdata" else len(pa[k]), pb[k] if k != "imgdata" else len(pb[k]))
                                for k in pa if pa[k] != pb[k]}
                        print(f"    first differing frame: fields (ours, editor) {diff}")
                        break
        print(f"  our encoder reproduces every DEVICEPRINT frame: {all(job.section_frames(s) == run['frames'][s] for s in run['frames'])}")

    ctrl = [d for d in ev if d["dir"] == "write" and not d.get("print")]
    for d in ctrl:
        want = rj.ctrl_frame(d["event"], d.get("sendint") or 0) if d["event"] in rj.EVENT else None
        if want is not None and want != d["frame"]:
            print(f"  control frame {d['event']} differs: editor {d['frame'].hex()} ours {want.hex()}")
            ok = False
    w = [d for d in ev if d["dir"] == "write"]
    gaps = [b["t"] - a["t"] for a, b in zip(w, w[1:])]
    rtts = [d["doneT"] - d["t"] for d in w if d.get("doneT")]
    if gaps:
        print(f"\nwrite gaps ms: median {statistics.median(gaps):.1f} max {max(gaps):.1f}")
    if rtts:
        print(f"write round trip ms: median {statistics.median(rtts):.1f} max {max(rtts):.1f}")
    print("\nRESULT:", "MATCH" if ok else "DIFFERENCES FOUND")
    return 0 if ok else 1


def save_png(ba: bytes, stride: int, rows: int, path: str):
    from PIL import Image  # 1 = black in the protocol; PIL mode "1" uses 1 = white
    inv = bytes(b ^ 0xFF for b in ba[:stride * rows])
    Image.frombytes("1", (stride * 8, rows), inv).save(path)
    print(f"  editor bitmap saved to {path}")


def make_png(path: str):
    """An exact 816x1216 black/white test image (the editor's 4x6 canvas is 102x152 mm = 816x1216 dots),
    so the editor draws it at scale 1. Uses the repo's own self-test layout, run from the repo root."""
    from PIL import Image
    sys.path.insert(0, os.getcwd())
    from munbyn.labels import parse_size
    from munbyn.tspl import JobSettings, selftest_image
    img = selftest_image(JobSettings(size=parse_size("4x6"), feed_scale=1.0)).convert("1")
    canvas = Image.new("1", (816, 1216), 1)
    canvas.paste(img.crop((0, 0, min(816, img.width), min(1216, img.height))), (0, 0))
    canvas.convert("L").save(path)
    print(f"wrote {path} ({canvas.width}x{canvas.height}) from a {img.width}x{img.height} self-test")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
