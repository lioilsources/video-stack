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

## Referenční čísla Wan (naměřeno 31. 8., stejný zdroj)

Drift **uvnitř** prvního beatu (`bench_identity_face`, ArcFace proti
`leather_shorts_src.png`, 5 snímků rovnoměrně po klipu):

| snímek | 0 | 40 | 80 | 119 | 159 |
|---|---|---|---|---|---|
| podobnost | 0.901 | 0.783 | 0.760 | 0.551 | 0.518 |

průměr **0.703**, min 0.518. Tohle je to, co uživatel popisuje jako „první
beat sedí, dál to ujíždí" — identita se láme i uvnitř jednoho klipu, nejen
na střizích. Oživení tváře drží handoffy na ~0.63 (phase4), ale samotný
model během 5 s ztratí 0.38.

Rychlost Wan pro srovnání (phase4, draft 464×944): 105–185 s na 81 snímků
= 5 s videa, tedy **~35 s GPU na sekundu videa**.

## F3 — naměřeno (31. 8. 2026)

### Smoke: I2V + zvuk, 480×960, 249 snímků (10 s @ 25 fps)

| co | LTX-2.3 | Wan 2.2 (phase4) |
|---|---|---|
| GPU čas | **156 s** na 10 s videa = **15,6 s/s** | ~35 s/s |
| délka klipu | 249 snímků v kuse | max 81 (5 s) |
| zvuk | **AAC 48 kHz, 9,96 s, mean −23,5 dB** (reálná stopa) | žádný |
| paměť | model `loaded completely` (23,8 GB), minimum volno 6,5 GB | — |

**LTX je 2,2× rychlejší na sekundu videa** a klip je dvakrát delší než
maximum Wan. Zvuk se opravdu generuje — není to ticho.

Vizuálně (snímky 0/71/141/247): pohyb plynulý a čitelný, scéna, oblečení i
boty drží celý klip, žádné rozmazané končetiny, kterými trpí Wan na rychlém
pohybu. Kvalita obrazu je nad Wan draftem.

### Identita: LTX drží hůř než Wan

ArcFace proti zdrojové fotce, `face_drift --video`:

| čas v klipu | ~0 s | ~1,4 s | ~2,8 s | ~4,2 s | ~5,6 s | ~7 s | ~8,5 s | ~9,9 s |
|---|---|---|---|---|---|---|---|---|
| **LTX** (249 sn) | 0.851 | 0.630 | 0.350 | 0.270 | 0.382 | 0.287 | 0.374 | 0.310 |
| **Wan** (81 sn) | 0.901 | 0.783 (1,25 s) | 0.760 (2,5 s) | 0.551 (3,7 s) | 0.518 (5 s) | — | — | — |

průměr LTX **0.432** (min 0.270) proti Wan **0.703** (min 0.518).

Na společném úseku je rozdíl jednoznačný: ve 2,8 s má Wan 0.76, LTX 0.35.
LTX se pod hranici „jiný člověk" (0.4) dostane už kolem 3. sekundy, Wan tam
nedojde ani na konci pětisekundového klipu. Delší klip tedy sám o sobě
identitu nezachrání — jen posune problém z handoffů dovnitř klipu.

### Rozlišení: identitu i rychlost zlepší

Stejný prompt na 704×1408 (0,99 Mpx), 121 snímků = 4,84 s, model už v cache:

| | draft 480×960 | hd 704×1408 | Wan draft 464×944 |
|---|---|---|---|
| identita průměr | 0.432 | **0.540** | 0.703 |
| identita první sn. | 0.851 | **0.967** | 0.901 |
| identita ~5 s | 0.382 (5,6 s) | **0.489** (4,8 s) | 0.518 (5 s) |
| GPU čas | 98 s sampling + 58 s načtení | **90 s** | ~175 s |

Přepočteno na jednotku práce (Mpx × snímky / s): LTX **1,2–1,3**, Wan **0,20**
— tedy **~6× rychlejší**, a to na modelu, který má 22B parametrů proti 14B.
Vyšší rozlišení stojí skoro nic (2,15× pixelů = 1,2× čas), což je opak Wan,
kde `--hd` cenu zdvojnásobí.

**Pozor na metriku:** minimum 0.183 v hd běhu není drift — na tom snímku má
postava **ruku před obličejem**. ArcFace měří zakrytí, ne identitu. Bez toho
outlieru je průměr hd běhu **0.611**, tedy blízko Wan 0.703, při dvojnásobném
rozlišení. Vizuálně první snímek prakticky nerozeznatelný od předlohy, oblečení,
boty a scéna drží celý klip.

### Moonwalk z textu: neumí (stejně jako Wan)

`bench_ltx_mw` (121 sn, 70 s): postava se místo klouzání vzad **otočí zády ke
kameře** a přešlapuje. Žádná moonwalk technika. Větší text encoder (Gemma 12B)
tedy nepomohl — **kostry z `drive/` zůstávají pro ikonické tance nutné.**

### Control z kostry: nefunguje na 0.19.3

