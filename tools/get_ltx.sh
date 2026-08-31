#!/usr/bin/env bash
# get_ltx.sh — stáhne váhy LTX-2.3 do ComfyUI models/ (offline, ComfyUI nemusí běžet).
# Zdroje dle https://docs.comfy.org/tutorials/video/ltx/ltx-2-3
# Celkem ~40.6 GB. Resume: curl -C - přes .part soubory, opakované spuštění je bezpečné.
set -euo pipefail

COMFY="${COMFY_DIR:-$HOME/Code/ComfyUI}"
cd "$COMFY/models"

# dir|expected_bytes|url
MANIFEST="
checkpoints|29145431166|https://huggingface.co/Lightricks/LTX-2.3-fp8/resolve/main/ltx-2.3-22b-dev-fp8.safetensors
loras|2741024390|https://huggingface.co/Comfy-Org/ltx-2.3/resolve/main/split_files/loras/ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors
loras|628203616|https://huggingface.co/Comfy-Org/ltx-2/resolve/main/split_files/loras/gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors
text_encoders|9447702218|https://huggingface.co/Comfy-Org/ltx-2/resolve/main/split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors
latent_upscale_models|995743560|https://huggingface.co/Lightricks/LTX-2.3/resolve/main/ltx-2.3-spatial-upscaler-x2-1.1.safetensors
loras|654465352|https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Union-Control/resolve/main/ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors
"

fail=0
while IFS='|' read -r dir bytes url; do
  [ -z "$dir" ] && continue
  name=$(basename "$url")
  dest="$dir/$name"
  mkdir -p "$dir"
  have=$(stat -c %s "$dest" 2>/dev/null || echo 0)
  if [ "$have" = "$bytes" ]; then
    echo "OK (už máme): $dest"
    continue
  fi
  echo "== $dest ($bytes B) =="
  curl -L --fail --retry 5 --retry-delay 10 -C - -o "$dest.part" "$url"
  got=$(stat -c %s "$dest.part")
  if [ "$got" != "$bytes" ]; then
    echo "CHYBA: $dest.part má $got B, čekáno $bytes B" >&2
    fail=1
    continue
  fi
  mv "$dest.part" "$dest"
  echo "OK: $dest"
done <<< "$MANIFEST"

[ "$fail" = 0 ] && echo "HOTOVO: všechny LTX-2.3 váhy na místě." || { echo "NEKOMPLETNÍ — spusť znovu (resume)."; exit 1; }
