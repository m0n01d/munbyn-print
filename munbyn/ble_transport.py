"""Bluetooth LE transport to the Munbyn RW403B, via ``bleak``.

**Not yet run against the printer** (2026-09-27): the protocol is from the
Munbyn web editor's JS (``PLANS/BLE-PROTOCOL.md``) and the frames are proven
byte-identical to the editor's, but the first real Bluetooth print is still to
come (P2 in ``PLANS/BLE-IMPLEMENTATION.md``). Everything here logs every frame
in hex at DEBUG level (``print_label.py --ble --debug``) so that first run is
diagnosable.

What a print does (spec sections in brackets)::

    find the printer (scan for a name starting "RW403B", or --ble-address)
    connect (2 tries x 4 s), start notifications on 0xABF3            [1]
    DEVICEINFO on 0xABF1, wait <= 4 s; refuse unless every printstatus bit is 0
      (busy/calibrating only: wait 4 s and ask once more)              [5]
    [opt-in only: PRINTINCONCENTRATION / PRINTINGSPEED on 0xABF1]      [11]
    for each page send, for each section:
        write its packets to 0xABF4, awaited, 3 ms apart               [9]
        wait <= 10 s for the section ack; a resend request rewinds to that section
        (a second request for the same section aborts)
    PRINTINEND on 0xABF1                                               [10]
    wait for one "printed" report per page x copy
    disconnect (so the phone app or Chrome can connect again)

Ctrl-C while a job is in flight sends CANCELPRINTING (0xABF1), waits briefly
for the printer's OK and disconnects; a second Ctrl-C stops waiting.

``bleak`` is imported lazily, so the USB path, ``--test`` and the tests never
need it (and never touch the radio). Tests drive ``BlePrinter`` with a fake
client through ``client_factory``/``find_device``.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, TypeVar

from . import ble_protocol as bp
from .usb_transport import PrinterError

log = logging.getLogger("munbyn.ble")

T = TypeVar("T")

#: Longest value CoreBluetooth/bleak take for a write *with* response (ATT long write).
MAX_WRITE_WITH_RESPONSE = 512

WRITE_MODES = ("auto", "response", "no-response")


# --------------------------------------------------------------------------- errors


class BleError(PrinterError):
    """Anything that goes wrong talking to the printer over Bluetooth."""


class BleUnavailable(BleError):
    """``bleak`` is not installed."""


class BlePrinterNotFound(BleError):
    """No matching printer was seen while scanning."""


class BleConnectError(BleError):
    """Found the printer but could not connect / set it up."""


class BleTimeout(BleError):
    """The printer did not answer in time."""


class BleDisconnected(BleError):
    """The link dropped mid-job."""


class BleFrameTooLarge(BleError):
    """A frame is longer than this connection lets us write in one go."""


class BleWriteError(BleError):
    """A GATT write failed."""


class BlePrinterStatus(BleError):
    """DEVICEINFO or a status report says the printer can't print right now."""


class BleJobFailed(BleError):
    """The printer rejected the job (error code, repeated resend, stop report)."""


class BleCancelled(BleError):
    """The job was cancelled (Ctrl-C); CANCELPRINTING was sent."""


class _CancelRequested(Exception):
    pass


# --------------------------------------------------------------------------- options / results


@dataclass
class BleOptions:
    """Knobs for one Bluetooth session. Defaults follow the editor (spec 1, 5, 9)."""

    address: Optional[str] = None  # CoreBluetooth UUID (macOS) from --ble-scan; None = scan by name
    name_prefix: str = bp.NAME_PREFIX
    scan_timeout: float = 10.0
    connect_timeout: float = 4.0
    connect_attempts: int = 2
    post_connect_delay: float = 1.0  # the editor sleeps 1000 ms after connecting
    post_notify_delay: float = 0.2  # ... and 200 ms after starting notifications
    deviceinfo_timeout: float = bp.DEVICEINFO_TIMEOUT_S
    busy_retry_delay: float = bp.BUSY_RETRY_DELAY_S
    ack_timeout: float = bp.SECTION_ACK_TIMEOUT_S
    write_timeout: float = 5.0  # per GATT write (not in the editor; a hung write must not hang us)
    done_timeout: float = 30.0  # after PRINTINEND, wait this long ...
    done_timeout_per_page: float = 15.0  # ... plus this per expected page, for the "printed" reports
    cancel_reply_timeout: float = 2.0
    per_size: Optional[int] = None  # None = 400, or 148 when DEVICEINFO says BLE firmware 1.0.8
    write_mode: str = "auto"  # auto | response | no-response (see choose_response)
    density: Optional[int] = None  # opt-in PRINTINCONCENTRATION 1..16 before the job
    speed: Optional[int] = None  # opt-in PRINTINGSPEED 1..8 before the job
    setting_delay: float = 0.2  # pause after a density/speed write (a guess; the editor doesn't wait)