`bench_ltx_ctl` (gangnam kostra, 73 sn, 85 s) — výstup je **doslovná kopie
kostry**: barevný panáček na černém pozadí, jen první snímek je fotka.
Model kostru vzal jako obsah, ne jako řídicí signál.

Potvrzuje to omezení #1: bez `GetICLoRAParameters` (v 0.19.3 chybí,
v ComfyUI master je) dostane `LTXVAddGuide` referenci s downscale 1 místo
trénovaných 0.5 a IC-LoRA se nechytne. **Control beaty na LTX vyžadují
upgrade ComfyUI** — dokud k němu nedojde, ikonické tance musí zůstat na
Wan (VACE/fun_control), které fungují.

### Maximální délka: 19 s v kuse jede, ale identita se rozpadne

`bench_ltx_long` (481 sn = 19,2 s, 480×960): **215 s GPU**, tedy 11,2 s na
sekundu videa — nejlepší poměr ze všech běhů (načtení modelu se amortizuje).
Minimum volné paměti ale kleslo na **2,8 GB** — delší klip už je na hraně.

Identita: 0.906 → 0.201 (průměr 0.315, min 0.162), a **není to artefakt
měření** — od poloviny klipu je to vizuálně jiná žena, mění se i boty a
šperky. Scéna, oblečení a kvalita obrazu naopak drží celých 19 s.

Závěr: delší klip identitu **nezachrání, zhorší** — v 249snímkovém běhu je
ve 2,8 s ještě 0.350, v 481snímkovém už ve 2,7 s jen 0.234.

## Verdikt F3

| kritérium | LTX-2.3 | Wan 2.2 | vítěz |
|---|---|---|---|
| rychlost (Mpx·sn/s) | 1,0–1,3 | 0,20 | **LTX ~6×** |
| cena vyššího rozlišení | 2,15× px = 1,2× čas | ~2× čas | **LTX** |
| zvuk | v jednom průchodu | žádný | **LTX** |
| délka klipu | 481 sn (19 s) | 81 sn (5 s) | **LTX** |
| kvalita pohybu, ostrost | bez rozmazaných končetin | rozmazání při rychlém pohybu | **LTX** |
| identita (srovnatelný úsek) | 0.61 hd / 0.43 draft | **0.70** | **Wan** |
| identita v dlouhém klipu | 0.32 — jiná osoba | n/a (5 s max) | **Wan** |
| tanec z textu (moonwalk) | ne | ne | remíza |
| control z kostry | **nefunguje** (0.19.3) | funguje (VACE) | **Wan** |

**Doporučení: neintegrovat zatím jako náhradu Wan, ale nasadit vedle něj.**

1. **Kde LTX vyhrává hned:** krátké klipy (do ~5 s) na vyšším rozlišení,
   kde je potřeba zvuk a rychlost. Šestinásobná rychlost znamená, že hd
   render stojí míň než dnešní Wan draft.
2. **Co brání nasazení na minutová videa:** identita. Slib „19 s v kuse =
   míň seamů" se nekoná — drift se jen přesune z handoffů dovnitř klipu a
   je rychlejší. LTX by potřeboval stejnou berličku jako Wan (oživení tváře
   z originálu), ta se ale s 27GB checkpointem **nevejde do jednoho grafu**
   (FLUX+PuLID ~10 GB navíc → `loaded partially`). Řešení pro F4: pustit
   oživení jako **samostatný prompt** po LTX, ne ve stejném workflow.
3. **Control beaty nechat na Wan** (VACE funguje) do upgradu ComfyUI.
4. **Upgrade ComfyUI je pákový bod:** odemkl by `GetICLoRAParameters`
   (control), ID-LoRA na identitu a rovnou LTX-2.5. Do té doby je LTX-2.3
   u nás model pro krátké ozvučené klipy, ne pro dlouhé příběhy.

## F4 — nasazeno do appky (31. 8. 2026)

Dvě scény se zvukem, obě jednobeatové (podle verdiktu: krátké klipy, hd):
`ltx_dance_music` (121 sn, 4,8 s) a `ltx_belly_music` (249 sn, 10 s).

Co k tomu bylo potřeba v kódu:

- `segments()` u LTX bere `seg*_av_*.mp4` — němé webm by vygenerovanou hudbu
  zahodilo hned v montáži.
- `concat(with_audio=True)` mapuje zvuk ze vstupů: acrossfade u prolínačky,
  concat u tvrdého střihu; beze změny zůstává dolepené ticho pro Wan.
- LTX jede v **hd** (naměřeno: 2,15× pixelů = 1,2× čas a identita 0.43 → 0.61).
- `cmd_smooth` u LTX vrací hotový soubor místo druhého překódování.
- `serve.py`: `result_path` zná i `_full.mp4` (LTX nedělá RIFE), katalog
  respektuje volitelné `order` (nové scény nahoru) a hlásí `audio: true`.
- Odhad času pro LTX počítá zvlášť **načtení 27GB checkpointu (~70 s)** —
  u 5s klipu je to většina času (slibovaná 1 min proti reálným 2,9 min).

