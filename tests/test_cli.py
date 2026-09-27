"""Tests for print_label.py (CLI). USB is always mocked -- never touches real hardware."""
from __future__ import annotations

import json

import pytest

import print_label
from munbyn import usb_transport


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Every test gets its own config file so runs never see a developer's real one."""
    monkeypatch.setenv("MUNBYN_CONFIG", str(tmp_path / "config.json"))
    yield


@pytest.fixture
def sample_png(tmp_path):
    from PIL import Image

    path = tmp_path / "sample.png"
    Image.new("RGB", (200, 300), "white").save(path)
    return str(path)


def _refuse_usb(*_a, **_k):
    raise AssertionError("USB must never be touched in --test mode")


class FakePrinter:
    """Records what was written instead of touching real hardware."""

    last_written = None

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, data, chunk_size=4096):
        FakePrinter.last_written = data
        return len(data)


# --- --test / dry run -------------------------------------------------------


def test_selftest_dry_run_describes_job_without_touching_usb(monkeypatch, capsys):
    monkeypatch.setattr(usb_transport, "Printer", _refuse_usb)
    rc = print_label.main(["--selftest", "--test", "--size", "4x6"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SIZE" in out
    assert "CLS" in out


def test_print_file_dry_run_never_touches_usb(monkeypatch, sample_png):
    monkeypatch.setattr(usb_transport, "Printer", _refuse_usb)
    monkeypatch.setattr(usb_transport, "find_printers", _refuse_usb)
    rc = print_label.main([sample_png, "--test", "--size", "4x6"])
    assert rc == 0


def test_list_dry_run_never_touches_usb(monkeypatch):
    monkeypatch.setattr(usb_transport, "find_printers", _refuse_usb)
    rc = print_label.main(["--list", "--test"])
    assert rc == 0


def test_status_dry_run_never_touches_usb(monkeypatch):
    monkeypatch.setattr(usb_transport, "Printer", _refuse_usb)
    rc = print_label.main(["--status", "--test"])
    assert rc == 0


def test_calibrate_dry_run_never_touches_usb(monkeypatch):
    monkeypatch.setattr(usb_transport, "Printer", _refuse_usb)
    rc = print_label.main(["--calibrate", "--test"])
    assert rc == 0


def test_feed_dry_run_never_touches_usb(monkeypatch):
    monkeypatch.setattr(usb_transport, "Printer", _refuse_usb)
    rc = print_label.main(["--feed", "--test"])
    assert rc == 0


# --- real (mocked) USB path --------------------------------------------------


def test_print_file_writes_to_mocked_printer(monkeypatch, sample_png):
    FakePrinter.last_written = None
    monkeypatch.setattr(usb_transport, "Printer", FakePrinter)
    rc = print_label.main([sample_png, "--size", "4x6"])
    assert rc == 0
    assert FakePrinter.last_written  # a non-empty job was written


def test_printer_not_found_exits_2(monkeypatch, sample_png):
    class BoomPrinter:
        def __init__(self, *a, **k):
            raise usb_transport.PrinterNotFound("no printer")

    monkeypatch.setattr(usb_transport, "Printer", BoomPrinter)
    rc = print_label.main([sample_png, "--size", "4x6"])
    assert rc == 2


def test_printer_busy_exits_2(monkeypatch, sample_png, capsys):
    class BoomPrinter:
        def __init__(self, *a, **k):
            raise usb_transport.PrinterBusy("busy: cancel -a Munbyn_RW403B")

    monkeypatch.setattr(usb_transport, "Printer", BoomPrinter)
    rc = print_label.main([sample_png, "--size", "4x6"])
    assert rc == 2
    assert "error" in capsys.readouterr().err.lower()


# --- output files -------------------------------------------------------


def test_hex_output_is_written(tmp_path, sample_png):
    hex_path = tmp_path / "job.hex"
    rc = print_label.main([sample_png, "--test", "--size", "4x6", "--hex", str(hex_path)])
    assert rc == 0
    assert hex_path.exists()
    assert hex_path.stat().st_size > 0


def test_preview_output_is_written(tmp_path, sample_png):
    preview_base = tmp_path / "preview"
    rc = print_label.main(
        [sample_png, "--test", "--size", "4x6", "--preview", str(preview_base)]
    )
    assert rc == 0
    assert (tmp_path / "preview.png").exists()


# --- usage / input errors -> exit code 1 ------------------------------------


def test_bad_size_is_a_usage_error(capsys, sample_png):
    rc = print_label.main([sample_png, "--test", "--size", "not-a-size"])
    assert rc == 1
    assert "error" in capsys.readouterr().err.lower()


def test_no_file_and_no_action_is_a_usage_error(capsys):
    rc = print_label.main([])
    assert rc == 1
    assert "error" in capsys.readouterr().err.lower()


# --- config: --save-defaults / defaults-from-config -------------------------


def test_save_defaults_writes_only_cli_given_keys(tmp_path, monkeypatch):
    cfg_path = tmp_path / "saved.json"
    monkeypatch.setenv("MUNBYN_CONFIG", str(cfg_path))
    rc = print_label.main(
        ["--selftest", "--test", "--size", "3x2", "--density", "7", "--save-defaults"]
    )
    assert rc == 0
    with cfg_path.open() as f:
        saved = json.load(f)
    assert saved["size"] == "3x2"
    assert saved["density"] == 7
    # --copies is per-run only, never persisted
    assert "copies" not in saved


def test_defaults_come_from_config_when_no_flag_given(tmp_path, monkeypatch, capsys):
    from munbyn import config as config_mod

    monkeypatch.setenv("MUNBYN_CONFIG", str(tmp_path / "cfg.json"))
    config_mod.save({"size": "2x1", "density": 3})
    rc = print_label.main(["--selftest", "--test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DENSITY 3" in out  # from the TSPL header, built from the loaded config


def test_selftest_preview_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(usb_transport, "Printer", _refuse_usb)
    rc = print_label.main(["--selftest", "--test", "--preview", str(tmp_path / "st.png")])
    assert rc == 0
    from PIL import Image

    img = Image.open(tmp_path / "st.png")
    assert img.size == (812, 1218)


def test_zero_copies_is_a_usage_error(sample_png, capsys):
    assert print_label.main([sample_png, "--test", "--copies", "0"]) == 1
