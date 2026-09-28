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
from dataclasses import replace
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

    from munbyn import ble_bridge_client as bridge_client
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
_FLOAT_FIELDS = {"gap_mm", "gap_offset_mm", "offset_mm", "x_shift_mm", "y_shift_mm", "feed_scale"}
_INT_FIELDS = {"density", "speed", "direction", "threshold"}
_SETTINGS_FORM_KEYS = (
    "size", "media", "gap_mm", "gap_offset_mm", "density", "speed",
    "direction", "offset_mm", "x_shift_mm", "y_shift_mm", "feed_scale",
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


def _transport_from_form(form: Any, settings: Dict[str, Any]) -> str:
    """"usb" or "ble" (Bluetooth via the MunbynBLE bridge): the form's
    ``transport`` field, else the config's."""
    value = str(form.get("transport") or settings.get("transport") or "usb").lower()
    if value not in ("usb", "ble"):
        raise ValueError("transport must be usb or ble, not {!r}".format(value))
    return value


def _settings_from_form(form: Any) -> Dict[str, Any]:
    settings = dict(config_mod.load())
    settings["transport"] = _transport_from_form(form, settings)
    if settings["transport"] == "ble":
        # Bluetooth jobs have their own feed correction; the page's feed-scale
        # field is switched to it (static/app.js), and API callers that send
        # no feed_scale get the config's ble_feed_scale.
        ble_scale = settings.get("ble_feed_scale")
        settings["feed_scale"] = ble_scale if ble_scale is not None else settings["feed_scale"]
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


def _render_options_from_form(
    form: Any, settings: Dict[str, Any], feed_scale_override: Optional[float] = None
) -> "render_mod.RenderOptions":
    """Build RenderOptions from the form. ``feed_scale_override`` is used by
    ``/api/preview`` to force an un-stretched (physical-size) preview
    regardless of the configured feed_scale -- see that route."""
    scale = form.get("scale")
    margin = form.get("margin_mm")
    invert = form.get("invert")
    feed_scale = (
        feed_scale_override if feed_scale_override is not None else float(settings["feed_scale"])
    )
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
        feed_scale=feed_scale,
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
        feed_scale=float(settings["feed_scale"]),
    )


def _print_via_bridge(
    settings: Dict[str, Any], job_settings: "tspl_mod.JobSettings", images: Any, test_mode: bool
) -> Any:
    """Bluetooth: hand the pages to the MunbynBLE bridge as a TSPL job (the
    web server itself never opens Bluetooth -- macOS would kill it when it's
    started from an app without the Bluetooth permission)."""
    try:
        port = bridge_client.bridge_port(settings)
        job = bridge_client.build_bridge_job(job_settings, list(images))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if test_mode:
        return jsonify({
            "ok": True, "dry_run": True, "transport": "ble",
            "describe": tspl_mod.describe(job) + "\n(dry run) would send this job to the MunbynBLE bridge on "
                        "127.0.0.1:{} for Bluetooth; nothing was sent.".format(port),
        })
    labels = len(images) * job_settings.copies
    try:
        bridge_client.status(port)  # make sure it's our bridge before sending a job to that port
        reply = bridge_client.send_job(port, job, timeout=bridge_client.job_timeout(labels))
    except bridge_client.BridgeError as exc:
        return jsonify({"ok": False, "transport": "ble", "error": str(exc)}), 503
    return jsonify({
        "ok": True, "transport": "ble", "pages": len(images), "bytes": len(job), "job": reply.get("job"),
        "printed": reply.get("printed"), "expected": reply.get("expected"), "seconds": reply.get("seconds"),
    })


