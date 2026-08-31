# Fáze 5 — LTX-2.3 jako druhý engine

31. 8. 2026. Výzkum + offline příprava (F1 stažení, F2 workflow a chain.py)
proběhly bez běžícího ComfyUI. GPU A/B (F3) čeká, až se uvolní paměť.

## Proč LTX-2.3 (proti Wan 2.2)

| bolest dnes | co dává LTX-2.3 |
|---|---|
| tance bez hudby | **video + synchronní zvuk jedním průchodem** |
| 5s beaty → střihy, identita se láme na handoffech | klipy do ~19 s (481 sn @ 25 fps) = minutové video ze 3 klipů místo 12 |
| telefonní fotky mimo trénované pásmo Wan | nativní vertikála až 1080×1920 |
| moonwalk z textu nejde | IC-LoRA Union Control (pose/depth/canny) |
| GB10 Blackwell | fp8/nvfp4 optimalizace přímo pro tuhle architekturu |

Neznámé (proto A/B, ne rovnou integrace): identita v I2V vs Wan, kvalita
tance, reálná rychlost 22B na GB10. Existuje už LTX-2.5 — chce ale novější
ComfyUI; verdikt F3 může znít „počkat na 2.5 + upgrade".

## F1 — váhy (40,6 GB, `tools/get_ltx.sh`)

