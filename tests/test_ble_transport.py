"""Tests for munbyn/ble_transport.py with a fake BleakClient.

Nothing here scans for, connects to, or writes to a real Bluetooth device:
every BlePrinter gets ``client_factory``/``find_device`` fakes and a ``sleep``
that doesn't wait. bleak itself is never imported by these tests.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from munbyn import ble_protocol as bp
from munbyn import ble_transport as bt

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ble")


def _load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def selftest_page():
    g = _load("golden_selftest_4x6.json")
    with gzip.open(os.path.join(FIX, g["input"]["bits_file"])) as f:
        return bp.BlePage.from_bytes(f.read(), 816)


def tiny_page():
    return bp.BlePage.from_bytes(bytes.fromhex("f00faa55"), 16)


READY = bp.DeviceInfo(firmwarever="SIM", printstatus="0", blever="1.0.9", supportfunction=2, concentration=8,
                      speed=4, elec=100)


# --------------------------------------------------------------------------- fakes


class FakeChar:
    def __init__(self, uuid, properties, max_wwr):
        self.uuid = uuid
        self.properties = list(properties)
        self.max_write_without_response_size = max_wwr


class FakeServices:
    def __init__(self, chars):
        self._c = {c.uuid: c for c in chars}

    def get_characteristic(self, uuid):
        return self._c.get(uuid)


class FakeDevice:
    def __init__(self, address="FAKE-UUID-0001", name="RW403B-TEST"):
        self.address = address
        self.name = name


class Model:
    """A fake RW403B that answers the way the editor's fake printer did."""

    def __init__(self, *, infos=(READY,), reply_deviceinfo=True, resend=None, withhold_ack=(), extra=None,
                 printed=True, cancel_reply=True, on_data_write=None,
                 props=("read", "write", "write-without-response"), max_wwr=512, missing=(), fail_connects=0,
                 write_error=None, disconnect_fires_callback=False):
        self.infos = list(infos)
        self.reply_deviceinfo = reply_deviceinfo
        self.resend = dict(resend or {})
        self.withhold_ack = set(withhold_ack)
        self.extra = dict(extra or {})
        self.printed = printed
        self.cancel_reply = cancel_reply
        self.on_data_write = on_data_write
        self.props = props
        self.max_wwr = max_wwr
        self.missing = set(missing)
        self.fail_connects = fail_connects
        self.write_error = write_error
        self.disconnect_fires_callback = disconnect_fires_callback
        self.pending_printed = 0
        self.writes = []  # (short char, frame, response)
        self.data_writes = 0
        self.clients = []
        self.disconnects = 0
        self.stop_notifies = 0
        self.dead = False

    # what the "printer" says back to one write
    def on_write(self, uuid, frame, response, client):
        self.writes.append((bp.short_uuid(uuid), frame, response))
        (payload,) = bp.unpack_all(frame)
        m = bp.decode_send(payload)
        if m.eventtype == bp.EventType.DEVICEINFO:
            if not self.reply_deviceinfo:
                return []
            info = self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]
            return [bp.deviceinfo_frame(info)]
        if m.eventtype == bp.EventType.DEVICEPRINT:
            self.data_writes += 1
            if self.write_error is not None:
                raise self.write_error
            if self.on_data_write is not None:
                r = self.on_data_write(self, client, self.data_writes)
                if r is not None:
                    return r
            p = bp.decode_print(m.senddata)
            if p.indexpackage != p.totalpackage:
                return []
            s = p.indexsection
            if self.resend.get(s, 0) > 0:
                self.resend[s] -= 1
                return [bp.resend_frame(s)]
            if s in self.withhold_ack:
                return []
            if s == p.totalsection:
                self.pending_printed += p.page
            return [bp.ACK_FRAME] + list(self.extra.get(s, []))
        if m.eventtype == bp.EventType.PRINTINEND:
            if not self.printed:
                return []
            out = [bp.PRINTED_FRAME] * self.pending_printed
            self.pending_printed = 0
            return out
        if m.eventtype == bp.EventType.CANCELPRINTING:
            return [bp.respond_frame(bp.EventType.CANCELPRINTING, code=200)] if self.cancel_reply else []
        return []

    def events(self):
        return [bp.describe_frame(f) for _c, f, _r in self.writes]

    def labels(self):
        return [bp.describe_frame(f).split()[0] for _c, f, _r in self.writes]


