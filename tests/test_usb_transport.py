"""Tests for munbyn.usb_transport against a fake pyusb device.

Never opens the real device: usb.core.find/usb.util.claim_interface and
friends are monkeypatched to fakes for every test.
"""
from __future__ import annotations

import pytest
import usb.core
import usb.util

import munbyn.usb_transport as usb_transport
from munbyn.usb_transport import (
    PRODUCT_ID,
    VENDOR_ID,
    Printer,
    PrinterBusy,
    PrinterError,
    PrinterNotFound,
    find_printers,
    get_backend,
)

DEVICE_ID_TEXT = "MFG:Munbyn;CMD:TSPL;MDL:RW403B;CMT:Label Printer;"


def _device_id_response(text=DEVICE_ID_TEXT):
    body = text.encode("ascii")
    length = len(body) + 2
    return bytes([length >> 8, length & 0xFF]) + body


class FakeEndpoint:
    def __init__(self, address):
        self.bEndpointAddress = address
        self.writes = []
        self.raise_on_write = None
        self.raise_on_read = None
        self.read_response = b""

    def write(self, data, timeout=None):
        if self.raise_on_write is not None:
            raise self.raise_on_write
        chunk = bytes(data)
        self.writes.append(chunk)
        return len(chunk)

    def read(self, size, timeout=None):
        if self.raise_on_read is not None:
            raise self.raise_on_read
        return self.read_response


class FakeDevice:
    def __init__(
        self,
        serial_number=None,
        product="Munbyn RW403B",
        bus=20,
        address=5,
        configured=True,
        kernel_driver_active=False,
        device_id_text=DEVICE_ID_TEXT,
        ctrl_transfer_fails=False,
    ):
        self.serial_number = serial_number
        self.product = product
        self.bus = bus
        self.address = address
        self._configured = configured
        self._kernel_active = kernel_driver_active
        self._ctrl_transfer_fails = ctrl_transfer_fails
        self._device_id_response = _device_id_response(device_id_text)
        self.out_ep = FakeEndpoint(0x03)
        self.in_ep = FakeEndpoint(0x83)
        self.cfg = {(0, 0): [self.out_ep, self.in_ep]}
        self.set_configuration_calls = 0
        self.detach_calls = 0

    def ctrl_transfer(self, bmRequestType, bRequest, wValue, wIndex, length):
        if self._ctrl_transfer_fails:
            raise usb.core.USBError("no reply")
        return list(self._device_id_response)

    def get_active_configuration(self):
        if not self._configured:
            raise usb.core.USBError("not configured")
        return self.cfg

    def set_configuration(self):
        self._configured = True
        self.set_configuration_calls += 1

    def is_kernel_driver_active(self, interface):
        return self._kernel_active

    def detach_kernel_driver(self, interface):
        self.detach_calls += 1
        self._kernel_active = False


@pytest.fixture
def patch_usb(monkeypatch):
    """Return a helper that wires fake devices in for a test."""
    state = {"released": [], "disposed": []}

    def _install(devices):
        monkeypatch.setattr(usb_transport, "get_backend", lambda: "FAKE_BACKEND")
        monkeypatch.setattr(
            usb.core, "find", lambda *a, **kw: list(devices)
        )
        monkeypatch.setattr(usb.util, "claim_interface", lambda dev, intf: None)

        def _release(dev, intf):
            state["released"].append((dev, intf))

        def _dispose(dev):
            state["disposed"].append(dev)

        monkeypatch.setattr(usb.util, "release_interface", _release)
        monkeypatch.setattr(usb.util, "dispose_resources", _dispose)
        return state

    return _install


# --------------------------------------------------------------------------
# find_printers()
# --------------------------------------------------------------------------


def test_find_printers_returns_expected_fields(patch_usb):
    dev = FakeDevice(serial_number="MP-RHHN1UV2", product="Munbyn RW403B")
    patch_usb([dev])
    results = find_printers()
    assert len(results) == 1
    info = results[0]
    assert info["serial"] == "MP-RHHN1UV2"
    assert info["product"] == "Munbyn RW403B"
    assert info["bus"] == 20
    assert info["address"] == 5
    assert info["device_id"] == DEVICE_ID_TEXT


def test_find_printers_tolerates_device_id_failure(patch_usb):
    dev = FakeDevice(serial_number="X", ctrl_transfer_fails=True)
    patch_usb([dev])
    results = find_printers()
    assert results[0]["device_id"] is None
    assert results[0]["serial"] == "X"


