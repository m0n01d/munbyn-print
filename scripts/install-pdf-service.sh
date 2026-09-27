#!/usr/bin/env bash
# Builds "Print to Munbyn RW403B.app" -- a tiny AppleScript droplet that
# shells out to scripts/print-from-dialog.sh -- and links it into
# ~/Library/PDF Services so it shows up in the macOS print dialog's PDF
# dropdown.
#
# CAVEAT (see PLANS/PLAN.md): a first-hand report (Ventura, Dec 2022) found
# *executable scripts* placed directly in ~/Library/PDF Services broken
# since Big Sur -- the print dialog's host process is sandboxed and refuses
# to run scripts/binaries/Automator plugins there. Placing an *application*
# there (or a Finder alias / symlink to one, as this script does) is a
# different, still-working mechanism: the dialog hands the spooled PDF to
# the app via LaunchServices, the same as dropping a file onto it in the
# Finder -- it never tries to execute the PDF Services entry itself. No
# first-hand confirmation of this exists yet for macOS 15/26 either --
# test it with a real Print dialog before relying on it.
#
# Usage:
#   scripts/install-pdf-service.sh                    # install to ~/Applications
#   scripts/install-pdf-service.sh --uninstall
#   scripts/install-pdf-service.sh --dest DIR          # app + PDF Services link both under DIR (testing)
#
# Idempotent: re-running rebuilds the same app bundle and re-links the same
# symlink.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="Print to Munbyn RW403B.app"
WRAPPER="$REPO_DIR/scripts/print-from-dialog.sh"

DEST_APPS="$HOME/Applications"
DEST_PDFSVC="$HOME/Library/PDF Services"

uninstall=0
dest_override=""
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall)
      uninstall=1
      ;;
    --dest)
      dest_override="${2:-}"
      if [ -z "$dest_override" ]; then
        echo "error: --dest requires a directory argument" >&2
        exit 1
      fi
      shift
      ;;
    *)
      echo "usage: $0 [--uninstall] [--dest DIR]" >&2
      exit 1
      ;;
  esac
  shift
done

if [ -n "$dest_override" ]; then
  # Resolve to an absolute path first: ln -s below writes whatever
  # $APP_PATH is verbatim, and a relative one resolves (wrongly) from the
  # link's own directory, not the caller's cwd, producing a dangling link.
  mkdir -p "$dest_override"
  dest_override="$(cd "$dest_override" && pwd)"
  # Mirror the real Applications/PDF-Services split as two subdirectories
  # under one throwaway root, so the app bundle and the symlink to it don't
  # collide on the same path (they'd otherwise share both dir and name).
  DEST_APPS="$dest_override/Applications"
  DEST_PDFSVC="$dest_override/PDF Services"
fi

APP_PATH="$DEST_APPS/$APP_NAME"
LINK_PATH="$DEST_PDFSVC/$APP_NAME"

if [ "$uninstall" = "1" ]; then
  removed=0
  if [ -e "$LINK_PATH" ] || [ -L "$LINK_PATH" ]; then
    rm -f "$LINK_PATH"
    echo "Removed: $LINK_PATH"
    removed=1
  fi
  if [ -e "$APP_PATH" ]; then
    rm -rf "$APP_PATH"
    echo "Removed: $APP_PATH"
    removed=1
  fi
  if [ "$removed" = "0" ]; then
    echo "Not installed ($APP_PATH not found)."
  fi
  exit 0
fi

mkdir -p "$DEST_APPS" "$DEST_PDFSVC"

TMP_DIR="$(mktemp -d -t munbyn-pdfsvc)"
trap 'rm -rf "$TMP_DIR"' EXIT
SCRIPT_SRC="$TMP_DIR/droplet.applescript"

# Escape backslash and double-quote before interpolating $WRAPPER into an
# AppleScript string literal below -- a repo checkout path containing either
# (e.g. a name with a literal quote) would otherwise break the compile.
WRAPPER_AS=${WRAPPER//\\/\\\\}
WRAPPER_AS=${WRAPPER_AS//\"/\\\"}

cat > "$SCRIPT_SRC" <<APPLESCRIPT
on open theFiles
	repeat with aFile in theFiles
		set posixPath to POSIX path of aFile
		try
			do shell script quoted form of "$WRAPPER_AS" & " " & quoted form of posixPath
		on error errText
			-- The wrapper (scripts/print-from-dialog.sh) already logs and
			-- shows its own notification on every failure it can detect; the
			-- generic message below is all "do shell script" gives back for
			-- any nonzero exit, so showing it too would just be a second,
			-- less useful notification for the same failure. Only show this
			-- one when the wrapper couldn't even run (a different, more
			-- specific errText -- e.g. a missing file).
			if errText is not "The command exited with a non-zero status." then
				display notification errText with title "Munbyn RW403B" subtitle "Print failed"
			end if
		end try
	end repeat
end open

on run
	display dialog "Drop a PDF (or any file) onto this app to print it to the Munbyn RW403B." & return & return & "Or, in the macOS Print dialog, choose \"${APP_NAME%.app}\" from the PDF dropdown (bottom-left) to print whatever's showing there." with title "Print to Munbyn RW403B" buttons {"OK"} default button "OK"
end run
APPLESCRIPT

# Compile to a temp path first: only replace a working installed app once
# the compile has actually succeeded, so a bad re-run (e.g. a compile
# failure) can't delete a working app and leave the PDF Services symlink
# dangling.
TMP_APP="$TMP_DIR/$APP_NAME"
osacompile -o "$TMP_APP" "$SCRIPT_SRC"
rm -rf "$APP_PATH"
mv "$TMP_APP" "$APP_PATH"

ln -sfn "$APP_PATH" "$LINK_PATH"

echo "Installed: $APP_PATH"
echo "Linked into: $LINK_PATH"
echo "It should appear in the macOS print dialog's PDF dropdown as \"${APP_NAME%.app}\"."
echo
echo "Caveat: this replaces the (reportedly broken since Big Sur) executable-script"
echo "PDF Service mechanism with an application instead -- test with a real Print"
echo "dialog before relying on it. See PLANS/PLAN.md and the README."
