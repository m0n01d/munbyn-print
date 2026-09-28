#!/bin/bash
# Install (or remove) MunbynBLE.app, the per-user Bluetooth print bridge, and
# the LaunchAgent that keeps it running. No sudo.
#
# Why an app: macOS only lets a process use Bluetooth if its *responsible*
# app declares NSBluetoothAlwaysUsageDescription, and remembers the user's
# answer per app. cupsd (a system daemon) can never have it, and anything
# started from an app without it (the Claude app, a web server started from
# there) is killed by macOS at the first Bluetooth call. MunbynBLE.app has
# the key; its executable is a tiny C launcher (macos/munbyn-ble-launcher.c)
# that stays alive as the app and runs `python -m munbyn.ble_bridge` as a
# child, so the bridge gets MunbynBLE's permission. The bridge listens on
# 127.0.0.1:9100 (config ble_bridge_port) for TSPL jobs from the CUPS
# "Munbyn RW403B (Bluetooth)" queue, print_label.py --ble and the web UI.
#
# Usage:
#   scripts/install-ble-bridge.sh               # install / update, then (re)start the bridge
#   scripts/install-ble-bridge.sh --uninstall   # stop it and remove what this script added
#
# Options:
#   --in-place         Run the bridge straight from this repo (.venv, munbyn/) instead of
#                      from a copy in ~/Library/Application Support/MunbynBLE. The repo is
#                      under ~/Documents, which macOS protects, so MunbynBLE will also have
#                      to be allowed into Documents. Default: copy (re-run after pulling
#                      changes to munbyn/ so the bridge picks them up).
#   --launch-mode M    direct (default): the LaunchAgent runs
#                      MunbynBLE.app/Contents/MacOS/MunbynBLE itself. open: it runs
#                      `/usr/bin/open -W -g -a MunbynBLE.app` (a LaunchServices launch);
#                      the fallback if the Bluetooth prompt doesn't name MunbynBLE.
#   --dest DIR         Dry run, for checking the build: put the app, the runtime copy and
#                      the LaunchAgent plist under DIR (DIR/Applications,
#                      DIR/Application Support/MunbynBLE, DIR/LaunchAgents) and never run
#                      launchctl, lsregister or pkill. Nothing starts.
#   -h, --help         This text.
#
# It touches only:
#   ~/Applications/MunbynBLE.app                                  (the app; ad-hoc signed)
#   ~/Library/Application Support/MunbynBLE/                      (venv + munbyn copy; not with --in-place)
#   ~/Library/LaunchAgents/com.m0n01d.munbyn-ble-bridge.plist     (the LaunchAgent)
#   launchd job gui/$UID/com.m0n01d.munbyn-ble-bridge             (bootstrap / bootout)
# Logs: ~/Library/Logs/munbyn-ble-bridge.log (the bridge) and
#       ~/Library/Logs/munbyn-ble-bridge.launchd.log (launcher/Python stderr).
#
# The app is only rebuilt (and re-signed) when the launcher, its paths or the
# Info.plist change: macOS ties the Bluetooth permission of an ad-hoc signed
# app to its exact signature, so a rebuild means clicking Allow once more.
set -euo pipefail

LABEL=com.m0n01d.munbyn-ble-bridge
BUNDLE_ID=com.m0n01d.munbyn-ble-bridge
APP_NAME=MunbynBLE
EXE_NAME=MunbynBLE
MODULE=munbyn.ble_bridge
MIN_MACOS=11.0
BT_USAGE='MunbynBLE sends print jobs to your Munbyn RW403B label printer over Bluetooth.'
DOCS_USAGE='MunbynBLE runs the munbyn-print code kept in your Documents folder.'
BUILD_VERSION=1 # bump to force a rebuild of the app bundle

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER_SRC=$REPO_DIR/macos/munbyn-ble-launcher.c
REPO_PYTHON=$REPO_DIR/.venv/bin/python
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

say() { echo "==> $*"; }
run() {
  echo "+ $*"
  "$@"
}
die() {
  echo "error: $*" >&2
  exit 1
}
usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '/^set -euo/d' -e 's/^# \{0,1\}//'
}

