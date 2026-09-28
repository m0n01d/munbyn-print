"""print_label.py and web.py routing Bluetooth through the MunbynBLE bridge.

A real ``munbyn.ble_bridge.Bridge`` with a fake transport runs on an ephemeral
loopback port (put in the test's config as ``ble_bridge_port``). Bluetooth
and USB are never touched."""
from __future__ import annotations

import io
import socket
import threading

import pytest

import print_label
from bridge_fakes import BridgeThread, FakeTransport
from munbyn import ble_bridge_client as bc
from munbyn import ble_transport as bt
from munbyn import config as config_mod
from munbyn import usb_transport


def _refuse(*_a, **_k):
    raise AssertionError("Bluetooth/USB must never be touched here")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MUNBYN_CONFIG", str(tmp_path / "config.json"))
    for name in ("print_job", "query_device_info", "printer_selftest", "scan", "BlePrinter"):
        monkeypatch.setattr(bt, name, _refuse)
    monkeypatch.setattr(usb_transport, "Printer", _refuse)


@pytest.fixture
def bridge(allow_bridge_connections):
    fake = FakeTransport()
    with BridgeThread(fake) as b:
        config_mod.save({"ble_bridge_port": b.port})
        yield b


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def sample_png(tmp_path):
    from PIL import Image, ImageDraw

    path = tmp_path / "sample.png"
    img = Image.new("RGB", (200, 300), "white")
    ImageDraw.Draw(img).rectangle((20, 20, 120, 200), fill="black")
    img.save(path)
    return str(path)


def _direct_pages(monkeypatch, argv):
    """The pages --ble-direct would send for the same command line."""
    got = {}

    def fake_print_job(pages, copies, options, **kw):
        got["pages"], got["copies"] = pages, copies
        return bt.PrintResult(pages=len(pages), sends=1, copies=copies, expected=1, printed=1, writes=1,
                              bytes_sent=1, resends=0, per_size=400)

    monkeypatch.setattr(bt, "print_job", fake_print_job)
    assert print_label.main(argv + ["--ble-direct"]) == 0
    monkeypatch.setattr(bt, "print_job", _refuse)
    return got


@pytest.mark.parametrize("argv", [["--selftest"], ["--scale-test"], ["--feed"], ["--selftest", "--x-shift", "-3",
                                                                                   "--y-shift", "2"]])
def test_bridge_prints_exactly_what_ble_direct_would(argv, bridge, monkeypatch, capsys):
    assert print_label.main(argv + ["--ble"]) == 0
    out = capsys.readouterr().out
    assert "via the MunbynBLE bridge" in out
    (call,) = bridge.transport.calls
    direct = _direct_pages(monkeypatch, argv)
    assert [p.data for p in call["pages"]] == [p.data for p in direct["pages"]]
    assert [p.width_dots for p in call["pages"]] == [816]
    assert call["copies"] == direct["copies"] == 1


def test_file_with_copies_via_bridge(bridge, sample_png, capsys):
    assert print_label.main([sample_png, "--ble", "--copies", "2"]) == 0
    (call,) = bridge.transport.calls
    assert call["copies"] == 2 and call["pages"][0].height == 1242
    assert "2 copies" in capsys.readouterr().out


def test_config_transport_ble_routes_via_bridge(bridge, sample_png):
    config_mod.save({"transport": "ble"})
    assert print_label.main([sample_png]) == 0
    assert len(bridge.transport.calls) == 1


def test_bridge_uses_ble_feed_scale(bridge, capsys):
    assert print_label.main(["--selftest", "--ble", "--ble-feed-scale", "1.0"]) == 0
    assert bridge.transport.calls[0]["pages"][0].height == 1218  # 6 in at 203 dpi, unstretched


def test_bridge_not_running_is_a_clear_error(allow_bridge_connections, capsys):
    config_mod.save({"ble_bridge_port": _free_port()})
    assert print_label.main(["--selftest", "--ble"]) == 2
    err = capsys.readouterr().err
    assert "MunbynBLE bridge is not running" in err and "scripts/install-ble-bridge.sh" in err


def test_not_our_bridge_gets_no_job(allow_bridge_connections, capsys):
    received = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]

    def serve():
        for _ in range(3):
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                data = b""
                conn.settimeout(2)
                try:
                    while True:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                except socket.timeout:
                    pass
                received.append(data)
                conn.sendall(b"HTTP/1.0 400 nope\r\n\r\n")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    config_mod.save({"ble_bridge_port": port})
    try:
        assert print_label.main(["--selftest", "--ble"]) == 2
    finally:
        srv.close()
    assert "isn't the MunbynBLE bridge" in capsys.readouterr().err
    assert all(d.startswith(b"MUNBYN-STATUS") for d in received)  # the TSPL job was never sent


