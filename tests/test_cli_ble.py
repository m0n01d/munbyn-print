"""Tests for print_label.py's Bluetooth (--ble) paths.

Bluetooth is never touched: --test paths are checked against a transport that
raises if called, and "real" paths use a monkeypatched munbyn.ble_transport.
"""
from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import os
import subprocess
import sys

import pytest

import print_label
from munbyn import ble_protocol as bp
from munbyn import ble_transport as bt
from munbyn import tspl
from munbyn import usb_transport

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "ble")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MUNBYN_CONFIG", str(tmp_path / "config.json"))
    yield


def _refuse(*_a, **_k):
    raise AssertionError("Bluetooth/USB must never be touched here")


@pytest.fixture
def no_radio(monkeypatch):
    for name in ("print_job", "query_device_info", "printer_selftest", "scan", "BlePrinter"):
        monkeypatch.setattr(bt, name, _refuse)
    monkeypatch.setattr(usb_transport, "Printer", _refuse)
    monkeypatch.setattr(usb_transport, "find_printers", _refuse)


@pytest.fixture
def sample_png(tmp_path):
    from PIL import Image

    path = tmp_path / "sample.png"
    Image.new("RGB", (200, 300), "white").save(path)
    return str(path)


class _FixedDate(datetime.date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 27)


class _FixedDatetime:
    date = _FixedDate


class Recorder:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        if self.exc is not None:
            raise self.exc
        return self.result


def _result(pages=1, copies=1):
    return bt.PrintResult(pages=pages, sends=1, copies=copies, expected=copies * pages, printed=copies * pages,
                          writes=52, bytes_sent=19540, resends=0, per_size=400, seconds=1.5)


# --------------------------------------------------------------------------- --test: frames, no radio


