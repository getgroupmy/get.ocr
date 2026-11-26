#!/bin/bash
# Install GET.ocr Vision, the second vision reader, into the model server on this machine.
# The model behind it is MiniCPM-OCR 1.0 by OpenBMB (Apache-2.0, https://github.com/OpenBMB/MiniCPM-OCR),
# used unchanged. About 2 GB is downloaded once; each file is checked against a known fingerprint.
#   ./scripts/get_vision_model.sh
# Needs Ollama 0.40.12 or later. Nothing is sent anywhere: the model runs here.
set -euo pipefail
cd "$(dirname "$0")/.."
NAME="$(grep -E '^OCR_VLM2_MODEL=' .env 2>/dev/null | tail -1 | cut -d= -f2-)"; NAME="${NAME:-get-ocr-vision}"
DATA="$(grep -E '^OCR_DATA_DIR=' .env 2>/dev/null | tail -1 | cut -d= -f2-)"; DATA="${DATA:-./data}"
DIR="$DATA/models/vision"
NEED=0.40.12

command -v ollama >/dev/null || { echo "Ollama is not installed on this machine (README, the model for field extraction)."; exit 1; }
HAVE="$(ollama --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[ -n "$HAVE" ] || { echo "Could not tell which Ollama this is ('ollama --version')."; exit 1; }
if [ "$(printf '%s\n%s\n' "$NEED" "$HAVE" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)" != "$NEED" ]; then
  echo "This is Ollama $HAVE; GET.ocr Vision needs $NEED or later."
  echo "Update Ollama (on a Mac: open the Ollama app and choose Update, or download it again from ollama.com), then run this again."
  exit 1
fi

mkdir -p "$DIR"
fetch() {  # name, url, sha256
  local out="$DIR/$1"
  if [ -f "$out" ] && [ "$(shasum -a 256 "$out" | cut -d' ' -f1)" = "$3" ]; then echo "✓ $1 is already there"; return; fi
  echo "Fetching $1…"
  curl -fL --retry 3 --progress-bar -o "$out.part" "$2"
  if [ "$(shasum -a 256 "$out.part" | cut -d' ' -f1)" != "$3" ]; then rm -f "$out.part"; echo "✗ $1: the download is not the expected file"; exit 1; fi
  mv "$out.part" "$out"; echo "✓ $1"
}
HF=https://huggingface.co/openbmb
fetch MiniCPM-OCR-1_0-Q8_0.gguf "$HF/MiniCPM-OCR-1.0-gguf/resolve/main/MiniCPM-OCR-1_0-Q8_0.gguf" \
  791486a476bdacb48acb3f31aa3a18dbb5be87e723a4074bcd13a37d61c852e2
fetch mmproj-model-f16-4x.gguf "$HF/MiniCPM-OCR-1.0-gguf/resolve/main/mmproj-model-f16-4x.gguf" \
  ceed82261b3b1380c3165f8b37c10117a50093be1362cbfeeb692fa036444682
fetch dspark-MiniCPM-OCR-1_0-Q8_0.gguf "$HF/MiniCPM-OCR-1.0-DSpark-gguf/resolve/main/dspark-MiniCPM-OCR-1_0-Q8_0.gguf" \
  fd8f64bb62cf5a5c6b4c0999bfa58f64453690c9f75f1124ab613dfb44a822da

# As the model's own Ollama guide gives it: the model, its vision part, and the small draft model that speeds it up.
cat > "$DIR/Modelfile" <<'MODELFILE'
FROM ./MiniCPM-OCR-1_0-Q8_0.gguf
FROM ./mmproj-model-f16-4x.gguf
DRAFT ./dspark-MiniCPM-OCR-1_0-Q8_0.gguf
REQUIRES 0.40.12
PARAMETER num_ctx 8192
PARAMETER temperature 0
PARAMETER draft_num_predict 15
MODELFILE
echo "Registering it with Ollama as '$NAME'…"
ollama create "$NAME" -f "$DIR/Modelfile"
ollama show "$NAME" | grep -qi vision || echo "• Ollama does not list 'vision' for $NAME: reading pictures may not work. Send this output to whoever maintains the service."
echo "✓ Done. /health shows \"vision\": true within half a minute; in the console choose the Reader 'GET.ocr Vision', or send engine=vision."