class GoldenModel(Model):
    """Replays a golden log from the editor: every write must equal the
    editor's next write; the notifications that followed it are delivered."""

    def __init__(self, log, **kw):
        super().__init__(**kw)
        self.log = log
        self.pos = 0
        self.mismatches = []

    def on_write(self, uuid, frame, response, client):
        self.writes.append((bp.short_uuid(uuid), frame, response))
        if self.pos >= len(self.log):
            self.mismatches.append("extra write {}".format(bp.describe_frame(frame)))
            return []
        e = self.log[self.pos]
        self.pos += 1
        ok = e["dir"] == "write" and e["char"][2:] == bp.short_uuid(uuid)
        if "sha256" in e:
            ok = ok and hashlib.sha256(frame).hexdigest() == e["sha256"]
        if "hex" in e:
            ok = ok and frame.hex() == e["hex"]
        if not ok:
            self.mismatches.append("write #{} differs: {}".format(len(self.writes), bp.describe_frame(frame)))
        out = []
        while self.pos < len(self.log) and self.log[self.pos]["dir"] == "notify":
            out.append(bytes.fromhex(self.log[self.pos]["hex"]))
            self.pos += 1
        return out


class FakeClient:
    def __init__(self, model, target, disconnected_callback=None, timeout=None):
        self.model = model
        self.target = target
        self.dcb = disconnected_callback
        self.timeout = timeout
        self.cb = None
        chars = []
        for uuid in (bp.DATA_UUID, bp.CONTROL_UUID):
            if uuid not in model.missing:
                chars.append(FakeChar(uuid, model.props, model.max_wwr))
        if bp.NOTIFY_UUID not in model.missing:
            chars.append(FakeChar(bp.NOTIFY_UUID, ("notify",), 20))
        self.services = FakeServices(chars)
        model.clients.append(self)

    async def connect(self):
        if self.model.fail_connects > 0:
            self.model.fail_connects -= 1
            raise RuntimeError("fake connect failure")

    async def start_notify(self, uuid, cb):
        assert uuid == bp.NOTIFY_UUID
        self.cb = cb

    async def stop_notify(self, uuid):
        self.model.stop_notifies += 1

    async def disconnect(self):
        self.model.disconnects += 1
        if self.model.disconnect_fires_callback and self.dcb is not None:
            # Real CoreBluetooth/bleak can resolve a peripheral's disconnect
            # (and so fire our disconnected_callback) as part of tearing
            # down a client whose connect() failed or timed out -- while the
            # link was briefly up -- not just on a live, connected client.
            self.dcb(self)

    async def write_gatt_char(self, uuid, data, response=None):
        if self.model.dead:
            raise RuntimeError("Not connected")
        await asyncio.sleep(0)
        for f in self.model.on_write(uuid, bytes(data), response, self):
            self.cb(None, bytearray(f))

    def drop_link(self):
        self.model.dead = True
        self.dcb(self)


async def _fast_sleep(_s):
    await asyncio.sleep(0)


def fast_options(**kw):
    base = dict(post_connect_delay=0, post_notify_delay=0, deviceinfo_timeout=0.5, ack_timeout=0.5,
                done_timeout=0.5, done_timeout_per_page=0.1, cancel_reply_timeout=0.3, busy_retry_delay=0,
                setting_delay=0)
    base.update(kw)
    return bt.BleOptions(**base)


def inject(model):
    async def find(_opts):
        return FakeDevice()

    return dict(
        client_factory=lambda target, **kw: FakeClient(model, target, **kw),
        find_device=find,
        sleep=_fast_sleep,
        handle_sigint=False,
    )


def run_print(model, pages, copies=1, options=None, **kw):
    return bt.print_job(pages, copies, options or fast_options(), **kw, **inject(model))


# --------------------------------------------------------------------------- happy paths (golden replays)


