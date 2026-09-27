#!/bin/bash
# Install (or remove) the native Munbyn RW403B CUPS queue, so Preview's normal
# Print dialog works on Apple Silicon without Rosetta.
#
# It installs munbyn-print's own arm64/x86_64 filter (cups/rastertotspl, which
# emits only the TSPL subset verified on the printer) plus a PPD derived from
# Munbyn's, and adds a SECOND queue next to Munbyn's broken one.
#
# Usage (a human runs this; it needs root):
#   sudo scripts/install-cups-queue.sh                  # install / update
#   sudo scripts/install-cups-queue.sh --make-default   # ...and make it the default printer
#   sudo scripts/install-cups-queue.sh --uninstall      # remove only what this script added
#
# Environment: MUNBYN_URI=usb://... overrides the device URI lookup.
#
# It touches only:
#   /Library/Printers/Munbyn/rastertotspl                                (new file)
#   /Library/Printers/PPDs/Contents/Resources/munbyn-rw403b-native.ppd   (new file)
#   CUPS queue Munbyn_RW403B_Native                                       (new queue)
#   the default printer, only with --make-default
# Munbyn's own files in /Library/Printers/Munbyn and the existing
# Munbyn_RW403B queue are never modified or removed.
set -euo pipefail

QUEUE=Munbyn_RW403B_Native
QUEUE_DESC='Munbyn RW403B (native)'
QUEUE_LOCATION='USB'
FILTER_DIR=/Library/Printers/Munbyn
FILTER_DST=$FILTER_DIR/rastertotspl
PPD_DIR=/Library/Printers/PPDs/Contents/Resources
PPD_DST=$PPD_DIR/munbyn-rw403b-native.ppd
FALLBACK_URI='usb://Munbyn/RW403B?serial=MP-RHHN1UV2'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CUPS_SRC=$REPO_DIR/cups
FILTER_SRC=$CUPS_SRC/rastertotspl
PPD_SRC=$CUPS_SRC/munbyn-rw403b-native.ppd

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
MAKE_DEFAULT=0
for arg in "$@"; do
  case "$arg" in
    --uninstall) MODE=uninstall ;;
    --make-default) MAKE_DEFAULT=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown argument: $arg"
      ;;
  esac
done
if [[ $MODE == uninstall && $MAKE_DEFAULT == 1 ]]; then
  die "--make-default and --uninstall can't be combined"
fi

[[ $(uname -s) == Darwin ]] || die "this script is for macOS"
if [[ $EUID -ne 0 ]]; then
  die "must run as root: sudo $0 $*"
fi

queue_exists() { lpstat -p "$QUEUE" >/dev/null 2>&1; }

uninstall() {
  if queue_exists; then
    run lpadmin -x "$QUEUE"
  else
    say "no $QUEUE queue to remove"
  fi
  if [[ -e $FILTER_DST ]]; then
    run rm -f "$FILTER_DST"
  else
    say "no $FILTER_DST to remove"
  fi
  if [[ -e $PPD_DST ]]; then
    run rm -f "$PPD_DST"
  else
    say "no $PPD_DST to remove"
  fi
  say "done. Munbyn's own files and the Munbyn_RW403B queue were not touched."
  say "if $QUEUE was the default printer, pick a new default in System Settings > Printers & Scanners."
}

build_filter() {
  if [[ -x $FILTER_SRC && ! $CUPS_SRC/rastertotspl.c -nt $FILTER_SRC ]]; then
    say "using the already-built $FILTER_SRC"
    return
  fi
  say "building the filter (make -C cups)"
  # Build as the invoking user so the repo doesn't get root-owned files.
  if [[ -n ${SUDO_USER:-} && ${SUDO_USER} != root ]]; then
    run sudo -u "$SUDO_USER" make -C "$CUPS_SRC"
  else
    run make -C "$CUPS_SRC"
  fi
  [[ -x $FILTER_SRC ]] || die "build did not produce $FILTER_SRC"
}

find_uri() {
  local uri=${MUNBYN_URI:-}
  if [[ -n $uri ]]; then
    say "using MUNBYN_URI=$uri" >&2
    echo "$uri"
    return
  fi
  say "looking for the printer: lpinfo -v | grep -i 'usb://Munbyn/RW403B'" >&2
  uri=$(lpinfo -v 2>/dev/null | grep -i 'usb://Munbyn/RW403B' | awk '{print $2}' | head -n 1 || true)
  if [[ -z $uri ]]; then
    uri=$FALLBACK_URI
    say "printer not detected (off or unplugged?); using the known URI $uri" >&2
  else
    say "found $uri" >&2
  fi
  echo "$uri"
}

install_queue() {
  [[ -f $PPD_SRC ]] || die "missing $PPD_SRC"
  build_filter
  if ! lipo -archs "$FILTER_SRC" 2>/dev/null | grep -qw "$(uname -m)"; then
    die "$FILTER_SRC has no $(uname -m) slice (lipo -archs: $(lipo -archs "$FILTER_SRC" 2>&1))"
  fi

  if [[ ! -d $FILTER_DIR ]]; then
    run install -d -o root -g wheel -m 0755 "$FILTER_DIR"
  fi
  run install -o root -g wheel -m 0755 "$FILTER_SRC" "$FILTER_DST"
  if [[ ! -d $PPD_DIR ]]; then
    run install -d -o root -g wheel -m 0755 "$PPD_DIR"
  fi
  run install -o root -g wheel -m 0644 "$PPD_SRC" "$PPD_DST"

  local uri
  uri=$(find_uri)
  if queue_exists; then
    say "$QUEUE already exists; updating it in place"
  fi
  # lpadmin warns that PPD drivers are deprecated; that warning is expected.
  run lpadmin -p "$QUEUE" -D "$QUEUE_DESC" -L "$QUEUE_LOCATION" -E -v "$uri" -P "$PPD_DST" \
    -o printer-is-shared=false
  run cupsenable "$QUEUE"
  run cupsaccept "$QUEUE"
  if [[ $MAKE_DEFAULT == 1 ]]; then
    run lpadmin -d "$QUEUE"
  else
    say "default printer unchanged (pass --make-default to change it)"
  fi

  run lpstat -p "$QUEUE" -v "$QUEUE"
  cat <<EOF

Installed. In Preview: File > Print, Printer "$QUEUE_DESC",
set Paper Size (4.00x6.00" etc.) and Scale there; label options (darkness,
speed, media type, offsets, threshold) are under "Printer Features".
Munbyn's original Munbyn_RW403B queue is untouched.
Undo: sudo $0 --uninstall
EOF
}

if [[ $MODE == uninstall ]]; then
  uninstall
else
  install_queue
fi
