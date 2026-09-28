#!/usr/bin/env python3
"""CLI entry point: print a PDF or image to a Munbyn RW403B over USB, or
over Bluetooth LE with ``--ble`` -- through the MunbynBLE bridge app
(``munbyn/ble_bridge.py``, ``scripts/install-ble-bridge.sh``), which owns the
macOS Bluetooth permission, or in-process with ``--ble-direct`` (macOS
Terminal only). See the README's Bluetooth section.

Runs under the bare macOS ``/usr/bin/python3`` (3.9): if a dependency import
fails and this project's ``.venv`` exists, it re-execs into that venv's Python
with the same argv before doing anything else.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from typing import Any, Dict, List, Optional


def _venv_python() -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, ".venv", "bin", "python")


def _reexec_into_venv_or_raise(exc: BaseException) -> None:
    # Compare prefixes, not executables: .venv/bin/python is a symlink that
    # resolves to the very same binary as /usr/bin/python3.
    venv_python = _venv_python()
    venv_dir = os.path.dirname(os.path.dirname(venv_python))
    in_venv = os.path.realpath(sys.prefix) == os.path.realpath(venv_dir)
    if os.path.exists(venv_python) and not in_venv and not os.environ.get("MUNBYN_REEXEC"):
        os.environ["MUNBYN_REEXEC"] = "1"  # never loop
        os.execv(venv_python, [venv_python, os.path.abspath(__file__)] + sys.argv[1:])
    raise exc


try:
    from munbyn import config as config_mod
    from munbyn import labels as labels_mod
    from munbyn import render as render_mod
    from munbyn import tspl as tspl_mod
    from munbyn import usb_transport
except ImportError as _import_exc:  # bare system python is missing a dependency
    _reexec_into_venv_or_raise(_import_exc)
    raise

# Config keys that both --flags and --save-defaults understand (a subset of
# munbyn.config.DEFAULTS -- things like --copies/--margin/--pages/--invert/
# --align are per-run only and are never persisted).
_FLOAT_KEYS = ("gap_mm", "gap_offset_mm", "offset_mm", "x_shift_mm", "y_shift_mm", "feed_scale",
               "ble_feed_scale")
_INT_KEYS = ("density", "speed", "direction", "threshold")
_STR_KEYS = ("size", "media", "fit", "rotate", "crop", "dither", "transport", "ble_address")

#: --ble-packet-size bounds: 16 is silly-but-valid; 479 keeps the biggest
#: DEVICEPRINT frame (packet + 33 bytes) within one 512-byte ATT write.
_BLE_PACKET_SIZE_RANGE = (16, 479)
_BLE_WRITE_MODES = ("auto", "response", "no-response")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="print_label.py",
        description="Print a PDF or image to a Munbyn RW403B thermal label printer over USB (TSPL), "
                    "or over Bluetooth LE with --ble.",
    )
    p.add_argument("files", nargs="*", metavar="FILE", help='File(s) to print; "-" reads stdin.')

    p.add_argument("--test", "--dry-run", dest="test", action="store_true",
                    help="Build the job and print a description; never touch the USB device (with --ble: "
                         "dump every Bluetooth frame, never touch Bluetooth).")
    p.add_argument("--hex", metavar="PATH", help="Also write the raw TSPL job bytes to PATH.")
    p.add_argument("--preview", metavar="PATH",
                    help="Save the rendered label as PATH.png (or PATH-1.png, PATH-2.png, ... for "
                         "multiple pages) -- shows the label as it will look on paper (physical size, "
                         "un-stretched), not the feed-scale-stretched bitmap actually sent to the "
                         "printer; see --feed-scale.")

    p.add_argument("--size", help='Label size, e.g. "4x6", "4x6in", "100x150mm", or a preset name.')
    p.add_argument("--media", choices=["gap", "bline", "continuous"])
    p.add_argument("--gap", dest="gap_mm", type=float, metavar="MM")
    p.add_argument("--gap-offset", dest="gap_offset_mm", type=float, metavar="MM")
    p.add_argument("--density", type=int)
    p.add_argument("--speed", type=int)
    p.add_argument("--direction", type=int, choices=[0, 1])
    p.add_argument("--offset", dest="offset_mm", type=float, metavar="MM", help="TSPL OFFSET (tear/peel stop).")
    p.add_argument("--x-shift", dest="x_shift_mm", type=float, metavar="MM")
    p.add_argument("--y-shift", dest="y_shift_mm", type=float, metavar="MM")
    p.add_argument("--copies", type=int)
    p.add_argument(
        "--feed-scale", dest="feed_scale", type=float, metavar="F",
        help="Compensate for this printer's mechanical feed shortfall: stretches every job's bitmap "
             "height and SIZE length by 1/F before sending (F = measured printed length / intended "
             "length along the paper feed; this printer measured 0.981 on 2026-09-27 -- see CLAUDE.md). "
             "Must be 0.9..1.1; 1.0 disables the correction. Use --scale-test to (re)calibrate it.",
    )

    p.add_argument("--fit", choices=["fit", "fill", "stretch", "actual"])
    p.add_argument("--scale", type=float, metavar="PERCENT", help='Like Preview\'s "Scale: N%%"; overrides --fit.')
    p.add_argument("--rotate", choices=["auto", "0", "90", "180", "270"])
    p.add_argument("--crop", choices=["auto", "none"])
    p.add_argument("--margin", dest="margin_mm", type=float, metavar="MM")
    p.add_argument("--align", choices=["center", "top", "top-left"])
    p.add_argument("--dither", choices=["threshold", "floyd"])
    p.add_argument("--threshold", type=int)
    p.add_argument("--invert", action="store_true", default=None)
    p.add_argument("--pages", help='Page spec, e.g. "1" or "1-3,5"; default is all pages.')
    p.add_argument("--black-is-one", dest="bitmap_black_is_one", choices=["0", "1"],
                    help="Bitmap polarity override.")

    p.add_argument("--selftest", action="store_true", help="Print the built-in alignment/polarity test label.")
    p.add_argument(
        "--scale-test", action="store_true",
        help="Print (or with --test: dry-run/--preview) a feed/x-alignment calibration label: a 100mm "
             "bar across the head (\"A across\") and a 100mm bar along the feed (\"B along feed\"), each "
             "with 10mm ticks, using the current --feed-scale. Measure B and set "
             "--feed-scale $(old*B_mm/100); measure A's left gap against its printed nominal value and "
             "set --x-shift. See the README's calibration section.",
    )
    p.add_argument("--calibrate", action="store_true",
                    help="Print the manual gap/label calibration procedure; sends nothing to the "
                         "printer (this firmware ignores TSPL's GAPDETECT -- verified on hardware).")
    p.add_argument("--feed", action="store_true",
                    help="Feed one label (prints a blank one: header+CLS+PRINT -- this firmware has "
                         "no dedicated feed command in its verified command subset).")
    p.add_argument("--status", action="store_true",
                    help="Show the device ID (a USB control request, not a TSPL command). "
                         "Add --probe to also try the TSPL status query.")
    p.add_argument("--probe", action="store_true",
                    help="With --status, also send the raw TSPL status-query byte (ESC !?). "
                         "This firmware is verified to never reply to it, and whether those "
                         "unterminated bytes are safe to send right before another job is "
                         "NOT verified -- off by default; see CLAUDE.md.")
    p.add_argument("--list", dest="list_printers", action="store_true", help="List attached printers.")
    p.add_argument("--serial", help="USB serial number, to select a specific printer.")

    ble = p.add_argument_group(
        "Bluetooth LE",
        "Print over Bluetooth instead of USB (verified on the printer 2026-09-28). By default the job goes to "
        "the MunbynBLE bridge app on 127.0.0.1 (scripts/install-ble-bridge.sh), which owns the macOS Bluetooth "
        "permission; --ble-direct talks Bluetooth from this process instead (macOS Terminal only). --test "
        "builds the frames and shows them without touching Bluetooth or the bridge. TSPL-only settings "
        "(--density, --speed, --gap, --media, --offset, --direction, --black-is-one) are not used over "
        "Bluetooth. See the README's Bluetooth section.",
    )
    ble.add_argument("--ble", action="store_true",
                     help="Use Bluetooth LE for this run: files, --selftest, --scale-test, --feed, --status. "
                          "Goes through the MunbynBLE bridge (the printer address is the config's ble_address).")
    ble.add_argument("--ble-direct", action="store_true",
                     help="Like --ble, but open Bluetooth in this process instead of using the bridge. Only from "
                          "an app with the Bluetooth permission (macOS Terminal); macOS kills the process when run "
                          "from anything else (e.g. the Claude app). --ble-scan, --ble-printer-selftest and "
                          "--ble-density/--ble-speed always run in-process.")
    ble.add_argument("--usb", action="store_true",
                     help='Use USB for this run even if the config\'s "transport" is "ble".')
    ble.add_argument("--ble-address", metavar="ADDR",
                     help="The printer's Bluetooth address (a UUID on macOS, from --ble-scan); implies --ble. "
                          "Default: scan for a printer whose name starts with RW403B.")
    ble.add_argument("--ble-scan", action="store_true",
                     help="List nearby RW403B printers with signal strength (RSSI), then exit.")
    ble.add_argument("--ble-scan-timeout", type=float, metavar="S",
                     help="Seconds to scan, for --ble-scan and for finding the printer (default 10).")
    ble.add_argument("--ble-feed-scale", dest="ble_feed_scale", type=float, metavar="F",
                     help="feed_scale for Bluetooth jobs (config ble_feed_scale; default 0.981, the value "
                          "measured over USB -- UNVERIFIED over Bluetooth, re-measure with --scale-test --ble). "
                          "With --ble, a plain --feed-scale also works for one run.")
    ble.add_argument("--ble-packet-size", type=int, metavar="N",
                     help="Compressed bytes per print packet (default 400, or 148 when the printer reports "
                          "BLE firmware 1.0.8). Use 148 if a write fails as too large for the connection.")
    ble.add_argument("--ble-write", choices=_BLE_WRITE_MODES, default="auto",
                     help="GATT write type. auto (default) = with response when the characteristic "
                          "allows it, as Chrome does for the editor.")
    ble.add_argument("--ble-density", type=int, metavar="1-16",
                     help="Opt-in: set the printer's density (PRINTINCONCENTRATION, the editor's 1-16 scale) "
                          "before the job. Off by default: the printer is believed to store it, and how it "
                          "maps to TSPL DENSITY is unknown.")
    ble.add_argument("--ble-speed", type=int, metavar="1-8",
                     help="Opt-in: set the printer's speed (PRINTINGSPEED, 1-8) before the job; see --ble-density.")
    ble.add_argument("--ble-printer-selftest", action="store_true",
                     help="Ask the printer to print its OWN self-test page (the SELFTEST message) over "
                          "Bluetooth; not this tool's --selftest label.")

    p.add_argument("--save-defaults", action="store_true",
                    help="Save the settings given on this command line as defaults, then continue "
                         "(--ble/--usb save \"transport\", --ble-address saves the address).")
    p.add_argument("--debug", action="store_true",
                   help="Show full tracebacks on error. With --ble, also log every Bluetooth frame in hex.")
    return p


def _cli_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    """Settings explicitly given on the command line, as config-shaped keys."""
    overrides: Dict[str, Any] = {}
    direct = {
        "size": args.size,
        "media": args.media,
        "gap_mm": args.gap_mm,
        "gap_offset_mm": args.gap_offset_mm,
        "density": args.density,
        "speed": args.speed,
        "direction": args.direction,
        "offset_mm": args.offset_mm,
        "x_shift_mm": args.x_shift_mm,
        "y_shift_mm": args.y_shift_mm,
        "feed_scale": args.feed_scale,
        "ble_feed_scale": args.ble_feed_scale,
        "ble_address": args.ble_address,
        "fit": args.fit,
        "rotate": args.rotate,
        "crop": args.crop,
        "dither": args.dither,
        "threshold": args.threshold,
    }
    for key, value in direct.items():
        if value is not None:
            overrides[key] = value
    if args.bitmap_black_is_one is not None:
        overrides["bitmap_black_is_one"] = args.bitmap_black_is_one == "1"
    if args.ble or args.ble_direct:
        overrides["transport"] = "ble"
    elif args.usb:
        overrides["transport"] = "usb"
    return overrides


def _use_ble(args: argparse.Namespace, settings: Dict[str, Any]) -> bool:
    """Bluetooth for this run? Explicit flags win, then the config's transport."""
    if args.usb:
        return False
    if args.ble or args.ble_direct or args.ble_address or args.ble_scan or args.ble_printer_selftest:
        return True
    return str(settings.get("transport") or "usb").lower() == "ble"


