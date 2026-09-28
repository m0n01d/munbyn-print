"""MunbynBLE bridge: loopback TCP (TSPL in) -> Bluetooth LE (out).

Run by ``MunbynBLE.app`` (``scripts/install-ble-bridge.sh``), whose
Info.plist carries ``NSBluetoothAlwaysUsageDescription`` -- the only kind of
process macOS lets use Bluetooth. cupsd can't (it's a system daemon), and
neither can anything launched from an app without that key (the Claude app:
macOS kills the process). So the CUPS queue "Munbyn RW403B (Bluetooth)"
(``socket://127.0.0.1:9100``), ``print_label.py --ble`` and the web UI all
send a TSPL job here, and this process does the Bluetooth part.

Per connection (``127.0.0.1`` only, port = config ``ble_bridge_port``):

* first line ``MUNBYN-STATUS`` -> one JSON line back (bridge version, pid,
  printer address, busy/queued, last job, Bluetooth permission state).
  ``MUNBYN-STATUS DEVICEINFO`` also asks the printer (over Bluetooth, queued
  like a job) and adds ``deviceinfo``.
* anything else is a print job: read to EOF (the CUPS socket backend and the
  CLI half-close after sending), parse (``munbyn.tspl_parse``: the verified
  TSPL subset only; clear bit = black -> Bluetooth's 1 = black), print over
  Bluetooth, then reply with one JSON line (``{"ok": true, ...}`` or
  ``{"ok": false, "error": ...}``) and close. CUPS ignores the reply; the CLI
  shows it.

Jobs run one at a time, in arrival order. Rows are printed as received
(whoever built the TSPL already applied the feed scale); ``SIZE``/``GAP``/
``DENSITY``/``SPEED`` are logged and ignored (the printer uses its stored
settings); ``PRINT m,n`` becomes m*n Bluetooth copies. The printer address is
re-read from the config for every job.

Failures never stop the server: a job that doesn't parse is logged and posts
a macOS notification; a Bluetooth failure is retried once (fresh connection)
unless a label may already have come out (PRINTINEND sent or a "printed"
report seen), then logged and notified. Log:
``~/Library/Logs/munbyn-ble-bridge.log``.

Never run this from the Claude app or anything else without the Bluetooth
permission: macOS kills the process at the first Bluetooth call (SIGABRT).
Tests drive ``Bridge`` with a fake transport and never import bleak.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import config as config_mod
from . import tspl_parse
from .ble_bridge_client import BRIDGE_NAME, DEFAULT_PORT, HOST, STATUS_MAGIC, bridge_port

VERSION = "1.0"
LOG_PATH = Path.home() / "Library" / "Logs" / "munbyn-ble-bridge.log"
#: Refuse bigger jobs (a 4x6 page is ~127 kB; this is hundreds of pages).
MAX_JOB_BYTES = 64 * 1024 * 1024
#: A connection that stops sending for this long without closing is dropped.
READ_IDLE_TIMEOUT_S = 60.0
#: How long to wait before the one retry of a failed Bluetooth job.
RETRY_DELAY_S = 3.0
NOTIFY_TITLE = "Munbyn BLE"

log = logging.getLogger("munbyn.ble_bridge")
_log_path: Path = LOG_PATH  # where setup_logging() pointed the log (shown in status)

Notifier = Callable[[str, str], Awaitable[None]]


# --------------------------------------------------------------------------- transport


class BleakBridgeTransport:
    """The real thing: one fresh ``BlePrinter`` connection per job (the printer
    takes one central at a time; disconnecting between jobs lets the phone app
    or Chrome in)."""

    def __init__(self, options_factory: Callable[[], Any]) -> None:
        self._options_factory = options_factory

    async def print_pages(self, pages: List[Any], copies: int, *, label_height_mm: Optional[float]) -> Any:
        from . import ble_transport

        printer = ble_transport.BlePrinter(self._options_factory())
        try:
            async with printer:
                return await printer.print_pages(pages, copies, label_height_mm=label_height_mm)
        except Exception as exc:
            # Tell the retry policy whether a label may already be out.
            try:
                exc.committed = bool(printer.end_sent or printer.printed > 0)  # type: ignore[attr-defined]
            except Exception:
                pass
            raise

    async def device_info(self) -> Any:
        from . import ble_transport

        async with ble_transport.BlePrinter(self._options_factory()) as printer:
            return await printer.device_info()


def ble_options_from_config(cfg: Optional[Dict[str, Any]] = None) -> Any:
    from . import ble_transport

    cfg = cfg if cfg is not None else config_mod.load()
    return ble_transport.BleOptions(address=cfg.get("ble_address") or None)


# --------------------------------------------------------------------------- notifications / Bluetooth permission


async def notify_macos(title: str, message: str) -> None:
    """A macOS notification via osascript. The text travels as argv, never
    spliced into the AppleScript source."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/osascript",
            "-e", "on run argv",
            "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e", "end run",
            title, message[:240],
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), 10.0)
    except Exception as exc:  # a missing notification must never break printing
        log.warning("Could not post a notification (%s): %s", exc, message)


