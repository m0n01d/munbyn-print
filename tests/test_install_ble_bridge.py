"""scripts/install-ble-bridge.sh in its --dest dry-run mode, plus static checks
of the CUPS installer's --ble queue.

The dry run builds MunbynBLE.app and the LaunchAgent plist under a temp dir.
launchctl/pkill/pgrep/lsregister are shadowed by recorders on PATH and must
never be called. The launcher is then run with a stub module (never the real
bridge, never Bluetooth) to check it keeps its own process and runs Python as
a child."""
from __future__ import annotations

import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "install-ble-bridge.sh"
CUPS_SCRIPT = REPO / "scripts" / "install-cups-queue.sh"
LABEL = "com.m0n01d.munbyn-ble-bridge"

needs_macos_build = pytest.mark.skipif(
    sys.platform != "darwin" or not all(shutil.which(t) for t in ("clang", "codesign", "plutil", "rsync"))
    or not (REPO / ".venv" / "bin" / "python").exists(),
    reason="needs macOS with clang/codesign and the repo .venv",
)


def test_scripts_parse():
    for script in (SCRIPT, CUPS_SCRIPT):
        proc = subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr


def test_cups_installer_has_the_bluetooth_queue():
    text = CUPS_SCRIPT.read_text()
    assert "BLE_QUEUE=Munbyn_RW403B_BLE" in text
    assert "QUEUE_DESC='Munbyn RW403B (Bluetooth)'" in text
    assert 'BLE_URI="socket://127.0.0.1:$BLE_PORT"' in text and "DEFAULT_BLE_PORT=9100" in text
    # --uninstall --ble removes only the queue; the USB uninstall keeps the shared filter/PPD while it's used
    assert 'if [[ $BLE == 1 ]]; then\n    say "done. Only $QUEUE was removed' in text
    assert 'if queue_exists "$BLE_QUEUE"; then' in text