def _ble_feed_scale(args: argparse.Namespace, settings: Dict[str, Any]) -> float:
    """feed_scale for a Bluetooth run: --ble-feed-scale, else --feed-scale,
    else config ble_feed_scale (falling back to feed_scale if that is unset)."""
    if args.ble_feed_scale is not None:
        return float(args.ble_feed_scale)
    if args.feed_scale is not None:
        return float(args.feed_scale)
    value = settings.get("ble_feed_scale")
    return float(value if value is not None else settings["feed_scale"])


def _job_settings(settings: Dict[str, Any], size: "labels_mod.LabelSize", copies: int) -> "tspl_mod.JobSettings":
    return tspl_mod.JobSettings(
        size=size,
        media=settings["media"],
        gap_mm=float(settings["gap_mm"]),
        gap_offset_mm=float(settings["gap_offset_mm"]),
        density=int(settings["density"]),
        speed=int(settings["speed"]),
        direction=int(settings["direction"]),
        offset_mm=float(settings["offset_mm"]),
        x_shift_mm=float(settings["x_shift_mm"]),
        y_shift_mm=float(settings["y_shift_mm"]),
        copies=copies,
        bitmap_black_is_one=bool(settings["bitmap_black_is_one"]),
        feed_scale=float(settings["feed_scale"]),
    )


