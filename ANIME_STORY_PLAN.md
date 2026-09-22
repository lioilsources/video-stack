# Anime příběhy na minutu — plán pro Opus (handoff)

Cíl: z anime postaviček (T2I v Image Studiu) dělat ~1min dětské příběhy
s vyprávěním a hudbou, bez klikání v ComfyUI. Deset příběhů je na konci.

Sepsáno 22. 9. 2026 po průzkumu repa a SPARKu (`ssh spark`). Čísla a názvy
nodů níž jsou ověřené, ne odhad. Kde je něco domněnka, je to napsané.

**Stav 22. 9. 2026: E1–E5 implementované, E0 (měření na GPU) neproběhlo** — viz §7.

## 0. Jak se ke službě dostat

- `comfyui.ol1n.com` a `llm.ol1n.com` jsou za **Cloudflare Access** — z CLI
  vrací 403 (HTML „Error ・ Cloudflare Access"). Nezkoušet obcházet.
- Všechno jde přes **`ssh spark`** (ssh config na Macu funguje, BatchMode ok).
  ComfyUI běží nativně na `spark:8188`, verze 0.19.3, PyTorch 2.11 cu130.
  Stroj: DGX Spark GB10, 128 GB unified, disk 3.7 T / 582 G volných.
- Pracovní nástroje z Macu: `bin/spark-video` (nahraje obrázek + manifest,
  spustí `chain.py --all`, stáhne výsledek). Na SPARKu: `~/Code/video-stack`
  = klon tohohle repa, `chain.py`, `tools/drive.py`, `serve.py` (port 8096).
- Job API appky (`serve.py`, přes cloudflared `llm.ol1n.com/v1/video/*`):
  `GET /scenes`, `POST /jobs {scene, image b64, seed?}`, `GET /jobs/<id>`,
  `GET /jobs/<id>/result`. Jeden worker, sériově. Pro příběhy zatím nepotřebné.

## 1. Co služba umí (ověřeno)

| | Wan 2.2 I2V 14B (default) | LTX-2.3 22B (`"engine":"ltx"`) |
|---|---|---|
| klip najednou | 81 sn = 5.06 s @ 16 fps, délka 4n+1 | až 481 sn = 19 s @ 25 fps, 8n+1 |
| zvuk | ne (MMAudio dodatečně) | ano, synchronní, jedním průchodem |
| rychlost | ~150 s/beat draft 0.44 Mpx, ~330 s hd 1 Mpx | ~6× rychlejší na jednotku práce |
| identita postavy | 0.70 (ArcFace, fotky) | 0.61 hd / 0.43 draft — mění člověka |
| control z kostry | funguje (VACE / fun_control) | **nefunguje** na 0.19.3 |
| kamera | `workflows/camera_presets/*` (fun_camera: pan/zoom/rotace) | — |

- **Delší stopáž = řetěz beatů**: poslední snímek beatu N je vstup N+1, PNG
  handoff + ColorMatchV2 proti originálu (`chain.py build()`).
- **Manifest** `chains/<name>.json`: jeden `source`, `scenes[]` → `beats[]`,
  knoby `motion/boundary/shift`, `transition: cut|fade|slices`, `crossfade`.
  Přechody dělá ffmpeg xfade v `cmd_assemble`, RIFE 16→32 fps v `cmd_smooth`.
- **Control beaty** (`"control": "<id>"`): kostra `drive/<id>_pose.webm`
  (OpenPose z DWPose/Sapiens2 nebo přímo z Mixamo FBX přes `tools/drive.py draw`).
  K dispozici 15 kostr: booty_hip_hop, chicken_dance, flair, gangnam(+lasso),
  macarena, maraschino_step, moonwalk, robot, rumba, salsa(+leather), samba,
  snake_hip_hop, twerk. Vzhled control beatu drží `control_ref: original`
  (VACE reference_image = původní obrázek), tj. každý taneční klip se vrací
  k vzhledu z keyframu — přesně to, co příběh potřebuje.
- **Identita**: `identity: "face"` = FaceDetailer na FLUX dev + PuLID
  s **InsightFace** detekcí (`chain.py FACE`, positive prompt „photorealistic").
  **Na anime tváři to selže** (InsightFace stylizované obličeje nedetekuje,
  PuLID je natrénovaný na fotky) — pro anime **nepoužívat**, viz E0.
  `beat_ref: "original"` (beat startuje z původního obrázku) funguje nezávisle
  na stylu.
- **Audio**: MMAudio foley (`workflows/audio_mmaudio.json`, ~6 min i s načtením),
  hudba ACE-Step přes `llm.ol1n.com/v1/audio/vibe/{samples,analyze,generate}`
  (AiStack), LTX `soundtrack:` dárce. **TTS/vypravěč v žádném repu není** — E3.
- **Wan 2.2 Animate 14B** (`wan2.2_animate_14B_fp8`, `ComfyUI-WanAnimatePreprocess`,
  `nodes_wan.py`) je stažený, ale bez workflow v repu — kandidát na tanec
  anime postavy z referenčního obrázku + řídicího videa. Rezerva, ne první volba.

### T2I pro keyframy (Ol1nLLM Image Studio, `assets/comfyui/*.api.json`)

Anime checkpointy na SPARKu: **Illustrious-XL v2.0, NoobAI-XL eps11,
Animagine XL 4.0, Hassaku XL Illustrious 3.4, WAI Illustrious, Pony V6,
AutismMix**. Presety `sdxl_txt2img / sdxl_img2img / sdxl_inpaint(_ref)`,
`flux_manga_txt2img/img2img` (FLUX dev + manga LoRA), `flux_hair_kontext`
(FLUX Kontext = editace se zachováním postavy). Pomůcky pro konzistenci:
IPAdapter plus / plus-face SDXL (ViT-H), IPAdapter FaceID plusv2, ControlNet
openpose-sdxl-xinsir a union-promax, style LoRA `style-anime-screencap`,
`MeMaXL_Flat_Anime_Style`, `Stabilizer_ILNAICK`. LoRA trénink postavy je
připravený v `FineTuneGallery` (kohya, výstup rovnou do `models/loras/`).
Stylová matice modelů: `Ol1nLLM/docs/style-matrix.md`.

## 2. Klíčové rozhodnutí: jak držet postavu přes 12 záběrů

Řetěz 12×5 s z jednoho obrázku identitu neudrží (baseline 0.15 na 6. beatu)
a u příběhu se stejně mění prostředí i akce. Proto **záběr = vlastní keyframe**:

1. **Character sheet** postavy (T2I, 1× na postavu): čelní + profil + celá
   postava, jednoduché barvy, bílé pozadí. Uložit do `stories/characters/<id>/`.
2. **Keyframe každého záběru** vzniká z character sheetu, ne z textu:
   - **A (první zkusit): FLUX Kontext** — „the same character, now sitting at a
     school desk, morning light, anime style". Kontext drží design postavy
     napříč editacemi bez tréninku; preset `flux_hair_kontext` ukazuje graf.
   - **B: Illustrious + IPAdapter plus (ref = sheet) + openpose ControlNet**
     (póza záběru). Víc kontroly nad kompozicí, o něco horší držení designu.
   - **C: LoRA postavy** (FineTuneGallery, 20–40 obrázků ze sheetu + variant A).
     Nejvěrnější, ~hodina práce na postavu; jen pro hrdiny, které se vrací.
   E0 změří A vs. B na 3 záběrech a rozhodne. Do té doby default A.
3. **Uvnitř záběru** (1–2 beaty) jede normální řetěz; drift za 5–10 s je
   snesitelný a další záběr ho stejně resetuje. Control beaty s
   `control_ref: original` se vrací ke keyframu samy.

Poměr stran **9:16** (Wan trénovaný na 480×832, katalog scén je portrét,
cílová platforma telefon). Keyframy 832×1216 → draft 592×864, hd 832×1216.

Prompty: keyframy booru-styl pro Illustrious (`masterpiece, best quality,
1girl, ...`), přirozený jazyk pro Kontext a Wan. Wan `style_tail` pro anime:
`", anime style, 2D animation, cel shading, clean lineart, flat colors,
static camera, one continuous movement"`; negative navíc `"3d, realistic,
photo, photorealistic"`. Beat končí pózou („then stops"), ne stavem — pravidla
z `bin/README.md` platí beze změny.

## 3. Formát příběhu

Nový soubor `stories/<id>.json` (katalog jako `scenes/`, ale s víc vrstvami):

```json
{
  "id": "lost_umbrella", "title": "Ztracený deštník", "aspect": "9:16",
  "style": "anime", "engine": "wan",
  "characters": {
    "mia": {"sheet": "characters/mia/sheet.png",
            "desc": "8-year-old girl, short black bob, yellow raincoat, red boots"},
    "cat": {"sheet": "characters/cat/sheet.png", "desc": "fat grey cat, green eyes"}
  },
  "world": "rainy small town, cobblestone street, warm shop windows, soft watercolor backgrounds",
  "music": "gentle ukulele lullaby, playful, 80 bpm",
  "shots": [
    {"id": "01", "chars": ["mia"], "keyframe": "mia at the window, looking at rain",
     "motion": "she presses her nose to the glass, breath fogs it, then she turns away",
     "beats": 1, "narration": "Mia se těšila na déšť. Jenže kde je její deštník?"},
    {"id": "07", "chars": ["mia", "cat"], "keyframe": "mia in the street, cat wearing the umbrella as a hat",
     "control": "chicken_dance", "beats": 2,
     "narration": "A kočka? Ta tancovala jako o závod."}
  ]
}
```

Vrstvy: `keyframe` (T2I prompt záběru, doplní se `characters[].desc` + `world`),
`motion` (Wan prompt beatu) nebo `control` (kostra místo textu), `camera`
(preset z `camera_presets`), `beats` (1–2, tj. 5–10 s), `narration` (CZ text
pro TTS a titulky). 12 záběrů ≈ 60 s; předposlední rozuzlení může mít 2 beaty.

Kompilace: `stories/<id>.json` → (a) keyframy `stories/<id>/kf_NN.png`,
(b) manifest `chains/story_<id>.json` kde **scéna = záběr** a každá scéna má
vlastní `source`, (c) `narration.json` s časy pro TTS a titulky.

## 4. Etapy pro Opus

Každá etapa má kritérium hotovo. Pořadí je závazné jen u E0 → E1.

### E0 — Smoke test anime na Wan (½ dne GPU)
1. Vygenerovat na SPARKu 1 keyframe (Illustrious, 832×1216) dívky v pláštěnce.
2. `chains/story_smoke.json`: beat 1 klidný (`motion` prompt, anime style_tail),
   beat 2 `control: chicken_dance, control_ref: original`. Render draft + hd.
3. Totéž s `engine: ltx` (1 beat, 121 sn) — jen pro srovnání vzhledu a zvuku.
4. Ověřit, že `identity: "face"` na anime **padá nebo kazí** (očekávám: FaceDetailer
   nenajde bbox → projde beze změny, nebo přemaluje na fotorealistickou tvář).
   Zapsat do `reports/phase6_anime.md`.
5. Konzistence keyframů: 3 záběry téže postavy metodou A (Kontext) a B
   (IPAdapter + openpose). Hodnotit okem na kontaktním archu + CLIP kosinus
   proti sheetu (`tools/face_drift.py` ArcFace tady **nepoužitelný**; přidat
   `--clip` režim se `sigclip_vision_patch14_384`, který na SPARKu je).
Hotovo: report s verdiktem A/B, hodnoty knobů (`shift`, `motion`, `boundary`)
pro anime, a rozhodnutí Wan vs. LTX pro klidné záběry.

### E1 — `chain.py`: scéna s vlastním `source` (1 den)
- `scenes[].source`: scéna startuje z vlastního obrázku místo handoffu;
  `orig_img` (ColorMatch ref, VACE reference, `beat_ref: original`) je
  obrázek **té scény**. Bez `source` chování beze změny (zpětná kompatibilita
  pro `scenes/*.json` a appku).
- `cmd_assemble`: mezi scénami s vlastním `source` **tvrdý střih nebo krátký
  fade** (12 sn), ne `slices`; uvnitř scény zůstává manifestní `transition`.
  Duplicitní první snímek se zahazuje jen při handoffu (`select=gte(n,1)`).
- `--plan` vypíše u scény, z čeho startuje; `--resume` otisk zahrne
  mtime/sha zdroje scény.
- `spark-video new <name> --story` nahraje všechny `stories/<id>/kf_*.png`
  (dnes nahrává jeden obrázek).
Hotovo: `story_smoke` se 3 scénami × 1 beat se slepí, `--from 02` přegeneruje
jen 2. scénu.

### E2 — `tools/story.py`: keyframy a kompilace (1–2 dny)
```
./tools/story.py sheet    <id> <char>     # character sheet (T2I, 4 pohledy)
./tools/story.py keyframes <id> [--shot 07] [--seed N]   # T2I záběrů z sheetu
./tools/story.py contact  <id>            # kontaktní arch 12 keyframů (PIL) k odsouhlasení
./tools/story.py compile  <id>            # → chains/story_<id>.json + narration.json
./tools/story.py render   <id> [--hd]     # = spark-video run story_<id>
```
- T2I jede přes stejné `chain.submit()` jako `drive.py t2v` (stejné čekání,
  drop_page_cache). Grafy: vzít `Ol1nLLM/assets/comfyui/sdxl_txt2img.api.json`
  a `flux_hair_kontext.api.json` jako vzor, uložit do `workflows/t2i_*.json`
  a projet `validate.py`.
- Keyframe prompt = `characters[c].desc` + `keyframe` + `world` + stylový blok;
  negativ pro Illustrious standardní. Seed = `seed + shot`.
- Kontaktní arch je **brána**: bez `stories/<id>/approved.json` compile odmítne.
  Přegenerovat jde po záběru (`--shot`).
Hotovo: `lost_umbrella` má 12 schválených keyframů a vyrenderovaný draft.

### E3 — Zvuk: vypravěč, hudba, titulky (1 den + instalace TTS)
- **TTS chybí.** Doporučení: **Kokoro-82M** (rychlé, CPU stačí, ale bez češtiny)
  nebo **Piper `cs_CZ-jirka-medium`** (čeština, kvalita „rozhlas", ~0 GPU).
  Pro české dětské příběhy začít Piperem, případně XTTS-v2 (čeština, klon
  hlasu, GPU) až bude vadit intonace. Nasadit jako malou službu na SPARKu
  (`deploy/tts.service`, port 8098) nebo jen CLI v `tools/`.
- Časování: vypravěč čte `narration` záběru, klip se **neprodlužuje** —
  text delší než záběr (≥ 1 beat = 5 s ≈ 12 slov česky) compile odmítne.
- Hudba: `POST /v1/audio/vibe/generate` z `music` promptu, délka ~65 s, nebo
  LTX dárce (`soundtrack:`) — první je hotová cesta v AiStacku.
- Mix v `cmd_assemble` (ffmpeg): hudba -14 dB pod vypravěčem, ducking přes
  `sidechaincompress`, SRT titulky z `narration.json` + `subtitles` filtr do
  varianty `_sub.mp4`. Výstup 9:16 a navíc 16:9 s rozmazaným pozadím (pad).
Hotovo: `lost_umbrella_32fps.mp4` má vypravěče, hudbu a titulky.

### E4 — Katalog 10 příběhů a dávkový běh (2 dny, hlavně GPU)
- Napsat `stories/*.json` pro deset příběhů níž (prompty EN, narration CZ).
- `tools/story.py batch --draft` přes noc; ráno kontaktní archy, oprava
  promptů, `--hd` jen u schválených. Odhad na příběh: keyframy ~3 min,
  draft 12 beatů ~30 min, hd ~65 min GPU. Deset příběhů hd ≈ 11 h.
- `tools/check_stories.py` po vzoru `check_scenes.py` (délky, prázdné
  prompty, narace delší než záběr, chybějící sheet).
Hotovo: 10× mp4 v `out/stories/`, report `reports/phase6_stories.md`
(co selhalo a proč: ruce, více postav v záběru, tanec v anime).

### E5 — Volitelně: `serve.py` endpoint `/v1/video/stories` (½ dne)
Vstup: `{story, characters: {mia: <b64 sheet>}}`, výstup mp4. Umožní
TsumikiBotu/appce dosadit vlastní postavičku do hotového příběhu. Až po E4.

## 5. Známé pasti

- **Dvě postavy v jednom záběru** Wan míchá (výměna oblečení, splynutí).
  Držet pomocníka v samostatných záběrech nebo v roli rekvizity (kočka,
  robot), dialog řešit střihem záběr/protizáběr.
- **Ruce a rekvizity** (deštník, tužka): keyframe musí rekvizitu mít v ruce
  už na obrázku; Wan ji z textu nevymyslí.
- **Kostry** jsou z lidských proporcí; na chibi postavu sedí špatně
  (`tools/rig_proportions.py` A/B z 3D modelu vyšel záporně, commit eb62f64).
  Tanec dávat postavám s normálními proporcemi, nebo kostru škálovat `zoom`.
- **Paměť**: FLUX Kontext (T2I) vedle Wan 14B se nevejde; `story.py keyframes`
  pouštět před renderem, ne prokládat (viz LTX poznámka v `chain.py`).
- **LTX na anime** neměřeno; identita 0.43 draft u fotek napovídá, že design
  postavy neudrží ani tady. Použít jen na ambientní záběry bez hrdiny (déšť
  na okno, město v noci) — tam je zvuk zadarmo výhoda.
- Cloudflare Access: nic z Macu přes veřejné URL; vše `ssh spark`.

## 6. Deset dětských příběhů (12 záběrů = ~60 s, hrdina + pomocník)

Struktura vždy: 2 záběry úvod, 7 zápletka, 3 rozuzlení. `[tanec: id]` =
control beat z existující kostry. Vypravěč jedna věta na záběr.

1. **Ztracený deštník** — Mia (pláštěnka) + tlustá kočka. Okno a déšť · Mia
   hledá pod postelí · ve skříni · v kuchyni · vyjde ven bez deštníku · kaluže ·
   kočka na plotě s deštníkem na hlavě · Mia se směje `[tanec: chicken_dance
   kočka]` · kočka deštník vrátí · obě pod deštníkem · duha · domů.
2. **Dráček, který neuměl foukat oheň** — Pip (malý zelený drak) + moudrá sova.
   Dračí škola · velcí draci chrlí oheň · Pip vyfoukne bubliny · smích ostatních
   · Pip smutný na skále · sova: „každý dar je jiný" · vesnice hoří?
   ne, jen táborák zhasl · bubliny odrazí jiskry · děti tleskají ·
   `[tanec: samba Pip]` · Pip spí s úsměvem.
3. **První den ve škole** — Tomík (batoh větší než on) + školní robot Bip.
   Před bránou · velká chodba · ztracený · Bip zabliká · vede ho · učebna ·
   židle vedle holčičky · sdílí svačinu · přestávka `[tanec: robot Bip]` ·
   zvonek · cesta domů s kamarádkou · pokoj, kreslí robota.
4. **Zahrádka na balkóně** — Lenka + motýl. Semínko v dlani · květináč ·
   déšť · slunce · nic neroste · Lenka čeká u okna · první lístek ·
   poupě · motýl přiletí · květ se otevře · motýl tancuje `[tanec: rumba
   Lenka]` · balkón plný květin.
5. **Noční strach** — Kuba + plyšový medvěd. Zhasnuto · stín na zdi ·
   Kuba pod peřinou · šustění · medvěd spadl z police · stín roste ·
   Kuba rozsvítí lampičku · je to bunda na židli · medvěd zpátky v posteli ·
   `[tanec: macarena medvěd]` · Kuba se směje · spí.
6. **Závod se šnekem** — zajíc Rychlík + šnek Pomalík. Startovní čára ·
   zajíc vystřelí · šnek leze · zajíc se nudí u stromu · usne · šnek najde
   lesklý kámen · šnek najde jahodu · šnek pomůže brouku · zajíc se vzbudí ·
   cíl: šnek s pokladem · zajíc gratuluje `[tanec: moonwalk zajíc]` · oba jedí
   jahodu.
7. **Ztracená rukavice** — Aneta (červené rukavice) + sněhulák. Sněžení ·
   koulovačka · jedna rukavice pryč · studená ruka · sněhulák staví · noc,
   sněhulák má rukavici · ráno Aneta hledá · vidí sněhuláka · sněhulák „mává"
   `[tanec: maraschino_step sněhulák]` · rukavice zpátky · Aneta mu dá šálu ·
   oba se smějí.
8. **Kouzelná tužka** — Ondra + nakreslený pes. Prázdný pokoj · tužka pod
   postelí · kreslí míč, míč skáče · kreslí dort · kreslí psa · pes ožije
   `[tanec: booty_hip_hop pes]` · pes rozhází pokoj · Ondra kreslí koš na
   hračky · uklizeno · kreslí kamarádku · ta ožije a mává · tři hrají
   s míčem.
9. **Ryba, která chtěla létat** — rybka Fin + labuť. Jezero zespodu · rybka
   kouká na ptáky · skok, šplouch · smutná · labuť připluje · rybka do labutího
   peří · vzlet · jezero shora · mraky · přistání · rybka vypráví ostatním
   `[tanec: snake_hip_hop Fin]` · západ slunce.
10. **Nejlepší narozeninový dort** — Kája + babička. Kuchyň, recept · mouka
    všude · vejce na zemi · těsto přeteče · trouba kouří · Kája brečí ·
    babička pustí rádio · `[tanec: salsa babička]` · `[tanec: salsa Kája]` ·
    dort křivý, ale se svíčkou · sfouknutí · všichni jedí.

Doporučené pořadí realizace: **1, 5, 8** (jeden hrdina, rekvizity, jednoduché
interiéry) → **3, 6, 10** (dvě postavy, tanec) → zbytek.

## 7. Stav implementace (22. 9. 2026)

| etapa | stav | kde |
|---|---|---|
| E0 smoke + A/B Kontext vs. IPAdapter | **neproběhlo** — žádný GPU běh; default zůstává Kontext | — |
| E1 scéna s vlastním `source` | hotovo + `--only`, `cut_transition/cut_crossfade`, kamera (`camera`) | `chain.py`, `workflows/camera_beat_14b_lightning_portrait.json` |
| E2 postavy, keyframy, arch, brána, kompilace | hotovo | `tools/story.py`, `workflows/story_*.json` |
| E3 vypravěč, hudba, titulky, 16:9 | hotovo v kódu; **Piper na SPARKu ještě není** | `tools/story.py`, `tools/get_piper.sh` |
| E4 katalog 10 příběhů, batch, kontrola | hotovo | `stories/*.json`, `tools/check_stories.py`, `story.py batch` |
| E5 API pro StoryStudio | hotovo (s review/approve/redo) | `serve.py`, README „Příběhy pro StoryStudio" |

Ověřeno bez GPU: grafy keyframů a všech typů beatů proti `/object_info`
ze SPARKu, montáž se smíšenými přechody a celý mix (vypravěč, hudba,
sidechain, loudnorm, 16:9, SRT) na umělých vstupech ffmpegem. Neověřeno:
cokoli, co generuje obraz nebo zvuk (Kontext, Wan, kamera, Piper, ACE-Step).

Odchylky od plánu výš:

- **„Sheet" je jeden celopostavový obrázek zepředu na bílém**, ne arch 4
  pohledů — Kontext by vícepohledovou referenci kopíroval do keyframu.
- **Tančí lidští hrdinové**, ne zvířecí pomocníci (kostry jsou lidské). Ryba
  (příběh 9) netančí vůbec; tužka (8) má `snake_hip_hop` místo booty; ve
  škole (3) tančí robota Tomík, protože Bip jezdí na kolečku; dort (10) je
  pro tatínka. `robot` a `moonwalk` mají kostru jen na 81 snímků → 1 beat.
- **Dvojjazyčné texty** (`narration` + `narration_en`, `title_en`…) jako u
  scén — TsumikiBot a StoryStudio si vyberou; Piper má pro každý jazyk hlas.
- **Délka ~60–70 s** (13–15 beatů): při 5 s záběru se vejde jen ~8 českých
  slov, proto 2–3 dvoubeatové záběry na příběh.
- **Hudba má zálohu**: ACE-Step v noci (00–07) neběží → LTX dárce z
  `chain.py` → bez hudby.

Co zbývá: nasadit (`git pull` na SPARKu, `tools/get_piper.sh`,
`sudo systemctl restart video-api`), E0 na jednom příběhu (doporučeně
`lost_umbrella`: `spark-video story sheet …`, `keyframes`, `approve`, `run`),
a podle výsledku doladit `style_tail`/knoby (`wan` v příběhu) a sílu Kontextu.