def _unstretched(job_settings: "tspl_mod.JobSettings") -> "tspl_mod.JobSettings":
    """``job_settings`` with ``feed_scale`` forced to 1.0, for previews that
    should show the label as it will look on paper (physical size) rather
    than the feed-scale-stretched bitmap actually sent -- mirrors
    print_label.py's ``_unstretched``."""
    if job_settings.feed_scale == 1.0:
        return job_settings
    return replace(job_settings, feed_scale=1.0)


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
        # feed_scale's saved default (from config, not hardcoded like the
        # template's other fields) so the "advanced" field on the page
        # reflects whatever was last saved with --save-defaults or the web
        # form, not a compile-time constant.
        cfg = config_mod.load()
        feed_scale_default = cfg.get("feed_scale", config_mod.DEFAULTS["feed_scale"])
        ble_feed_scale_default = cfg.get("ble_feed_scale")
        if ble_feed_scale_default is None:
            ble_feed_scale_default = feed_scale_default
        transport_default = "ble" if str(cfg.get("transport") or "usb").lower() == "ble" else "usb"
        return render_template(
            "index.html",
            feed_scale_default=(ble_feed_scale_default if transport_default == "ble" else feed_scale_default),
            usb_feed_scale_default=feed_scale_default,
            ble_feed_scale_default=ble_feed_scale_default,
            transport_default=transport_default,
        )

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
            # Preview always shows the label as it will look on paper
            # (physical size, un-stretched), regardless of the configured
            # feed_scale -- see --preview's help text in print_label.py for
            # the same convention on the CLI side.
            opts = _render_options_from_form(request.form, settings, feed_scale_override=1.0)
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
            if settings["transport"] == "ble":
                return _print_via_bridge(settings, job_settings, pages, app.config["MUNBYN_TEST_MODE"])
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

    @app.route("/api/selftest", methods=["POST"])
    def api_selftest():
        """Preview or print the built-in alignment/polarity self-test label
        (no file upload -- just the current label/printer settings)."""
        sec_err = _security_error(request)
        if sec_err is not None:
            return sec_err
        try:
            settings = _settings_from_form(request.form)
            size = labels_mod.parse_size(str(settings["size"]))
            # Validate explicitly: the preview branch below forces feed_scale
            # to 1.0 (always valid) before it's ever used, so an out-of-range
            # value would otherwise sail through preview (200) while the
            # print branch's selftest_job() -> header() -> validate_feed_scale
            # raises ValueError uncaught (500) -- see the /api/scale-test
            # route below for the same fix.
            labels_mod.validate_feed_scale(float(settings["feed_scale"]))
            job_settings = _job_settings_from_form(request.form, settings, size)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        preview_only = (request.form.get("preview") or "") in ("1", "true", "True")
        if preview_only:
            # Un-stretched (physical-size) preview -- see /api/preview.
            try:
                preview_settings = _unstretched(job_settings)
                img = tspl_mod.selftest_image(preview_settings)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            return jsonify({
                "pages": [f"data:image/png;base64,{encoded}"],
                "width_dots": size.width_dots,
                "height_dots": size.height_dots,
            })

        try:
            if settings["transport"] == "ble":
                return _print_via_bridge(settings, job_settings, [tspl_mod.selftest_image(job_settings)],
                                         app.config["MUNBYN_TEST_MODE"])
            job = tspl_mod.selftest_job(job_settings)
        except ValueError as exc:
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

        return jsonify({"ok": True, "pages": 1, "bytes": len(job)})

    @app.route("/api/scale-test", methods=["POST"])
    def api_scale_test():
        """Preview or print the feed/x-alignment calibration label (no file
        upload -- just the current label/printer settings, including
        feed_scale). See munbyn.tspl.scale_test_image/scale_test_job."""
        sec_err = _security_error(request)
        if sec_err is not None:
            return sec_err
        try:
            settings = _settings_from_form(request.form)
            size = labels_mod.parse_size(str(settings["size"]))
            # See the matching comment in /api/selftest: validate explicitly
            # so preview and print reject the same out-of-range feed_scale.
            labels_mod.validate_feed_scale(float(settings["feed_scale"]))
            job_settings = _job_settings_from_form(request.form, settings, size)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        preview_only = (request.form.get("preview") or "") in ("1", "true", "True")
        if preview_only:
            try:
                preview_settings = _unstretched(job_settings)
                img = tspl_mod.scale_test_image(preview_settings)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            return jsonify({
                "pages": [f"data:image/png;base64,{encoded}"],
                "width_dots": size.width_dots,
                "height_dots": size.height_dots,
            })

        try:
            if settings["transport"] == "ble":
                return _print_via_bridge(settings, job_settings, [tspl_mod.scale_test_image(job_settings)],
                                         app.config["MUNBYN_TEST_MODE"])
            job = tspl_mod.scale_test_job(job_settings)
        except ValueError as exc:
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

        return jsonify({"ok": True, "pages": 1, "bytes": len(job)})

    @app.route("/api/status")
    def api_status():
        sec_err = _security_error(request)
        if sec_err is not None:
            return sec_err
        try:
            cfg = config_mod.load()
            transport = _transport_from_form(request.args, cfg)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if transport == "ble":
            return _bridge_status(cfg, app.config["MUNBYN_TEST_MODE"])
        if app.config["MUNBYN_TEST_MODE"]:
            return jsonify({
                "connected": False, "device_id": None, "status": None, "dry_run": True,
                "status_note": "dry-run mode (--test): USB is never touched",
            })
        if not _USB_LOCK.acquire(timeout=2.0):
            return jsonify({
                "connected": True, "device_id": None, "status": ["busy: job in progress"],
                "dry_run": False, "status_note": None,
            })
        try:
            with usb_transport.Printer() as printer:
                # Device ID is a USB control request, not a TSPL command --
                # always safe. The TSPL status query (ESC !?) is NOT sent
                # here: this firmware never replies to it (verified on
                # hardware), and whether those unterminated bytes are safe to
                # send right before the next job's SIZE line has not been
                # verified -- see CLAUDE.md. Use `print_label.py --status
                # --probe` to send it deliberately, off the web UI's path.
                device_id = printer.device_id()
        except usb_transport.PrinterError:
            return jsonify({
                "connected": False, "device_id": None, "status": None, "dry_run": False,
                "status_note": None,
            })
        finally:
            _USB_LOCK.release()

        return jsonify({
            "connected": True, "device_id": device_id, "status": None, "dry_run": False,
            "status_note": (
                "status not queried (the web UI never sends the TSPL status query -- "
                "see CLAUDE.md)"
            ),
        })

    return app


def _bridge_status(cfg: Dict[str, Any], test_mode: bool) -> Any:
    """Is the MunbynBLE bridge up? (Its status only -- this never makes it
    connect to the printer.)"""
    try:
        port = bridge_client.bridge_port(cfg)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if test_mode:
        return jsonify({
            "transport": "ble", "connected": False, "device_id": None, "status": None, "dry_run": True,
            "status_note": "dry-run mode (--test): the Bluetooth bridge is never contacted",
        })
    try:
        reply = bridge_client.status(port, timeout=2.0)
    except bridge_client.BridgeError as exc:
        return jsonify({
            "transport": "ble", "connected": False, "device_id": None, "status": None, "dry_run": False,
            "status_note": str(exc),
        })
    return jsonify({
        "transport": "ble", "connected": True, "device_id": None, "status": None, "dry_run": False,
        "status_note": bridge_client.describe_status(reply, port), "bridge": reply,
    })


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
