# Fáze 3 — Workflow knihovna + benchmark matice (2026-08-01)

Všech 12 kombinací prošlo bez selhání. Časy včetně načtení modelů z NVMe
(--cache-none ⇒ každý běh znovu, ~30–60 s). Peak = unified RAM celkem
(vLLM shop stack ~35 GB zůstal rezidentní po celou dobu; žádné OOM).

| Workflow | 832×480/33f | 832×480/81f | 1280×704/33f | 1280×704/81f |
|---|---|---|---|---|
| t2v_draft_5b (30 kroků, cfg 5) | 215 s | 336 s | 290 s | 711 s |
| t2v_final_14b_lightning (4 kroky, cfg 1) | **135 s** | **285 s** | 255 s | 780 s |
| i2v_final_14b_lightning (4 kroky, cfg 1) | 155 s | 290 s | 266 s | 806 s |

Peak paměti: 83.6–90.8 GB (ze 121 GB).

## Klíčová zjištění

1. **„Draft" 5B je pomalejší než „final" 14B Lightning.** 30 kroků 5B modelu
   stojí víc než 4 kroky 14B — Lightning destilace obrací intuici plánu.
   Důsledek: 14B Lightning je správný default pro VŠECHNO; 5B má smysl jen
   se sníženými kroky (~10–15) jako skutečný draft, jinak je k ničemu.
2. **Cíl plánu splněn pro 480p:** 5s klip (81f) za ~5 min. 720p/81f je
   ~13 min — použitelné, ale pro iterace doporučuji 480p → upscale_interp.
3. I2V ≈ T2V + ~15 s (VAE encode vstupního obrázku + clip embedding).
4. Kvalita 14B Lightning: konzistentní objekty, čitelný pohyb kamery,
   žádné černé framy — viz output/t2v_14b_lightning_*.webm.

## Workflow knihovna (workflows/, API formát)

t2v_draft_5b, t2v_final_14b_lightning, i2v_final_14b_lightning,
flf2v_chain (jeden FLF segment; multi-shot = orchestrace, fáze 5),
upscale_interp (lanczos 1.5× + RIFE 16→32 fps).
Kopie pro interaktivní použití: ComfyUI user/default/workflows/video-stack/
(frontend 1.42 importuje API formát s auto-layoutem).

## Otevřené

- camera_control.json čeká na Tier D checkpoint (Fun Camera) — dle plánu
  až po vyhodnocení; benchmarky ho ospravedlňují.
- Prompt enhancement přes AiStack :8080 — gateway žije (aliasy lab/dev/…);
  potřeba potvrdit, který alias je vision model pro I2V.
- RIFE checkpoint (rife47.pth) se stáhne při prvním použití upscale_interp.

## Dodatek — Tier D (2026-08-01 večer)

Wan 2.1 VACE z plánu nahrazen Wan 2.2 fun_* fp8_scaled z Comfy-Org repacku
(novější, poloviční velikost). Staženo: fun_control (26.6 GB), fun_camera
(28.6 GB), fun_vace (32.4 GB).

Nové workflow:
- v2v_control_14b — driving video přes Canny → Wan22FunControlToVideo;
  832×480/33f/20 kroků = 666 s. Ověřeno vizuálně: pohyb a kompozice kopírují
  control video 1:1, vzhled řídí prompt + ref_image.
- camera_control — Fun Camera, preset trajektorie (Static/Pan×4/Zoom×2/CW/ACW)
  přes WanCameraEmbedding. Test čeká na doběhnutí download.
