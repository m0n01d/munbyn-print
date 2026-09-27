#!/usr/bin/env python3
"""CLI entry point: print a PDF or image to a Munbyn RW403B over USB.

Runs under the bare macOS ``/usr/bin/python3`` (3.9): if a dependency import
fails and this project's ``.venv`` exists, it re-execs into that venv's Python
with the same argv before doing anything else.
"""
from __future__ import annotations

import argparse
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
_FLOAT_KEYS = ("gap_mm", "gap_offset_mm", "offset_mm", "x_shift_mm", "y_shift_mm")
_INT_KEYS = ("density", "speed", "direction", "threshold")
_STR_KEYS = ("size", "media", "fit", "rotate", "crop", "dither")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="print_label.py",
        description="Print a PDF or image to a Munbyn RW403B thermal label printer over USB (TSPL).",
    )
    p.add_argument("files", nargs="*", metavar="FILE", help='File(s) to print; "-" reads stdin.')

    p.add_argument("--test", "--dry-run", dest="test", action="store_true",
                    help="Build the job and print a description; never touch the USB device.")
    p.add_argument("--hex", metavar="PATH", help="Also write the raw TSPL job bytes to PATH.")
    p.add_argument("--preview", metavar="PATH",
                    help="Save the rendered label as PATH.png (or PATH-1.png, PATH-2.png, ... for multiple pages).")

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
    p.add_argument("--calibrate", action="store_true", help="Send the gap/label auto-detect command.")
    p.add_argument("--feed", action="store_true", help="Feed one label.")
    p.add_argument("--status", action="store_true", help="Show device ID and decoded status byte.")
    p.add_argument("--list", dest="list_printers", action="store_true", help="List attached printers.")
    p.add_argument("--serial", help="USB serial number, to select a specific printer.")

    p.add_argument("--save-defaults", action="store_true",
                    help="Save the settings given on this command line as defaults, then continue.")
    p.add_argument("--debug", action="store_true", help="Show full tracebacks on error.")
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
    return overrides


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


def _cmd_status(args: argparse.Namespace) -> int:
    if args.test:
        print("(dry run) --status would query the printer over USB; not touching USB.")
        return 0
    with usb_transport.Printer(serial=args.serial) as printer:
        device_id = printer.device_id()
        raw = printer.query_status()
    print(f"device: {device_id}")
    if raw is None:
        print("status: no reply")
    else:
        flags = tspl_mod.decode_status(raw)
        print(f"status: 0x{raw:02x} ({', '.join(flags) if flags else 'ready'})")
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    job = tspl_mod.calibrate_job()
    rc = _finish_job(args, job)
    if rc == 0 and not args.test:
        print("Calibration sent; the printer will feed a few labels to detect gap/label size.")
    return rc


def _cmd_feed(args: argparse.Namespace) -> int:
    job = tspl_mod.feed_job()
    rc = _finish_job(args, job)
    if rc == 0 and not args.test:
        print("Fed one label.")
    return rc


def _cmd_selftest(args: argparse.Namespace, job_settings: "tspl_mod.JobSettings") -> int:
    job = tspl_mod.selftest_job(job_settings)
    if args.preview:
        # BOX/BAR/TEXT are drawn by the printer itself; this is an
        # approximation (stand-in font) decoded from the exact job bytes.
        _save_previews(
            args.preview,
            tspl_mod.simulate(
                job,
                job_settings.size.width_dots,
                job_settings.size.height_dots,
                job_settings.bitmap_black_is_one,
            ),
        )
    rc = _finish_job(args, job)
    if rc == 0 and not args.test:
        print("Self-test label sent.")
    return rc


def _cmd_print_files(
    args: argparse.Namespace, settings: Dict[str, Any], job_settings: "tspl_mod.JobSettings"
) -> int:
    opts = _render_options(settings, args)
    all_pages: List[Any] = []
    for path in args.files:
        pages = render_mod.render_file(
            path, job_settings.size, opts, filename=(None if path == "-" else path)
        )
        all_pages.extend(pages)

    if not all_pages:
        print("error: nothing to print (no pages rendered)", file=sys.stderr)
        return 1

    if args.preview:
        _save_previews(args.preview, all_pages)

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

    if args.save_defaults and overrides:
        config_mod.save(overrides)

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
        if args.list_printers:
            return _cmd_list(args)
        if args.status:
            return _cmd_status(args)
        if args.calibrate:
            return _cmd_calibrate(args)
        if args.feed:
            return _cmd_feed(args)
        if args.selftest:
            return _cmd_selftest(args, job_settings)
        if args.files:
            return _cmd_print_files(args, settings, job_settings)
        print("error: no file given (or use --selftest/--calibrate/--feed/--status/--list)", file=sys.stderr)
        return 1
    except usb_transport.PrinterError as exc:
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
