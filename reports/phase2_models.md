# Fáze 2 — Modely + baseline (2026-08-01)

## Staženo (86 GiB, ~4 h při ~6 MB/s)

Tier A: Wan 2.2 I2V+T2V A14B fp8_scaled (4× 13.3 GB, high+low noise experti),
Lightning 4-step LoRA (I2V Seko-V1, T2V Seko-V1.1, high+low), umt5-xxl fp8,
VAE 2.1 i 2.2. Tier B: TI2V-5B fp16 (9.3 GB). Vše z Comfy-Org repacku +
lightx2v, integrita ověřena hf klientem, velikosti v models_manifest.json.
Tier C (LTX) a D (VACE, Fun Camera) záměrně odloženy dle plánu.

## Baseline benchmark — TI2V-5B (nativní Comfy nody, sage attention)

| Parametr | Hodnota |
|---|---|
| Workflow | UNETLoader → ModelSamplingSD3(shift 8) → KSampler → VAEDecode |
| Rozlišení / délka | 832×480, 33 frames (16 fps → ~2 s) |
| Kroky / CFG | 30 / 5.0 (euler, simple) |
| **Čas celkem** | **166 s vč. načtení modelů (Prompt executed: 164.3 s)** |
| Peak unified RAM | 88.5 GB (baseline 70.9 GB → +17.6 GB) |
| Výstup | validní video s pohybem, žádný černý frame |

Kontext: plán očekával 15–25 min bez optimalizací; se SageAttention + fp16 5B
jsme na ~2.7 min. Pozn.: --cache-none znamená, že každý běh načítá modely
znovu (~30 s z toho času).

## Další krok (fáze 3)

Workflow knihovna: t2v_draft_5b (hotový v podstatě = benchmark workflow),
t2v/i2v_final_14b_lightning (4 kroky, cfg 1), flf2v_chain, upscale_interp.
Benchmark matice {480p,720p} × {33,81}.
