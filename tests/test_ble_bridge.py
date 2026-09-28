"""munbyn.ble_bridge: the loopback TSPL -> Bluetooth bridge, driven through a
fake transport. Bluetooth is never touched and bleak is never imported; the
server runs on an ephemeral 127.0.0.1 port."""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import threading

import pytest
from PIL import Image, ImageDraw

from bridge_fakes import BridgeThread, FakeTransport, Notes, _no_sleep
from munbyn import ble_bridge
from munbyn import ble_bridge_client as bc
from munbyn import ble_protocol as bp
from munbyn import ble_transport as bt
from munbyn import labels, tspl


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MUNBYN_CONFIG", str(tmp_path / "config.json"))


def _settings(**kw):
    kw.setdefault("feed_scale", 0.981)
    return tspl.JobSettings(size=labels.parse_size("4x6"), **kw)


def _image(s):
    h = labels.stretched_height_dots(s.size.height_dots, s.feed_scale)
    img = Image.new("1", (s.size.width_dots, h), 255)
    ImageDraw.Draw(img).rectangle((50, 80, 400, 300), fill=0)
    return img


def _job(copies=1, pages=1, **kw):
    s = _settings(copies=copies, **kw)
    return bc.build_bridge_job(s, [_image(s)] * pages)


def _bridge(transport, **kw):
    notes = Notes()
    kw.setdefault("sleep", _no_sleep)
    b = ble_bridge.Bridge(transport, port=0, notifier=notes,
                          config_loader=lambda: {"ble_address": "U-1", "ble_feed_scale": 0.981}, **kw)
    return b, notes


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- job flow (no sockets)


def test_job_prints_the_same_pages_as_ble_direct():
    fake = FakeTransport()
    b, notes = _bridge(fake)
    s = _settings(copies=2, x_shift_mm=-3.0)
    img = _image(s)
    res = _run(b.run_print(bc.build_bridge_job(s, [img]), 7))
    assert res["ok"] and res["job"] == 7 and res["pages"] == 1 and res["labels"] == 2
    assert res["printed"] == 2 and res["attempts"] == 1
    (call,) = fake.calls
    assert call["copies"] == 2
    direct = bp.BlePage.from_image(bp.compose_page(img, x_shift_mm=-3.0, feed_scale=0.981))
    assert call["pages"][0].data == direct.data and call["pages"][0].width_dots == 816
    assert call["label_height_mm"] == pytest.approx(152.4, abs=0.2)  # physical length, not stretched rows
    assert notes.sent == [] and b.last_job["ok"]


def test_malformed_job_notifies_and_never_reaches_bluetooth():
    fake = FakeTransport()
    b, notes = _bridge(fake)
    res = _run(b.run_print(b'CLS\r\nTEXT 1,1,"3",0,1,1,"x"\r\nPRINT 1\r\n', 1))
    assert not res["ok"] and "TEXT" in res["error"]
    assert fake.calls == []
    assert len(notes.sent) == 1 and "Print failed" in notes.sent[0][1]
    assert b.jobs_failed == 1 and b.last_job["ok"] is False


def test_bluetooth_failure_is_retried_once_on_a_fresh_connection():
    fake = FakeTransport(failures=[bt.BleConnectError("could not connect"), None])
    slept = []

    async def sleep(s):
        slept.append(s)

    b, notes = _bridge(fake, sleep=sleep, retry_delay=3.0)
    res = _run(b.run_print(_job(), 1))
    assert res["ok"] and res["attempts"] == 2 and len(fake.calls) == 2
    assert slept == [3.0] and notes.sent == []


def test_second_failure_gives_up_with_a_notification():
    fake = FakeTransport(failures=[bt.BleTimeout("no ack"), bt.BleTimeout("no ack again")])
    b, notes = _bridge(fake)
    res = _run(b.run_print(_job(), 1))
    assert not res["ok"] and res["attempts"] == 2 and "no ack again" in res["error"]
    assert len(notes.sent) == 1 and "no ack again" in notes.sent[0][1]


def test_no_retry_once_a_label_may_have_printed():
    exc = bt.BleTimeout("only 0 of 1 printed")
    exc.committed = True
    fake = FakeTransport(failures=[exc, None])
    b, notes = _bridge(fake)
    res = _run(b.run_print(_job(), 1))
    assert not res["ok"] and len(fake.calls) == 1 and "may already have printed" in res["error"]
    assert len(notes.sent) == 1


@pytest.mark.parametrize("exc", [bt.BlePrinterStatus("paper out"), bt.BleFrameTooLarge("frame"),
                                 bt.BleJobFailed("too tall"), bt.BleCancelled("cancelled")])
