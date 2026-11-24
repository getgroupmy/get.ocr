#!/bin/bash
# Start Ollama when the Mac starts, before anyone logs in, so field extraction and the vision reader
# come back by themselves after a restart or a power cut. By default Ollama only starts at login.
#
#   ./scripts/ollama_at_boot.sh              install
#   ./scripts/ollama_at_boot.sh --uninstall  remove (back to starting at login)
#
# It runs `ollama serve` as you, not as root, and listens on this Mac only (127.0.0.1:11434).
# NOT YET RUN ON A MAC: it follows the same pattern as install_service.sh.
set -euo pipefail
[ "$(uname)" = Darwin ] || { echo "This is for macOS (the Mac mini)."; exit 1; }
[ "$(id -u)" != 0 ] || { echo "Run it as yourself, not with sudo: it asks for the password itself."; exit 1; }
APP=my.getride.ollama
PLIST=/Library/LaunchDaemons/$APP.plist
ME="$(id -un)"
BIN="$(command -v ollama || true)"

if [ "${1:-}" = "--uninstall" ]; then
  sudo launchctl bootout "system/$APP" 2>/dev/null || true
  sudo rm -f "$PLIST"
  echo "Removed. Start Ollama at login again with: brew services start ollama"
  exit 0
fi
[ -n "$BIN" ] || { echo "ollama is not installed (brew install ollama)."; exit 1; }

echo "Asking for your Mac password to install the startup service…"
sudo -v
# the login-time copy would fight this one for the port
brew services stop ollama >/dev/null 2>&1 || true
osascript -e 'quit app "Ollama"' >/dev/null 2>&1 || true

TMP="$(mktemp)"
cat > "$TMP" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$APP</string>
  <key>UserName</key><string>$ME</string>
  <key>ProgramArguments</key><array><string>$BIN</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>$HOME</string>
    <key>OLLAMA_HOST</key><string>127.0.0.1:11434</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$HOME/ollama.log</string>
  <key>StandardErrorPath</key><string>$HOME/ollama.log</string>
</dict></plist>
PLIST
plutil -lint "$TMP" >/dev/null
sudo launchctl bootout "system/$APP" 2>/dev/null || true
sudo install -o root -g wheel -m 644 "$TMP" "$PLIST"
rm -f "$TMP"
sudo launchctl bootstrap system "$PLIST"

printf "Waiting for Ollama"
for _ in $(seq 1 20); do
  if curl -fs localhost:11434/api/tags >/dev/null; then
    echo; echo "✓ Ollama now starts with the Mac. Models installed:"; ollama list
    exit 0
  fi
  printf "."; sleep 2
done
echo; echo "✗ Ollama has not answered after 40 s. Last lines of $HOME/ollama.log:"; tail -20 "$HOME/ollama.log"; exit 1
