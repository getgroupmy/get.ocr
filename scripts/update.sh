#!/bin/bash
# Update get.ocr on the Mac mini to the latest main and restart it.
#   ./scripts/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${OCR_PORT:-8090}"
git fetch origin main
git checkout main
git merge --ff-only origin/main
.venv/bin/pip install -q -r requirements.txt
./scripts/get_models.sh | grep -v "^Done" || echo "! the optional models could not be fetched; the service runs without them"
echo "Restarting (asks for your Mac password)…"
sudo launchctl kickstart -k system/my.getride.ocr
for _ in $(seq 1 30); do
  if curl -fs "localhost:$PORT/health" >/dev/null; then curl -s "localhost:$PORT/health"; echo; echo "✓ Updated to $(git log --oneline -1)"; exit 0; fi
  sleep 2
done
echo "✗ The server has not answered after 60 s. Last lines of server.log:"; tail -20 server.log; exit 1