def test_bridge_job_failure_is_rc_2(bridge, capsys):
    bridge.transport.failures = [bt.BlePrinterStatus("printer reports: paper_out")]
    assert print_label.main(["--selftest", "--ble"]) == 2
    assert "paper_out" in capsys.readouterr().err


def test_status_via_bridge_shows_deviceinfo(bridge, capsys):
    assert print_label.main(["--status", "--ble"]) == 0
    out = capsys.readouterr().out
    assert "MunbynBLE bridge" in out and "FAKE-UUID" in out
    assert "firmware: '1.1.16'" in out and "status: ready" in out
    assert bridge.transport.info_calls == 1


def test_status_via_bridge_deviceinfo_error(bridge, capsys):
    bridge.transport.info_exc = bt.BleConnectError("printer is off")
    assert print_label.main(["--status", "--ble"]) == 2
    assert "printer is off" in capsys.readouterr().err


def test_density_needs_ble_direct(bridge, capsys):
    assert print_label.main(["--selftest", "--ble", "--ble-density", "9"]) == 1
    assert "--ble-direct" in capsys.readouterr().err
    assert bridge.transport.calls == []


def test_ble_address_mismatch_is_noted(bridge, capsys):
    assert print_label.main(["--selftest", "--ble", "--ble-address", "OTHER"]) == 0
    assert "only used with --ble-direct" in capsys.readouterr().err


def test_dry_run_never_contacts_the_bridge(monkeypatch, capsys):
    monkeypatch.setattr(bc, "_connect", _refuse)
    assert print_label.main(["--selftest", "--ble", "--test"]) == 0
    assert "MunbynBLE bridge on 127.0.0.1:9100" in capsys.readouterr().out
    assert print_label.main(["--status", "--ble", "--test"]) == 0


def test_ble_direct_and_usb_conflict(capsys):
    assert print_label.main(["--selftest", "--ble-direct", "--usb"]) == 1


# --------------------------------------------------------------------------- web UI


@pytest.fixture
def client():
    import web

    return web.create_app(test_mode=False).test_client()


def _png_bytes():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (100, 150), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_web_print_via_bridge(bridge, client):
    resp = client.post("/api/print", headers={"X-Munbyn": "1"},
                       data={"transport": "ble", "copies": "2", "file": (io.BytesIO(_png_bytes()), "a.png")},
                       content_type="multipart/form-data")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] and body["transport"] == "ble" and body["printed"] == 2
    assert bridge.transport.calls[0]["copies"] == 2


def test_web_selftest_via_bridge_uses_ble_feed_scale(bridge, client):
    config_mod.save({"ble_feed_scale": 1.0, "feed_scale": 0.981})
    resp = client.post("/api/selftest", headers={"X-Munbyn": "1"}, data={"transport": "ble"})
    assert resp.status_code == 200, resp.get_json()
    assert bridge.transport.calls[0]["pages"][0].height == 1218


def test_web_bridge_down_is_503_with_hint(allow_bridge_connections, client):
    config_mod.save({"ble_bridge_port": _free_port()})
    resp = client.post("/api/scale-test", headers={"X-Munbyn": "1"}, data={"transport": "ble"})
    assert resp.status_code == 503 and "install-ble-bridge.sh" in resp.get_json()["error"]


def test_web_status_for_bluetooth(bridge, client):
    body = client.get("/api/status?transport=ble", headers={"X-Munbyn": "1"}).get_json()
    assert body["transport"] == "ble" and body["connected"] is True and "FAKE-UUID" in body["status_note"]
    assert bridge.transport.info_calls == 0  # status never makes the bridge touch Bluetooth


def test_web_test_mode_never_contacts_the_bridge(monkeypatch):
    import web

    monkeypatch.setattr(bc, "_connect", _refuse)
    c = web.create_app(test_mode=True).test_client()
    resp = c.post("/api/selftest", headers={"X-Munbyn": "1"}, data={"transport": "ble"})
    body = resp.get_json()
    assert body["dry_run"] and "MunbynBLE bridge" in body["describe"] and "BITMAP 0,0,102," in body["describe"]
    assert c.get("/api/status?transport=ble", headers={"X-Munbyn": "1"}).get_json()["dry_run"] is True


def test_web_index_has_transport_selector():
    import web

    config_mod.save({"transport": "ble", "ble_feed_scale": 0.975})
    html = web.create_app(test_mode=True).test_client().get("/").data.decode()
    assert 'id="opt-transport"' in html and 'value="ble" selected' in html
    assert 'data-ble-feed-scale="0.9750"' in html and 'value="0.9750"' in html


def test_web_bad_transport_is_400(client):
    resp = client.post("/api/selftest", headers={"X-Munbyn": "1"}, data={"transport": "carrier-pigeon"})
    assert resp.status_code == 400