def test_selftest_ble_dry_run_is_the_editor_golden_byte_for_byte(tmp_path, monkeypatch, capsys, no_radio):
    # the golden was recorded from this repo's 4x6 self-test drawn on 2026-09-27 at feed_scale 0.981
    monkeypatch.setattr(tspl, "datetime", _FixedDatetime)
    hexfile = tmp_path / "frames.bin"
    rc = print_label.main(["--selftest", "--ble", "--test", "--hex", str(hexfile)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "816x1242 dots" in out and "16 section(s)" in out and "50 packet(s)" in out
    assert "52 write(s): 50 DEVICEPRINT on 0xABF4, 2 control on 0xABF1" in out
    frames = [bp.enpack(p) for p in bp.unpack_all(hexfile.read_bytes())]
    with open(os.path.join(FIX, "golden_selftest_4x6.json")) as f:
        golden = [e for e in json.load(f)["log"] if e["dir"] == "write"]
    assert [hashlib.sha256(fr).hexdigest() for fr in frames] == [e["sha256"] for e in golden]
    # the page itself is the committed golden input bitmap
    with gzip.open(os.path.join(FIX, "selftest_4x6_816x1242.bits.gz")) as f:
        bits = f.read()
    page = bp.compose_page(tspl.selftest_image(tspl.JobSettings(size=print_label.labels_mod.parse_size("4x6"),
                                                                feed_scale=0.981)))
    assert bp.pack_page(page)[2] == bits


def test_print_file_ble_dry_run(sample_png, capsys, no_radio):
    rc = print_label.main([sample_png, "--ble", "--test", "--copies", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "copies=2" in out and "expecting 2 'printed'" in out
    assert "PRINTINEND" in out and "SIZE" not in out  # a BLE frame dump, not TSPL


def test_feed_and_scale_test_ble_dry_run(capsys, no_radio):
    assert print_label.main(["--feed", "--ble", "--test"]) == 0
    assert "816x1242 dots" in capsys.readouterr().out
    assert print_label.main(["--scale-test", "--ble", "--test"]) == 0
    assert "Bluetooth (BLE) job" in capsys.readouterr().out


def test_other_ble_commands_dry_run(capsys, no_radio):
    assert print_label.main(["--status", "--ble", "--test"]) == 0
    assert "DEVICEINFO (5502405d0801 on 0xABF1)" in capsys.readouterr().out
    assert print_label.main(["--ble-scan", "--test"]) == 0
    assert "not touching Bluetooth" in capsys.readouterr().out
    assert print_label.main(["--ble-printer-selftest", "--test"]) == 0
    assert "5502405d0802" in capsys.readouterr().out


def test_ble_test_never_imports_bleak(tmp_path):
    code = (
        "import sys, print_label\n"
        "rc = print_label.main(['--selftest', '--ble', '--test'])\n"
        "assert rc == 0\n"
        "assert 'bleak' not in sys.modules, 'bleak was imported'\n"
    )
    env = dict(os.environ, MUNBYN_CONFIG=str(tmp_path / "c.json"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=REPO,
                         env=env)
    assert out.returncode == 0, out.stderr
    assert "52 write(s)" in out.stdout


def test_ble_density_speed_show_in_dry_run(capsys, no_radio):
    rc = print_label.main(["--selftest", "--ble", "--test", "--ble-density", "8", "--ble-speed", "4"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PRINTINCONCENTRATION sendint=8" in out and "PRINTINGSPEED sendint=4" in out


def test_tspl_only_flags_are_noted(capsys, no_radio):
    assert print_label.main(["--selftest", "--ble", "--test", "--density", "5"]) == 0
    assert "--density is TSPL-only" in capsys.readouterr().err


# --------------------------------------------------------------------------- feed scale over BLE


def _dry_run_height(capsys, argv):
    assert print_label.main(argv) == 0
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if l.startswith("page 1:")][0]
    return int(line.split()[2].split("x")[1])


def test_ble_feed_scale_default_is_0_981(capsys, no_radio):
    assert _dry_run_height(capsys, ["--feed", "--ble", "--test"]) == round(1218 / 0.981) == 1242


def test_ble_feed_scale_flags_and_config(capsys, monkeypatch, tmp_path, no_radio):
    assert _dry_run_height(capsys, ["--feed", "--ble", "--test", "--ble-feed-scale", "1.0"]) == 1218
    assert _dry_run_height(capsys, ["--feed", "--ble", "--test", "--feed-scale", "1.0"]) == 1218
    from munbyn import config as config_mod

    config_mod.save({"ble_feed_scale": 0.97, "feed_scale": 1.0})
    assert _dry_run_height(capsys, ["--feed", "--ble", "--test"]) == round(1218 / 0.97)
    # USB keeps its own feed_scale
    assert print_label.main(["--feed", "--test"]) == 0
    assert "SIZE 102 mm,152 mm" in capsys.readouterr().out


def test_bad_ble_feed_scale(capsys, no_radio):
    assert print_label.main(["--feed", "--ble", "--test", "--ble-feed-scale", "2"]) == 1
    assert print_label.main(["--feed", "--ble", "--test", "--ble-feed-scale", "2", "--save-defaults"]) == 1


# --------------------------------------------------------------------------- config / flags


def test_save_defaults_remembers_ble_settings(tmp_path, monkeypatch, capsys, no_radio):
    cfg = tmp_path / "saved.json"
    monkeypatch.setenv("MUNBYN_CONFIG", str(cfg))
    rc = print_label.main(["--selftest", "--test", "--ble-address", "ABCD-1234", "--ble-feed-scale", "0.97",
                           "--save-defaults"])
    assert rc == 0
    saved = json.loads(cfg.read_text())
    # --ble-address/--ble-feed-scale alone save only those two keys, not
    # "transport": only an explicit --ble/--usb decides the default transport,
    # so a later plain run (e.g. the "Print to Munbyn RW403B" PDF service)
    # must stay on USB even after an address has been saved.
    assert saved == {"ble_address": "ABCD-1234", "ble_feed_scale": 0.97}
    capsys.readouterr()
    assert print_label.main(["--selftest", "--test"]) == 0
    assert "SIZE 102 mm" in capsys.readouterr().out
    # an explicit --ble --save-defaults (reusing the saved address) is what
    # switches later plain runs to Bluetooth ...
    assert print_label.main(["--selftest", "--test", "--ble", "--save-defaults"]) == 0
    assert json.loads(cfg.read_text())["transport"] == "ble"
    capsys.readouterr()
    assert print_label.main(["--selftest", "--test"]) == 0
    assert "Bluetooth (BLE) job" in capsys.readouterr().out
    # ... unless --usb
    assert print_label.main(["--selftest", "--test", "--usb"]) == 0
    assert "SIZE 102 mm" in capsys.readouterr().out


def test_ble_address_alone_does_not_imply_transport_for_later_runs(tmp_path, monkeypatch, capsys, no_radio):
    """Regression: --ble-address --save-defaults must not also save
    transport": "ble" -- every later plain run (including scripts/
    print-from-dialog.sh and --status) would otherwise switch to the
    unverified Bluetooth path."""
    cfg = tmp_path / "saved.json"
    monkeypatch.setenv("MUNBYN_CONFIG", str(cfg))
    assert print_label.main(["--status", "--test", "--ble-address", "ABCD", "--save-defaults"]) == 0
    assert "transport" not in json.loads(cfg.read_text())
    capsys.readouterr()
    assert print_label.main(["--status", "--test"]) == 0
    out = capsys.readouterr().out
    assert "would check the printer over USB" in out  # stayed on USB, not the --ble dry-run message


def test_feed_scale_save_with_ble_refuses_to_overwrite_usb_calibration(capsys, tmp_path, monkeypatch, no_radio):
    """Regression: a plain --feed-scale (documented to work 'for one run
    with --ble') must not silently overwrite the USB-measured feed_scale
    when --save-defaults is also given."""
    cfg = tmp_path / "saved.json"
    monkeypatch.setenv("MUNBYN_CONFIG", str(cfg))
    rc = print_label.main(["--scale-test", "--ble", "--test", "--feed-scale", "0.97", "--save-defaults"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "--ble-feed-scale" in err
    assert not cfg.exists()
    # --ble-feed-scale is the correct way to save a Bluetooth measurement
    rc = print_label.main(["--scale-test", "--ble", "--test", "--ble-feed-scale", "0.97", "--save-defaults"])
    assert rc == 0
    saved = json.loads(cfg.read_text())
    assert saved == {"transport": "ble", "ble_feed_scale": 0.97}
    # giving both explicitly is unambiguous and both are saved
    capsys.readouterr()
    rc = print_label.main(
        ["--scale-test", "--ble", "--test", "--feed-scale", "0.96", "--ble-feed-scale", "0.98", "--save-defaults"]
    )
    assert rc == 0
    saved = json.loads(cfg.read_text())
    assert saved["feed_scale"] == 0.96 and saved["ble_feed_scale"] == 0.98


def test_check_ble_args_validated_before_save(tmp_path, monkeypatch, capsys, no_radio):
    """Regression: an invalid/conflicting Bluetooth flag combination must be
    rejected before --save-defaults writes anything to disk."""
    cfg = tmp_path / "saved.json"
    monkeypatch.setenv("MUNBYN_CONFIG", str(cfg))
    rc = print_label.main(["--selftest", "--test", "--ble", "--usb", "--save-defaults"])
    assert rc == 1
    assert not cfg.exists()


@pytest.mark.parametrize("argv", [
    ["--selftest", "--test", "--ble", "--usb"],
    ["--selftest", "--test", "--ble", "--ble-packet-size", "5"],
    ["--selftest", "--test", "--ble", "--ble-packet-size", "480"],
    ["--selftest", "--test", "--ble", "--ble-density", "17"],
    ["--selftest", "--test", "--ble", "--ble-speed", "0"],
    ["--list", "--ble"],
])
def test_bad_ble_usage_is_rc_1(argv, capsys, no_radio):
    assert print_label.main(argv) == 1


# --------------------------------------------------------------------------- real paths (transport mocked)


def test_ble_print_calls_transport_with_pages_and_options(monkeypatch, sample_png, capsys):
    rec = Recorder(result=_result(copies=3))
    monkeypatch.setattr(bt, "print_job", rec)
    monkeypatch.setattr(usb_transport, "Printer", _refuse)
    rc = print_label.main([sample_png, "--ble", "--copies", "3", "--ble-address", "UUID-1", "--ble-packet-size",
                           "148", "--ble-write", "response", "--ble-density", "9"])
    assert rc == 0
    (args, kwargs), = rec.calls
    pages, copies, opts = args
    assert copies == 3
    assert len(pages) == 1 and pages[0].width_dots == 816 and pages[0].height == 1242
    assert (opts.address, opts.per_size, opts.write_mode, opts.density, opts.speed) == ("UUID-1", 148, "response",
                                                                                          9, None)
    assert kwargs["label_height_mm"] == pytest.approx(152.4)
    assert "over Bluetooth" in capsys.readouterr().out


def test_ble_selftest_uses_saved_address(monkeypatch, capsys):
    from munbyn import config as config_mod

    config_mod.save({"ble_address": "SAVED-UUID"})
    rec = Recorder(result=_result())
    monkeypatch.setattr(bt, "print_job", rec)
    assert print_label.main(["--selftest", "--ble"]) == 0
    assert rec.calls[0][0][2].address == "SAVED-UUID"
    assert rec.calls[0][0][2].density is None and rec.calls[0][0][2].speed is None  # opt-in only


def test_ble_x_shift_is_baked_into_the_page(monkeypatch):
    rec = Recorder(result=_result())
    monkeypatch.setattr(bt, "print_job", rec)
    assert print_label.main(["--selftest", "--ble", "--x-shift", "-3"]) == 0
    shifted = rec.calls[0][0][0][0]
    rec2 = Recorder(result=_result())
    monkeypatch.setattr(bt, "print_job", rec2)
    assert print_label.main(["--selftest", "--ble"]) == 0
    plain = rec2.calls[0][0][0][0]
    assert shifted.width_dots == plain.width_dots == 816
    assert shifted.data != plain.data


def test_ble_errors_map_to_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(bt, "print_job", Recorder(exc=bt.BleTimeout("no ack for section 3")))
    assert print_label.main(["--selftest", "--ble"]) == 2
    assert "no ack for section 3" in capsys.readouterr().err
    monkeypatch.setattr(bt, "print_job", Recorder(exc=bt.BleCancelled("Cancelled: sent CANCELPRINTING")))
    assert print_label.main(["--selftest", "--ble"]) == 130
    assert "CANCELPRINTING" in capsys.readouterr().err
    monkeypatch.setattr(bt, "print_job", Recorder(exc=KeyboardInterrupt()))
    assert print_label.main(["--selftest", "--ble"]) == 130


def test_ble_status_prints_deviceinfo(monkeypatch, capsys):
    info = bp.DeviceInfo(firmwarever="1.2.3", blever="1.0.9", printstatus="8", concentration=12, speed=4,
                         supportfunction=2)
    rec = Recorder(result=info)
    monkeypatch.setattr(bt, "query_device_info", rec)
    assert print_label.main(["--status", "--ble"]) == 0
    out = capsys.readouterr().out
    assert "status: hatch_open" in out and "'1.2.3'" in out and "packet size 400" in out


def test_ble_scan_lists_printers(monkeypatch, capsys):
    monkeypatch.setattr(bt, "scan", Recorder(result={"printers": [{"address": "U1", "name": "RW403B-9",
                                                                     "rssi": -55}], "others": 4}))
    assert print_label.main(["--ble-scan"]) == 0
    assert "U1  RW403B-9  RSSI -55 dBm" in capsys.readouterr().out
    monkeypatch.setattr(bt, "scan", Recorder(result={"printers": [], "others": 4}))
    assert print_label.main(["--ble-scan"]) == 1
    assert "4 other device(s)" in capsys.readouterr().out


def test_ble_printer_selftest(monkeypatch, capsys):
    monkeypatch.setattr(bt, "printer_selftest", Recorder(result=True))
    assert print_label.main(["--ble-printer-selftest"]) == 0
    assert "printer confirmed" in capsys.readouterr().out