Ověřeno end-to-end přes appkové API: job `1ca84d92`, 173 s, výsledek
704×1408 h264 High/BT.709 + **AAC 48 kHz, −18,5 dB**, tedy zvuk projde až
na `/result`. Appka 1.14.0 scény se zvukem odliší štítkem, ale katalog je
serverový — objeví se i ve starší verzi.

### Oživení tváře u LTX funguje — a drží líp než Wan

`bench_ltx_refresh` (2 beaty × 185 sn, hd, `identity: face`): oživení běží
jako samostatný prompt, ComfyUI mezi ním a LTX vymění model.

| | beat | oživení | handoff sim |
|---|---|---|---|
| beat 01 | 175 s | 75 s | **0.767** |
| beat 02 | 215 s | — | **0.589** |

průměr **0.678** proti Wan `identity: face` **0.630** (phase4, 6 beatů).

Takže na **střizích** drží LTX identitu lépe než Wan — pravděpodobně proto,
že oživení pracuje na hd snímku. Uvnitř klipu drift zůstává (0.97 → 0.49 za
4,8 s), takže delší beaty = víc rozpadu mezi dvěma opravami. Minutový preset
je proto postavený na 8 beatech po 185 snímcích, ne na 3 × 481.

Cena: 75 s na handoff navíc (výměna 27GB LTX ↔ 12GB FLUX), tedy minutová
scéna ~30 min proti 26 min u Wan ekvivalentu — za to je v ní hudba.

## Hudební dárce (soundtrack) — nasazeno, GPU ověření PŘERUŠENO

31. 8. večer: vícebeatové scény měnily hudbu s každým střihem (každý beat =
samostatná generace) a LTX identita uvnitř 7,4s beatů driftovala. Řešení:

- kořenový klíč `soundtrack` — JEDEN 19s LTX AV klip jako dárce, stopa se
  zacyklí (acrossfade 1 s na švech) a namuxuje přes finální mp4
  (`-c:v copy`, video beze změny);
- minutovky čtyřmo na porovnání: `belly/dance_long_music` (Wan video,
  stabilní tvář) a `ltx_*_long_music` (LTX, beaty 121 sn = 12 oprav tváře).

**Ověřeno offline** (check_scenes, Wan materialize bit-shodný). **GPU bench
`bench_soundtrack` nedoběhl** — uprostřed běhu (22:00) se ComfyUI zastavilo
(uživatel vzal paměť pro Nemotron-3-Super-120B). Před spuštěním minutovek
z appky proto zbývá ověřit: souvislá stopa přes celé video, švy smyčky,
video stream po muxu netknutý, identita Wan ~0.63. Spustit až bude ComfyUI
zpět: `python3 chain.py chains/bench_soundtrack.json --all` (+ face_drift).

## Ztráta identity v dalších beatech — příčina a fix (1. 9. 2026)

Uživatel hlásil, že „identity driftují do nepoznání". A/B na **4 beatech**
(hd 704×1408, 121 sn) měřené `face_drift --video` přes celé klipy:

| beat | handoff + oživení tváře | `beat_ref: "original"` |
|---|---|---|
| 01 | 0.646 | 0.646 |
| 02 | 0.576 | 0.629 |
| 03 | 0.376 | **0.848** |
| 04 | **−0.007** | **0.531** |
| **průměr** | **0.398** | **0.664** |

**Příčina není drift rysů, ale ztráta orientace.** Beat 03 měl prompt „otočí
se od kamery a zpět" — postava se otočila a nevrátila. Navazovací snímek byl
tedy záda, oživení tváře na něm nemělo co dělat (ArcFace hlásí „tvář
nenalezena") a **každý další beat startoval zády** → 0.003 / −0.007. Řetěz
se nemá jak vzpamatovat, protože jediná vazba na originál byl obličej.

**Fix — `beat_ref: "original"`:** beat startuje z původní fotky, ne
z navazovacího snímku. Startovní podobnost je pak **0.97 u každého beatu**
(u handoffu klesá 0.97 → 0.84 → 0.70 → −0.01), drift se nekumuluje a jedna
zvrtnutá pozice nezničí zbytek videa. Stejný princip jako `control_ref:
"original"` u Wan VACE. Bonus: oživení tváře odpadá → beat 145 s místo
190–245 s + 75 s.

Cena: na střihu postava skočí do výchozí pózy — u tance na místě to
prolínačka `slices` schová. Nasazeno do obou LTX minutovek.

Vedlejší potvrzení: s `free_models()` už časy beatů nerostou
(245 → 190 → 190 s), zatímco 31. 8. bez něj šly 205 → 310 s.

## Co zbývá změřit (až bude čas)

- Oživení tváře jako samostatný prompt po LTX — sníží drift na použitelnou
  úroveň? To rozhodne, jestli LTX může na minutová videa.
- Nativní 1080×1920 (2 Mpx) — trend „vyšší rozlišení = lepší identita"
  naznačuje další zisk; cena podle měření ~1,2× za 2× pixelů.
- Po upgradu ComfyUI: control přes IC-LoRA, ID-LoRA na identitu, LTX-2.5.

## Licence

LTX-2 Community License: do $10M ročního obratu zdarma, výstupy naše;
povinné **označení AI výstupu**; zákaz deepfake/impersonace. Pro presety OK.
