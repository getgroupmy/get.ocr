#!/bin/bash
# Make the Mac back the database up by itself every night.
#   ./scripts/install_backup.sh                      into ./backups, at 02:15
#   ./scripts/install_backup.sh /Volumes/NAS/ocr     into a folder you name (another disk is better)
#   ./scripts/install_backup.sh --uninstall
# It runs scripts/backup.sh as you, whether or not anyone is logged in, and writes backup.log.
# Asks for your Mac password once (sudo). The archives are useless without OCR_STORAGE_KEY from
# .env: keep a copy of that key in a password manager, never beside the backups.
set -euo pipefail

[ "$(uname)" = Darwin ] || { echo "This is for macOS (the Mac mini). On Linux use cron: see the README."; exit 1; }
[ "$(id -u)" != 0 ] || { echo "Run it as yourself, not with sudo: it asks for the password itself."; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ME="$(id -un)"
JOB=my.getride.ocr.backup
DIR=/Library/LaunchDaemons
DATA="$({ grep -E '^OCR_DATA_DIR=' "$REPO/.env" 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"; DATA="${DATA:-./data}"
case "$DATA" in /*) ;; *) DATA="$REPO/${DATA#./}" ;; esac

stop() {
  sudo launchctl bootout "system/$JOB" 2>/dev/null || true
  for _ in $(seq 1 20); do sudo launchctl print "system/$JOB" >/dev/null 2>&1 || break; sleep 0.5; done
  sudo rm -f "$DIR/$JOB.plist"
}

if [ "${1:-}" = "--uninstall" ]; then
  sudo -v; stop; rm -f "$DATA/backup_scheduled"
  echo "✓ The nightly backup is off. The archives already made are untouched."
  exit 0
fi

DEST="${1:-$REPO/backups}"
case "$DEST" in /*) ;; *) DEST="$REPO/${DEST#./}" ;; esac
mkdir -p "$DEST" || { echo "Cannot create $DEST"; exit 1; }
[ -w "$DEST" ] || { echo "You cannot write to $DEST"; exit 1; }

echo "A first backup now, to see that it works…"
"$REPO/scripts/backup.sh" "$DEST"

echo "Asking for your Mac password to schedule it…"
sudo -v
TMP="$(mktemp)"
cat > "$TMP" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$JOB</string>
  <key>UserName</key><string>$ME</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>$REPO/scripts/backup.sh</string><string>$DEST</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>2</integer><key>Minute</key><integer>15</integer></dict>
  <key>StandardOutPath</key><string>$REPO/backup.log</string>
  <key>StandardErrorPath</key><string>$REPO/backup.log</string>
</dict></plist>
PLIST
plutil -lint "$TMP" >/dev/null
stop
sudo install -o root -g wheel -m 644 "$TMP" "$DIR/$JOB.plist"
rm -f "$TMP"
for _ in 1 2 3 4 5; do
  sudo launchctl bootstrap system "$DIR/$JOB.plist" 2>/dev/null && break
  sleep 2
done
sudo launchctl print "system/$JOB" >/dev/null 2>&1 || sudo launchctl bootstrap system "$DIR/$JOB.plist"  # shows the error
touch "$DATA/backup_scheduled"   # the service now warns (OCR_ALERT_WEBHOOK, /health) when a night is missed
echo "✓ Backups run every night at 02:15 into $DEST (archives older than 30 days are removed)."
echo "  Log: $REPO/backup.log. If the Mac is asleep at 02:15 it runs when it next wakes."
case "$DEST" in "$REPO"/*) echo "  • $DEST is on the same disk as the data: a folder on the NAS or another disk would survive this disk failing." ;; esac
