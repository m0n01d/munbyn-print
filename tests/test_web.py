"""Tests for web.py (Flask). USB is always mocked -- never touches real hardware."""
from __future__ import annotations

import io

import pytest

import web
from munbyn import usb_transport


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MUNBYN_CONFIG", str(tmp_path / "config.json"))
    yield


@pytest.fixture
def app():
    return web.create_app(test_mode=True)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sample_png_bytes():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (200, 300), "white").save(buf, format="PNG")
    return buf.getvalue()


def _headers(origin=None):
    h = {"X-Munbyn": "1"}
    if origin is not None:
        h["Origin"] = origin
    return h


def _refuse_usb(*_a, **_k):
    raise AssertionError("USB must never be touched in test_mode")


def test_index_ok(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Munbyn" in resp.data


def test_status_dry_run_never_touches_usb(client, monkeypatch):
    monkeypatch.setattr(usb_transport, "Printer", _refuse_usb)
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["dry_run"] is True
    assert data["connected"] is False


def test_status_reports_disconnected_when_printer_not_found(monkeypatch):
    app = web.create_app(test_mode=False)
    client = app.test_client()

    class BoomPrinter:
        def __init__(self, *a, **k):
            raise usb_transport.PrinterNotFound("no printer")

    monkeypatch.setattr(usb_transport, "Printer", BoomPrinter)
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["connected"] is False
    assert data["dry_run"] is False


def test_preview_requires_munbyn_header(client, sample_png_bytes):
    resp = client.post(
        "/api/preview",
        data={"file": (io.BytesIO(sample_png_bytes), "sample.png")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 403


def test_preview_rejects_foreign_origin(client, sample_png_bytes):
    resp = client.post(
        "/api/preview",
        data={"file": (io.BytesIO(sample_png_bytes), "sample.png")},
        content_type="multipart/form-data",
        headers=_headers(origin="http://evil.example"),
    )
    assert resp.status_code == 403


def test_preview_returns_pages(client, sample_png_bytes):
    resp = client.post(
        "/api/preview",
        data={"file": (io.BytesIO(sample_png_bytes), "sample.png"), "size": "4x6"},
        content_type="multipart/form-data",
        headers=_headers(),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["pages"]) >= 1
    assert data["pages"][0].startswith("data:image/png;base64,")
    assert data["width_dots"] > 0
    assert data["height_dots"] > 0


def test_preview_bad_size_is_400(client, sample_png_bytes):
    resp = client.post(
        "/api/preview",
        data={"file": (io.BytesIO(sample_png_bytes), "sample.png"), "size": "not-a-size"},
        content_type="multipart/form-data",
        headers=_headers(),
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_print_dry_run_describes_job(client, sample_png_bytes):
    resp = client.post(
        "/api/print",
        data={"file": (io.BytesIO(sample_png_bytes), "sample.png"), "size": "4x6"},
        content_type="multipart/form-data",
        headers=_headers(),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["dry_run"] is True
    assert "SIZE" in data["describe"]


def test_print_writes_to_mocked_printer_when_not_test_mode(sample_png_bytes, monkeypatch):
    app = web.create_app(test_mode=False)
    client = app.test_client()
    written = {}

    class FakePrinter:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, data, chunk_size=4096):
            written["data"] = data
            return len(data)

    monkeypatch.setattr(usb_transport, "Printer", FakePrinter)
    resp = client.post(
        "/api/print",
        data={"file": (io.BytesIO(sample_png_bytes), "sample.png"), "size": "4x6"},
        content_type="multipart/form-data",
        headers=_headers(),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["bytes"] > 0
    assert written["data"]


def test_print_reports_printer_not_found(sample_png_bytes, monkeypatch):
    app = web.create_app(test_mode=False)
    client = app.test_client()

    class BoomPrinter:
        def __init__(self, *a, **k):
            raise usb_transport.PrinterNotFound("no printer")

    monkeypatch.setattr(usb_transport, "Printer", BoomPrinter)
    resp = client.post(
        "/api/print",
        data={"file": (io.BytesIO(sample_png_bytes), "sample.png"), "size": "4x6"},
        content_type="multipart/form-data",
        headers=_headers(),
    )
    assert resp.status_code == 503
    assert resp.get_json()["ok"] is False


def test_oversize_upload_returns_413(client):
    max_len = client.application.config["MAX_CONTENT_LENGTH"]
    big = b"0" * (max_len + 1024)
    resp = client.post(
        "/api/print",
        data={"file": (io.BytesIO(big), "sample.png"), "size": "4x6"},
        content_type="multipart/form-data",
        headers=_headers(),
    )
    assert resp.status_code == 413