@dataclass
class PrintResult:
    pages: int
    sends: int
    copies: int
    expected: int
    printed: int
    writes: int
    bytes_sent: int
    resends: int
    per_size: int
    device: Optional[bp.DeviceInfo] = None
    seconds: float = 0.0


def choose_response(properties: Sequence[str], mode: str = "auto") -> bool:
    """Write type for a characteristic: ``True`` = write-with-response.

    ``auto`` mirrors what Chrome's ``writeValue()`` does for the editor (spec
    1): with-response when the characteristic has the Write property, else
    without-response when it only has Write Without Response. The property
    flags themselves are still unknown (spec 12, item 1)."""
    if mode not in WRITE_MODES:
        raise ValueError("write mode must be one of {}".format(", ".join(WRITE_MODES)))
    if mode == "response":
        return True
    if mode == "no-response":
        return False
    props = set(properties or ())
    if "write" in props:
        return True
    if "write-without-response" in props:
        return False
    return True


def _import_bleak():
    try:
        import bleak  # noqa: F401
    except ImportError as exc:
        raise BleUnavailable(
            "Bluetooth printing needs the bleak package: `.venv/bin/pip install -r requirements.txt` "
            "(or re-run ./setup.sh)"
        ) from exc
    return bleak


def _adv_name(device: Any, adv: Any) -> str:
    return (getattr(adv, "local_name", None) or getattr(device, "name", None) or "").strip()


# --------------------------------------------------------------------------- the printer


