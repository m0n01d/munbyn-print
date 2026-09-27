"""Raw USB bulk transport to the Munbyn RW403B printer interface.

Verified on hardware: composite device, vid=0x0d28 pid=0xccdd, interface 0
is the printer class (bidirectional, bulk OUT 0x03 / bulk IN 0x83, 64-byte
packets); interface 1 is mass storage and is never touched here.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import usb.core
import usb.util

from .tspl import STATUS_QUERY

VENDOR_ID = 0x0D28
PRODUCT_ID = 0xCCDD
PRINTER_INTERFACE = 0

_BUSY_HINT = (
    "another program (a stuck CUPS job) may hold the printer: "
    "`cancel -a Munbyn_RW403B`"
)


class PrinterError(Exception):
    """Base error for anything that goes wrong talking to the printer."""


class PrinterNotFound(PrinterError):
    """No matching USB device was found."""


class PrinterBusy(PrinterError):
    """The device exists but is claimed by something else (e.g. a stuck CUPS job)."""


def get_backend():
    """Locate a usable libusb1 backend.

    Tries, in order: ``libusb_package`` (bundled binary), the Homebrew
    dylib, ``/usr/local/lib`` (Intel Homebrew prefix), then pyusb's own
    default search. Raises ``PrinterError`` with an install hint if none
    work.
    """
    try:
        import libusb_package

        backend = libusb_package.get_libusb1_backend()
        if backend is not None:
            return backend
    except Exception:
        pass

    import usb.backend.libusb1 as libusb1

    for candidate in (
        "/opt/homebrew/lib/libusb-1.0.dylib",
        "/usr/local/lib/libusb-1.0.dylib",
    ):
        try:
            backend = libusb1.get_backend(find_library=lambda _lib, c=candidate: c)
        except Exception:
            backend = None
        if backend is not None:
            return backend

    try:
        backend = libusb1.get_backend()
    except Exception:
        backend = None
    if backend is not None:
        return backend

    raise PrinterError(
        "No usable libusb backend found. Try `pip install libusb-package` "
        "inside the project virtualenv, or `brew install libusb`."
    )


def _safe_str(getter):
    try:
        return getter()
    except Exception:
        return None


def _read_device_id(dev) -> Optional[str]:
    try:
        raw = dev.ctrl_transfer(0xA1, 0, 0, 0, 1024)
    except Exception:
        return None
    data = bytes(raw)
    if len(data) < 2:
        return ""
    return data[2:].decode("ascii", errors="replace").rstrip("\x00")


def find_printers(
    vid: int = VENDOR_ID, pid: int = PRODUCT_ID
) -> List[Dict[str, Any]]:
    """List attached printers matching vid/pid, tolerating partial failures."""
    backend = get_backend()
    results: List[Dict[str, Any]] = []
    devices = usb.core.find(find_all=True, idVendor=vid, idProduct=pid, backend=backend)
    for dev in devices:
        results.append(
            {
                "serial": _safe_str(lambda: dev.serial_number),
                "bus": _safe_str(lambda: dev.bus),
                "address": _safe_str(lambda: dev.address),
                "product": _safe_str(lambda: dev.product),
                "device_id": _read_device_id(dev),
            }
        )
    return results


def _wrap_usb_error(exc: "usb.core.USBError") -> PrinterError:
    errno = getattr(exc, "errno", None)
    if errno in (16, 13):  # EBUSY, EACCES
        return PrinterBusy(
            "Printer is busy or inaccessible (errno={}): {}. {}".format(
                errno, exc, _BUSY_HINT
            )
        )
    return PrinterError(str(exc))


class Printer:
    """Context-managed handle to one Munbyn RW403B over USB bulk transfer."""

    def __init__(
        self,
        vid: int = VENDOR_ID,
        pid: int = PRODUCT_ID,
        serial: Optional[str] = None,
        timeout_ms: int = 15000,
    ) -> None:
        self.vid = vid
        self.pid = pid
        self.serial = serial
        self.timeout_ms = timeout_ms
        self._interface = PRINTER_INTERFACE
        self._dev = None
        self._out_ep = None
        self._in_ep = None

    def open(self) -> "Printer":
        backend = get_backend()
        devices = list(
            usb.core.find(
                find_all=True, idVendor=self.vid, idProduct=self.pid, backend=backend
            )
        )
        if not devices:
            raise PrinterNotFound(
                "No Munbyn printer found (vid=0x{:04x} pid=0x{:04x}).".format(
                    self.vid, self.pid
                )
            )

        dev = None
        if self.serial:
            for candidate in devices:
                if _safe_str(lambda c=candidate: c.serial_number) == self.serial:
                    dev = candidate
                    break
            if dev is None:
                raise PrinterNotFound(
                    "No Munbyn printer with serial {!r} found.".format(self.serial)
                )
        else:
            dev = devices[0]

        try:
            cfg = dev.get_active_configuration()
        except usb.core.USBError:
            cfg = None
        if cfg is None:
            try:
                dev.set_configuration()
            except usb.core.USBError as exc:
                raise _wrap_usb_error(exc)
            cfg = dev.get_active_configuration()

        intf = cfg[(self._interface, 0)]

        try:
            if dev.is_kernel_driver_active(self._interface):
                dev.detach_kernel_driver(self._interface)
        except NotImplementedError:
            pass
        except usb.core.USBError:
            pass

        try:
            usb.util.claim_interface(dev, self._interface)
        except usb.core.USBError as exc:
            raise _wrap_usb_error(exc)

        out_ep = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_OUT,
        )
        in_ep = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_IN,
        )
        if out_ep is None or in_ep is None:
            raise PrinterError("Could not locate bulk endpoints on printer interface.")

        self._dev = dev
        self._out_ep = out_ep
        self._in_ep = in_ep
        return self

    def close(self) -> None:
        if self._dev is not None:
            try:
                usb.util.release_interface(self._dev, self._interface)
            except Exception:
                pass
            try:
                usb.util.dispose_resources(self._dev)
            except Exception:
                pass
        self._dev = None
        self._out_ep = None
        self._in_ep = None

    def __enter__(self) -> "Printer":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def device_id(self) -> str:
        return _read_device_id(self._dev) or ""

    def write(self, data: bytes, chunk_size: int = 4096) -> int:
        if self._out_ep is None:
            raise PrinterError("Printer is not open (use it as a context manager).")
        view = memoryview(data)
        total = 0
        while total < len(view):
            chunk = view[total : total + chunk_size]
            try:
                sent = self._out_ep.write(chunk, timeout=self.timeout_ms)
            except usb.core.USBTimeoutError as exc:
                raise PrinterError(
                    "USB write timed out after {} ms: {}".format(
                        self.timeout_ms, exc
                    )
                )
            except usb.core.USBError as exc:
                raise _wrap_usb_error(exc)
            if not sent:
                raise PrinterError(
                    "USB write made no progress after {} of {} bytes".format(total, len(view))
                )
            total += sent
        return total

    def read(self, size: int = 64, timeout_ms: int = 500) -> bytes:
        try:
            data = self._in_ep.read(size, timeout=timeout_ms)
        except usb.core.USBTimeoutError:
            return b""
        except usb.core.USBError as exc:
            raise _wrap_usb_error(exc)
        return bytes(data)

    def query_status(self) -> Optional[int]:
        self.write(STATUS_QUERY)
        reply = self.read(1, timeout_ms=500)
        if not reply:
            return None
        return reply[0]
