#!/usr/bin/env bash
# Fáze 2 — Tier A + B: Wan 2.2 modely (Comfy-Org repack, fp8_scaled) + Lightning LoRA.
# Sekvenčně (bandwidth), hf má resume — skript lze kdykoli přerušit a pustit znovu.
set -euo pipefail
HF=~/Code/ComfyUI/.venv/bin/hf
M=~/Code/ComfyUI/models
REPACK=Comfy-Org/Wan_2.2_ComfyUI_Repackaged

dl() { # dl <repo> <soubor-v-repu> <cílový-adresář>
  echo "=== $2 ==="
  "$HF" download "$1" "$2" --local-dir /tmp/hfdl >/dev/null
  mkdir -p "$3"
  mv "/tmp/hfdl/$2" "$3/"
}

# text encoder + VAE (obě verze — 2.1 pro A14B, 2.2 pro TI2V-5B)
dl "$REPACK" split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors "$M/text_encoders"
dl "$REPACK" split_files/vae/wan_2.1_vae.safetensors "$M/vae"
dl "$REPACK" split_files/vae/wan2.2_vae.safetensors "$M/vae"

# Tier B — draft model (nejmenší, ať je co benchmarkovat co nejdřív)
dl "$REPACK" split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors "$M/diffusion_models"

# Tier A — I2V + T2V A14B fp8_scaled (high + low noise experti)
for f in wan2.2_i2v_high_noise_14B_fp8_scaled wan2.2_i2v_low_noise_14B_fp8_scaled \
         wan2.2_t2v_high_noise_14B_fp8_scaled wan2.2_t2v_low_noise_14B_fp8_scaled; do
  dl "$REPACK" "split_files/diffusion_models/$f.safetensors" "$M/diffusion_models"
done

# Lightning 4-step LoRA (lightx2v) — I2V i T2V, high+low noise
for v in Wan2.2-I2V-A14B-4steps-lora-rank64-Seko-V1 Wan2.2-T2V-A14B-4steps-lora-rank64-Seko-V1.1; do
  for n in high_noise_model low_noise_model; do
    echo "=== $v/$n ==="
    "$HF" download lightx2v/Wan2.2-Lightning "$v/$n.safetensors" --local-dir /tmp/hfdl >/dev/null
    mkdir -p "$M/loras"
    mv "/tmp/hfdl/$v/$n.safetensors" "$M/loras/${v}_${n}.safetensors"
  done
done

rm -rf /tmp/hfdl
echo "=== DOWNLOAD COMPLETE ==="