class BlePrinter:
    """Async handle to one RW403B over Bluetooth LE.

    ``client_factory(target, disconnected_callback=..., timeout=...)`` builds
    the GATT client (default: ``bleak.BleakClient``); ``find_device(options)``
    returns what to connect to (default: a bleak scan). Tests inject fakes for
    both, plus a ``sleep`` that doesn't wait."""

    def __init__(
        self,
        options: Optional[BleOptions] = None,
        *,
        client_factory: Optional[Callable[..., Any]] = None,
        find_device: Optional[Callable[[BleOptions], Awaitable[Any]]] = None,
        sleep: Optional[Callable[[float], Awaitable[Any]]] = None,
    ) -> None:
        self.options = options or BleOptions()
        choose_response((), self.options.write_mode)  # validate early
        self._client_factory = client_factory
        self._find = find_device
        self._sleep = sleep or asyncio.sleep
        self._client: Any = None
        self._queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._decoder = bp.NotificationDecoder()
        self._response: Dict[str, bool] = {}
        self._max_len: Dict[str, int] = {}
        self._notifying = False
        self._disconnected = False
        self._closing = False
        self._cancel_requested = False
        self._printing = False
        self._printed = 0
        self._expected = 0
        self._status_bits = [0] * 9
        self.device: Optional[bp.DeviceInfo] = None
        self.target_description = ""
        self.writes = 0
        self.bytes_sent = 0
        self.resends = 0
        #: True once PRINTINEND has been (or was being) written for the current
        #: job: from then on a label may come out, so a caller must not retry
        #: the job blindly (``munbyn.ble_bridge`` checks this and ``printed``).
        self.end_sent = False

    # -- state

    @property
    def connected(self) -> bool:
        return self._client is not None and not self._disconnected

    @property
    def printing(self) -> bool:
        return self._printing

    @property
    def printed(self) -> int:
        """"Printed" reports received for the current/last job."""
        return self._printed

    def request_cancel(self) -> None:
        """Ask the running job to stop: it sends CANCELPRINTING at its next
        step and raises ``BleCancelled``. Safe to call from a signal handler
        registered on the event loop."""
        if self._cancel_requested:
            return
        self._cancel_requested = True
        if self._queue is not None:
            self._queue.put_nowait(bp.Event("cancel_requested"))

    def _check_cancel(self) -> None:
        if self._cancel_requested:
            raise _CancelRequested()

    # -- connection

    async def __aenter__(self) -> "BlePrinter":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        await self.disconnect()
        return False

    async def _find_device(self) -> Any:
        if self._find is not None:
            return await self._find(self.options)
        bleak = _import_bleak()
        o = self.options
        if o.address:
            log.info("Looking for Bluetooth device %s (up to %.0f s)...", o.address, o.scan_timeout)
            dev = await bleak.BleakScanner.find_device_by_address(o.address, timeout=o.scan_timeout)
            if dev is None:
                raise BlePrinterNotFound(
                    "No Bluetooth device with address {} seen within {:.0f} s. Is the printer on, in range, "
                    "and not connected to the phone app or Chrome? (--ble-scan lists what's nearby)".format(
                        o.address, o.scan_timeout
                    )
                )
            return dev
        log.info("Scanning for a printer named %s* (up to %.0f s)...", o.name_prefix, o.scan_timeout)
        dev = await bleak.BleakScanner.find_device_by_filter(
            lambda d, adv: _adv_name(d, adv).startswith(o.name_prefix), timeout=o.scan_timeout
        )
        if dev is None:
            raise BlePrinterNotFound(
                "No Bluetooth printer named {}* found within {:.0f} s. Is it on, in range, and not "
                "connected to the phone app or Chrome (it takes one connection at a time)? If macOS "
                "never asked for Bluetooth permission, see the README's Bluetooth section.".format(
                    o.name_prefix, o.scan_timeout
                )
            )
        return dev

    def _make_client(self, target: Any) -> Any:
        factory = self._client_factory
        if factory is None:
            factory = _import_bleak().BleakClient
        return factory(target, disconnected_callback=self._on_disconnect, timeout=self.options.connect_timeout)

    async def connect(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self._queue is None:
            self._queue = asyncio.Queue()
        o = self.options
        target = await self._find_device()
        self.target_description = "{} ({})".format(
            getattr(target, "name", None) or "?", getattr(target, "address", target)
        )
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max(1, o.connect_attempts) + 1):
            client = self._make_client(target)
            log.info("Connecting to %s (attempt %d/%d)...", self.target_description, attempt, o.connect_attempts)
            try:
                await asyncio.wait_for(client.connect(), o.connect_timeout + 1.0)
            except asyncio.CancelledError:
                try:  # Ctrl-C mid-connect: don't leave a half-open link holding the printer
                    await asyncio.wait_for(client.disconnect(), 2.0)
                except BaseException:
                    pass
                raise
            except Exception as exc:
                last_exc = exc
                log.warning("Connect attempt %d failed: %s: %s", attempt, type(exc).__name__, exc or "timed out")
                try:
                    await asyncio.wait_for(client.disconnect(), 2.0)
                except Exception:
                    pass
                continue
            self._client = client
            self._disconnected = False
            break
        else:
            raise BleConnectError(
                "Could not connect to {} after {} attempt(s): {}. Is the printer on and not connected to "
                "the phone app or Chrome (it takes one connection at a time)?".format(
                    self.target_description, o.connect_attempts, last_exc or "timed out"
                )
            )
        try:
            await self._sleep(o.post_connect_delay)
            self._resolve_characteristics()
            await self._client.start_notify(bp.NOTIFY_UUID, self._on_notify)
            self._notifying = True
            await self._sleep(o.post_notify_delay)
        except BaseException:
            await self.disconnect()
            raise
        log.info("Connected to %s.", self.target_description)

    def _resolve_characteristics(self) -> None:
        services = getattr(self._client, "services", None)
        found: Dict[str, Any] = {}
        missing = []
        for uuid in (bp.DATA_UUID, bp.CONTROL_UUID, bp.NOTIFY_UUID):
            ch = services.get_characteristic(uuid) if services is not None else None
            if ch is None:
                missing.append("0x" + bp.short_uuid(uuid))
            found[uuid] = ch
        if missing:
            raise BleConnectError(
                "Connected, but the printer has no characteristic {} (service 0x{}). Is this an RW403B?".format(
                    ", ".join(missing), bp.short_uuid(bp.SERVICE_UUID)
                )
            )
        for uuid in (bp.DATA_UUID, bp.CONTROL_UUID):
            ch = found[uuid]
            props = list(getattr(ch, "properties", []) or [])
            resp = choose_response(props, self.options.write_mode)
            if resp:
                limit = MAX_WRITE_WITH_RESPONSE
            else:
                try:
                    limit = int(ch.max_write_without_response_size)
                except Exception:
                    limit = 20  # the BLE minimum (default MTU 23 - 3)
            self._response[uuid] = resp
            self._max_len[uuid] = limit
            log.info(
                "0x%s properties [%s] -> write %s, max %d B per write",
                bp.short_uuid(uuid), ", ".join(props) or "?", "with response" if resp else "without response",
                limit,
            )
        nprops = list(getattr(found[bp.NOTIFY_UUID], "properties", []) or [])
        log.info("0x%s properties [%s]", bp.short_uuid(bp.NOTIFY_UUID), ", ".join(nprops) or "?")

    async def disconnect(self) -> None:
        client = self._client
        if client is None:
            return
        self._closing = True
        if self._notifying and not self._disconnected:
            try:
                await asyncio.wait_for(client.stop_notify(bp.NOTIFY_UUID), 2.0)
            except Exception as exc:
                log.debug("stop_notify failed: %s", exc)
        self._notifying = False
        try:
            await asyncio.wait_for(client.disconnect(), 5.0)
        except Exception as exc:
            log.debug("disconnect failed: %s", exc)
        self._client = None
        log.info("Disconnected.")

    # -- callbacks (run on the event loop thread; bleak dispatches them there)

    def _on_disconnect(self, client: Any = None) -> None:
        # bleak calls this with the BleakClient instance it belongs to (the
        # top-level ``BleakClient.__init__`` wraps our callback with
        # ``functools.partial(disconnected_callback, self)``). A client from
        # a connect attempt we already tore down (a failed retry, or the
        # previous session) can still fire this after we've moved on to a
        # new, healthy ``self._client`` -- ignore anything that isn't the
        # client we're currently using, so a stale callback can't poison the
        # event queue for the new connection.
        if client is not None and client is not self._client:
            log.debug("Ignoring a disconnect callback from a stale/previous client.")
            return
        self._disconnected = True
        if not self._closing:
            log.warning("The printer disconnected.")
            if self._queue is not None:
                self._queue.put_nowait(bp.Event("disconnected"))

    def _on_notify(self, _sender: Any, data: Any) -> None:
        raw = bytes(data)
        log.debug("RX ABF3 %4dB %s", len(raw), raw.hex())
        for ev in self._decoder.feed(raw):
            log.debug("     = %s", ev)
            self._note(ev)
            if self._queue is not None:
                self._queue.put_nowait(ev)

    def _note(self, ev: bp.Event) -> None:
        """State the editor updates straight from the notification handler."""
        if ev.kind == "deviceinfo" and ev.info is not None:
            self._status_bits = ev.info.printstatus_bits
        elif ev.kind == "printed":
            self._status_bits = [0] * 9
            if self._printing:
                self._printed += 1
                log.info("Printer reports page %d of %d printed.", self._printed, self._expected)
        elif ev.kind == "printer_error":
            self._status_bits = bp.status_bits(ev.status)

    # -- low-level I/O

    async def _write(self, uuid: str, frame: bytes, label: str) -> None:
        if self._client is None:
            raise BleError("Not connected.")
        if self._disconnected:
            raise BleDisconnected("The printer disconnected (before {}).".format(label))
        short = bp.short_uuid(uuid)
        limit = self._max_len.get(uuid, MAX_WRITE_WITH_RESPONSE)
        resp = self._response.get(uuid, True)
        if len(frame) > limit:
            raise BleFrameTooLarge(
                "{} is a {}-byte frame, but this connection takes at most {} bytes per write on 0x{} "
                "(write {} response). {}".format(
                    label, len(frame), limit, short, "with" if resp else "without", _packet_size_hint(limit)
                )
            )
        log.debug("TX %s %4dB %s %s | %s", short, len(frame), "req" if resp else "cmd", frame.hex(), label)
        try:
            await asyncio.wait_for(
                self._client.write_gatt_char(uuid, frame, response=resp), self.options.write_timeout
            )
        except asyncio.TimeoutError as exc:
            raise BleTimeout(
                "Writing {} to 0x{} did not complete within {:.0f} s.".format(label, short, self.options.write_timeout)
            ) from exc
        except (BleError, asyncio.CancelledError):
            raise
        except Exception as exc:
            if self._disconnected:
                raise BleDisconnected("The printer disconnected during {}.".format(label)) from exc
            msg = "{}: {}".format(type(exc).__name__, exc)
            hint = ""
            if any(w in msg.lower() for w in ("length", "size", "too long", "too large", "mtu")):
                hint = " " + _packet_size_hint(None)
            raise BleWriteError(
                "Writing {} ({} bytes) to 0x{} failed: {}.{}".format(label, len(frame), short, msg, hint)
            ) from exc
        self.writes += 1
        self.bytes_sent += len(frame)

    async def _next_event(self, deadline: float) -> Optional[bp.Event]:
        """The next notification event, or ``None`` at ``deadline``. Raises on
        disconnect / cancel."""
        assert self._queue is not None and self._loop is not None
        remaining = deadline - self._loop.time()
        if remaining <= 0:
            try:
                ev = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return None
        else:
            try:
                ev = await asyncio.wait_for(self._queue.get(), remaining)
            except asyncio.TimeoutError:
                return None
        if ev.kind == "disconnected":
            raise BleDisconnected("The printer disconnected.")
        if ev.kind == "cancel_requested":
            raise _CancelRequested()
        return ev

    def _drain_idle(self) -> None:
        """Drop queued notifications before a request/response exchange
        (outside a job nothing queued matters). Cancel/disconnect still count."""
        assert self._queue is not None
        while True:
            try:
                ev = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if ev.kind == "disconnected":
                raise BleDisconnected("The printer disconnected.")
            if ev.kind == "cancel_requested":
                raise _CancelRequested()
            log.debug("dropping stale notification: %s", ev)

    # -- control messages

    async def device_info(self) -> bp.DeviceInfo:
        """Send DEVICEINFO and wait for the reply (spec 5)."""
        assert self._loop is not None
        self._drain_idle()
        await self._write(bp.CONTROL_UUID, bp.DEVICEINFO_FRAME, "DEVICEINFO")
        deadline = self._loop.time() + self.options.deviceinfo_timeout
        while True:
            ev = await self._next_event(deadline)
            if ev is None:
                raise BleTimeout(
                    "No DEVICEINFO reply within {:.0f} s (the editor gives up the same way). Run with "
                    "--debug to see what, if anything, came back.".format(self.options.deviceinfo_timeout)
                )
            if ev.kind == "deviceinfo" and ev.info is not None:
                self.device = ev.info
                log.info(
                    "Printer: firmware %r, BLE firmware %r, printstatus %r (%s), density %d, speed %d, "
                    "supportfunction %d",
                    ev.info.firmwarever, ev.info.blever, ev.info.printstatus,
                    ", ".join(ev.info.printstatus_flags) or "ready", ev.info.concentration, ev.info.speed,
                    ev.info.supportfunction,
                )
                return ev.info
            log.debug("ignoring %s while waiting for DEVICEINFO", ev)

    async def set_density(self, level: int) -> None:
        """PRINTINCONCENTRATION (1..16). INFERRED to persist in the printer; the
        editor sends it only when its dropdown changes and waits for nothing."""
        await self._write(bp.CONTROL_UUID, bp.density_frame(level), "PRINTINCONCENTRATION={}".format(level))
        await self._sleep(self.options.setting_delay)

    async def set_speed(self, level: int) -> None:
        """PRINTINGSPEED (1..8); see ``set_density``."""
        await self._write(bp.CONTROL_UUID, bp.speed_frame(level), "PRINTINGSPEED={}".format(level))
        await self._sleep(self.options.setting_delay)

    async def printer_selftest(self) -> bool:
        """SELFTEST: the printer prints its own self-test page. ``True`` if it
        answered OK within the DEVICEINFO timeout."""
        assert self._loop is not None
        self._drain_idle()
        await self._write(bp.CONTROL_UUID, bp.SELFTEST_FRAME, "SELFTEST")
        deadline = self._loop.time() + self.options.deviceinfo_timeout
        while True:
            ev = await self._next_event(deadline)
            if ev is None:
                log.warning("No reply to SELFTEST (the editor doesn't wait for one either).")
                return False
            if ev.kind == "ok" and ev.detail == "SELFTEST":
                return True
            log.debug("ignoring %s while waiting for the SELFTEST reply", ev)

    async def check_ready(self, label_height_mm: Optional[float] = None) -> bp.DeviceInfo:
        """The editor's pre-print checks (spec 5)."""
        info = await self.device_info()
        if not info.ready and info.only_busy_or_calibrating:
            log.info(
                "Printer is %s; waiting %.0f s and asking again (as the editor does).",
                " and ".join(info.printstatus_flags), self.options.busy_retry_delay,
            )
            await self._sleep(self.options.busy_retry_delay)
            info = await self.device_info()
        if not info.ready:
            raise BlePrinterStatus(
                "The printer is not ready: {} (printstatus {!r}). Nothing was printed.".format(
                    ", ".join(info.printstatus_flags), info.printstatus
                )
            )
        limit = bp.MAX_LABEL_MM_WITHOUT_SEND_WHILE_PRINTING
        if label_height_mm is not None and label_height_mm > limit and not info.support_send_when_printing:
            raise BleJobFailed(
                "Labels taller than {:.0f} mm need a printer that reports 'send while printing' "
                "(supportfunction bit 7); this one doesn't (supportfunction {}).".format(
                    limit, info.supportfunction
                )
            )
        return info

    # -- printing

    async def print_pages(
        self, pages: Sequence[bp.BlePage], copies: int = 1, *, label_height_mm: Optional[float] = None
    ) -> PrintResult:
        """Print already-built pages (see ``ble_protocol.BlePage``). Raises a
        ``BleError`` subclass on any failure; ``BleCancelled`` after Ctrl-C."""
        if not pages:
            raise ValueError("nothing to print")
        if copies < 1:
            raise ValueError("copies must be at least 1")
        assert self._loop is not None
        started = time.monotonic()
        try:
            self._check_cancel()
            info = await self.check_ready(label_height_mm)
        except _CancelRequested:
            raise BleCancelled("Cancelled before anything was sent.") from None
        per_size = self.options.per_size or info.per_size
        self._preflight(pages, copies, per_size)
        sends, expected = bp.plan_sends(pages, copies, support_send_when_printing=info.support_send_when_printing)
        self._printed = 0
        self._expected = expected
        self._printing = True
        self.end_sent = False
        try:
            if self.options.density is not None:
                await self.set_density(self.options.density)
            if self.options.speed is not None:
                await self.set_speed(self.options.speed)
            for send_no, (pi, page_field) in enumerate(sends, 1):
                page = pages[pi]
                log.info(
                    "Sending page %d of %d (send %d/%d, page field %d): %dx%d dots, %d section(s), %d packet(s).",
                    pi + 1, len(pages), send_no, len(sends), page_field, page.width_dots, page.height,
                    len(page.sections),
                    sum(len(page.packets(s, per_size)) for s in range(1, len(page.sections) + 1)),
                )
                await self._send_page(page, page_field, per_size)
                await self._sleep(bp.PER_PAGE_DELAY_S)
            self._check_cancel()
            self.end_sent = True
            await self._write(bp.CONTROL_UUID, bp.PRINTINEND_FRAME, "PRINTINEND")
            await self._wait_printed(expected)
        except _CancelRequested:
            confirmed = await self._send_cancel()
            raise BleCancelled(
                "Cancelled: sent CANCELPRINTING ({}).".format(
                    "the printer confirmed" if confirmed else
                    "no confirmation from the printer" if confirmed is False else "could not be sent"
                )
            ) from None
        except asyncio.CancelledError:
            log.warning("Interrupted: sending CANCELPRINTING.")
            try:
                await asyncio.wait_for(
                    self._send_cancel(), self.options.write_timeout + self.options.cancel_reply_timeout
                )
            except BaseException:
                pass
            raise
        finally:
            self._printing = False
        return PrintResult(
            pages=len(pages), sends=len(sends), copies=copies, expected=expected, printed=self._printed,
            writes=self.writes, bytes_sent=self.bytes_sent, resends=self.resends, per_size=per_size,
            device=info, seconds=time.monotonic() - started,
        )

    def _preflight(self, pages: Sequence[bp.BlePage], copies: int, per_size: int) -> None:
        """Fail before the first DEVICEPRINT if any frame won't fit one write."""
        limit = self._max_len.get(bp.DATA_UUID, MAX_WRITE_WITH_RESPONSE)
        biggest = 0
        for page in pages:
            for s in range(1, len(page.sections) + 1):
                biggest = max(biggest, len(page.section_frames(s, page_field=copies, per_size=per_size)[0]))
        if biggest > limit:
            raise BleFrameTooLarge(
                "The largest print frame is {} bytes ({}-byte packets), but this connection takes at most {} "
                "bytes per write on 0x{} (write {} response). Nothing was printed. {}".format(
                    biggest, per_size, limit, bp.short_uuid(bp.DATA_UUID),
                    "with" if self._response.get(bp.DATA_UUID, True) else "without",
                    _packet_size_hint(limit, overhead=biggest - per_size),
                )
            )

    async def _send_page(self, page: bp.BlePage, page_field: int, per_size: int) -> None:
        n = len(page.sections)
        resent = set()
        s = 1
        while s <= n:
            frames = page.section_frames(s, page_field=page_field, per_size=per_size)
            for k, frame in enumerate(frames, 1):
                self._check_cancel()
                await self._write(bp.DATA_UUID, frame, "DEVICEPRINT sec {}/{} pkt {}/{}".format(s, n, k, len(frames)))
                await self._sleep(bp.PER_PACKET_DELAY_S)
            reply = await self._wait_section_reply(s, n)
            pause = bp.ack_delay_s(len(page.sections[s - 1]))
            if reply.kind == "ack":
                log.debug("section %d/%d acknowledged", s, n)
                s += 1
            else:
                k = reply.section
                if not 1 <= k <= n:
                    raise BleJobFailed(
                        "The printer asked to resend section {}, but the page only has {}.".format(k, n)
                    )
                if k in resent:
                    raise BleJobFailed(
                        "The printer asked for section {} a second time; giving up (the editor does the "
                        "same).".format(k)
                    )
                resent.add(k)
                self.resends += 1
                log.warning("Printer asked to resend section %d of %d; resending from there.", k, n)
                s = k
            await self._sleep(pause)
            if pause > 0:
                await self._drop_late_replies()

    async def _wait_section_reply(self, s: int, n: int) -> bp.Event:
        assert self._loop is not None
        deadline = self._loop.time() + self.options.ack_timeout
        while True:
            ev = await self._next_event(deadline)
            if ev is None:
                raise BleTimeout(
                    "No acknowledgement for section {} of {} within {:.0f} s; aborting (the editor gives "
                    "up the same way: 'PrintTimeout').".format(s, n, self.options.ack_timeout)
                )
            reply = await self._job_event(ev)
            if reply is not None:
                return reply

    async def _job_event(self, ev: bp.Event) -> Optional[bp.Event]:
        """Handle a notification during a job the way the editor's ``V()`` does.
        Returns ack/resend events to the caller, absorbs the rest, raises on
        errors."""
        k = ev.kind
        if k in ("ack", "resend"):
            return ev
        if k == "printer_error":
            raise BlePrinterStatus(
                "The printer reported an error while printing: {} ({}).".format(", ".join(ev.flags), ev.detail)
            )
        if k == "stopped":
            await self._sleep(0.5)
            if self._status_bits[bp.HATCH_OPEN_BIT]:
                log.warning("Printer sent 'print stopped' with the hatch open; carrying on, as the editor does.")
                return None
            raise BleJobFailed("The printer stopped the job (report 13/'1').")
        if k == "print_error":
            raise BleJobFailed("The printer rejected the print data: DEVICEPRINT code {}, {}.".format(ev.code, ev.detail))
        if k == "cancel_error":
            log.warning("Printer reported a CANCELPRINTING error: %s", ev.detail)
        else:
            log.debug("(%s)", ev)
        return None

    async def _drop_late_replies(self) -> None:
        """Acks/resend requests that arrive during the post-section pause are
        ignored by the editor; so are they here. Anything else is handled."""
        assert self._queue is not None
        while True:
            try:
                ev = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if ev.kind == "disconnected":
                raise BleDisconnected("The printer disconnected.")
            if ev.kind == "cancel_requested":
                raise _CancelRequested()
            if ev.kind in ("ack", "resend"):
                log.debug("ignoring %s that arrived during the post-section pause (as the editor does)", ev)
                continue
            await self._job_event(ev)

    async def _wait_printed(self, expected: int) -> None:
        assert self._loop is not None
        timeout = self.options.done_timeout + self.options.done_timeout_per_page * expected
        deadline = self._loop.time() + timeout
        while self._printed < expected:
            ev = await self._next_event(deadline)
            if ev is None:
                raise BleTimeout(
                    "Every section was acknowledged and PRINTINEND was sent, but the printer reported only "
                    "{} of {} page(s) printed within {:.0f} s. Check the printer.".format(
                        self._printed, expected, timeout
                    )
                )
            reply = await self._job_event(ev)
            if reply is not None:
                log.debug("ignoring %s after the last section", reply)

    async def _send_cancel(self) -> Optional[bool]:
        """CANCELPRINTING, then up to ``cancel_reply_timeout`` for the OK.
        ``True`` confirmed, ``False`` no/negative reply, ``None`` not sent."""
        if self._client is None or self._disconnected or self._loop is None or self._queue is None:
            return None
        try:
            await self._write(bp.CONTROL_UUID, bp.CANCELPRINTING_FRAME, "CANCELPRINTING")
        except BleError as exc:
            log.warning("Could not send CANCELPRINTING: %s", exc)
            return None
        deadline = self._loop.time() + self.options.cancel_reply_timeout
        while True:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                return False
            try:
                ev = await asyncio.wait_for(self._queue.get(), remaining)
            except asyncio.TimeoutError:
                return False
            if ev.kind == "cancel_ok":
                return True
            if ev.kind == "cancel_error":
                log.warning("Printer answered CANCELPRINTING with an error: %s", ev.detail)
                return False
            if ev.kind == "disconnected":
                return None