def test_no_retry_when_retrying_cant_help(exc):
    fake = FakeTransport(failures=[exc, None])
    b, _notes = _bridge(fake)
    res = _run(b.run_print(_job(), 1))
    assert not res["ok"] and len(fake.calls) == 1


def test_unknown_errors_retry_too():
    fake = FakeTransport(failures=[RuntimeError("Bluetooth device is turned off"), None])
    b, _notes = _bridge(fake)
    assert _run(b.run_print(_job(), 1))["ok"] and len(fake.calls) == 2


def test_real_transport_marks_committed_failures(monkeypatch):
    """BleakBridgeTransport tags a failure after PRINTINEND as committed."""

    class FakePrinter:
        def __init__(self, options):
            self.end_sent, self.printed = True, 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def print_pages(self, *a, **k):
            raise bt.BleTimeout("no printed report")

    monkeypatch.setattr(bt, "BlePrinter", FakePrinter)
    t = ble_bridge.BleakBridgeTransport(lambda: bt.BleOptions(address="U"))
    with pytest.raises(bt.BleTimeout) as info:
        _run(t.print_pages([], 1, label_height_mm=None))
    assert info.value.committed is True


def test_ble_printer_tracks_end_sent():
    p = bt.BlePrinter(bt.BleOptions())
    assert p.end_sent is False and p.printed == 0


def test_options_come_from_the_config():
    assert ble_bridge.ble_options_from_config({"ble_address": "ABC"}).address == "ABC"
    assert ble_bridge.ble_options_from_config({"ble_address": None}).address is None


# --------------------------------------------------------------------------- the server


def test_server_binds_loopback_only_and_answers_status(allow_bridge_connections):
    with BridgeThread(FakeTransport()) as bt_:
        addr = bt_.bridge._server.sockets[0].getsockname()
        assert addr[0] == "127.0.0.1"
        st = bc.status(bt_.port)
        assert st["bridge"] == "munbyn-ble-bridge" and st["version"] == ble_bridge.VERSION
        assert st["printer_address"] == "FAKE-UUID" and st["busy"] is False and st["last_job"] is None
        assert st["bluetooth_authorization"] == "allowed"
        assert bc.probe(bt_.port) is not None


def test_job_over_tcp_prints_and_replies(allow_bridge_connections):
    fake = FakeTransport()
    with BridgeThread(fake) as b:
        reply = bc.send_job(b.port, _job(copies=3))
        assert reply["ok"] and reply["labels"] == 3 and reply["printed"] == 3
        assert fake.calls[0]["copies"] == 3
        st = bc.status(b.port)
        assert st["last_job"]["ok"] is True and st["jobs_done"] == 1


def test_status_deviceinfo_goes_through_the_queue(allow_bridge_connections):
    fake = FakeTransport()
    with BridgeThread(fake) as b:
        st = bc.status(b.port, deviceinfo=True)
        assert st["deviceinfo"]["firmwarever"] == "1.1.16" and fake.info_calls == 1
        fake.info_exc = bt.BleConnectError("printer off")
        st = bc.status(b.port, deviceinfo=True)
        assert "printer off" in st["deviceinfo_error"] and "deviceinfo" not in st


def test_bad_job_over_tcp_fails_but_the_server_keeps_running(allow_bridge_connections):
    fake = FakeTransport()
    with BridgeThread(fake) as b:
        with pytest.raises(bc.BridgeJobFailed, match="unsupported TSPL"):
            bc.send_job(b.port, b"CLS\r\nBOX 0,0,10,10,1\r\nPRINT 1\r\n")
        assert b.notes.sent and fake.calls == []
        # an empty connection (a port check) is ignored
        with socket.create_connection(("127.0.0.1", b.port), timeout=2) as s:
            s.shutdown(socket.SHUT_WR)
            assert s.recv(10) == b""
        assert bc.send_job(b.port, _job())["ok"]


def test_jobs_run_one_at_a_time_in_order(allow_bridge_connections):
    fake = FakeTransport(hold=True)
    replies = {}

    def send(name, copies):
        replies[name] = bc.send_job(b.port, _job(copies=copies))

    with BridgeThread(fake) as b:
        t1 = threading.Thread(target=send, args=("first", 1))
        t1.start()
        assert fake.started.wait(10)
        t2 = threading.Thread(target=send, args=("second", 2))
        t2.start()
        # the second job is queued, not running
        for _ in range(100):
            st = bc.status(b.port)
            if st["queued"] == 1:
                break
            threading.Event().wait(0.02)
        assert st["busy"] is True and st["queued"] == 1 and len(fake.calls) == 1
        b.release()
        t1.join(10)
        t2.join(10)
    assert fake.max_active == 1
    assert [c["copies"] for c in fake.calls] == [1, 2]
    assert replies["first"]["job"] < replies["second"]["job"]