def _render_options(settings: Dict[str, Any], args: argparse.Namespace) -> "render_mod.RenderOptions":
    return render_mod.RenderOptions(
        fit=settings["fit"],
        scale=args.scale,
        rotate=settings["rotate"],
        crop=settings["crop"],
        margin_mm=args.margin_mm if args.margin_mm is not None else 0.0,
        align=args.align if args.align is not None else "center",
        dither=settings["dither"],
        threshold=int(settings["threshold"]),
        invert=bool(args.invert) if args.invert is not None else False,
        pages=args.pages,
        feed_scale=float(settings["feed_scale"]),
    )


def _write_hex(path: str, job: bytes) -> None:
    with open(path, "wb") as f:
        f.write(job)


def _save_previews(path: str, pages: List[Any]) -> None:
    base = path[: -len(".png")] if path.lower().endswith(".png") else path
    if len(pages) == 1:
        pages[0].save(base + ".png")
    else:
        for i, img in enumerate(pages, start=1):
            img.save(f"{base}-{i}.png")


def _setup_ble_logging(debug: bool) -> None:
    """Progress (INFO) on stderr for every Bluetooth run; with --debug every
    frame in hex, timestamped, so a first hardware run is diagnosable."""
    import logging

    logger = logging.getLogger("munbyn.ble")
    handler = getattr(logger, "_munbyn_cli_handler", None)
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        logger.addHandler(handler)
        logger._munbyn_cli_handler = handler  # type: ignore[attr-defined]
    if debug:
        handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d ble %(levelname).1s %(message)s", "%H:%M:%S"))
        logger.setLevel(logging.DEBUG)
    else:
        handler.setFormatter(logging.Formatter("ble: %(message)s"))
        logger.setLevel(logging.INFO)