@pytest.mark.parametrize("name,writes,resends", [
    ("golden_selftest_4x6.json", 52, 0),
    ("golden_selftest_4x6_resend.json", 57, 1),
])
def test_replays_editor_golden_byte_for_byte(selftest_page, name, writes, resends):
    model = GoldenModel(_load(name)["log"])
    res = run_print(model, [selftest_page])
    assert model.mismatches == []
    assert model.pos == len(model.log)  # every editor write happened, every notification was consumed
    assert (res.writes, res.resends, res.printed, res.expected, res.per_size) == (writes, resends, 1, 1, 400)
    # print packets on ABF4, everything else on ABF1, all with response (the char has Write)
    assert {c for c, f, _r in model.writes if bp.describe_frame(f).startswith("DEVICEPRINT")} == {"ABF4"}
    assert {c for c, f, _r in model.writes if not bp.describe_frame(f).startswith("DEVICEPRINT")} == {"ABF1"}
    assert all(r is True for _c, _f, r in model.writes)
    assert model.disconnects == 1 and model.stop_notifies == 1


def test_replays_tiny_copies3_golden():
    model = GoldenModel(_load("golden_tiny16x2_copies3.json")["log"])
    res = run_print(model, [tiny_page()], copies=3)
    assert model.mismatches == [] and model.pos == len(model.log)
    assert (res.printed, res.expected, res.sends) == (3, 3, 1)


def test_multi_page_copies_send_each_page_per_copy():
    model = Model()
    res = run_print(model, [tiny_page(), tiny_page()], copies=2)
    assert (res.sends, res.expected, res.printed) == (4, 4, 4)
    pages = [bp.decode_print(bp.decode_send(bp.unpack_all(f)[0]).senddata).page
             for c, f, _r in model.writes if c == "ABF4"]
    assert pages == [1, 1, 1, 1]
    assert model.labels() == ["DEVICEINFO"] + ["DEVICEPRINT"] * 4 + ["PRINTINEND"]


# --------------------------------------------------------------------------- resend / acks


def test_resend_rewinds_to_requested_section(selftest_page):
    model = Model(resend={3: 1})
    res = run_print(model, [selftest_page])
    msgs = [bp.decode_print(bp.decode_send(bp.unpack_all(f)[0]).senddata)
            for c, f, _r in model.writes if c == "ABF4"]
    assert res.resends == 1
    starts = [m.indexsection for m in msgs if m.indexpackage == 1]
    assert starts == [1, 2, 3, 3] + list(range(4, 17))
    assert res.printed == 1


def test_resend_of_earlier_section_resends_everything_from_there(selftest_page):
    # printer asks for section 1 after section 2's last packet
    def hook(model, client, n):
        if n == 9:  # section 1 = 4 packets, section 2 = 5 packets -> write 9 is sec 2's last
            return [bp.resend_frame(1)]
        return None

    model = Model(on_data_write=hook)
    res = run_print(model, [selftest_page])
    assert res.resends == 1
    assert model.data_writes == 50 + 9


def test_second_resend_of_same_section_aborts(selftest_page):
    model = Model(resend={2: 2})
    with pytest.raises(bt.BleJobFailed, match="section 2 a second time"):
        run_print(model, [selftest_page])
    assert "PRINTINEND" not in model.labels()
    assert model.disconnects == 1


def test_duplicate_ack_during_pause_is_ignored(selftest_page):
    # two acks for section 1, none for section 2: the duplicate must not count as section 2's ack
    model = Model(extra={1: [bp.ACK_FRAME]}, withhold_ack={2})
    with pytest.raises(bt.BleTimeout, match="section 2 of 16"):
        run_print(model, [selftest_page], options=fast_options(ack_timeout=0.3))


def test_resend_request_for_unknown_section_aborts():
    def hook(model, client, n):
        return [bp.resend_frame(7)]

    with pytest.raises(bt.BleJobFailed, match="resend section 7"):
        run_print(Model(on_data_write=hook), [tiny_page()])


# --------------------------------------------------------------------------- timeouts


def test_section_ack_timeout(selftest_page):
    model = Model(withhold_ack={3})
    t0 = time.monotonic()
    with pytest.raises(bt.BleTimeout, match="section 3 of 16"):
        run_print(model, [selftest_page], options=fast_options(ack_timeout=0.3))
    assert time.monotonic() - t0 < 5
    assert "PRINTINEND" not in model.labels()
    assert model.disconnects == 1


def test_deviceinfo_timeout_sends_no_print_data(selftest_page):
    model = Model(reply_deviceinfo=False)
    with pytest.raises(bt.BleTimeout, match="DEVICEINFO"):
        run_print(model, [selftest_page])
    assert model.labels() == ["DEVICEINFO"]
    assert model.disconnects == 1


