"""Client side of the MunbynBLE bridge (``munbyn.ble_bridge``).

The bridge is a small server on ``127.0.0.1:<ble_bridge_port>`` (default
9100) that owns Bluetooth: it runs inside ``MunbynBLE.app``, which carries the
macOS Bluetooth permission. Anything launched from somewhere without that
permission -- the Claude app, cupsd, a web server started from there -- is
killed by macOS the moment it touches Bluetooth, so with transport ``ble`` the
CLI and the web UI hand the bridge an ordinary TSPL job instead of opening
Bluetooth themselves (``print_label.py --ble-direct`` still does it in-process,
from Terminal only).

Wire protocol (loopback TCP, one request per connection):

* **Print:** send a TSPL job (``build_bridge_job``), half-close the socket,
  read one JSON line back: ``{"ok": true, "job": 3, "pages": 1, ...}`` or
  ``{"ok": false, "error": "..."}``. The CUPS socket backend speaks the same
  thing, minus reading the reply.
* **Status:** send ``MUNBYN-STATUS\\n`` (or ``MUNBYN-STATUS DEVICEINFO\\n`` to
  also have the bridge ask the printer for DEVICEINFO over Bluetooth), read
  one JSON line back.

No Bluetooth here, and no bleak import. Every socket goes through
``_connect`` so tests can refuse all connections (``tests/conftest.py``) and
opt back in against a fake bridge on an ephemeral port.
"""
from __future__ import annotations

import dataclasses
import json
import socket
from typing import Any, Dict, List, Optional

from .usb_transport import PrinterError

HOST = "127.0.0.1"
DEFAULT_PORT = 9100
STATUS_MAGIC = b"MUNBYN-STATUS"
#: What a bridge's status reply says in "bridge"; anything else on the port is not ours.
BRIDGE_NAME = "munbyn-ble-bridge"
INSTALL_HINT = (
    "Bluetooth printing goes through the MunbynBLE bridge app: install/start it with "
    "`scripts/install-ble-bridge.sh` (no sudo) and allow Bluetooth when macOS asks "
    "(\"MunbynBLE would like to use Bluetooth\"). From macOS Terminal only, --ble-direct "
    "prints without the bridge."
)


class BridgeError(PrinterError):
    """Talking to the bridge failed, or it reported a failed job."""


class BridgeUnavailable(BridgeError):
    """Nothing (or not our bridge) answers on the bridge port."""


class BridgeJobFailed(BridgeError):
    """The bridge got the job but could not print it."""


def bridge_port(settings: Optional[Dict[str, Any]] = None) -> int:
    value = (settings or {}).get("ble_bridge_port")
    try:
        port = int(value) if value not in (None, "") else DEFAULT_PORT
    except (TypeError, ValueError):
        raise ValueError("ble_bridge_port must be a port number, not {!r}".format(value)) from None
    if not 1 <= port <= 65535:
        raise ValueError("ble_bridge_port must be 1..65535, not {}".format(port))
    return port


def connect_tcp(port: int, timeout: float) -> socket.socket:
    """Loopback only: the bridge never listens anywhere else."""
    return socket.create_connection((HOST, port), timeout=timeout)


#: The one place a socket is opened (tests swap it out).
_connect = connect_tcp


def _read_line(sock: socket.socket, limit: int = 1 << 20) -> bytes:
    buf = bytearray()
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise BridgeError("the bridge's reply is too long")
    return bytes(buf.split(b"\n", 1)[0])


def _decode(line: bytes, port: int) -> Dict[str, Any]:
    try:
        reply = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        reply = None
    if not isinstance(reply, dict):
        raise BridgeUnavailable(
            "Something is listening on 127.0.0.1:{} but it isn't the MunbynBLE bridge (reply {!r}). {}".format(
                port, line[:60], INSTALL_HINT
            )
        )
    return reply


def status(port: int, *, deviceinfo: bool = False, timeout: float = 3.0) -> Dict[str, Any]:
    """The bridge's status JSON. ``deviceinfo=True`` makes the bridge connect to
    the printer over Bluetooth for DEVICEINFO (queued behind any running job).
    Raises ``BridgeUnavailable`` if the bridge isn't there."""
    request = STATUS_MAGIC + (b" DEVICEINFO" if deviceinfo else b"") + b"\n"
    try:
        sock = _connect(port, timeout)
    except OSError as exc:
        raise BridgeUnavailable(
            "The MunbynBLE bridge is not running (nothing answers on 127.0.0.1:{}: {}). {}".format(
                port, exc, INSTALL_HINT
            )
        ) from None
    with sock:
        try:
            sock.settimeout(timeout)
            sock.sendall(request)
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            line = _read_line(sock)
        except socket.timeout:
            raise BridgeUnavailable(
                "No status reply from 127.0.0.1:{} within {:.0f} s. {}".format(port, timeout, INSTALL_HINT)
            ) from None
        except OSError as exc:
            raise BridgeUnavailable("Lost the connection to the bridge on 127.0.0.1:{}: {}".format(port, exc)) from None
    reply = _decode(line, port)
    if reply.get("bridge") != BRIDGE_NAME:
        raise BridgeUnavailable(
            "Something is listening on 127.0.0.1:{} but it isn't the MunbynBLE bridge. {}".format(port, INSTALL_HINT)
        )
    return reply


