#!/usr/bin/env python3
"""CLI entry point: print a PDF or image to a Munbyn RW403B over USB.

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
_FLOAT_KEYS = ("gap_mm", "gap_offset_mm", "offset_mm", "x_shift_mm", "y_shift_mm", "feed_scale")
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
        "feed_scale": args.feed_scale,
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


def _cmd_feed(args: argparse.Namespace, job_settings: "tspl_mod.JobSettings") -> int:
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


def _cmd_selftest(args: argparse.Namespace, job_settings: "tspl_mod.JobSettings") -> int:
    job = tspl_mod.selftest_job(job_settings)
    if args.preview:
        # The self-test is a single BITMAP now, so (feed_scale aside) the
        # preview is the exact image sent, not an approximation.
        _save_previews(args.preview, [tspl_mod.selftest_image(_unstretched(job_settings))])
    rc = _finish_job(args, job)
    if rc == 0 and not args.test:
        print("Self-test label sent.")
    return rc


def _cmd_scale_test(args: argparse.Namespace, job_settings: "tspl_mod.JobSettings") -> int:
    job = tspl_mod.scale_test_job(job_settings)
    if args.preview:
        _save_previews(args.preview, [tspl_mod.scale_test_image(_unstretched(job_settings))])
    rc = _finish_job(args, job)
    if rc == 0 and not args.test:
        print("Scale-test label sent.")
    return rc


def _cmd_print_files(
    args: argparse.Namespace, settings: Dict[str, Any], job_settings: "tspl_mod.JobSettings"
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
        if "feed_scale" in overrides:
            # Validate before saving: a typo here (e.g. a stray percentage
            # like 98 instead of 0.98) used to be written straight to disk,
            # breaking every later run (CLI and web UI both load it as the
            # default) until it was noticed and overwritten.
            try:
                labels_mod.validate_feed_scale(overrides["feed_scale"])
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
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
            return _cmd_feed(args, job_settings)
        if args.selftest:
            return _cmd_selftest(args, job_settings)
        if args.scale_test:
            return _cmd_scale_test(args, job_settings)
        if args.files:
            return _cmd_print_files(args, settings, job_settings)
        print(
            "error: no file given (or use --selftest/--scale-test/--calibrate/--feed/--status/--list)",
            file=sys.stderr,
        )
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
