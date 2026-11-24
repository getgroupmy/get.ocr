#!/bin/bash
# Run get.ocr as a macOS startup service (Mac mini), without Docker.
#
#   ./scripts/install_service.sh              install / reinstall
#   ./scripts/install_service.sh --uninstall  remove (back to starting it by hand)
#
# It starts at boot, before anyone logs in, runs as you (not root), and is
# restarted by macOS if it ever stops. Asks for your Mac password once (sudo).
# Port: 8090 by default, so it sits beside get.ekyc (8080). Change with OCR_PORT=… in front.
set -euo pipefail

[ "$(uname)" = Darwin ] || { echo "This is for macOS (the Mac mini)."; exit 1; }
[ "$(id -u)" != 0 ] || { echo "Run it as yourself, not with sudo: it asks for the password itself."; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ME="$(id -un)"
APP=my.getride.ocr
DIR=/Library/LaunchDaemons
PORT="${OCR_PORT:-8090}"

stop() {
  sudo launchctl bootout "system/$APP" 2>/dev/null || true
  # bootout returns before the job is gone; bootstrapping too soon fails with error 5
  for _ in $(seq 1 20); do sudo launchctl print "system/$APP" >/dev/null 2>&1 || break; sleep 0.5; done
  sudo rm -f "$DIR/$APP.plist"
}

if [ "${1:-}" = "--uninstall" ]; then
  sudo -v
  stop
  echo "Removed. Start the server by hand again (see the README)."
  exit 0
fi

[ -x "$REPO/.venv/bin/uvicorn" ] || { echo "No $REPO/.venv/bin/uvicorn: set up the virtualenv first (README, Mac mini)."; exit 1; }
[ -f "$REPO/.env" ] || { echo "No $REPO/.env: create it first (README, Mac mini)."; exit 1; }
grep -q '^OCR_DATA_DIR=' "$REPO/.env" || { echo "Add OCR_DATA_DIR=./data to $REPO/.env first (README, Mac mini)."; exit 1; }

echo "Asking for your Mac password to install the startup service…"
sudo -v

TMP="$(mktemp)"
cat > "$TMP" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$APP</string>
  <key>UserName</key><string>$ME</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key><array>
    <string>$REPO/.venv/bin/uvicorn</string><string>app.main:app</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>$PORT</string>
    <string>--proxy-headers</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$REPO/server.log</string>
  <key>StandardErrorPath</key><string>$REPO/server.log</string>
</dict></plist>
PLIST
plutil -lint "$TMP" >/dev/null
stop
sudo install -o root -g wheel -m 644 "$TMP" "$DIR/$APP.plist"
rm -f "$TMP"
for _ in 1 2 3 4 5; do
  sudo launchctl bootstrap system "$DIR/$APP.plist" 2>/dev/null && break
  sleep 2
done
sudo launchctl print "system/$APP" >/dev/null 2>&1 || sudo launchctl bootstrap system "$DIR/$APP.plist"  # shows the error
echo "✓ get.ocr installed as a startup service ($APP) on port $PORT"

# A server should come back after a power cut and not go to sleep.
sudo pmset -a autorestart 1 sleep 0 >/dev/null && echo "✓ Mac set to restart after a power failure and never sleep (display sleep unchanged)"
if fdesetup isactive >/dev/null 2>&1; then
  echo "! FileVault is on: after a reboot the Mac waits for your password at the login screen before anything starts."
fi

printf "Waiting for the server"
for _ in $(seq 1 30); do
  if curl -fs "localhost:$PORT/health" >/dev/null; then
    echo; curl -s "localhost:$PORT/health"; echo
    echo "✓ Running. It now starts by itself at boot and restarts if it stops."
    exit 0
  fi
  printf "."; sleep 2
done
echo; echo "✗ The server has not answered after 60 s. Last lines of $REPO/server.log:"; tail -20 "$REPO/server.log"
exit 1