Zdroje dle [docs.comfy.org](https://docs.comfy.org/tutorials/video/ltx/ltx-2-3);
**staženo a ověřeno 31. 8. 2026** (velikosti bajt po bajtu, žádné .part):

| soubor | kam | B |
|---|---|---|
| ltx-2.3-22b-dev-fp8.safetensors (Lightricks/LTX-2.3-fp8) | checkpoints/ | 29 145 431 166 |
| ltx_2.3_22b_distilled_1.1_lora…bf16 (Comfy-Org/ltx-2.3) | loras/ | 2 741 024 390 |
| gemma-3-12b-it-abliterated_lora_rank64_bf16 (Comfy-Org/ltx-2) | loras/ | 628 203 616 |
| gemma_3_12B_it_fp4_mixed (Comfy-Org/ltx-2) | text_encoders/ | 9 447 702 218 |
| ltx-2.3-spatial-upscaler-x2-1.1 (Lightricks/LTX-2.3) | latent_upscale_models/ | 995 743 560 |
| ltx-2.3-22b-ic-lora-union-control-ref0.5 (Lightricks) | loras/ | 654 465 352 |

Vynecháno (stáhnout až bude potřeba): distilled-fp8 checkpoint (27 GB — dev +
distilled LoRA je totéž), temporal upscaler, MoGe (depth z RGB — kostry máme
vlastní), ID-LoRA talkvid, motion-track/HDR/lipdub IC-LoRA.

## F2 — zapojení (offline, ověřeno proti zdrojákům 0.19.3)

ComfyUI 0.19.3 na SPARKu má LTX-2 nativně: `nodes_lt.py` (I2V, guide, AV
latenty), `nodes_lt_audio.py` (audio VAE + Gemma loader), chunked VAE.
Kanonické zapojení převzato z oficiální šablony (subgraf
`template_ltx2_3_ic_lora_ingredients.json`): KSampler **euler_ancestral +
linear_quadratic, 8 kroků, cfg 1.0** (distilled), `LTXVConcatAVLatent` →
sampler → `LTXVSeparateAVLatent` → VAEDecodeTiled + `LTXVAudioVAEDecode`.

- `workflows/ltx_i2v_portrait.json` — I2V + zvuk. Stejná ID nodů jako Wan báze
  (8/9 prompty, 10 vstup, 12 I2V, 13 sampler, 15 decode, 16 webm), takže
  handoff/colormatch/face-refresh mašinérie v `build()` je sdílená. Navíc
  62–69: audio větev + `seg<id>_av.mp4` se zvukem (CreateVideo/SaveVideo).
- `workflows/ltx_control_portrait.json` — + union-control LoRA a kostra přes
  core `LoadVideo` → `GetVideoComponents` → `LTXVAddGuide`; po samplingu
  `LTXVCropGuides`. **Experiment** — viz omezení.
- `chain.py`: kořenový `"engine": "ltx"` (default `wan`). LTX: fps 25,
  length 8n+1 ≤ 481 (default 121), rozměry násobky 32, seed do KSampler,
  audio větev drží délku a tempo s videem; Wan knoby motion/boundary/shift
  se ignorují s hláškou; `--smooth` přeskakuje RIFE (25 fps nativně).

Offline validace: Wan cesta bit-shodná s HEAD (materialize diff prázdný);
LTX beaty materializují, všechny class_type existují v 0.19.3, dosazení
(seed, délky, rozměry, kostra) zkontrolováno strojově.

### Známá omezení (vyřeší F3/F4 nebo upgrade ComfyUI)

1. **IC-LoRA bez parametrů reference**: 0.19.3 nemá `GetICLoRAParameters`
   (master ComfyUI ho má, `LTXVAddGuide` tam bere ICLoRAParameters vstup).
   Union-control LoRA je ref0.5 — reference se u nás enkóduje s downscale 1,
   což podle model card degraduje kontrolu. Control beat je proto experiment;
   čistá cesta = upgrade ComfyUI (a pak rovnou zvážit 2.5). Pozn.: na SPARKu
   jsou nad 0.19.3 lokální patche (unpin_weight, cache-lru) — upgrade =
   rebase.
2. **Kostry mají 16 fps**: `drive/*_pose.webm` vznikly pro Wan (16 fps, 81
   snímků). LTX čte snímky 1:1 při 25 fps → pohyb ~1.56× rychlejší a na 73+
   snímků nemusí stačit délka. Pro ostré použití přegenerovat
   `tools/drive.py pose` s upraveným `--speed`.
3. **Zvuk zatím jen v `seg*_av.mp4`**: montáž (`concat`) pořád skládá webm
   bez zvuku + tichou AAC. Lepení audia přes střihy (xfade i pro audio) je
   druhá iterace — první klipy jsou single-scene, tam stačí `_av.mp4`.
4. Gemma abliterated LoRA se aplikuje jen na text encoder
   (strength_model 0) — soubor je LoRA text encoderu, model side je no-op.

## Paměťová past při načítání LTX (naměřeno 31. 8., opraveno)

První dva pokusy o smoke skončily v lowvram: ComfyUI zalogoval
`loaded partially … 10816 MB offloaded, lowvram patches: 1368`, podruhé
u Gemma encoderu `2033 MB usable, 0.00 MB loaded, 11200 MB offloaded`.

Příčina není v LLM kontejnerech (dohromady drží 0,7 GB) — je to unified
paměť: **27GB checkpoint se při čtení uloží dvakrát**, jednou do page cache
a jednou do vah. 27 + 27 = 54 GB proti 52 GB volným, takže ComfyUI si vah
nenačte ani polovinu. Naměřeno `vram_free` během načítání: 67 → 9 GB.

Jednorázové `drop_page_cache()` před promptem (dosavadní stav) nestačí —
načítání trvá jednotky sekund a cache naroste až po něm. `submit()` proto
pouští cache **i během běhu promptu** ve vlákně na pozadí
(`CACHE_EVERY`, default 15 s, přes `CHAIN_CACHE_EVERY` laditelné). Při 3 s
se `vram_free` drží kolem 20–35 GB a model se načte `loaded completely`.

Pozor: agresivní interval (3 s) zdražuje čtení modelů i **cizím** jobům ve
frontě — pro měření ano, pro sdílený provoz nechat default.

## F3 — protokol A/B (až bude GPU volno, ~1 h)

Vstup: `bench_src.png` v ComfyUI `input/` (portrétní fotka),
`chains/bench_ltx.json` (2×249 sn belly dance, identity face, slices),
`chains/bench_ltx_ctl.json` (gangnam kostra, 73 sn).

1. Smoke: `./chain.py chains/bench_ltx.json --until 01` → čas/VRAM špička,
   zvuk v `seg01_av.mp4`. Cíl ≤ Wan ekvivalent (~300 s GPU na 10 s videa).
2. Identita: `tools/face_drift.py` na snímcích klipu vs fotka; srovnat
   s Wan beat 1 (~0.7+) a handoff beat 2 (≥0.7 s refresh).
3. Tanec z textu: pohyb/blur vs Wan belly dance; navíc moonwalk z textu
   (větší text encoder ho možná zná).
4. Control: bench_ltx_ctl — kopíruje pohyb kostry? vs VACE/fun_control.
5. Verdikt sem: co integrovat (minutové presety na LTX / control beaty /
   počkat na 2.5 + upgrade ComfyUI).

## Licence

LTX-2 Community License: do $10M ročního obratu zdarma, výstupy naše;
povinné **označení AI výstupu**; zákaz deepfake/impersonace. Pro presety OK.