def _check_ble_args(args: argparse.Namespace) -> None:
    """Range-check the Bluetooth-only flags (raises ValueError)."""
    if (args.ble or args.ble_direct) and args.usb:
        raise ValueError("--ble/--ble-direct and --usb can't both be given")
    lo, hi = _BLE_PACKET_SIZE_RANGE
    if args.ble_packet_size is not None and not lo <= args.ble_packet_size <= hi:
        raise ValueError("--ble-packet-size must be {}..{}, not {}".format(lo, hi, args.ble_packet_size))
    if args.ble_density is not None and not 1 <= args.ble_density <= 16:
        raise ValueError("--ble-density must be 1..16 (the editor's scale), not {}".format(args.ble_density))
    if args.ble_speed is not None and not 1 <= args.ble_speed <= 8:
        raise ValueError("--ble-speed must be 1..8, not {}".format(args.ble_speed))
    if args.ble_scan_timeout is not None and not 0 < args.ble_scan_timeout <= 120:
        raise ValueError("--ble-scan-timeout must be between 0 and 120 seconds")


def _warn_tspl_only_flags(args: argparse.Namespace) -> None:
    ignored = [flag for flag, value in (
        ("--density", args.density), ("--speed", args.speed), ("--black-is-one", args.bitmap_black_is_one),
        ("--media", args.media), ("--gap", args.gap_mm), ("--gap-offset", args.gap_offset_mm),
        ("--offset", args.offset_mm), ("--direction", args.direction),
    ) if value is not None]
    if ignored:
        print(
            "note: {} {} TSPL-only and not sent over Bluetooth (see --ble-density/--ble-speed).".format(
                ", ".join(ignored), "is" if len(ignored) == 1 else "are"
            ),
            file=sys.stderr,
        )


def _ble_options(args: argparse.Namespace, settings: Dict[str, Any]) -> Any:
    from munbyn import ble_transport

    return ble_transport.BleOptions(
        address=args.ble_address or settings.get("ble_address") or None,
        scan_timeout=args.ble_scan_timeout or 10.0,
        per_size=args.ble_packet_size,
        write_mode=args.ble_write,
        density=args.ble_density,
        speed=args.ble_speed,
    )


def _finish_ble(
    args: argparse.Namespace,
    settings: Dict[str, Any],
    job_settings: "tspl_mod.JobSettings",
    images: List[Any],
    what: str,
) -> int:
    """Bluetooth tail for selftest/scale-test/feed/print: build the frames,
    then --hex, --test (a frame dump, no Bluetooth), or send."""
    from munbyn import ble_protocol as bp

    _warn_tspl_only_flags(args)
    pages = [
        bp.BlePage.from_image(
            bp.compose_page(
                img, x_shift_mm=job_settings.x_shift_mm, y_shift_mm=job_settings.y_shift_mm,
                feed_scale=job_settings.feed_scale,
            )
        )
        for img in images
    ]
    per_size = args.ble_packet_size or bp.PER_SIZE
    writes = bp.planned_writes(
        pages, job_settings.copies, per_size=per_size, density=args.ble_density, speed=args.ble_speed
    )
    if args.hex:
        _write_hex(args.hex, b"".join(w.frame for w in writes))
    if args.test:
        print("feed_scale (Bluetooth): {}".format(job_settings.feed_scale))
        print(bp.describe_job(pages, job_settings.copies, writes, per_size=per_size))
        print("(dry run) a real run would {}; not touching Bluetooth or the bridge.".format(
            "open Bluetooth in this process (--ble-direct)" if args.ble_direct else
            "send this job as TSPL to the MunbynBLE bridge on 127.0.0.1:{}".format(_bridge_port_or_default(settings))
        ))
        return 0
    if not args.ble_direct:
        return _send_via_bridge(args, settings, job_settings, images, what)

    from munbyn import ble_transport

    _setup_ble_logging(args.debug)
    result = ble_transport.print_job(
        pages, job_settings.copies, _ble_options(args, settings), label_height_mm=job_settings.size.height_mm
    )
    print(
        "{} over Bluetooth: {} page(s) x {} cop{}, {} writes / {} bytes, {} resend(s), printer reported "
        "{} of {} printed, {:.1f} s.".format(
            what, result.pages, result.copies, "y" if result.copies == 1 else "ies", result.writes,
            result.bytes_sent, result.resends, result.printed, result.expected, result.seconds,
        )
    )
    return 0


