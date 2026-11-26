#!/bin/bash
# Fetch the optional model files the service can use, into the data folder. Run once, and again
# after an update that names a new model. Each file is checked against a known fingerprint.
#   ./scripts/get_models.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="$({ grep -E '^OCR_DATA_DIR=' .env 2>/dev/null || true; } | tail -1 | cut -d= -f2-)"; DATA="${DATA:-./data}"
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
# Arabic, Tamil and Thai reading (PP-OCRv5, Apache-2.0, 8 MB each). Also fetched on first use.
REC="https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv5/rec"
fetch arabic_PP-OCRv5_rec_mobile.onnx "$REC/arabic_PP-OCRv5_rec_mobile.onnx" c1192e632d0baa9146ae5b756a0e635e3dc63c1733737ebfd1629e87144e9295
fetch ta_PP-OCRv5_rec_mobile.onnx "$REC/ta_PP-OCRv5_rec_mobile.onnx" a42448808b7dea87597336f12438935f40353f1949e8360acd9e06b4da21bfe1
fetch th_PP-OCRv5_rec_mobile.onnx "$REC/th_PP-OCRv5_rec_mobile.onnx" de541dd83161c241ff426f7ecfd602a0ba77d686cf3ab9a6c255ea82fd08006e
echo "Done. Restart the service to use them: sudo launchctl kickstart -k system/my.getride.ocr"