def test_printed_report_timeout_is_an_error_after_all_acks():
    model = Model(printed=False)
    with pytest.raises(bt.BleTimeout, match="only 0 of 1 page"):
        run_print(model, [tiny_page()])
    assert model.labels()[-1] == "PRINTINEND"


# --------------------------------------------------------------------------- disconnects


def test_disconnect_while_waiting_for_ack_fails_fast(selftest_page):
    def hook(model, client, n):
        if n == 9:  # last packet of section 2: link drops instead of an ack
            client.drop_link()
            return []
        return None

    model = Model(on_data_write=hook)
    t0 = time.monotonic()
    with pytest.raises(bt.BleDisconnected):
        run_print(model, [selftest_page], options=fast_options(ack_timeout=5.0))
    assert time.monotonic() - t0 < 2.0  # didn't sit out the 5 s ack timeout
    assert model.disconnects == 1


def test_disconnect_mid_section_write_fails(selftest_page):
    def hook(model, client, n):
        if n == 6:
            client.drop_link()
            raise RuntimeError("Peripheral disconnected")
        return None

    model = Model(on_data_write=hook)
    with pytest.raises(bt.BleDisconnected):
        run_print(model, [selftest_page])
    assert model.data_writes == 6


# --------------------------------------------------------------------------- frame size / write type


def test_oversized_frame_is_refused_before_printing(selftest_page):
    model = Model(props=("write-without-response",), max_wwr=182)
    with pytest.raises(bt.BleFrameTooLarge) as ei:
        run_print(model, [selftest_page])
    msg = str(ei.value)
    assert "433 bytes" in msg and "182" in msg and "--ble-packet-size 148" in msg
    assert model.labels() == ["DEVICEINFO"]  # nothing was printed
    assert model.writes[0][2] is False  # auto mode: this char only has write-without-response


def test_packet_size_148_fits_the_small_mtu(selftest_page):
    model = Model(props=("write-without-response",), max_wwr=182)
    res = run_print(model, [selftest_page], options=fast_options(per_size=148))
    assert res.printed == 1
    assert max(len(f) for _c, f, _r in model.writes) <= 182
    assert all(r is False for _c, _f, r in model.writes)


def test_blever_1_0_8_switches_to_148_byte_packets(selftest_page):
    info = bp.DeviceInfo(printstatus="0", blever="1.0.8")
    model = Model(infos=(info,))
    res = run_print(model, [selftest_page])
    assert res.per_size == 148
    assert max(len(f) for c, f, _r in model.writes if c == "ABF4") <= 181


def test_forced_write_mode():
    model = Model(props=("write-without-response",))
    run_print(model, [tiny_page()], options=fast_options(write_mode="response"))
    assert all(r is True for _c, _f, r in model.writes)


def test_write_error_mentions_packet_size():
    model = Model(write_error=RuntimeError("The value's length is invalid."))
    with pytest.raises(bt.BleWriteError, match="--ble-packet-size 148"):
        run_print(model, [tiny_page()])


def test_choose_response():
    assert bt.choose_response(["write", "write-without-response"]) is True
    assert bt.choose_response(["write-without-response"]) is False
    assert bt.choose_response([]) is True
    assert bt.choose_response(["write"], "no-response") is False
    with pytest.raises(ValueError):
        bt.choose_response([], "sometimes")


# --------------------------------------------------------------------------- cancel / Ctrl-C


def test_cancel_mid_job_sends_cancelprinting(selftest_page):
    holder = {}

    def hook(model, client, n):
        if n == 5:
            holder["printer"].request_cancel()
        return None

    model = Model(on_data_write=hook)
    orig = bt.BlePrinter.__init__

    def capture(self, *a, **k):
        orig(self, *a, **k)
        holder["printer"] = self

    bt.BlePrinter.__init__ = capture
    try:
        with pytest.raises(bt.BleCancelled, match="printer confirmed"):
            run_print(model, [selftest_page])
    finally:
        bt.BlePrinter.__init__ = orig
    labels = model.labels()
    assert labels[-1] == "CANCELPRINTING"
    assert model.writes[-1][0] == "ABF1" and model.writes[-1][1] == bp.CANCELPRINTING_FRAME
    assert labels.count("DEVICEPRINT") == 5  # stopped at the next packet
    assert "PRINTINEND" not in labels
    assert model.disconnects == 1