_AUTH_NAMES = {0: "not determined", 1: "restricted", 2: "denied", 3: "allowed"}


def bluetooth_authorization() -> Optional[str]:
    """This process's Bluetooth permission, without asking for it or touching
    the radio (``CBManager.authorization``, macOS 10.15+). ``None`` if unknown."""
    try:
        import CoreBluetooth  # pyobjc, installed with bleak

        value = int(CoreBluetooth.CBManager.authorization())
    except Exception:
        return None
    return _AUTH_NAMES.get(value, str(value))


_warmup_keep: List[Any] = []


def request_bluetooth_permission() -> Optional[str]:
    """If macOS hasn't asked yet, create a ``CBCentralManager`` (no scan, no
    connection) so the "MunbynBLE would like to use Bluetooth" prompt shows at
    install time instead of during the first print. Returns the state before."""
    state = bluetooth_authorization()
    if state != "not determined":
        return state
    # If the app running us lacks NSBluetoothAlwaysUsageDescription, macOS
    # kills this process right here (SIGABRT, TCC) with nothing raised for
    # Python to catch -- so this line, not the one after the call, is what
    # tells a reader of the log why the process vanished.
    log.info("Bluetooth permission: not determined; creating a CBCentralManager to trigger the prompt "
              "(if this is the last line in the log, macOS just killed this process for touching Bluetooth "
              "without a usage description -- see CLAUDE.md's TCC note)")
    try:
        import CoreBluetooth
        from libdispatch import dispatch_queue_create

        queue = dispatch_queue_create(b"com.m0n01d.munbyn-ble-bridge.permission", None)
        _warmup_keep.append(CoreBluetooth.CBCentralManager.alloc().initWithDelegate_queue_(None, queue))
    except Exception as exc:
        log.warning("Could not ask for Bluetooth permission up front: %s", exc)
    return state


# --------------------------------------------------------------------------- the bridge


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


@dataclasses.dataclass
class _Request:
    kind: str  # "print" | "deviceinfo"
    job_id: int
    data: bytes
    peer: str
    future: "asyncio.Future[Dict[str, Any]]"
    received: float = dataclasses.field(default_factory=time.monotonic)