def _packet_size_hint(limit: Optional[int], overhead: int = 33) -> str:
    """What to tell the user when a frame is too long. ``overhead`` = frame
    bytes around the image data (33 for this protocol's DEVICEPRINT frames)."""
    fallback = bp.PER_SIZE_BLE_1_0_8
    overhead = max(33, overhead)
    if limit is not None and limit - overhead < fallback:
        size = max(16, limit - overhead)
        return (
            "Retry with --ble-packet-size {} (or --ble-write response, if the characteristic allows "
            "writes with response); see the README's Bluetooth section.".format(size)
        )
    return (
        "Retry with --ble-packet-size {} (the packet size the editor itself uses on BLE firmware 1.0.8; "
        "181-byte frames); see the README's Bluetooth section.".format(fallback)
    )


# --------------------------------------------------------------------------- sync entry points (CLI)


def make_sigint_handler(printer: BlePrinter, task: "asyncio.Task[Any]") -> Callable[[], None]:
    """First Ctrl-C during a job: cancel it cleanly (CANCELPRINTING). Any other
    Ctrl-C (or the first one when no job is running): stop now."""
    presses = [0]

    def handler() -> None:
        presses[0] += 1
        if presses[0] == 1 and printer.printing:
            log.warning("Ctrl-C: cancelling the job (sending CANCELPRINTING). Press Ctrl-C again to stop waiting.")
            printer.request_cancel()
        else:
            log.warning("Ctrl-C: stopping.")
            task.cancel()

    return handler