def test_find_printers_tolerates_serial_read_failure(patch_usb):
    class BrokenSerialDevice(FakeDevice):
        @property
        def serial_number(self):
            raise usb.core.USBError("cannot read string descriptor")

        @serial_number.setter
        def serial_number(self, value):
            pass

    dev = BrokenSerialDevice()
    patch_usb([dev])
    results = find_printers()
    assert results[0]["serial"] is None


def test_find_printers_empty(patch_usb):
    patch_usb([])
    assert find_printers() == []


def test_find_printers_uses_given_vid_pid(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    assert find_printers(vid=VENDOR_ID, pid=PRODUCT_ID) == find_printers()


# --------------------------------------------------------------------------
# Printer.open() / close()
# --------------------------------------------------------------------------


def test_open_happy_path_claims_interface_and_finds_endpoints(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    p = Printer()
    p.open()
    try:
        assert p._dev is dev
        assert p._out_ep is dev.out_ep
        assert p._in_ep is dev.in_ep
    finally:
        p.close()


def test_open_sets_configuration_when_unset(patch_usb):
    dev = FakeDevice(configured=False)
    patch_usb([dev])
    p = Printer()
    p.open()
    try:
        assert dev.set_configuration_calls == 1
    finally:
        p.close()


def test_open_does_not_set_configuration_when_already_set(patch_usb):
    dev = FakeDevice(configured=True)
    patch_usb([dev])
    p = Printer()
    p.open()
    try:
        assert dev.set_configuration_calls == 0
    finally:
        p.close()


def test_open_detaches_kernel_driver_when_active(patch_usb):
    dev = FakeDevice(kernel_driver_active=True)
    patch_usb([dev])
    p = Printer()
    p.open()
    try:
        assert dev.detach_calls == 1
    finally:
        p.close()


def test_open_survives_kernel_driver_not_implemented(patch_usb, monkeypatch):
    dev = FakeDevice()

    def _raise_not_implemented(interface):
        raise NotImplementedError("kernel driver introspection unsupported")

    dev.is_kernel_driver_active = _raise_not_implemented
    patch_usb([dev])
    p = Printer()
    p.open()  # must not raise
    p.close()


def test_open_not_found_raises(patch_usb):
    patch_usb([])
    with pytest.raises(PrinterNotFound):
        Printer().open()


def test_open_serial_filter_selects_matching_device(patch_usb):
    dev_a = FakeDevice(serial_number="AAA")
    dev_b = FakeDevice(serial_number="BBB")
    patch_usb([dev_a, dev_b])
    p = Printer(serial="BBB")
    p.open()
    try:
        assert p._dev is dev_b
    finally:
        p.close()


def test_open_serial_filter_not_found_raises(patch_usb):
    dev_a = FakeDevice(serial_number="AAA")
    patch_usb([dev_a])
    with pytest.raises(PrinterNotFound):
        Printer(serial="ZZZ").open()


def test_open_busy_raises_printer_busy_with_hint(patch_usb, monkeypatch):
    dev = FakeDevice()
    patch_usb([dev])
    monkeypatch.setattr(
        usb.util,
        "claim_interface",
        lambda d, i: (_ for _ in ()).throw(usb.core.USBError("busy", -1, 16)),
    )
    with pytest.raises(PrinterBusy) as exc_info:
        Printer().open()
    assert "cancel -a Munbyn_RW403B" in str(exc_info.value)


def test_open_permission_denied_raises_printer_busy(patch_usb, monkeypatch):
    dev = FakeDevice()
    patch_usb([dev])
    monkeypatch.setattr(
        usb.util,
        "claim_interface",
        lambda d, i: (_ for _ in ()).throw(usb.core.USBError("denied", -1, 13)),
    )
    with pytest.raises(PrinterBusy):
        Printer().open()


def test_open_other_usb_error_raises_plain_printer_error(patch_usb, monkeypatch):
    dev = FakeDevice()
    patch_usb([dev])
    monkeypatch.setattr(
        usb.util,
        "claim_interface",
        lambda d, i: (_ for _ in ()).throw(usb.core.USBError("weird failure", -1, 5)),
    )
    with pytest.raises(PrinterError) as exc_info:
        Printer().open()
    assert not isinstance(exc_info.value, PrinterBusy)


def test_close_releases_and_disposes(patch_usb):
    dev = FakeDevice()
    state = patch_usb([dev])
    p = Printer()
    p.open()
    p.close()
    assert (dev, 0) in state["released"]
    assert dev in state["disposed"]
    assert p._dev is None
    assert p._out_ep is None
    assert p._in_ep is None


def test_close_is_safe_to_call_twice(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    p = Printer()
    p.open()
    p.close()
    p.close()  # must not raise


def test_context_manager_closes_on_exit(patch_usb):
    dev = FakeDevice()
    state = patch_usb([dev])
    with Printer() as p:
        assert p._dev is dev
    assert dev in state["disposed"]


def test_context_manager_closes_even_on_exception(patch_usb):
    dev = FakeDevice()
    state = patch_usb([dev])
    with pytest.raises(RuntimeError):
        with Printer():
            raise RuntimeError("boom")
    assert dev in state["disposed"]


# --------------------------------------------------------------------------
# device_id()
# --------------------------------------------------------------------------


def test_device_id_reads_and_strips_length_prefix(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    with Printer() as p:
        assert p.device_id() == DEVICE_ID_TEXT


# --------------------------------------------------------------------------
# write(): chunk loop + timeout
# --------------------------------------------------------------------------


def test_write_chunks_data_and_returns_total(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    with Printer() as p:
        data = bytes(range(256)) * 4  # 1024 bytes
        total = p.write(data, chunk_size=100)
    assert total == len(data)
    assert b"".join(dev.out_ep.writes) == data
    assert len(dev.out_ep.writes) == 11  # ceil(1024/100)
    assert all(len(c) <= 100 for c in dev.out_ep.writes)


def test_write_single_chunk_when_chunk_size_covers_all(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    with Printer() as p:
        data = b"short payload"
        total = p.write(data, chunk_size=4096)
    assert total == len(data)
    assert dev.out_ep.writes == [data]


def test_write_timeout_raises_printer_error(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    dev.out_ep.raise_on_write = usb.core.USBTimeoutError("timed out", -7, 110)
    with Printer() as p:
        with pytest.raises(PrinterError):
            p.write(b"data")


def test_write_busy_error_raises_printer_busy(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    dev.out_ep.raise_on_write = usb.core.USBError("busy", -1, 16)
    with Printer() as p:
        with pytest.raises(PrinterBusy):
            p.write(b"data")


# --------------------------------------------------------------------------
# read() / query_status()
# --------------------------------------------------------------------------


def test_read_returns_bytes(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    dev.in_ep.read_response = b"\x00"
    with Printer() as p:
        assert p.read(1) == b"\x00"


def test_read_timeout_returns_empty_bytes(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    dev.in_ep.raise_on_read = usb.core.USBTimeoutError("timed out", -7, 110)
    with Printer() as p:
        assert p.read(1, timeout_ms=250) == b""


def test_query_status_returns_int(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    dev.in_ep.read_response = b"\x20"  # printing
    with Printer() as p:
        assert p.query_status() == 0x20


def test_query_status_returns_none_on_no_reply(patch_usb):
    dev = FakeDevice()
    patch_usb([dev])
    dev.in_ep.raise_on_read = usb.core.USBTimeoutError("timed out", -7, 110)
    with Printer() as p:
        assert p.query_status() is None


def test_query_status_sends_status_query_bytes(patch_usb):
    from munbyn.tspl import STATUS_QUERY

    dev = FakeDevice()
    patch_usb([dev])
    dev.in_ep.read_response = b"\x00"
    with Printer() as p:
        p.query_status()
    assert dev.out_ep.writes == [STATUS_QUERY]


# --------------------------------------------------------------------------
# get_backend()
# --------------------------------------------------------------------------


def test_get_backend_prefers_libusb_package(monkeypatch):
    import libusb_package

    sentinel = object()
    monkeypatch.setattr(libusb_package, "get_libusb1_backend", lambda: sentinel)
    assert get_backend() is sentinel


def test_get_backend_raises_printer_error_when_nothing_available(monkeypatch):
    import libusb_package
    import usb.backend.libusb1 as libusb1

    monkeypatch.setattr(
        libusb_package,
        "get_libusb1_backend",
        lambda: (_ for _ in ()).throw(RuntimeError("no libusb_package binary")),
    )
    monkeypatch.setattr(libusb1, "get_backend", lambda find_library=None: None)
    with pytest.raises(PrinterError):
        get_backend()