class Bridge:
    """The TCP server plus its one-at-a-time worker.

    ``transport`` needs ``async print_pages(pages, copies, *, label_height_mm)``
    (returning something with ``printed``/``expected``/``seconds``/``resends``)
    and ``async device_info()``. A failure may carry ``committed=True`` when a
    label may already have printed (then it isn't retried)."""

    def __init__(
        self,
        transport: Any,
        *,
        port: int = DEFAULT_PORT,
        notifier: Optional[Notifier] = None,
        config_loader: Callable[[], Dict[str, Any]] = config_mod.load,
        authorization: Callable[[], Optional[str]] = lambda: None,
        retry_delay: float = RETRY_DELAY_S,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        read_idle_timeout: float = READ_IDLE_TIMEOUT_S,
        max_job_bytes: int = MAX_JOB_BYTES,
    ) -> None:
        self.transport = transport
        self.port = port
        self._notify = notifier or notify_macos
        self._config_loader = config_loader
        self._authorization = authorization
        self._retry_delay = retry_delay
        self._sleep = sleep
        self._idle = read_idle_timeout
        self._max_bytes = max_job_bytes
        self._queue: "Optional[asyncio.Queue[_Request]]" = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._worker_task: "Optional[asyncio.Task[None]]" = None
        self._current: Optional[_Request] = None
        self._next_id = 1
        self.started = _now()
        self.last_job: Optional[Dict[str, Any]] = None
        self.jobs_done = 0
        self.jobs_failed = 0
        self._handlers: "set[asyncio.Task[Any]]" = set()

    # -- lifecycle

    async def start(self) -> int:
        """Bind 127.0.0.1:<port> (0 = any free port, for tests) and start the
        worker. Returns the bound port."""
        self._queue = asyncio.Queue()
        self._server = await asyncio.start_server(self._on_connection, HOST, self.port)
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]
        self._worker_task = asyncio.ensure_future(self._worker())
        log.info("MunbynBLE bridge %s listening on %s:%d (pid %d).", VERSION, HOST, self.port, os.getpid())
        return self.port

    async def close(self) -> None:
        """Stop accepting, cancel the running job (the transport sends
        CANCELPRINTING) and fail anything still queued."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await asyncio.wait_for(self._worker_task, 15.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        if self._queue is not None:
            while not self._queue.empty():
                req = self._queue.get_nowait()
                if not req.future.done():
                    req.future.set_result({"ok": False, "job": req.job_id, "error": "the bridge is shutting down"})
        for task in list(self._handlers):
            task.cancel()
        log.info("MunbynBLE bridge stopped.")

    # -- status

    def status(self) -> Dict[str, Any]:
        cfg = self._safe_config()
        return {
            "bridge": BRIDGE_NAME,
            "version": VERSION,
            "pid": os.getpid(),
            "port": self.port,
            "started": self.started,
            "printer_address": cfg.get("ble_address") or None,
            "busy": self._current is not None,
            "queued": self._queue.qsize() if self._queue is not None else 0,
            "jobs_done": self.jobs_done,
            "jobs_failed": self.jobs_failed,
            "last_job": self.last_job,
            "bluetooth_authorization": self._authorization(),
            "log": str(_log_path),
        }

    def _safe_config(self) -> Dict[str, Any]:
        try:
            return dict(self._config_loader())
        except Exception as exc:
            log.warning("Could not read the config: %s", exc)
            return dict(config_mod.DEFAULTS)

    # -- connections

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        peer = "{}:{}".format(*(writer.get_extra_info("peername") or ("?", "?"))[:2])
        try:
            await self._serve(reader, writer, peer)
        except asyncio.CancelledError:
            raise
        except Exception:  # one bad connection must never take the server down
            log.exception("Error while handling a connection from %s", peer)
        finally:
            if task is not None:
                self._handlers.discard(task)
            try:
                writer.close()
            except Exception:
                pass

    async def _read_some(self, reader: asyncio.StreamReader) -> bytes:
        return await asyncio.wait_for(reader.read(65536), self._idle)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, peer: str) -> None:
        buf = bytearray()
        eof = False
        # Enough to see whether the first line is MUNBYN-STATUS.
        while not eof and b"\n" not in buf[:128] and len(buf) < 128:
            try:
                chunk = await self._read_some(reader)
            except asyncio.TimeoutError:
                log.warning("Connection from %s sent nothing for %.0f s; dropped.", peer, self._idle)
                return
            if not chunk:
                eof = True
            buf += chunk
        first = bytes(buf.split(b"\n", 1)[0]).strip()
        if first.startswith(STATUS_MAGIC):
            args = first[len(STATUS_MAGIC):].strip().upper().split()
            reply = await self._status_request(peer, deviceinfo=b"DEVICEINFO" in args)
            await self._reply(writer, reply)
            return
        while not eof:
            try:
                chunk = await self._read_some(reader)
            except asyncio.TimeoutError:
                # CUPS's socket backend and our client half-close after the job
                # (EOF). If a sender doesn't, but what arrived is a whole job,
                # print it rather than lose it.
                if _is_complete_job(bytes(buf)):
                    log.warning("Job from %s: no end-of-file after %.0f s idle, but the %d bytes received parse as "
                                "a complete job; printing them.", peer, self._idle, len(buf))
                    break
                log.error("Job from %s stalled after %d bytes (no data for %.0f s); dropped.", peer, len(buf),
                          self._idle)
                await self._reply(writer, {"ok": False, "error": "job stalled; nothing printed"})
                return
            if not chunk:
                eof = True
            buf += chunk
            if len(buf) > self._max_bytes:
                msg = "job larger than {} MB; refused, nothing printed".format(self._max_bytes // (1024 * 1024))
                log.error("Job from %s: %s", peer, msg)
                await self._notify(NOTIFY_TITLE, "Print job refused: " + msg)
                await self._reply(writer, {"ok": False, "error": msg})
                return
        if not buf:
            log.info("Empty connection from %s (a port check?); ignored.", peer)
            return
        result = await self.submit("print", bytes(buf), peer)
        await self._reply(writer, result)

    async def _status_request(self, peer: str, *, deviceinfo: bool) -> Dict[str, Any]:
        reply = self.status()
        if deviceinfo:
            res = await self.submit("deviceinfo", b"", peer)
            if res.get("ok"):
                reply["deviceinfo"] = res.get("deviceinfo")
            else:
                reply["deviceinfo_error"] = res.get("error")
        return reply

    async def _reply(self, writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
        try:
            writer.write((json.dumps(obj, default=str) + "\n").encode("utf-8"))
            await asyncio.wait_for(writer.drain(), 5.0)
        except Exception as exc:  # e.g. CUPS already hung up -- the job result is logged anyway
            log.debug("Could not send the reply: %s", exc)

    # -- the queue

    async def submit(self, kind: str, data: bytes, peer: str = "local") -> Dict[str, Any]:
        """Queue one request and wait for its result dict."""
        assert self._queue is not None, "start() first"
        job_id = self._next_id
        self._next_id += 1
        fut: "asyncio.Future[Dict[str, Any]]" = asyncio.get_running_loop().create_future()
        req = _Request(kind, job_id, data, peer, fut)
        ahead = self._queue.qsize() + (1 if self._current is not None else 0)
        if kind == "print":
            log.info("Job #%d from %s: %d bytes%s.", job_id, peer, len(data),
                     " ({} ahead of it)".format(ahead) if ahead else "")
        await self._queue.put(req)
        return await asyncio.shield(fut)

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            req = await self._queue.get()
            self._current = req
            try:
                if req.kind == "deviceinfo":
                    result = await self._run_deviceinfo(req)
                else:
                    result = await self.run_print(req.data, req.job_id)
            except asyncio.CancelledError:
                if not req.future.done():
                    req.future.set_result({"ok": False, "job": req.job_id, "error": "cancelled (bridge stopping)"})
                raise
            except Exception as exc:  # belt and braces: run_print shouldn't raise
                log.exception("Job #%d crashed", req.job_id)
                result = {"ok": False, "job": req.job_id, "error": "{}: {}".format(type(exc).__name__, exc)}
            finally:
                self._current = None
            if not req.future.done():
                req.future.set_result(result)

    async def _run_deviceinfo(self, req: _Request) -> Dict[str, Any]:
        log.info("DEVICEINFO request #%d from %s.", req.job_id, req.peer)
        try:
            info = await self.transport.device_info()
        except Exception as exc:
            log.error("DEVICEINFO #%d failed: %s: %s", req.job_id, type(exc).__name__, exc)
            return {"ok": False, "job": req.job_id, "error": "{}: {}".format(type(exc).__name__, exc)}
        d = dataclasses.asdict(info) if dataclasses.is_dataclass(info) else dict(info)
        log.info("DEVICEINFO #%d: %s", req.job_id, d)
        return {"ok": True, "job": req.job_id, "deviceinfo": d}

    # -- one print job

    def _finish(self, result: Dict[str, Any]) -> Dict[str, Any]:
        result["finished"] = _now()
        self.last_job = dict(result)
        if result.get("ok"):
            self.jobs_done += 1
        else:
            self.jobs_failed += 1
        return result

    async def _fail(self, job_id: int, message: str, **extra: Any) -> Dict[str, Any]:
        log.error("Job #%d FAILED: %s", job_id, message)
        await self._notify(NOTIFY_TITLE, "Print failed: {}".format(message))
        return self._finish(dict({"ok": False, "job": job_id, "error": message}, **extra))

    async def run_print(self, data: bytes, job_id: int = 0) -> Dict[str, Any]:
        """Parse and print one TSPL job. Never raises (except cancellation)."""
        from . import ble_protocol as bp

        started = time.monotonic()
        try:
            job = tspl_parse.parse_job(data)
        except tspl_parse.TsplParseError as exc:
            return await self._fail(job_id, "not a print job the bridge understands: {}".format(exc))
        ignored = ", ".join("{} {}".format(k, v) for k, v in job.header.items())
        if ignored:
            log.info("Job #%d header (not sent over Bluetooth; the printer uses its stored settings): %s",
                     job_id, ignored)
        for note in job.notes:
            log.warning("Job #%d: %s", job_id, note)
        cfg = self._safe_config()
        feed_scale = cfg.get("ble_feed_scale") or cfg.get("feed_scale") or 1.0
        try:
            runs = []
            for pages, copies in tspl_parse.group_pages(job.pages):
                ble_pages = [bp.BlePage.from_image(bp.compose_page(p.image)) for p in pages]
                tallest = max(p.image.height for p in pages)
                # Physical label length: rows are feed-stretched by 1/feed_scale.
                height_mm = tallest / tspl_parse.DOTS_PER_MM * float(feed_scale)
                runs.append((ble_pages, copies, height_mm))
        except (ValueError, ImportError) as exc:
            return await self._fail(job_id, "could not build the Bluetooth pages: {}".format(exc))
        log.info("Job #%d: %d page(s), %d label(s) in all, %s.", job_id, len(job.pages), job.total_labels,
                 ", ".join("{}x{} dots".format(p.image.width, p.image.height) for p in job.pages[:3])
                 + (" ..." if len(job.pages) > 3 else ""))
        printed = expected = resends = attempts = 0
        for n, (ble_pages, copies, height_mm) in enumerate(runs, 1):
            for attempt in (1, 2):
                attempts += 1
                try:
                    res = await self.transport.print_pages(ble_pages, copies, label_height_mm=height_mm)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    reason = "{}: {}".format(type(exc).__name__, exc)
                    committed = bool(getattr(exc, "committed", False))
                    if attempt == 1 and _retryable(exc) and not committed:
                        log.warning("Job #%d part %d/%d: Bluetooth failed (%s); reconnecting and retrying once in "
                                    "%.0f s.", job_id, n, len(runs), reason, self._retry_delay)
                        await self._sleep(self._retry_delay)
                        continue
                    why = "" if attempt == 2 else (
                        " (not retried: a label may already have printed)" if committed else " (not retried)")
                    return await self._fail(
                        job_id, "{}{}".format(reason, why), pages=len(job.pages), printed=printed,
                        attempts=attempts, seconds=round(time.monotonic() - started, 1),
                    )
                printed += int(getattr(res, "printed", 0) or 0)
                expected += int(getattr(res, "expected", 0) or 0)
                resends += int(getattr(res, "resends", 0) or 0)
                break
        seconds = round(time.monotonic() - started, 1)
        log.info("Job #%d printed: %d page(s), printer reported %d of %d label(s), %d resend(s), %d attempt(s), "
                 "%.1f s.", job_id, len(job.pages), printed, expected, resends, attempts, seconds)
        return self._finish({
            "ok": True, "job": job_id, "pages": len(job.pages), "labels": job.total_labels, "printed": printed,
            "expected": expected, "resends": resends, "attempts": attempts, "seconds": seconds,
        })


def _is_complete_job(data: bytes) -> bool:
    """Ends with a PRINT line and parses cleanly (see ``_serve``)."""
    tail = data.rstrip(b"\r\n \t").rsplit(b"\n", 1)[-1].strip().upper()
    if not tail.startswith(b"PRINT"):
        return False
    try:
        tspl_parse.parse_job(data)
    except tspl_parse.TsplParseError:
        return False
    return True


def _retryable(exc: BaseException) -> bool:
    """Worth one more try on a fresh connection? Not when retrying can't help
    (the job itself is wrong, the printer reported a fault such as no paper or
    an open cover, or the user cancelled)."""
    name = type(exc).__name__
    if name in ("BleFrameTooLarge", "BleJobFailed", "BlePrinterStatus", "BleCancelled", "BleUnavailable"):
        return False
    return not isinstance(exc, (ValueError, TypeError, AssertionError))


# --------------------------------------------------------------------------- entry point


def setup_logging(path: Path, debug: bool = False) -> None:
    global _log_path
    _log_path = path
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(str(path), maxBytes=2 * 1024 * 1024, backupCount=3,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("munbyn")  # the bridge and munbyn.ble (the transport)
    root.addHandler(handler)
    if sys.stderr.isatty():
        root.addHandler(logging.StreamHandler(sys.stderr))
    root.setLevel(logging.DEBUG if debug else logging.INFO)


async def _amain(args: argparse.Namespace) -> int:
    port = args.port if args.port is not None else bridge_port(config_mod.load())
    bridge = Bridge(BleakBridgeTransport(ble_options_from_config), port=port, authorization=bluetooth_authorization)
    try:
        await bridge.start()
    except OSError as exc:
        # No notification: launchd restarts us every ThrottleInterval (10 s), which would repeat it.
        log.error("Cannot listen on %s:%d: %s. Is another bridge (or something else) using the port? "
                  "(`lsof -nP -iTCP:%d -sTCP:LISTEN`)", HOST, port, exc, port)
        return 75  # EX_TEMPFAIL; launchd retries after ThrottleInterval
    if not args.no_permission_prompt:
        before = request_bluetooth_permission()
        log.info("Bluetooth permission: %s%s", before or "unknown",
                 " -- macOS should be asking now (\"MunbynBLE would like to use Bluetooth\")"
                 if before == "not determined" else "")
        if before in ("denied", "restricted"):
            await notify_macos(NOTIFY_TITLE, "Bluetooth is not allowed for MunbynBLE. Turn it on in System Settings "
                                             "> Privacy & Security > Bluetooth.")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    await stop.wait()
    log.info("Stopping (signal).")
    await bridge.close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m munbyn.ble_bridge",
        description="Loopback TSPL -> Bluetooth print bridge for the Munbyn RW403B. Normally started by "
                    "MunbynBLE.app's LaunchAgent (scripts/install-ble-bridge.sh), never by hand from an app "
                    "without the Bluetooth permission.",
    )
    p.add_argument("--port", type=int, help="TCP port on 127.0.0.1 (default: config ble_bridge_port, 9100).")
    p.add_argument("--log", default=str(LOG_PATH), help="Log file (default %(default)s).")
    p.add_argument("--debug", action="store_true", help="Log every Bluetooth frame in hex.")
    p.add_argument("--no-permission-prompt", action="store_true",
                   help="Don't ask macOS for Bluetooth permission at startup (it's asked at the first job instead).")
    args = p.parse_args(argv)
    setup_logging(Path(args.log).expanduser(), args.debug)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