async def run_session(
    body: Callable[[BlePrinter], Awaitable[T]],
    options: Optional[BleOptions] = None,
    *,
    handle_sigint: bool = True,
    **inject: Any,
) -> T:
    """Connect, run ``body(printer)``, always disconnect. With ``handle_sigint``
    Ctrl-C is routed through ``make_sigint_handler`` while connected."""
    printer = BlePrinter(options, **inject)
    loop = asyncio.get_running_loop()
    installed = False
    if handle_sigint:
        task = asyncio.current_task()
        try:
            loop.add_signal_handler(signal.SIGINT, make_sigint_handler(printer, task))  # type: ignore[arg-type]
            installed = True
        except (NotImplementedError, RuntimeError, ValueError):
            installed = False
    try:
        async with printer:
            return await body(printer)
    except _CancelRequested:
        raise BleCancelled("Cancelled.") from None
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)


def _run(coro: Awaitable[T]) -> T:
    try:
        return asyncio.run(coro)  # type: ignore[arg-type]
    except asyncio.CancelledError:
        raise KeyboardInterrupt from None


def print_job(
    pages: Sequence[bp.BlePage],
    copies: int = 1,
    options: Optional[BleOptions] = None,
    *,
    label_height_mm: Optional[float] = None,
    **inject: Any,
) -> PrintResult:
    """Synchronous: connect, print, disconnect."""

    async def body(p: BlePrinter) -> PrintResult:
        return await p.print_pages(pages, copies, label_height_mm=label_height_mm)

    return _run(run_session(body, options, **inject))


