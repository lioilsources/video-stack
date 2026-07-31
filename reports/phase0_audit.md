# Fáze 0 — Audit prostředí (2026-07-31)

Read-only audit podle VIDEO_PLAN.md. Nic nebylo instalováno ani měněno.

## Hardware / systém

| | |
|---|---|
| Stroj | spark-99bb, NVIDIA GB10, ARM64 (aarch64) |
| Kernel | 6.17.0-1026-nvidia, Ubuntu, uptime 12 dní |
| Driver / CUDA | 595.71.05, CUDA 13.0 (nvcc v /usr/local/cuda-13.0/bin, NENÍ v PATH) |
| Python | 3.12.3 systémový; ComfyUI venv v ~/Code/ComfyUI/.venv |
| torch | 2.11.0+cu130 |
| RAM (unified) | 121 GB total, 68 GB obsazeno, ~53 GB available |
| Disk | 3.7 TB NVMe, 1.2 TB volných — na ~150 GB modelů v pohodě |

## Klíčová zjištění

### 1. SageAttention NENÍ nainstalovaný (největší ROI z plánu potvrzeno)
- `import sageattention` → ModuleNotFoundError
- journal běžící instance: `Using pytorch attention`, `[ATTENTION] Using backend: sdpa`
- `python3.12-dev` UŽ nainstalovaný je (3.12.3-1ubuntu0.15) — známý tichý bug
  kompilace tedy nehrozí, prerekvizita fáze 1 splněna.
- Test z plánu (spustit s --use-sage-attention) nebyl proveden — vyžadoval by
  restart služby a je zbytečný: modul neexistuje, výsledek je jistý.

### 2. ComfyUI
- ~/Code/ComfyUI, git v0.19.3 (90fb34eb), frontend 1.42.11 — aktuální.
- systemd `comfyui.service` → `run.sh` → flagy:
  `--listen --reserve-vram 8 --disable-async-offload --disable-pinned-memory`
- Chybí `--use-sage-attention` i `--cache-none` (plán fáze 1).
- run.sh má promyšlený RESERVE_VRAM mechanismus kvůli koexistenci s vLLM —
  fáze 1 musí tento vzor zachovat, ne přepsat.
- POZOR: běží AnimateDiff práce (watcher na mm_sdxl_v10_beta.ckpt) — službu
  nerestartovat bez domluvy.

### 3. Power limit přes nvidia-smi NEFUNGUJE (odchylka od plánu)
- `nvidia-smi -q -d POWER` → všechna pole N/A (GB10 je neexponuje).
- `nvidia-smi -pl` tedy není použitelná mitigace power-spike crashů.
- Alternativy k prověření ve fázi 1: firmware update, nvidia-smi boost-slider
  / clock capping (`-lgc`), případně playbooky v ~/Code/dgx-spark-playbooks.

### 4. Baseline benchmark NEPROVEDEN — není čím
- V models/ není žádný Wan model (diffusion_models obsahuje jen FLUX kontext).
- Baseline (TI2V-5B, 480p, 33 frames) se přesouvá za download Tier B ve fázi 2.

### 5. Koexistence s vLLM (paměťový rozpočet pro video)
Unified paměť aktuálně drží:
| Proces | Paměť |
|---|---|
| vllm serve nvidia/Qwen3-32B-FP4 (shop) | ~29.6 GB |
| mpi4py.futures.server (běží 2+ dny, účel nejasný) | ~26 GB |
| vllm serve BAAI/bge-m3 (shop-embed) | ~1.5 GB |
| vllm serve LFM2.5-350M (fallback) | ~3 GB |
| volné | ~53 GB |

Wan 2.2 A14B FP8 (oba experti ~30 GB) + umt5-xxl + VAE se do 53 GB vejde
těsně; plánovaná integrace (fáze 5: uvolňovat LLM před video jobem) je
opodstatněná. Ten 26GB mpi4py server stojí za prověření — pokud je to
pozůstatek, jeho ukončení uvolní čtvrtinu potřebné paměti.

### 6. Custom nodes — co už je a co chybí
| Plán | Stav |
|---|---|
| ComfyUI-VideoHelperSuite | ✓ nainstalováno |
| ComfyUI-Frame-Interpolation (RIFE) | ✓ nainstalováno |
| ComfyUI-WanVideoWrapper (Kijai) | ✗ chybí |
| ComfyUI-KJNodes | ✗ chybí |
| ComfyUI-MMAudio | ✗ chybí (fáze 4) |

## Doporučené pořadí pro fázi 1 (po schválení)
1. SageAttention kompilace (prerekvizity splněny; CUDA 13 + torch 2.11 — ověřit
   podporovanou kombinaci, případně sageattention 2.x).
2. run.sh: přidat --use-sage-attention (+ zvážit --cache-none; PYTORCH_CUDA_ALLOC_CONF).
3. Power-spike mitigace: prověřit firmware + lgc místo nefunkčního -pl.
4. WanVideoWrapper + KJNodes.
5. Re-běh baseline až po fázi 2 Tier B.
