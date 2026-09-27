#!/bin/bash
# Install (or remove) the native Munbyn RW403B CUPS queue, so Preview's normal
# Print dialog works on Apple Silicon without Rosetta.
#
# It installs munbyn-print's own arm64/x86_64 filter (cups/rastertotspl, which
# emits only the TSPL subset verified on the printer) plus a PPD derived from
# Munbyn's, and adds a SECOND queue next to Munbyn's broken one.
#
# Usage (a human runs this; it needs root):
#   sudo scripts/install-cups-queue.sh                     # install / update
#   sudo scripts/install-cups-queue.sh --feed-scale 0.981  # ...with a measured feed scale
#   sudo scripts/install-cups-queue.sh --make-default      # ...and make it the default printer
#   sudo scripts/install-cups-queue.sh --uninstall         # remove only what this script added
#
# --feed-scale F (default 0.981, allowed 0.90..1.10) is printed length /
# intended length along the paper feed. The RW403B's head is exact across
# (8 dots/mm) but its feed is short: an 800-row bar printed 98.1 mm
# (2026-09-27), so 0.981. The installed PPD's default Resolution becomes
# 203 x round(203 / F) dpi (0.981 -> 203x207dpi), so CUPS renders pages with
# that many rows per inch and they print at true length. To re-measure, print
# a 100 mm bar along the feed with Resolution "203 dpi, uncorrected"
# (-o Resolution=203x203dpi) and use F = measured mm / 100 (with the
# correction on, use F = current F * measured mm / 100 instead). Re-run with
# the new value to update the queue in place.
#
# Environment: MUNBYN_URI=usb://... overrides the device URI lookup.
#
# It touches only:
#   /Library/Printers/Munbyn/rastertotspl                                (new file)
#   /Library/Printers/PPDs/Contents/Resources/munbyn-rw403b-native.ppd   (new file,
#     generated from cups/munbyn-rw403b-native.ppd for --feed-scale by
#     `make -C cups ppd` into a temp dir and checked with cupstestppd first)
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
DEFAULT_FEED_SCALE=0.981

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
FEED_SCALE=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --uninstall) MODE=uninstall ;;
    --make-default) MAKE_DEFAULT=1 ;;
    --feed-scale)
      [[ $# -ge 2 ]] || die "--feed-scale needs a value, e.g. --feed-scale 0.981"
      FEED_SCALE=$2
      shift
      ;;
    --feed-scale=*) FEED_SCALE=${1#--feed-scale=} ;;
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
if [[ $MODE == uninstall && $MAKE_DEFAULT == 1 ]]; then
  die "--make-default and --uninstall can't be combined"
fi
if [[ $MODE == uninstall && -n $FEED_SCALE ]]; then
  die "--feed-scale and --uninstall can't be combined"
fi
FEED_SCALE=${FEED_SCALE:-$DEFAULT_FEED_SCALE}
if ! [[ $FEED_SCALE =~ ^[0-9]+(\.[0-9]+)?$ ]] ||
  ! awk -v f="$FEED_SCALE" 'BEGIN { exit !(f + 0 >= 0.9 && f + 0 <= 1.1) }'; then
  die "--feed-scale must be a decimal between 0.90 and 1.10 (printed length / intended length), got '$FEED_SCALE'"
fi
FEED_DPI=$(awk -v f="$FEED_SCALE" 'BEGIN { printf "%d", int(203 / f + 0.5) }')
RES_CHOICE="203x${FEED_DPI}dpi"

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

# Writes the PPD to install into $1: the template with its default Resolution
# regenerated for FEED_SCALE, validated with cupstestppd. Touches nothing
# outside $1's directory (a fresh temp dir).
generate_ppd() {
  local out=$1 check
  say "generating the PPD for feed scale $FEED_SCALE (Resolution $RES_CHOICE)"
  run make -s -C "$CUPS_SRC" ppd FEED_SCALE="$FEED_SCALE" PPD_OUT="$out"
  grep -qx "\*DefaultResolution: $RES_CHOICE" "$out" ||
    die "generated PPD does not default to $RES_CHOICE"
  command -v cupstestppd >/dev/null 2>&1 || die "cupstestppd not found; can't validate the PPD"
  # -I filters: the filter is checked separately (lipo) and installed below.
  if ! check=$(cupstestppd -W translations -I filters "$out" 2>&1); then
    echo "$check" >&2
    die "cupstestppd rejected the generated PPD; nothing was installed"
  fi
  say "cupstestppd: PASS"
}

install_queue() {
  local tmp ppd_gen
  [[ -f $PPD_SRC ]] || die "missing $PPD_SRC"
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/munbyn-ppd.XXXXXX")
  # shellcheck disable=SC2064  # expand $tmp now
  trap "rm -rf '$tmp'" EXIT
  ppd_gen=$tmp/munbyn-rw403b-native.ppd
  generate_ppd "$ppd_gen"
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
  run install -o root -g wheel -m 0644 "$ppd_gen" "$PPD_DST"

  local uri
  uri=$(find_uri)
  if queue_exists; then
    say "$QUEUE already exists; updating it in place"
  fi
  # lpadmin warns that PPD drivers are deprecated; that warning is expected.
  # -P replaces the queue's PPD, so re-running with a new --feed-scale takes
  # effect (the queue's option defaults go back to the PPD's; the check below
  # confirms the Resolution).
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
  local queue_ppd=/etc/cups/ppd/$QUEUE.ppd queue_res
  queue_res=$(sed -n 's/^\*DefaultResolution: *//p' "$queue_ppd" 2>/dev/null | head -n 1 || true)
  if [[ $queue_res == "$RES_CHOICE" ]]; then
    say "queue default Resolution: $queue_res (feed scale $FEED_SCALE)"
  else
    say "WARNING: $queue_ppd defaults to Resolution '${queue_res:-?}', expected $RES_CHOICE;" \
      "set it with: lpadmin -p $QUEUE -o Resolution=$RES_CHOICE"
  fi
  cat <<EOF

Installed. In Preview: File > Print, Printer "$QUEUE_DESC",
set Paper Size (4.00x6.00" etc.) and Scale there; label options (darkness,
speed, media type, offsets, threshold) are under "Printer Features".
Feed correction: Resolution $RES_CHOICE (feed scale $FEED_SCALE), so Scale
100% prints at true length. To change it: sudo $0 --feed-scale <F>
(see --help for how to measure F).
Munbyn's original Munbyn_RW403B queue is untouched.
Undo: sudo $0 --uninstall
EOF
}

if [[ $MODE == uninstall ]]; then
  uninstall
else
  install_queue
fi
