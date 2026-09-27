"""Tests for munbyn.config (defaults + JSON persistence)."""
from __future__ import annotations

import json

import pytest

from munbyn import config


def test_load_returns_a_copy_of_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MUNBYN_CONFIG", str(tmp_path / "missing.json"))
    values = config.load()
    assert values == config.DEFAULTS
    values["density"] = -999
    assert config.DEFAULTS["density"] != -999  # load() must not leak a mutable ref


def test_config_path_honors_env_override_live(tmp_path, monkeypatch):
    target = tmp_path / "cfg.json"
    monkeypatch.setenv("MUNBYN_CONFIG", str(target))
    assert config.CONFIG_PATH == target


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MUNBYN_CONFIG", str(tmp_path / "cfg.json"))
    config.save({"density": 9, "size": "3x2"})
    values = config.load()
    assert values["density"] == 9
    assert values["size"] == "3x2"
    # keys not touched by save() still come from DEFAULTS
    assert values["speed"] == config.DEFAULTS["speed"]


def test_save_merges_with_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    monkeypatch.setenv("MUNBYN_CONFIG", str(path))
    config.save({"density": 9})
    config.save({"speed": 7})
    with path.open() as f:
        raw = json.load(f)
    assert raw["density"] == 9
    assert raw["speed"] == 7


def test_load_tolerates_bad_json(tmp_path, monkeypatch, capsys):
    path = tmp_path / "cfg.json"
    path.write_text("not json {{{")
    monkeypatch.setenv("MUNBYN_CONFIG", str(path))
    values = config.load()
    assert values == config.DEFAULTS
    assert capsys.readouterr().err  # warned to stderr, did not raise


def test_load_tolerates_non_object_json(tmp_path, monkeypatch, capsys):
    path = tmp_path / "cfg.json"
    path.write_text("[1, 2, 3]")
    monkeypatch.setenv("MUNBYN_CONFIG", str(path))
    values = config.load()
    assert values == config.DEFAULTS
    assert capsys.readouterr().err


def test_save_returns_path_and_creates_parent_dirs(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "dir" / "cfg.json"
    monkeypatch.setenv("MUNBYN_CONFIG", str(path))
    result = config.save({"density": 5})
    assert result == path
    assert path.exists()


def test_defaults_include_this_printers_measured_feed_scale():
    # This printer (RW403B, this Mac) measured 0.981 on 2026-09-27 -- see
    # CLAUDE.md/PLANS/PLAN.md. A fresh install with no saved config must
    # apply this correction automatically.
    assert config.DEFAULTS["feed_scale"] == 0.981


def test_feed_scale_round_trips_through_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setenv("MUNBYN_CONFIG", str(tmp_path / "cfg.json"))
    config.save({"feed_scale": 1.0})
    assert config.load()["feed_scale"] == 1.0
