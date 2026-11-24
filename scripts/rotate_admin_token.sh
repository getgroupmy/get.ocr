#!/bin/bash
# Replace the admin token (the console's sign-in) with a new one and restart.
#   ./scripts/rotate_admin_token.sh
# API keys of the apps are not affected. The storage key is not touched.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env here"; exit 1; }
NEW="$(openssl rand -hex 32)"
cp .env ".env.before-rotation"
if grep -q '^OCR_ADMIN_TOKEN=' .env; then
  sed -i.tmp "s|^OCR_ADMIN_TOKEN=.*|OCR_ADMIN_TOKEN=$NEW|" .env && rm -f .env.tmp
else
  echo "OCR_ADMIN_TOKEN=$NEW" >> .env
fi
chmod 600 .env .env.before-rotation
echo "Restarting (asks for your Mac password)…"
sudo launchctl kickstart -k system/my.getride.ocr
echo "✓ New admin token is in .env (grep OCR_ADMIN_TOKEN .env). The old one no longer works."
echo "  The previous .env is in .env.before-rotation; delete it once you have signed in with the new token."
