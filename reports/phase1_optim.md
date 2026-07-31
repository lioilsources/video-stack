# Fáze 1 — Optimalizace runtime (2026-07-31)

## Provedeno

1. **SageAttention 2.2.0** zkompilován ze zdroje v ComfyUI venv.
   - upstream setup.py má nativní podporu sm_121 (GB10) — žádné patche
   - python3.12-dev byl už na stroji, tichý fallback bug nehrozil
   - build ~35 min na 20 jádrech (EXT_PARALLEL=4, NVCC --threads 8, MAX_JOBS=8)
   - ověřeno: import, `from sageattention import sageattn`, log "Using sage attention"
2. **run.sh** rozšířen (commit 3970d79e v ComfyUI-Custom-SPARK):
   - `--use-sage-attention` (kill switch `SAGE=0 ./run.sh`)
   - `--cache-none` (unified memory: žádné dvojité držení modelů)
   - `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
   - RESERVE_VRAM mechanismus zachován, default 8 GB (vědomá odchylka od
     plánu, který chtěl 4 — koexistence s vLLM má přednost, env-overridable)
   - run.sh + comfyui.service nově trackované v ComfyUI-Custom-SPARK
     (dřív jen netrackované na disku)
3. **Custom nodes**: ComfyUI-WanVideoWrapper, ComfyUI-KJNodes, ComfyUI-MMAudio
   naklonovány + requirements do venv. (VideoHelperSuite a Frame-Interpolation
   už byly.)
4. **Smoke test inference se sage**: SDXL Lightning 4 kroky, 768×768 — 19 s
   včetně načtení checkpointu, výstup vizuálně v pořádku (žádný černý frame,
   mean 101/255), žádné chyby v logu.

## Power-spike mitigace — vyřešeno jinak než v plánu

- `nvidia-smi -pl` na GB10 neexistuje (všechna POWER pole N/A).
- Journal přes všechny boot cykly: ŽÁDNÝ hard crash v historii stroje —
  všechny reboot cykly byly čisté shutdowny. Riziko je zatím teoretické.
- Rozhodnutí: clock cap plošně NEnasazovat. Připravená mitigace pro případ
  prvního pádu: `sudo nvidia-smi -lgc 0,2600` (max clock 3003 MHz).
- Firmware 5.36_0ACUM023 — při případném pádu zkontrolovat novější.

## Otevřené položky

- **ComfyUI běží dočasně mimo systemd** (manual-run.log, setsid). Start
  služby vyžaduje sudo heslo, které agent nemá; uživatel neměl terminál.
  Reconciliace až bude terminál:
  `pkill -f "python main.py" && sudo systemctl start comfyui.service`
  Doporučení: NOPASSWD pravidlo pro systemctl {start,stop,restart} comfyui.
- Baseline benchmark stále čeká na Wan modely (fáze 2 Tier B).
- Pre-existing: ComfyUI-RMBG SAM3Segment nejde načíst (libgobject, ARM64) —
  bylo už před fází 1, netýká se video stacku.

## Poznámka k zrychlení

Plánové "až 20×" se týká video difuze (Wan DiT) — SDXL smoke test je jen
funkční ověření. Reálné číslo dá až baseline po fázi 2.