def test_real_sigint_cancels_the_job(selftest_page):
    """An actual SIGINT (Ctrl-C) mid-job goes through the loop's handler, not KeyboardInterrupt."""
    state = {}

    def hook(model, client, n):
        if n == 5 and not state:
            handler = signal.getsignal(signal.SIGINT)
            state["handler_installed"] = handler is not signal.default_int_handler
            if state["handler_installed"]:  # never send a SIGINT that would reach pytest
                os.kill(os.getpid(), signal.SIGINT)
        return None

    model = Model(on_data_write=hook)
    kw = inject(model)
    kw["handle_sigint"] = True
    with pytest.raises(bt.BleCancelled):
        bt.print_job([selftest_page], 1, fast_options(), **kw)
    assert state["handler_installed"]
    assert model.labels()[-1] == "CANCELPRINTING"
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # restored


def test_sigint_handler_first_cancels_job_then_stops():
    class P:
        printing = True
        cancelled = 0

        def request_cancel(self):
            self.cancelled += 1

    class Task:
        cancelled = 0

        def cancel(self):
            self.cancelled += 1

    p, t = P(), Task()
    h = bt.make_sigint_handler(p, t)
    h()
    assert (p.cancelled, t.cancelled) == (1, 0)
    h()
    assert (p.cancelled, t.cancelled) == (1, 1)
    p2, t2 = P(), Task()
    p2.printing = False
    bt.make_sigint_handler(p2, t2)()
    assert (p2.cancelled, t2.cancelled) == (0, 1)


# --------------------------------------------------------------------------- printer status / errors


def test_not_ready_printer_is_refused(selftest_page):
    model = Model(infos=(bp.DeviceInfo(printstatus="2"),))
    with pytest.raises(bt.BlePrinterStatus, match="out_of_paper"):
        run_print(model, [selftest_page])
    assert model.labels() == ["DEVICEINFO"]


def test_busy_printer_is_asked_again_once():
    model = Model(infos=(bp.DeviceInfo(printstatus="1"), READY))
    res = run_print(model, [tiny_page()])
    assert model.labels()[:3] == ["DEVICEINFO", "DEVICEINFO", "DEVICEPRINT"]
    assert res.printed == 1


def test_still_busy_after_retry_is_refused():
    model = Model(infos=(bp.DeviceInfo(printstatus="256"), bp.DeviceInfo(printstatus="256")))
    with pytest.raises(bt.BlePrinterStatus, match="calibrating_paper"):
        run_print(model, [tiny_page()])


def test_printer_error_report_mid_job_aborts(selftest_page):
    model = Model(extra={2: [bp.report_frame(10, "8")]})
    with pytest.raises(bt.BlePrinterStatus, match="hatch_open"):
        run_print(model, [selftest_page])
    assert "PRINTINEND" not in model.labels()


def test_stop_report_aborts_unless_hatch_open(selftest_page):
    model = Model(extra={1: [bp.report_frame(13, "1")]})
    with pytest.raises(bt.BleJobFailed, match="stopped"):
        run_print(model, [selftest_page])


def test_print_error_code_aborts():
    def hook(model, client, n):
        return [bp.respond_frame(bp.EventType.DEVICEPRINT, code=500, responddata=bp.CodeMsg(3, "").encode())]

    with pytest.raises(bt.BleJobFailed, match="code 500"):
        run_print(Model(on_data_write=hook), [tiny_page()])


def test_label_taller_than_200mm_needs_send_while_printing():
    with pytest.raises(bt.BleJobFailed, match="200 mm"):
        run_print(Model(), [tiny_page()], label_height_mm=250)
    ok = Model(infos=(bp.DeviceInfo(printstatus="0", supportfunction=0x80),))
    assert run_print(ok, [tiny_page()], label_height_mm=250).printed == 1


# --------------------------------------------------------------------------- density / speed are opt-in


def test_density_and_speed_are_not_sent_by_default():
    model = Model()
    run_print(model, [tiny_page()])
    assert not any(e.startswith(("PRINTINCONCENTRATION", "PRINTINGSPEED")) for e in model.events())


