"""Test helpers for the MunbynBLE bridge: a fake Bluetooth transport and a
real ``munbyn.ble_bridge.Bridge`` running on an ephemeral loopback port in a
background thread. Nothing here touches Bluetooth or imports bleak."""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Optional

from munbyn import ble_bridge
from munbyn import ble_protocol as bp
from munbyn import ble_transport as bt


class FakeTransport:
    """Records every print; ``failures`` is a list of exceptions raised by
    successive print_pages calls (None = succeed)."""

    def __init__(self, failures: Optional[List[Optional[BaseException]]] = None, hold: bool = False) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.failures = list(failures or [])
        self.active = 0
        self.max_active = 0
        self.info_calls = 0
        self.info = bp.DeviceInfo(firmwarever="1.1.16", blever="1.2.1", printstatus="0", concentration=8, speed=4)
        self.info_exc: Optional[BaseException] = None
        self.hold = hold
        self.release: Optional[asyncio.Event] = None
        self.started = threading.Event()

    async def print_pages(self, pages, copies, *, label_height_mm=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append({"pages": list(pages), "copies": copies, "label_height_mm": label_height_mm})
            self.started.set()
            if self.hold:
                if self.release is None:
                    self.release = asyncio.Event()
                await self.release.wait()
            if self.failures:
                exc = self.failures.pop(0)
                if exc is not None:
                    raise exc
            n = len(pages) * copies
            return bt.PrintResult(pages=len(pages), sends=1, copies=copies, expected=n, printed=n, writes=10,
                                  bytes_sent=1000, resends=0, per_size=400, seconds=0.1)
        finally:
            self.active -= 1

    async def device_info(self):
        self.info_calls += 1
        if self.info_exc is not None:
            raise self.info_exc
        return self.info


class Notes:
    def __init__(self) -> None:
        self.sent: List[tuple] = []

    async def __call__(self, title: str, message: str) -> None:
        self.sent.append((title, message))


async def _no_sleep(_s: float) -> None:
    return None


class BridgeThread:
    """A real Bridge on 127.0.0.1:<ephemeral> with an injected transport."""

    def __init__(self, transport: Any, config: Optional[Dict[str, Any]] = None, **kw: Any) -> None:
        self.transport = transport
        self.notes = Notes()
        self.config = dict(config or {"ble_address": "FAKE-UUID", "ble_feed_scale": 0.981})
        self.loop = asyncio.new_event_loop()
        self.bridge = ble_bridge.Bridge(
            transport, port=0, notifier=self.notes, config_loader=lambda: dict(self.config),
            authorization=lambda: "allowed", sleep=_no_sleep, **kw
        )
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.port = 0

    def __enter__(self) -> "BridgeThread":
        self.thread.start()
        self.port = asyncio.run_coroutine_threadsafe(self.bridge.start(), self.loop).result(10)
        return self

    def call(self, fn, *a, **k):
        """Run a coroutine function on the bridge's loop and wait for it."""
        return asyncio.run_coroutine_threadsafe(fn(*a, **k), self.loop).result(30)

    def release(self) -> None:
        def _set():
            if self.transport.release is None:
                self.transport.release = asyncio.Event()
            self.transport.release.set()

        self.loop.call_soon_threadsafe(_set)

    def __exit__(self, *exc: Any) -> None:
        try:
            asyncio.run_coroutine_threadsafe(self.bridge.close(), self.loop).result(20)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(10)
            self.loop.close()
