"""Persisted user defaults for munbyn-print.

Settings are stored as JSON at ``CONFIG_PATH`` (``~/.config/munbyn-print/config.json``
by default). Set the ``MUNBYN_CONFIG`` environment variable to use a different path
instead -- this is how tests isolate themselves from a developer's real config, and
it is honoured live (not just at import time), so setting the env var before calling
``load()``/``save()`` is enough; no reimport is required.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

#: Where the config lives when MUNBYN_CONFIG is not set.
_DEFAULT_CONFIG_PATH = Path.home() / ".config" / "munbyn-print" / "config.json"

#: Hardcoded defaults, merged with (and overridden by) the on-disk file.
DEFAULTS: Dict[str, Any] = {
    "size": "4x6",
    "media": "gap",
    "gap_mm": 3.0,
    "gap_offset_mm": 0.0,
    "density": 12,
    "speed": 4,
    "direction": 0,
    "offset_mm": 0.0,
    "x_shift_mm": 0.0,
    "y_shift_mm": 0.0,
    "bitmap_black_is_one": False,
    # This printer (RW403B, this Mac) is mechanically short along the paper
    # feed: an 800-row bar printed at 98.1mm, not 100mm (caliper-measured
    # 2026-09-27; Munbyn's own phone app shows the same kind of error, so
    # it's mechanical, not this repo's math). feed_scale = printed length /
    # intended length; every job path stretches bitmaps/SIZE length by
    # 1/feed_scale before sending to compensate. 1.0 disables the
    # correction. See CLAUDE.md and PLANS/PLAN.md.
    "feed_scale": 0.981,
    # Bluetooth LE (print_label.py --ble; see README "Bluetooth" and
    # PLANS/BLE-IMPLEMENTATION.md). "transport" picks the default link when
    # neither --ble nor --usb is given. ble_address is the printer's
    # CoreBluetooth UUID from --ble-scan (None = scan by name each time).
    # ble_feed_scale is feed_scale for Bluetooth jobs: it defaults to the
    # USB-measured 0.981 but is UNVERIFIED over BLE (Munbyn's phone app, which
    # prints over Bluetooth, measured 97.23mm for 100mm) -- re-measure with
    # `--scale-test --ble` and save it with --ble-feed-scale F --save-defaults.
    "transport": "usb",
    "ble_address": None,
    "ble_feed_scale": 0.981,
    "fit": "fit",
    "rotate": "auto",
    "crop": "auto",
    "dither": "threshold",
    "threshold": 160,
}


def _config_path() -> Path:
    override = os.environ.get("MUNBYN_CONFIG")
    return Path(override) if override else _DEFAULT_CONFIG_PATH


def __getattr__(name: str) -> Any:
    # PEP 562: makes `config.CONFIG_PATH` reflect the current MUNBYN_CONFIG
    # override live, even though it reads like a plain module constant.
    if name == "CONFIG_PATH":
        return _config_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def load() -> Dict[str, Any]:
    """Return ``DEFAULTS`` merged with the on-disk config file, if any.

    A missing file is silent (just the defaults). A present-but-unreadable or
    non-JSON-object file is not fatal: it's reported to stderr and DEFAULTS is
    used as-is.
    """
    values = dict(DEFAULTS)
    path = _config_path()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"munbyn-print: could not read config at {path}: {exc}", file=sys.stderr)
            return values
        if isinstance(data, dict):
            values.update(data)
        else:
            print(f"munbyn-print: config at {path} is not a JSON object, ignoring", file=sys.stderr)
    return values


def save(values: Dict[str, Any]) -> Path:
    """Merge ``values`` into the on-disk config (creating it if needed).

    Returns the path written.
    """
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    current: Dict[str, Any] = {}
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                current = data
        except (OSError, ValueError):
            current = {}

    current.update(values)
    with path.open("w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, sort_keys=True)
        f.write("\n")
    return path
