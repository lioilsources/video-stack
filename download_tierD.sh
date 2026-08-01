#!/usr/bin/env bash
set -euo pipefail
HF=~/Code/ComfyUI/.venv/bin/hf
M=~/Code/ComfyUI/models
REPACK=Comfy-Org/Wan_2.2_ComfyUI_Repackaged
for f in wan2.2_fun_control_high_noise_14B_fp8_scaled wan2.2_fun_control_low_noise_14B_fp8_scaled \
         wan2.2_fun_camera_high_noise_14B_fp8_scaled wan2.2_fun_camera_low_noise_14B_fp8_scaled \
         wan2.2_fun_vace_high_noise_14B_fp8_scaled wan2.2_fun_vace_low_noise_14B_fp8_scaled; do
  echo "=== $f ==="
  "$HF" download "$REPACK" "split_files/diffusion_models/$f.safetensors" --local-dir /tmp/hfdl >/dev/null
  mv "/tmp/hfdl/split_files/diffusion_models/$f.safetensors" "$M/diffusion_models/"
  echo "=== DONE $f ==="
done
rm -rf /tmp/hfdl
echo "=== TIER D COMPLETE ==="