def probe(port: int, timeout: float = 1.5) -> Optional[Dict[str, Any]]:
    """Status JSON if our bridge answers, else ``None``. Never touches Bluetooth."""
    try:
        return status(port, timeout=timeout)
    except BridgeError:
        return None


def send_job(port: int, job: bytes, *, timeout: float = 180.0) -> Dict[str, Any]:
    """Send one TSPL job and wait for the bridge's verdict (it replies once the
    printer reported the labels printed, or the job failed). Raises
    ``BridgeUnavailable`` (not running) or ``BridgeJobFailed`` (it failed)."""
    try:
        sock = _connect(port, 3.0)
    except OSError as exc:
        raise BridgeUnavailable(
            "The MunbynBLE bridge is not running (nothing answers on 127.0.0.1:{}: {}). {}".format(
                port, exc, INSTALL_HINT
            )
        ) from None
    with sock:
        try:
            sock.settimeout(30.0)
            sock.sendall(job)
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(timeout)
            line = _read_line(sock)
        except socket.timeout:
            raise BridgeError(
                "The bridge took the job but hasn't answered within {:.0f} s; it may still print. See "
                "~/Library/Logs/munbyn-ble-bridge.log.".format(timeout)
            ) from None
        except OSError as exc:
            raise BridgeError("Lost the connection to the bridge: {}. See ~/Library/Logs/munbyn-ble-bridge.log."
                              .format(exc)) from None
    if not line:
        raise BridgeError("The bridge closed the connection without a reply. See "
                          "~/Library/Logs/munbyn-ble-bridge.log.")
    reply = _decode(line, port)
    if not reply.get("ok"):
        raise BridgeJobFailed("Bluetooth print failed (via the MunbynBLE bridge): {}".format(
            reply.get("error") or "unknown error"))
    return reply


def job_timeout(labels: int) -> float:
    """How long to wait for a job's reply: queueing plus the transport's own
    worst case (connect, DEVICEINFO, sections, 30 s + 15 s per label)."""
    return 120.0 + 20.0 * max(1, labels)


def build_bridge_job(job_settings: Any, images: List[Any]) -> bytes:
    """The TSPL job the bridge turns into Bluetooth pages: the same pages
    ``--ble-direct`` would send (``x_shift``/``y_shift`` baked into the bitmap
    by ``ble_protocol.compose_page``, width padded to whole bytes), as a normal
    header + ``CLS``/``BITMAP 0,0``/``PRINT 1,<copies>`` job with the clear-bit
    = black polarity the bridge (and the USB firmware) expect. Pages must
    already be rendered with ``job_settings.feed_scale`` (the Bluetooth one);
    the bridge does not rescale."""
    from . import ble_protocol as bp
    from . import tspl

    composed = [
        bp.compose_page(img, x_shift_mm=job_settings.x_shift_mm, y_shift_mm=job_settings.y_shift_mm,
                        feed_scale=job_settings.feed_scale)
        for img in images
    ]
    flat = dataclasses.replace(job_settings, x_shift_mm=0.0, y_shift_mm=0.0, bitmap_black_is_one=False)
    return tspl.build_job(flat, composed)


def describe_status(reply: Dict[str, Any], port: int) -> str:
    """One or two lines for humans (CLI --status, web status line)."""
    auth = reply.get("bluetooth_authorization")
    parts = [
        "MunbynBLE bridge {} on 127.0.0.1:{} (pid {})".format(reply.get("version", "?"), port, reply.get("pid", "?")),
        "printer {}".format(reply.get("printer_address") or "not set (scans by name)"),
    ]
    if auth:
        parts.append("Bluetooth permission: {}".format(auth))
    if reply.get("busy"):
        parts.append("printing now ({} queued)".format(reply.get("queued", 0)))
    last = reply.get("last_job")
    if isinstance(last, dict):
        if last.get("ok"):
            parts.append("last job #{} OK at {}".format(last.get("job"), last.get("finished", "?")))
        else:
            parts.append("last job #{} FAILED at {}: {}".format(last.get("job"), last.get("finished", "?"),
                                                              last.get("error")))
    return ", ".join(parts)