def _fake_tools(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    for tool in ("launchctl", "pkill", "pgrep", "lsregister", "open", "tccutil"):
        f = bindir / tool
        f.write_text('#!/bin/sh\necho "{} $*" >> "{}"\nexit 1\n'.format(tool, calls))
        f.chmod(0o755)
    return bindir, calls


def _run_installer(tmp_path, *args):
    bindir, calls = _fake_tools(tmp_path) if not (tmp_path / "fakebin").exists() else (
        tmp_path / "fakebin", tmp_path / "calls.log")
    env = dict(os.environ, PATH="{}:{}".format(bindir, os.environ.get("PATH", "")))
    proc = subprocess.run(["/bin/bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=180,
                          env=env, cwd=str(REPO))
    return proc, calls


def _dest_paths(dest):
    app = dest / "Applications" / "MunbynBLE.app"
    return app, app / "Contents" / "MacOS" / "MunbynBLE", dest / "LaunchAgents" / (LABEL + ".plist")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One default (copy-mode) dry-run build shared by the checks below."""
    if sys.platform != "darwin" or not all(shutil.which(t) for t in ("clang", "codesign", "plutil", "rsync")):
        pytest.skip("needs macOS build tools")
    tmp = tmp_path_factory.mktemp("bridge")
    dest = tmp / "dest"
    proc, calls = _run_installer(tmp, "--dest", str(dest))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return tmp, dest, proc, calls


@needs_macos_build
def test_dry_run_never_runs_launchctl(built):
    _tmp, _dest, proc, calls = built
    assert not calls.exists(), calls.read_text()
    assert "Dry run complete" in proc.stdout and "(dry run) would run: launchctl" in proc.stdout


@needs_macos_build
def test_bundle_info_plist_and_signature(built):
    _tmp, dest, _proc, _calls = built
    app, exe, _plist = _dest_paths(dest)
    info_path = app / "Contents" / "Info.plist"
    assert subprocess.run(["plutil", "-lint", str(info_path)], capture_output=True).returncode == 0
    info = plistlib.loads(info_path.read_bytes())
    assert info["CFBundleIdentifier"] == LABEL
    assert info["CFBundleName"] == "MunbynBLE" and info["CFBundleExecutable"] == "MunbynBLE"
    assert info["LSUIElement"] is True and info["LSMinimumSystemVersion"]
    assert info["NSBluetoothAlwaysUsageDescription"] == (
        "MunbynBLE sends print jobs to your Munbyn RW403B label printer over Bluetooth.")
    assert "NSDocumentsFolderUsageDescription" not in info  # copy mode stays out of ~/Documents
    verify = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True)
    assert verify.returncode == 0, verify.stderr
    dv = subprocess.run(["codesign", "-dv", str(app)], capture_output=True, text=True).stderr
    assert "Identifier=" + LABEL in dv and "Signature=adhoc" in dv
    assert os.access(str(exe), os.X_OK)


@needs_macos_build
def test_launch_agent_plist(built):
    _tmp, dest, _proc, _calls = built
    _app, exe, plist_path = _dest_paths(dest)
    assert subprocess.run(["plutil", "-lint", str(plist_path)], capture_output=True).returncode == 0
    agent = plistlib.loads(plist_path.read_bytes())
    assert agent["Label"] == LABEL
    assert agent["ProgramArguments"] == [str(exe)]
    assert agent["RunAtLoad"] is True and agent["KeepAlive"] is True
    assert agent["AssociatedBundleIdentifiers"] == [LABEL]
    assert agent["LimitLoadToSessionType"] == "Aqua" and agent["ProcessType"] == "Interactive"


@needs_macos_build
def test_runtime_copy_can_import_the_bridge(built):
    _tmp, dest, _proc, _calls = built
    support = dest / "Application Support" / "MunbynBLE"
    code = ("import importlib.util as u, sys; missing = [m for m in ('munbyn.ble_bridge', 'bleak', 'heatshrink2', "
            "'CoreBluetooth') if u.find_spec(m) is None]; print(sys.prefix); sys.exit(1 if missing else 0)")
    proc = subprocess.run([str(support / "venv" / "bin" / "python"), "-c", code], cwd=str(support / "app"),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == str(support / "venv")


@needs_macos_build
def test_launcher_runs_python_as_a_child_and_forwards_sigterm(built, tmp_path):
    _tmp, dest, _proc, _calls = built
    _app, exe, _plist = _dest_paths(dest)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    out = tmp_path / "out.txt"
    (stub_dir / "munbyn_stub_child.py").write_text(
        "import os, signal, sys, time\n"
        "def log(m):\n    open(os.environ['STUB_OUT'], 'a').write(m + '\\n')\n"
        "def term(*a):\n    log('TERM'); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, term)\n"
        "log('{} {} {}'.format(os.getpid(), os.getppid(), ' '.join(sys.argv[1:])))\n"
        "for _ in range(400):\n    time.sleep(0.05)\n"
        "log('timeout'); sys.exit(3)\n"
    )
    env = dict(os.environ, STUB_OUT=str(out), MUNBYN_BLE_LAUNCHER_MODULE="munbyn_stub_child",
               PYTHONPATH=str(stub_dir))
    launcher = subprocess.Popen([str(exe), "--flag"], env=env)
    try:
        for _ in range(200):
            if out.exists() and out.read_text().strip():
                break
            time.sleep(0.05)
        child_pid, parent_pid, args = out.read_text().split("\n")[0].split(" ", 2)
        assert int(parent_pid) == launcher.pid  # a child, not exec'd over the launcher
        assert args == "--flag"
        comm = subprocess.run(["ps", "-o", "comm=", "-p", str(launcher.pid)], capture_output=True,
                              text=True).stdout.strip()
        assert comm.endswith("MunbynBLE.app/Contents/MacOS/MunbynBLE")  # the app keeps its identity
        launcher.send_signal(signal.SIGTERM)
        assert launcher.wait(10) == 0
        assert out.read_text().split("\n")[1] == "TERM"
    finally:
        if launcher.poll() is None:
            launcher.kill()


@needs_macos_build
def test_launcher_logs_a_hint_when_the_child_is_killed_by_a_signal(built, tmp_path):
    """A TCC kill (SIGABRT, no NSBluetoothAlwaysUsageDescription) leaves the
    Python side of the log silent -- the launcher must say something on its
    own stderr (-> munbyn-ble-bridge.launchd.log) or a restart loop looks
    like nothing happened at all."""
    _tmp, dest, _proc, _calls = built
    _app, exe, _plist = _dest_paths(dest)
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    (stub_dir / "munbyn_stub_abort.py").write_text("import os\nos.abort()\n")
    env = dict(os.environ, MUNBYN_BLE_LAUNCHER_MODULE="munbyn_stub_abort", PYTHONPATH=str(stub_dir))
    proc = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, env=env)
    assert proc.returncode == 128 + signal.SIGABRT
    assert "killed by signal {}".format(signal.SIGABRT) in proc.stderr
    assert "TCC" in proc.stderr


@needs_macos_build
def test_rerun_keeps_the_signed_app(built):
    tmp, dest, _proc, _calls = built
    _app, exe, _plist = _dest_paths(dest)
    before = exe.read_bytes()
    proc, calls = _run_installer(tmp, "--dest", str(dest))
    assert proc.returncode == 0, proc.stderr
    assert "is up to date" in proc.stdout and exe.read_bytes() == before
    assert not calls.exists()


@needs_macos_build
def test_in_place_open_mode_and_uninstall(tmp_path):
    dest = tmp_path / "dest"
    proc, calls = _run_installer(tmp_path, "--dest", str(dest), "--in-place", "--launch-mode", "open")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    app, _exe, plist_path = _dest_paths(dest)
    agent = plistlib.loads(plist_path.read_bytes())
    assert agent["ProgramArguments"] == ["/usr/bin/open", "-W", "-g", "-a", str(app)]
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert "NSDocumentsFolderUsageDescription" in info  # in place = running from ~/Documents
    assert not (dest / "Application Support" / "MunbynBLE").exists()
    proc, calls = _run_installer(tmp_path, "--dest", str(dest), "--uninstall")
    assert proc.returncode == 0, proc.stderr
    assert not app.exists() and not plist_path.exists()
    assert not calls.exists()


def test_bad_arguments(tmp_path):
    dest = str(tmp_path / "dest")  # even a bad invocation stays a dry run
    proc, _ = _run_installer(tmp_path, "--dest", dest, "--launch-mode", "sideways")
    assert proc.returncode != 0 and "direct or open" in proc.stderr
    proc, _ = _run_installer(tmp_path, "--dest", dest, "--bogus")
    assert proc.returncode != 0 and "unknown argument" in proc.stderr