def test_density_and_speed_opt_in():
    model = Model()
    run_print(model, [tiny_page()], options=fast_options(density=10, speed=3))
    assert model.events()[:3] == ["DEVICEINFO", "PRINTINCONCENTRATION sendint=10", "PRINTINGSPEED sendint=3"]
    assert [c for c, _f, _r in model.writes[:3]] == ["ABF1"] * 3


# --------------------------------------------------------------------------- connect / setup


def test_connect_retries_once():
    model = Model(fail_connects=1)
    assert run_print(model, [tiny_page()]).printed == 1
    assert len(model.clients) == 2


def test_connect_retry_ignores_stale_disconnect_callback():
    # Attempt 1's client fires disconnected_callback (client1) while we're
    # tearing it down after connect() failed -- attempt 2's client (client2)
    # then connects fine and must not see attempt 1's stale event.
    model = Model(fail_connects=1, disconnect_fires_callback=True)
    assert run_print(model, [tiny_page()]).printed == 1
    assert len(model.clients) == 2


def test_connect_gives_up_after_two_attempts():
    model = Model(fail_connects=2)
    with pytest.raises(bt.BleConnectError, match="2 attempt"):
        run_print(model, [tiny_page()])
    assert model.writes == []


def test_missing_characteristic_is_a_clear_error():
    model = Model(missing={bp.DATA_UUID})
    with pytest.raises(bt.BleConnectError, match="0xABF4"):
        run_print(model, [tiny_page()])
    assert model.disconnects == 1 and model.writes == []


def test_query_device_info_and_printer_selftest():
    model = Model()
    info = bt.query_device_info(fast_options(), **inject(model))
    assert info.firmwarever == "SIM" and model.labels() == ["DEVICEINFO"]

    class SelftestModel(Model):
        def on_write(self, uuid, frame, response, client):
            if frame == bp.SELFTEST_FRAME:
                self.writes.append((bp.short_uuid(uuid), frame, response))
                return [bp.respond_frame(bp.EventType.SELFTEST, code=200)]
            return super().on_write(uuid, frame, response, client)

    m2 = SelftestModel()
    assert bt.printer_selftest(fast_options(), **inject(m2)) is True
    assert m2.labels() == ["DEVICEINFO", "SELFTEST"]
    assert m2.writes[-1][0] == "ABF1"


def test_query_device_info_can_set_density_first():
    model = Model()
    bt.query_device_info(fast_options(density=9), **inject(model))
    assert model.events() == ["DEVICEINFO", "PRINTINCONCENTRATION sendint=9", "DEVICEINFO"]


def test_scan_filters_by_name_and_sorts_by_rssi():
    class Adv:
        def __init__(self, name, rssi):
            self.local_name = name
            self.rssi = rssi

    async def discover(timeout, return_adv):
        assert return_adv is True
        return {
            "a": (FakeDevice("A", None), Adv("RW403B-1", -80)),
            "b": (FakeDevice("B", "Someone's headphones"), Adv(None, -40)),
            "c": (FakeDevice("C", "RW403B-2"), Adv(None, -50)),
        }

    res = bt.scan(1.0, discover=discover)
    assert [p["address"] for p in res["printers"]] == ["C", "A"]
    assert res["printers"][0] == {"address": "C", "name": "RW403B-2", "rssi": -50}
    assert res["others"] == 1


def test_importing_the_ble_modules_does_not_import_bleak():
    code = (
        "import sys; import munbyn.ble_transport, munbyn.ble_protocol; "
        "assert 'bleak' not in sys.modules, 'bleak imported'; print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         cwd=os.path.dirname(os.path.dirname(__file__)))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_debug_log_has_every_frame_in_hex(caplog):
    caplog.set_level("DEBUG", logger="munbyn.ble")
    run_print(Model(), [tiny_page()])
    tx = [r.getMessage() for r in caplog.records if r.getMessage().startswith("TX ")]
    rx = [r.getMessage() for r in caplog.records if r.getMessage().startswith("RX ")]
    assert len(tx) == 3 and bp.DEVICEINFO_FRAME.hex() in tx[0] and bp.PRINTINEND_FRAME.hex() in tx[-1]
    assert any(bp.ACK_FRAME.hex() in m for m in rx)