def query_device_info(options: Optional[BleOptions] = None, **inject: Any) -> bp.DeviceInfo:
    """Synchronous: connect, [opt-in density/speed], DEVICEINFO, disconnect."""

    async def body(p: BlePrinter) -> bp.DeviceInfo:
        o = p.options
        if o.density is not None or o.speed is not None:
            await p.device_info()
            if o.density is not None:
                await p.set_density(o.density)
            if o.speed is not None:
                await p.set_speed(o.speed)
        return await p.device_info()

    return _run(run_session(body, options, **inject))


def printer_selftest(options: Optional[BleOptions] = None, **inject: Any) -> bool:
    """Synchronous: ask the printer to print its own self-test page."""

    async def body(p: BlePrinter) -> bool:
        await p.check_ready()
        return await p.printer_selftest()

    return _run(run_session(body, options, **inject))


async def scan_async(
    timeout: float = 10.0,
    name_prefix: str = bp.NAME_PREFIX,
    *,
    discover: Optional[Callable[..., Awaitable[Any]]] = None,
) -> Dict[str, Any]:
    """Scan for ``timeout`` s; ``{"printers": [{address, name, rssi}, ...]
    (strongest first), "others": n}`` -- other devices are only counted."""
    if discover is None:
        discover = _import_bleak().BleakScanner.discover
    found = await discover(timeout=timeout, return_adv=True)
    printers: List[Dict[str, Any]] = []
    others = 0
    for dev, adv in found.values():
        name = _adv_name(dev, adv)
        if name.startswith(name_prefix):
            printers.append({"address": dev.address, "name": name, "rssi": getattr(adv, "rssi", None)})
        else:
            others += 1
    printers.sort(key=lambda d: -(d["rssi"] if d["rssi"] is not None else -999))
    return {"printers": printers, "others": others}


def scan(timeout: float = 10.0, name_prefix: str = bp.NAME_PREFIX, **inject: Any) -> Dict[str, Any]:
    """Synchronous ``scan_async``. The only code path that lists devices."""
    return _run(scan_async(timeout, name_prefix, **inject))