def test_complete_job_without_eof_still_prints_after_the_idle_timeout(allow_bridge_connections):
    fake = FakeTransport()
    with BridgeThread(fake, read_idle_timeout=0.3) as b:
        with socket.create_connection(("127.0.0.1", b.port), timeout=10) as s:
            s.sendall(_job())  # no shutdown(SHUT_WR)
            reply = s.makefile("rb").readline()
        assert b'"ok": true' in reply and len(fake.calls) == 1
        with socket.create_connection(("127.0.0.1", b.port), timeout=10) as s:
            s.sendall(_job()[:5000])  # a truncated job is dropped, not guessed at
            reply = s.makefile("rb").readline()
        assert b"stalled" in reply and len(fake.calls) == 1


def test_oversized_job_is_refused(allow_bridge_connections):
    fake = FakeTransport()
    with BridgeThread(fake, max_job_bytes=1000) as b:
        with pytest.raises(bc.BridgeJobFailed, match="larger than"):
            bc.send_job(b.port, _job())
        assert fake.calls == []


def test_close_cancels_the_running_job_and_fails_the_queue(allow_bridge_connections):
    fake = FakeTransport(hold=True)
    out = {}

    def send():
        try:
            out["reply"] = bc.send_job(b.port, _job())
        except Exception as exc:  # the reply says the bridge stopped
            out["exc"] = exc

    b = BridgeThread(fake)
    b.__enter__()
    t = threading.Thread(target=send)
    t.start()
    assert fake.started.wait(10)
    b.__exit__(None, None, None)
    t.join(10)
    assert "exc" in out or not out["reply"]["ok"]


def test_status_magic_is_what_the_client_sends():
    assert bc.STATUS_MAGIC == ble_bridge.STATUS_MAGIC == b"MUNBYN-STATUS"


def test_module_help_never_touches_bluetooth(tmp_path):
    """`python -m munbyn.ble_bridge --help` must not import bleak/CoreBluetooth."""
    code = ("import sys, runpy; sys.argv=['x','--help']\n"
            "try:\n    runpy.run_module('munbyn.ble_bridge', run_name='__main__')\n"
            "except SystemExit: pass\n"
            "assert 'bleak' not in sys.modules and 'CoreBluetooth' not in sys.modules, 'touched Bluetooth'\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                          env={"MUNBYN_CONFIG": str(tmp_path / "c.json"), "HOME": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    assert "Loopback TSPL" in proc.stdout


# --------------------------------------------------------------------------- the CUPS path: real filter output

import test_cups_filter as cf  # noqa: E402
from test_cups_filter import filter_bin  # noqa: E402,F401  (session fixture: builds cups/rastertotspl)


def test_cups_filter_output_prints_via_the_bridge(filter_bin, tmp_path):  # noqa: F811
    """What Preview -> "Munbyn RW403B (Bluetooth)" hands the bridge: the real
    cups/rastertotspl output for a feed-corrected 4x6 raster, 2 pages x 2 copies."""
    img = cf.test_label(cf.W4X6, cf.H4X6_FEED)
    page = cf.raster_page(img, res=cf.FEED_RES, num_copies=2)
    job = cf.ok_job(filter_bin, cf.raster(page, page), tmp_path, copies="2")
    assert job.count(b"PRINT 1,2") == 2
    fake = FakeTransport()
    b, notes = _bridge(fake)
    res = _run(b.run_print(job, 1))
    assert res["ok"], res
    # 2 copies -> each page is its own Bluetooth job (AAABBB order, not the
    # interleaved ABAB a single 2-page/2-copy job would send); see
    # tspl_parse.group_pages.
    assert len(fake.calls) == 2
    assert [c["copies"] for c in fake.calls] == [2, 2]
    assert [len(c["pages"]) for c in fake.calls] == [1, 1]
    call = fake.calls[0]
    ble = call["pages"][0]
    assert (ble.width_dots, ble.height) == (816, cf.H4X6_FEED)
    stride = ble.width_dots // 8
    # top-left 100x100 square is black: BLE 1 = black
    assert ble.data[:12] == b"\xff" * 12 and ble.data[12] >> 4 == 0xF
    # the rightmost printed column (dot 811) is black, the 4 padding dots after it are white
    assert ble.data[stride - 1] & 0x1F == 0x10
    assert call["label_height_mm"] == pytest.approx(152.4, abs=0.5)