MODE=install
IN_PLACE=0
LAUNCH_MODE=direct
DEST=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --uninstall) MODE=uninstall ;;
    --in-place) IN_PLACE=1 ;;
    --launch-mode)
      [[ $# -ge 2 ]] || die "--launch-mode needs direct or open"
      LAUNCH_MODE=$2
      shift
      ;;
    --launch-mode=*) LAUNCH_MODE=${1#--launch-mode=} ;;
    --dest)
      [[ $# -ge 2 ]] || die "--dest needs a directory"
      DEST=$2
      shift
      ;;
    --dest=*) DEST=${1#--dest=} ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $1"
      ;;
  esac
  shift
done
[[ $LAUNCH_MODE == direct || $LAUNCH_MODE == open ]] || die "--launch-mode must be direct or open, not '$LAUNCH_MODE'"
[[ $(uname -s) == Darwin ]] || die "this script is for macOS"
[[ $EUID -ne 0 ]] || die "run this as yourself, not with sudo: the bridge is a per-user LaunchAgent"

if [[ -n $DEST ]]; then
  DRY=1
  mkdir -p "$DEST"
  DEST=$(cd "$DEST" && pwd)
  APPS_DIR=$DEST/Applications
  AGENTS_DIR=$DEST/LaunchAgents
  SUPPORT_DIR="$DEST/Application Support/MunbynBLE"
  LOG_DIR=$DEST/Logs
else
  DRY=0
  APPS_DIR=$HOME/Applications
  AGENTS_DIR=$HOME/Library/LaunchAgents
  SUPPORT_DIR="$HOME/Library/Application Support/MunbynBLE"
  LOG_DIR=$HOME/Library/Logs
fi
APP=$APPS_DIR/$APP_NAME.app
EXE=$APP/Contents/MacOS/$EXE_NAME
PLIST=$AGENTS_DIR/$LABEL.plist
DOMAIN=gui/$(id -u)
if [[ $IN_PLACE == 1 ]]; then
  PYTHON=$REPO_PYTHON
  WORKDIR=$REPO_DIR
else
  PYTHON=$SUPPORT_DIR/venv/bin/python
  WORKDIR=$SUPPORT_DIR/app
fi

# These paths end up in a C string literal and an XML plist.
for p in "$APP" "$PLIST" "$PYTHON" "$WORKDIR" "$LOG_DIR"; do
  case "$p" in
    *'"'* | *'\'* | *'<'* | *'>'* | *'&'*) die "unsupported character in path: $p" ;;
  esac
done

stop_agent() {
  if [[ $DRY == 1 ]]; then
    say "(dry run) would run: launchctl bootout $DOMAIN/$LABEL; pkill -TERM -f '$EXE'"
    return
  fi
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    run launchctl bootout "$DOMAIN/$LABEL" || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
      sleep 0.5
    done
  fi
  # With --launch-mode open the app is LaunchServices' process, not the job's.
  if pgrep -f "$EXE" >/dev/null 2>&1; then
    run pkill -TERM -f "$EXE" || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      pgrep -f "$EXE" >/dev/null 2>&1 || break
      sleep 0.5
    done
  fi
}

uninstall() {
  stop_agent
  if [[ -e $PLIST ]]; then run rm -f "$PLIST"; else say "no $PLIST to remove"; fi
  if [[ -d $APP ]]; then
    if [[ $DRY == 0 && -x $LSREGISTER ]]; then "$LSREGISTER" -u "$APP" >/dev/null 2>&1 || true; fi
    run rm -rf "$APP"
  else
    say "no $APP to remove"
  fi
  if [[ -d $SUPPORT_DIR ]]; then run rm -rf "$SUPPORT_DIR"; else say "no $SUPPORT_DIR to remove"; fi
  cat <<EOF
Done. Left alone: the logs in $LOG_DIR/munbyn-ble-bridge*.log, the CUPS queue
(remove it with: sudo scripts/install-cups-queue.sh --uninstall --ble), and the
Bluetooth permission (forget it with: tccutil reset BluetoothAlways $BUNDLE_ID).
EOF
}

preflight() {
  command -v clang >/dev/null 2>&1 || die "clang not found (install the Xcode command line tools: xcode-select --install)"
  command -v codesign >/dev/null 2>&1 || die "codesign not found"
  command -v plutil >/dev/null 2>&1 || die "plutil not found"
  [[ -f $LAUNCHER_SRC ]] || die "missing $LAUNCHER_SRC"
  [[ -x $REPO_PYTHON ]] || die "no $REPO_PYTHON -- run ./setup.sh first"
  # find_spec only: nothing is imported, so nothing touches Bluetooth here.
  "$REPO_PYTHON" - <<'PY' || die "the repo's .venv is missing Bluetooth packages: .venv/bin/pip install -r requirements.txt"
import importlib.util, sys
missing = [m for m in ("bleak", "heatshrink2", "PIL", "CoreBluetooth", "libdispatch") if importlib.util.find_spec(m) is None]
if missing:
    print("missing: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
PY
}

sync_runtime() {
  if [[ $IN_PLACE == 1 ]]; then
    say "running in place from $REPO_DIR (macOS may also ask MunbynBLE for Documents access)"
    if [[ -d $SUPPORT_DIR ]]; then run rm -rf "$SUPPORT_DIR"; fi
  else
    say "copying the runtime to $SUPPORT_DIR"
    mkdir -p "$SUPPORT_DIR/app"
    run rsync -a --delete "$REPO_DIR/.venv/" "$SUPPORT_DIR/venv/"
    run rsync -a --delete --exclude __pycache__ "$REPO_DIR/munbyn/" "$SUPPORT_DIR/app/munbyn/"
    printf 'copied from %s on %s\n' "$REPO_DIR" "$(date '+%Y-%m-%d %H:%M:%S')" >"$SUPPORT_DIR/app/SOURCE"
  fi
  # The bridge module must be importable by that python from that directory.
  (cd "$WORKDIR" && "$PYTHON" -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$MODULE') else 1)") ||
    die "$PYTHON can't find $MODULE in $WORKDIR"
}

c_string() { # $1 -> a C string literal (paths are checked for " and \ above)
  printf '"%s"' "$1"
}

write_info_plist() { # $1 = output file
  local docs=
  if [[ $IN_PLACE == 1 ]]; then
    docs="	<key>NSDocumentsFolderUsageDescription</key>
	<string>$DOCS_USAGE</string>"
  fi
  cat >"$1" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key>
	<string>en</string>
	<key>CFBundleDisplayName</key>
	<string>$APP_NAME</string>
	<key>CFBundleExecutable</key>
	<string>$EXE_NAME</string>
	<key>CFBundleIdentifier</key>
	<string>$BUNDLE_ID</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleName</key>
	<string>$APP_NAME</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>1.0</string>
	<key>CFBundleVersion</key>
	<string>$BUILD_VERSION</string>
	<key>LSMinimumSystemVersion</key>
	<string>$MIN_MACOS</string>
	<key>LSUIElement</key>
	<true/>
	<key>NSBluetoothAlwaysUsageDescription</key>
	<string>$BT_USAGE</string>
$docs
</dict>
</plist>
EOF
  plutil -lint "$1" >/dev/null || die "generated Info.plist is invalid"
}

build_app() { # builds into $BUILD/$APP_NAME.app; sets APP_CHANGED
  local cfg=$BUILD/launcher_config.h bundle=$BUILD/$APP_NAME.app stamp
  {
    echo "/* generated by scripts/install-ble-bridge.sh */"
    echo "#define MUNBYN_PYTHON $(c_string "$PYTHON")"
    echo "#define MUNBYN_WORKDIR $(c_string "$WORKDIR")"
    echo "#define MUNBYN_MODULE $(c_string "$MODULE")"
  } >"$cfg"
  mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
  write_info_plist "$bundle/Contents/Info.plist"
  printf 'APPL????' >"$bundle/Contents/PkgInfo"
  stamp=$(cat "$LAUNCHER_SRC" "$cfg" "$bundle/Contents/Info.plist" <(echo "$BUILD_VERSION $(uname -m)") |
    shasum -a 256 | awk '{print $1}')
  echo "$stamp" >"$bundle/Contents/Resources/build-stamp"
  if [[ -f $APP/Contents/Resources/build-stamp && $(cat "$APP/Contents/Resources/build-stamp") == "$stamp" ]] &&
    codesign --verify --deep --strict "$APP" >/dev/null 2>&1; then
    say "$APP is up to date (same launcher, paths and Info.plist): keeping it, so its Bluetooth permission stays"
    APP_CHANGED=0
    return
  fi
  say "building $APP_NAME.app ($(uname -m))"
  run clang -std=c11 -O2 -Wall -Wextra -Werror -arch "$(uname -m)" -mmacosx-version-min="$MIN_MACOS" \
    -I"$BUILD" -o "$bundle/Contents/MacOS/$EXE_NAME" "$LAUNCHER_SRC"
  run codesign --force --deep -s - "$bundle"
  codesign --verify --deep --strict "$bundle" || die "codesign verification failed"
  APP_CHANGED=1
}

install_app() {
  if [[ $APP_CHANGED == 1 ]]; then
    mkdir -p "$APPS_DIR"
    if [[ -e $APP ]]; then run rm -rf "$APP"; fi
    run ditto "$BUILD/$APP_NAME.app" "$APP"
    codesign --verify --deep --strict "$APP" || die "codesign verification failed after copying"
    if [[ $DRY == 0 && -x $LSREGISTER ]]; then "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true; fi
  fi
}

write_agent_plist() {
  local tmp=$BUILD/$LABEL.plist progargs
  if [[ $LAUNCH_MODE == direct ]]; then
    progargs="		<string>$EXE</string>"
  else
    progargs="		<string>/usr/bin/open</string>
		<string>-W</string>
		<string>-g</string>
		<string>-a</string>
		<string>$APP</string>"
  fi
  cat >"$tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
$progargs
	</array>
	<key>AssociatedBundleIdentifiers</key>
	<array>
		<string>$BUNDLE_ID</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>ThrottleInterval</key>
	<integer>10</integer>
	<key>ProcessType</key>
	<string>Interactive</string>
	<key>LimitLoadToSessionType</key>
	<string>Aqua</string>
	<key>EnvironmentVariables</key>
	<dict>
		<key>PYTHONUNBUFFERED</key>
		<string>1</string>
	</dict>
	<key>StandardOutPath</key>
	<string>$LOG_DIR/munbyn-ble-bridge.launchd.log</string>
	<key>StandardErrorPath</key>
	<string>$LOG_DIR/munbyn-ble-bridge.launchd.log</string>
</dict>
</plist>
EOF
  plutil -lint "$tmp" >/dev/null || die "generated LaunchAgent plist is invalid"
  mkdir -p "$AGENTS_DIR" "$LOG_DIR"
  run install -m 0644 "$tmp" "$PLIST"
}

bridge_status_line() { # the running bridge's status, via the repo's client; fails if it doesn't answer
  # Status only: this never makes the bridge touch Bluetooth.
  (cd "$REPO_DIR" && "$REPO_PYTHON" -c '
from munbyn import ble_bridge_client as c, config
port = c.bridge_port(config.load())
r = c.probe(port, 1.0)
if not r:
    raise SystemExit(1)
print(c.describe_status(r, port))' 2>/dev/null)
}

start_agent() {
  if [[ $DRY == 1 ]]; then
    say "(dry run) would run: launchctl enable $DOMAIN/$LABEL; launchctl bootstrap $DOMAIN $PLIST"
    return
  fi
  run launchctl enable "$DOMAIN/$LABEL" || true
  local ok=0
  for _ in 1 2 3 4 5; do
    if run launchctl bootstrap "$DOMAIN" "$PLIST"; then
      ok=1
      break
    fi
    sleep 1 # the old instance may still be on its way out
  done
  [[ $ok == 1 ]] || die "launchctl bootstrap failed; see: launchctl print $DOMAIN/$LABEL"
  say "waiting for the bridge to answer on 127.0.0.1 ..."
  local status=
  for _ in $(seq 1 30); do
    if status=$(bridge_status_line); then
      break
    fi
    status=
    sleep 0.5
  done
  if [[ -z $status ]]; then
    echo "warning: the bridge hasn't answered yet. Check $LOG_DIR/munbyn-ble-bridge.log and" >&2
    echo "         $LOG_DIR/munbyn-ble-bridge.launchd.log, and: launchctl print $DOMAIN/$LABEL" >&2
  else
    say "$status"
  fi
}

if [[ $MODE == uninstall ]]; then
  uninstall
  exit 0
fi

preflight
BUILD=$(mktemp -d "${TMPDIR:-/tmp}/munbyn-ble-bridge.XXXXXX")
# shellcheck disable=SC2064  # expand $BUILD now
trap "rm -rf '$BUILD'" EXIT
APP_CHANGED=0
build_app
stop_agent
sync_runtime
install_app
write_agent_plist
start_agent

if [[ $DRY == 1 ]]; then
  cat <<EOF

Dry run complete (nothing was started):
  app:        $APP
  plist:      $PLIST
  runtime:    $WORKDIR (python: $PYTHON)
Check it with: plutil -lint "$APP/Contents/Info.plist" "$PLIST"; codesign -dv "$APP"
EOF
  exit 0
fi

if [[ $LAUNCH_MODE == direct ]]; then
  RESTART_CMD="launchctl kickstart -k $DOMAIN/$LABEL"
else
  # In --launch-mode open, ProgramArguments is `open -W -g -a $APP`: launchd's
  # job is `open`, not the app, so `kickstart -k` only restarts `open` -- it
  # finds $APP_NAME still running and just waits on it again, so the bridge
  # itself is untouched. Killing the app directly makes `open -W` return, the
  # job exit, and KeepAlive relaunch it (a fresh `open -a`, which starts the
  # app again).
  RESTART_CMD="pkill -TERM -f '$EXE'"
fi

cat <<EOF

Installed $APP_NAME ($LAUNCH_MODE launch).
Next:
  1. If macOS asks "$APP_NAME would like to use Bluetooth", click Allow.
     (Or: System Settings > Privacy & Security > Bluetooth > $APP_NAME on.)
  2. For Preview / File > Print:  sudo scripts/install-cups-queue.sh --ble
  3. Test from Terminal or here:  ./print_label.py --status --ble
Logs:    $LOG_DIR/munbyn-ble-bridge.log
Restart: $RESTART_CMD
Remove:  scripts/install-ble-bridge.sh --uninstall
EOF