def _bridge_port_or_default(settings: Dict[str, Any]) -> Any:
    from munbyn import ble_bridge_client as bc

    try:
        return bc.bridge_port(settings)
    except ValueError:
        return settings.get("ble_bridge_port")


def _check_bridge_args(args: argparse.Namespace, what: str) -> None:
    """Flags the bridge can't honour: refuse the opt-in printer settings (they
    write to the printer), note the per-run Bluetooth knobs it ignores."""
    if args.ble_density is not None or args.ble_speed is not None:
        raise ValueError(
            "--ble-density/--ble-speed change the printer's stored settings and don't go through the MunbynBLE "
            "bridge; run {} from macOS Terminal with --ble-direct instead".format(what)
        )
    ignored = [flag for flag, on in (
        ("--ble-packet-size", args.ble_packet_size is not None), ("--ble-write", args.ble_write != "auto"),
        ("--ble-scan-timeout", args.ble_scan_timeout is not None),
    ) if on]
    if ignored:
        print("note: {} only appl{} with --ble-direct; the bridge uses its defaults.".format(
            ", ".join(ignored), "ies" if len(ignored) == 1 else "y"), file=sys.stderr)


def _bridge_status(args: argparse.Namespace, settings: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
    """The bridge's status (raises BridgeUnavailable with the install hint),
    plus a note when --ble-address differs from the address the bridge uses."""
    from munbyn import ble_bridge_client as bc

    port = bc.bridge_port(settings)
    reply = bc.status(port, **kw)
    if args.ble_address and reply.get("printer_address") != args.ble_address:
        print(
            "note: --ble-address is only used with --ble-direct; the bridge prints to the config's ble_address "
            "({}). Save a new one with --ble-address ADDR --save-defaults.".format(
                reply.get("printer_address") or "unset"),
            file=sys.stderr,
        )
    return reply


def _send_via_bridge(
    args: argparse.Namespace,
    settings: Dict[str, Any],
    job_settings: "tspl_mod.JobSettings",
    images: List[Any],
    what: str,
) -> int:
    """Bluetooth through the MunbynBLE bridge: the same pages --ble-direct
    would send, as a TSPL job (ble_bridge_client.build_bridge_job)."""
    from munbyn import ble_bridge_client as bc

    _check_bridge_args(args, "this")
    port = bc.bridge_port(settings)
    _bridge_status(args, settings)  # is it our bridge? (never send a job to a stranger on the port)
    job = bc.build_bridge_job(job_settings, images)
    labels = len(images) * job_settings.copies
    print("Sending {} page(s) x {} to the MunbynBLE bridge (127.0.0.1:{}); waiting for the printer...".format(
        len(images), job_settings.copies, port), file=sys.stderr)
    reply = bc.send_job(port, job, timeout=bc.job_timeout(labels))
    print(
        "{} over Bluetooth via the MunbynBLE bridge (job #{}): {} page(s) x {} cop{}, printer reported {} of {} "
        "printed, {} attempt(s), {:.1f} s.".format(
            what, reply.get("job"), reply.get("pages", len(images)), job_settings.copies,
            "y" if job_settings.copies == 1 else "ies", reply.get("printed", "?"), reply.get("expected", "?"),
            reply.get("attempts", 1), float(reply.get("seconds") or 0.0),
        )
    )
    return 0


def _finish_job(args: argparse.Namespace, job: bytes) -> int:
    """Shared tail for selftest/calibrate/feed/print: --hex, --test, or send."""
    if args.hex:
        _write_hex(args.hex, job)
    if args.test:
        print(tspl_mod.describe(job))
        return 0
    with usb_transport.Printer(serial=args.serial) as printer:
        printer.write(job)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    if args.test:
        print("(dry run) --list would enumerate attached USB printers; not touching USB.")
        return 0
    printers = usb_transport.find_printers()
    if not printers:
        print("No Munbyn printers found.")
        return 0
    for p in printers:
        print(
            "serial={serial} bus={bus} address={address} product={product} device_id={device_id}".format(
                serial=p.get("serial"),
                bus=p.get("bus"),
                address=p.get("address"),
                product=p.get("product"),
                device_id=p.get("device_id"),
            )
        )
    return 0


def _format_deviceinfo(info: Any) -> str:
    flags = info.printstatus_flags
    return "\n".join([
        "status: {} (printstatus {!r})".format(", ".join(flags) if flags else "ready", info.printstatus),
        "firmware: {!r}  BLE firmware: {!r}  protocol: {}".format(info.firmwarever, info.blever, info.protocol),
        "density (editor scale 1-16): {}  speed (1-8): {}".format(info.concentration, info.speed),
        "battery/elec: {}  paper status: {}  paper size: {}  paper type: {}  close time: {}".format(
            info.elec, info.paperstatus, info.papersize, info.papertype, info.closetime),
        "supportfunction: {} (can resend: {}, send while printing: {})  -> packet size {}".format(
            info.supportfunction, "yes" if info.can_resend else "no",
            "yes" if info.support_send_when_printing else "no", info.per_size),
        "mac: {!r}  sn: {!r}  mfr: {!r}  eeid: {}".format(info.mac, info.sn, info.mfr, info.eeid),
    ])


def _cmd_ble_status(args: argparse.Namespace, settings: Dict[str, Any]) -> int:
    from munbyn import ble_protocol as bp

    if args.test:
        extra = []
        if args.ble_density is not None:
            extra.append("PRINTINCONCENTRATION={} ({})".format(args.ble_density, bp.density_frame(args.ble_density).hex()))
        if args.ble_speed is not None:
            extra.append("PRINTINGSPEED={} ({})".format(args.ble_speed, bp.speed_frame(args.ble_speed).hex()))
        print(
            "(dry run) --status --ble would {} and send DEVICEINFO ({} on 0x{}){}; "
            "not touching Bluetooth.".format(
                "connect over Bluetooth (--ble-direct)" if args.ble_direct else
                "ask the MunbynBLE bridge on 127.0.0.1:{} to connect over Bluetooth".format(
                    _bridge_port_or_default(settings)),
                bp.DEVICEINFO_FRAME.hex(), bp.short_uuid(bp.CONTROL_UUID),
                (", then " + ", ".join(extra) + " and DEVICEINFO again") if extra else "",
            )
        )
        return 0
    if not args.ble_direct:
        return _ble_status_via_bridge(args, settings)
    from munbyn import ble_transport

    _setup_ble_logging(args.debug)
    info = ble_transport.query_device_info(_ble_options(args, settings))
    print(_format_deviceinfo(info))
    return 0


def _ble_status_via_bridge(args: argparse.Namespace, settings: Dict[str, Any]) -> int:
    import dataclasses as dc

    from munbyn import ble_bridge_client as bc
    from munbyn import ble_protocol as bp

    _check_bridge_args(args, "--status")
    reply = _bridge_status(args, settings, deviceinfo=True, timeout=90.0)
    print(bc.describe_status(reply, bc.bridge_port(settings)))
    info = reply.get("deviceinfo")
    if not isinstance(info, dict):
        print("error: the bridge could not get DEVICEINFO from the printer: {}".format(
            reply.get("deviceinfo_error") or "no reply"), file=sys.stderr)
        return 2
    names = {f.name for f in dc.fields(bp.DeviceInfo)}
    print(_format_deviceinfo(bp.DeviceInfo(**{k: v for k, v in info.items() if k in names})))
    return 0


def _cmd_ble_scan(args: argparse.Namespace) -> int:
    from munbyn import ble_protocol as bp

    timeout = args.ble_scan_timeout or 10.0
    if args.test:
        print("(dry run) --ble-scan would scan {:.0f} s for Bluetooth printers named {}*; not touching "
              "Bluetooth.".format(timeout, bp.NAME_PREFIX))
        return 0
    from munbyn import ble_transport

    _setup_ble_logging(args.debug)
    res = ble_transport.scan(timeout)
    if not res["printers"]:
        print(
            "No Bluetooth printers named {}* found in {:.0f} s ({} other device(s) seen). Is it on and "
            "not connected to the phone app or Chrome? If macOS never asked for Bluetooth permission, see "
            "the README's Bluetooth section.".format(bp.NAME_PREFIX, timeout, res["others"])
        )
        return 1
    for p in res["printers"]:
        print("{}  {}  RSSI {} dBm".format(p["address"], p["name"], p["rssi"]))
    print("Use one with --ble --ble-address ADDR (add --save-defaults to remember both).")
    return 0


def _cmd_ble_printer_selftest(args: argparse.Namespace, settings: Dict[str, Any]) -> int:
    from munbyn import ble_protocol as bp

    if args.test:
        print("(dry run) --ble-printer-selftest would send DEVICEINFO then SELFTEST ({} on 0x{}); not "
              "touching Bluetooth.".format(bp.SELFTEST_FRAME.hex(), bp.short_uuid(bp.CONTROL_UUID)))
        return 0
    from munbyn import ble_transport

    _setup_ble_logging(args.debug)
    ok = ble_transport.printer_selftest(_ble_options(args, settings))
    print("Printer self-test requested ({}).".format("printer confirmed" if ok else "no confirmation from the printer"))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    if args.test:
        print("(dry run) --status would check the printer over USB; not touching USB.")
        return 0
    with usb_transport.Printer(serial=args.serial) as printer:
        device_id = printer.device_id()
        raw = printer.query_status() if args.probe else None
    print(f"device: {device_id}")
    if not args.probe:
        print(
            "status: not queried (the TSPL status query is not sent by default -- "
            "pass --probe to send it; see CLAUDE.md)"
        )
    elif raw is None:
        print(
            "status: no reply (this firmware does not answer TSPL status queries -- "
            "verified on hardware 2026-09-27; expected, not an error)"
        )
    else:
        flags = tspl_mod.decode_status(raw)
        print(
            f"status: 0x{raw:02x} ({', '.join(flags) if flags else 'ready'}) "
            "-- unexpected: a status reply came back this time"
        )
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    # Nothing is ever sent to the printer for calibration -- it's a manual,
    # physical procedure (GAPDETECT was verified on hardware to be ignored).
    print(tspl_mod.calibrate_instructions())
    return 0


def _cmd_feed(
    args: argparse.Namespace, settings: Dict[str, Any], job_settings: "tspl_mod.JobSettings", use_ble: bool
) -> int:
    if use_ble:
        from PIL import Image

        height = labels_mod.stretched_height_dots(job_settings.size.height_dots, job_settings.feed_scale)
        blank = Image.new("1", (job_settings.size.width_dots, height), 255)
        return _finish_ble(args, settings, job_settings, [blank], "Fed one (blank) label")
    job = tspl_mod.feed_job(job_settings)
    rc = _finish_job(args, job)
    if rc == 0 and not args.test:
        print("Fed one (blank) label.")
    return rc


def _unstretched(job_settings: "tspl_mod.JobSettings") -> "tspl_mod.JobSettings":
    """``job_settings`` with ``feed_scale`` forced to 1.0, for previews that
    should show the label as it will look on paper (physical size) rather
    than the feed-scale-stretched bitmap actually sent -- see --preview's
    help text."""
    if job_settings.feed_scale == 1.0:
        return job_settings
    return dataclasses.replace(job_settings, feed_scale=1.0)


def _cmd_selftest(
    args: argparse.Namespace, settings: Dict[str, Any], job_settings: "tspl_mod.JobSettings", use_ble: bool
) -> int:
    if args.preview:
        # The self-test is a single BITMAP now, so (feed_scale aside) the
        # preview is the exact image sent, not an approximation.
        _save_previews(args.preview, [tspl_mod.selftest_image(_unstretched(job_settings))])
    if use_ble:
        return _finish_ble(args, settings, job_settings, [tspl_mod.selftest_image(job_settings)],
                           "Self-test label sent")
    job = tspl_mod.selftest_job(job_settings)
    rc = _finish_job(args, job)
    if rc == 0 and not args.test:
        print("Self-test label sent.")
    return rc


def _cmd_scale_test(
    args: argparse.Namespace, settings: Dict[str, Any], job_settings: "tspl_mod.JobSettings", use_ble: bool
) -> int:
    if args.preview:
        _save_previews(args.preview, [tspl_mod.scale_test_image(_unstretched(job_settings))])
    if use_ble:
        return _finish_ble(args, settings, job_settings, [tspl_mod.scale_test_image(job_settings)],
                           "Scale-test label sent")
    job = tspl_mod.scale_test_job(job_settings)
    rc = _finish_job(args, job)
    if rc == 0 and not args.test:
        print("Scale-test label sent.")
    return rc


def _cmd_print_files(
    args: argparse.Namespace, settings: Dict[str, Any], job_settings: "tspl_mod.JobSettings", use_ble: bool = False
) -> int:
    opts = _render_options(settings, args)
    all_pages: List[Any] = []
    # Read each source's bytes exactly once (render_mod.load_bytes), then pass
    # those bytes -- not the path/"-" again -- to every render_file() call for
    # it. A file path is cheap to re-read, but "-" is stdin: reading it twice
    # (once for the stretched render below, once more for an un-stretched
    # --preview render) would see EOF on the second read and abort the whole
    # job with "error: empty input" -- a regression --preview with feed_scale
    # != 1.0 (the new default) used to hit even for a real (non --test) print.
    sources: List[Any] = []  # (data: bytes, filename: Optional[str])
    for path in args.files:
        data = render_mod.load_bytes(path)
        filename = None if path == "-" else path
        sources.append((data, filename))
        all_pages.extend(render_mod.render_file(data, job_settings.size, opts, filename=filename))

    if not all_pages:
        print("error: nothing to print (no pages rendered)", file=sys.stderr)
        return 1

    if args.preview:
        # Show the label as it will look on paper (physical size), not the
        # feed-scale-stretched bitmap actually sent -- see --preview's help
        # text. Only re-renders when feed_scale actually changes anything.
        if opts.feed_scale == 1.0:
            preview_pages = all_pages
        else:
            preview_opts = dataclasses.replace(opts, feed_scale=1.0)
            preview_pages = []
            for data, filename in sources:
                preview_pages.extend(
                    render_mod.render_file(data, job_settings.size, preview_opts, filename=filename)
                )
        _save_previews(args.preview, preview_pages)

    if use_ble:
        return _finish_ble(args, settings, job_settings, all_pages, "Sent {} page(s)".format(len(all_pages)))

    job = tspl_mod.build_job(job_settings, all_pages)

    if args.hex:
        _write_hex(args.hex, job)

    if args.test:
        print(tspl_mod.describe(job))
        return 0

    with usb_transport.Printer(serial=args.serial) as printer:
        printer.write(job)
    print(f"Sent {len(all_pages)} page(s), {len(job)} bytes, to the printer.")
    return 0


def _run(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    overrides = _cli_overrides(args)
    settings = dict(cfg)
    settings.update(overrides)
    use_ble = _use_ble(args, settings)

    # Validate the Bluetooth flags *before* anything is saved: an invalid or
    # conflicting combination (e.g. --ble --usb) must not still land in the
    # config file just because --save-defaults was also on the line.
    try:
        _check_ble_args(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.save_defaults and overrides:
        if use_ble and "feed_scale" in overrides and args.ble_feed_scale is None:
            # A plain --feed-scale works for one Bluetooth run (see
            # --ble-feed-scale's help), but saving it here would silently
            # overwrite the hardware-verified USB feed_scale with an
            # unverified Bluetooth measurement.
            print(
                "error: --feed-scale --ble --save-defaults would overwrite the USB feed_scale "
                "(hardware-verified) with a Bluetooth measurement; use --ble-feed-scale --save-defaults "
                "instead, or drop --save-defaults to use --feed-scale for this Bluetooth run only.",
                file=sys.stderr,
            )
            return 1
        for key in ("feed_scale", "ble_feed_scale"):
            if key in overrides:
                # Validate before saving: a typo here (e.g. a stray percentage
                # like 98 instead of 0.98) used to be written straight to disk,
                # breaking every later run (CLI and web UI both load it as the
                # default) until it was noticed and overwritten.
                try:
                    labels_mod.validate_feed_scale(overrides[key])
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
        config_mod.save(overrides)

    if use_ble:
        # Bluetooth jobs have their own (so far unverified) feed correction.
        try:
            settings["feed_scale"] = _ble_feed_scale(args, settings)
            labels_mod.validate_feed_scale(settings["feed_scale"])
        except (TypeError, ValueError) as exc:
            print(f"error: Bluetooth feed scale: {exc}", file=sys.stderr)
            return 1

    try:
        size = labels_mod.parse_size(str(settings["size"]))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    copies = args.copies if args.copies is not None else 1
    if copies < 1:
        print("error: --copies must be at least 1", file=sys.stderr)
        return 1
    job_settings = _job_settings(settings, size, copies)

    try:
        if args.ble_scan:
            return _cmd_ble_scan(args)
        if args.list_printers:
            if args.ble:
                print("error: --list is for USB printers; use --ble-scan to list Bluetooth ones",
                      file=sys.stderr)
                return 1
            return _cmd_list(args)
        if args.status:
            return _cmd_ble_status(args, settings) if use_ble else _cmd_status(args)
        if args.calibrate:
            return _cmd_calibrate(args)
        if args.ble_printer_selftest:
            return _cmd_ble_printer_selftest(args, settings)
        if args.feed:
            return _cmd_feed(args, settings, job_settings, use_ble)
        if args.selftest:
            return _cmd_selftest(args, settings, job_settings, use_ble)
        if args.scale_test:
            return _cmd_scale_test(args, settings, job_settings, use_ble)
        if args.files:
            return _cmd_print_files(args, settings, job_settings, use_ble)
        print(
            "error: no file given (or use --selftest/--scale-test/--calibrate/--feed/--status/--list"
            "/--ble-scan)",
            file=sys.stderr,
        )
        return 1
    except usb_transport.PrinterError as exc:
        if type(exc).__name__ == "BleCancelled":  # Ctrl-C during a Bluetooth job, handled cleanly
            print(str(exc), file=sys.stderr)
            return 130
        print(f"error: {exc}", file=sys.stderr)
        if args.debug:
            raise
        return 2
    except render_mod.RenderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.debug:
            raise
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.debug:
            raise
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.debug:
            raise
        return 1
    except Exception as exc:  # e.g. a raw usb.core.USBError escaping the transport
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.debug:
            raise
        return 2 if type(exc).__module__.startswith("usb") else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
