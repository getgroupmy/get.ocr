#!/bin/bash
# Fetch the optional model files the service can use, into the data folder. Run once, and again
# after an update that names a new model. Each file is checked against a known fingerprint.
#   ./scripts/get_models.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="$(grep -E '^OCR_DATA_DIR=' .env 2>/dev/null | tail -1 | cut -d= -f2-)"; DATA="${DATA:-./data}"
mkdir -p "$DATA/models"
fetch() {  # name, url, sha256
  local out="$DATA/models/$1"
  if [ -f "$out" ] && [ "$(shasum -a 256 "$out" | cut -d' ' -f1)" = "$3" ]; then echo "✓ $1 is already there"; return; fi
  echo "Fetching $1…"
  curl -fL --progress-bar -o "$out.part" "$2"
  if [ "$(shasum -a 256 "$out.part" | cut -d' ' -f1)" != "$3" ]; then rm -f "$out.part"; echo "✗ $1: the download is not the expected file"; exit 1; fi
  mv "$out.part" "$out"; echo "✓ $1"
}
# Signature detection (YOLOS-tiny, Apache-2.0, 26 MB)
fetch signature.onnx "https://huggingface.co/nuetzliches/yolos-tiny-signature-detection-onnx/resolve/main/model.onnx" \
  d6d5bf2b9ddba9888e77846507a3da90bfd11b113a9556deaebaf132a42a5f7c
echo "Done. Restart the service to use them: sudo launchctl kickstart -k system/my.getride.ocr"
