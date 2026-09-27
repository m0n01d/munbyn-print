#!/usr/bin/env python3
"""Flask web UI: drag-and-drop preview + print for the Munbyn RW403B.

Runs under the bare macOS ``/usr/bin/python3`` (3.9): if a dependency import
fails and this project's ``.venv`` exists, it re-execs into that venv's Python
with the same argv before doing anything else.
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import threading
from typing import Any, Dict, Optional
from urllib.parse import urlparse


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
    from flask import Flask, jsonify, render_template, request

    from munbyn import config as config_mod
    from munbyn import labels as labels_mod
    from munbyn import render as render_mod
    from munbyn import tspl as tspl_mod
    from munbyn import usb_transport
except ImportError as _import_exc:  # bare system python is missing a dependency
    _reexec_into_venv_or_raise(_import_exc)
    raise


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

_MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB

# One USB conversation at a time: the dev server is threaded, and a status
# query (ESC !?) must never interleave with a job being streamed.
_USB_LOCK = threading.Lock()

# Settings that both the CLI and the web form understand, mapped by name to
# the coercion applied to their raw (string) form-field values.
_FLOAT_FIELDS = {"gap_mm", "gap_offset_mm", "offset_mm", "x_shift_mm", "y_shift_mm"}
_INT_FIELDS = {"density", "speed", "direction", "threshold"}
_SETTINGS_FORM_KEYS = (
    "size", "media", "gap_mm", "gap_offset_mm", "density", "speed",
    "direction", "offset_mm", "x_shift_mm", "y_shift_mm",
    "fit", "rotate", "crop", "dither", "threshold",
)


def _security_error(req: "request") -> Optional[Any]:
    """None if the request passes the localhost-only POST checks, else a response."""
    if req.headers.get("X-Munbyn") != "1":
        return jsonify({"error": "missing X-Munbyn header"}), 403
    origin = req.headers.get("Origin")
    if origin:
        origin_host = urlparse(origin).netloc
        if origin_host and origin_host != req.host:
            return jsonify({"error": "bad origin"}), 403
    return None


def _settings_from_form(form: Any) -> Dict[str, Any]:
    settings = dict(config_mod.load())
    for key in _SETTINGS_FORM_KEYS:
        raw = form.get(key)
        if raw is None or raw == "":
            continue
        if key in _FLOAT_FIELDS:
            settings[key] = float(raw)
        elif key in _INT_FIELDS:
            settings[key] = int(raw)
        else:
            settings[key] = raw
    black_is_one = form.get("black_is_one")
    if black_is_one not in (None, ""):
        settings["bitmap_black_is_one"] = black_is_one in ("1", "true", "True")
    return settings


def _render_options_from_form(form: Any, settings: Dict[str, Any]) -> "render_mod.RenderOptions":
    scale = form.get("scale")
    margin = form.get("margin_mm")
    invert = form.get("invert")
    return render_mod.RenderOptions(
        fit=settings["fit"],
        scale=(float(scale) if scale not in (None, "") else None),
        rotate=settings["rotate"],
        crop=settings["crop"],
        margin_mm=(float(margin) if margin not in (None, "") else 0.0),
        align=(form.get("align") or "center"),
        dither=settings["dither"],
        threshold=int(settings["threshold"]),
        invert=bool(invert) and invert not in ("0", "false", "False"),
        pages=(form.get("pages") or None),
    )


def _job_settings_from_form(form: Any, settings: Dict[str, Any], size: "labels_mod.LabelSize") -> "tspl_mod.JobSettings":
    copies_raw = form.get("copies")
    copies = int(copies_raw) if copies_raw not in (None, "") else 1
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


def create_app(test_mode: bool = False) -> Flask:
    """Build a fresh Flask app. ``test_mode`` makes /api/print a dry run and
    keeps /api/status from ever touching USB -- used for ``--test`` and by
    the test suite alike.
    """
    app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
    app.config["MAX_CONTENT_LENGTH"] = _MAX_CONTENT_LENGTH
    app.config["MUNBYN_TEST_MODE"] = bool(test_mode)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/preview", methods=["POST"])
    def api_preview():
        sec_err = _security_error(request)
        if sec_err is not None:
            return sec_err
        if "file" not in request.files or not request.files["file"].filename:
            return jsonify({"error": "no file uploaded"}), 400
        upload = request.files["file"]
        data = upload.read()

        try:
            settings = _settings_from_form(request.form)
            size = labels_mod.parse_size(str(settings["size"]))
            opts = _render_options_from_form(request.form, settings)
            pages = render_mod.render_file(data, size, opts, filename=upload.filename)
        except (ValueError, render_mod.RenderError) as exc:
            return jsonify({"error": str(exc)}), 400

        page_data_urls = []
        for img in pages:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            page_data_urls.append(f"data:image/png;base64,{encoded}")

        return jsonify({
            "pages": page_data_urls,
            "width_dots": size.width_dots,
            "height_dots": size.height_dots,
        })

    @app.route("/api/print", methods=["POST"])
    def api_print():
        sec_err = _security_error(request)
        if sec_err is not None:
            return sec_err
        if "file" not in request.files or not request.files["file"].filename:
            return jsonify({"error": "no file uploaded"}), 400
        upload = request.files["file"]
        data = upload.read()

        try:
            settings = _settings_from_form(request.form)
            size = labels_mod.parse_size(str(settings["size"]))
            opts = _render_options_from_form(request.form, settings)
            pages = render_mod.render_file(data, size, opts, filename=upload.filename)
            job_settings = _job_settings_from_form(request.form, settings, size)
            job = tspl_mod.build_job(job_settings, pages)
        except (ValueError, render_mod.RenderError) as exc:
            return jsonify({"error": str(exc)}), 400

        if app.config["MUNBYN_TEST_MODE"]:
            return jsonify({"ok": True, "dry_run": True, "describe": tspl_mod.describe(job)})

        serial = request.form.get("serial") or None
        try:
            with _USB_LOCK:
                with usb_transport.Printer(serial=serial) as printer:
                    printer.write(job)
        except usb_transport.PrinterError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 503

        return jsonify({"ok": True, "pages": len(pages), "bytes": len(job)})

    @app.route("/api/status")
    def api_status():
        if app.config["MUNBYN_TEST_MODE"]:
            return jsonify({"connected": False, "device_id": None, "status": None, "dry_run": True})
        if not _USB_LOCK.acquire(timeout=2.0):
            return jsonify({"connected": True, "device_id": None, "status": ["busy: job in progress"], "dry_run": False})
        try:
            with usb_transport.Printer() as printer:
                device_id = printer.device_id()
                raw = printer.query_status()
        except usb_transport.PrinterError:
            return jsonify({"connected": False, "device_id": None, "status": None, "dry_run": False})
        finally:
            _USB_LOCK.release()

        flags = tspl_mod.decode_status(raw) if raw is not None else None
        return jsonify({"connected": True, "device_id": device_id, "status": flags, "dry_run": False})

    return app


# A default app instance, so `flask --app web run` or a WSGI server can find
# one without going through main(). `main()` below builds its own instead,
# since it needs to honour --test.
app = create_app()


def main(argv: Optional[Any] = None) -> int:
    parser = argparse.ArgumentParser(prog="web.py", description="Web UI for munbyn-print.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--test", action="store_true",
                         help="Dry-run mode: never touch USB; /api/print describes the job instead of sending it.")
    args = parser.parse_args(argv)

    flask_app = create_app(test_mode=args.test)
    flask_app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
