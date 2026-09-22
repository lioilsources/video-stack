
## validate.py

Ověří všechna workflow proti běžícímu ComfyUI — chytá to, co jinak vyleze
až při zařazení do fronty.

```bash
./validate.py                    # celé repo
./validate.py workflows/x.json   # vybraná
./validate.py --quiet            # jen chyby (návratový kód 1 při chybě)
```

Kontroluje: neznámé typy nodů, chybějící povinné vstupy, odkazy na
neexistující nody a výstupy, hodnoty mimo nabídku comba (např. soubor,
který v models/ nebo input/ není), a upozorní na zapomenuté placeholdery.

Placeholder `PROMPT_PLACEHOLDER` v šablonách je záměrný — dosazuje se při
volání, ve varováních tedy zůstává.

## api2ui.py — proč nešla workflow otevřít ze sidebaru

Workflow v repu jsou v **API formátu** (mapa nodů). ComfyUI má dvě různé cesty
načítání a jen jedna z nich API formát pozná:

- **drag & drop** → file reader → `isApiJson()` → `loadApiJson()` → převede a vykreslí
- **sidebar** → načte soubor rovnou jako UI graf → chybí `nodes`/`links` → prázdné plátno

`api2ui.py` dopočítá UI formát, takže sidebar funguje:

```bash
./api2ui.py workflows/*.json workflows/camera_presets/*.json ads/kiran/workflows/*.json \
  -o ~/Code/ComfyUI/user/default/workflows/video-stack
./api2ui.py --selftest    # round-trip proti skutečným UI workflow
```

Zdrojem pravdy zůstávají API soubory v repu; UI kopie jsou generovaný artefakt
a po změně workflow se přegenerují.

Dvě pasti, které se nedají uhodnout a selftest je odhalil:

| widget navíc | příznak v `/object_info` | hodnota |
|---|---|---|
| seed → control_after_generate | `control_after_generate: true` | `"fixed"` |
| LoadImage → upload tlačítko | `image_upload: true` | `"image"` |

Obojí je v `widgets_values` navíc a v API formátu nemá protějšek. Bez nich se
hodnoty widgetů posunou — graf se načte, ale s tichým rozhozením parametrů,
což je horší než prázdné plátno. Proto `--selftest`: vezme skutečná UI
workflow ze stroje, udělá z nich API a převede zpět; 25 z 25 sedí.

## bin/spark-video — celý běh z Macu

`chain.py` běží na SPARKu. Když nechceš SSH ani klikat v ComfyUI, je na Macu
`bin/spark-video`: nahraje obrázek a manifest, spustí `chain.py --all` a stáhne
obě verze výsledku.

```bash
spark-video new  mojevideo ~/Pictures/fotka.png              # 3-beatová ukázka
spark-video new  mojevideo ~/Pictures/fotka.png --scenes 4   # kostra 4 scén × 5 beatů
spark-video edit mojevideo
spark-video plan mojevideo
spark-video run  mojevideo --until intro     # náhled první scény
spark-video run  mojevideo --resume          # pokračuj, kde to skončilo
spark-video run  mojevideo --from turn       # přegeneruj od scény dál
```

Nic se neinstaluje a nebuilduje — je to bash a git drží bit spustitelnosti.
Skript si najde kořen repa sám, takže `./bin/spark-video ls` funguje odkudkoliv;
kdo ho chce mít pod rukou, přidá si `bin/` do PATH.

Manifesty jsou v `chains/` (stejné, které čte `chain.py`) a výstupy v `out/`,
což je v `.gitignore`. V home nezůstává nic. Podrobnosti v [bin/README.md](bin/README.md).

## serve.py — „Rozhýbat" v Ol1nLLM Image Studiu

Job server nad `chain.py` pro appku: telefon nahraje obrázek a vybere scénu,
render běží na SPARKu, appka polluje a stáhne mp4. Stdlib + PIL, běží z ComfyUI
venv jako `video-api.service` (`deploy/video-api.service`, port 8096), ven jde
přes cloudflared path rule `llm.ol1n.com/v1/video/*` (AiStack).

```
GET  /v1/video/scenes               katalog ze scenes/*.json
POST /v1/video/jobs                 {scene, image: base64, seed?} → 202 {job_id}
GET  /v1/video/jobs/<id>            {status: queued|running|done|error, position, beat, beats, phase}
GET  /v1/video/jobs/<id>/result     video/mp4
```

Scény jsou v `scenes/<id>.json` — kus manifestu (`style_tail`, `negative`,
`scenes[]` s beaty) + `id`/`label`/`desc`; server doplní `name`/`source`/`seed`.
Prompty se ladí tady, bez release appky. Jeden worker (GPU je sériová), stav
v `jobs/<id>.json`, výsledek je durable `output/<id>/<id>_32fps.mp4`. Před
startem jobu se kontroluje `vram_free` — pod 12 GB job skončí chybou místo
hodiny na CPU. TODO: úklid `input/<id>_src.png`, `output/<id>/`, `jobs/` po TTL.

Deploy: `git pull` na SPARKu, `sudo cp deploy/video-api.service /etc/systemd/system/
&& sudo systemctl daemon-reload && sudo systemctl enable --now video-api`;
po změně `serve.py` `sudo systemctl restart video-api` (scény se čtou při startu).

### Příběhy pro StoryStudio

Stejný server nese i minutové příběhy (`tools/story.py`, viz níž). Klient
pošle příběh z katalogu a obrázky postav podle rolí; co nepošle, doplní server
z výchozích postav, které vznikly přes `story.py sheet`/`cast`.

```
GET  /v1/video/stories                 {stories: [{id, title, title_en, desc, desc_en, setting, shots, beats,
                                         seconds, minutes_est, minutes_est_hd, languages, characters: [{role,
                                         name, name_en, desc, look, hero, default}], script: [{id, chars,
                                         action, narration, narration_en, control, camera, beats}]}]}
POST /v1/video/stories/jobs            {story, characters?: {role: base64}, seed?, lang?: cs|en,
                                        review?: bool, hd?: bool} → 202 {job_id, shots, beats, seconds, minutes_est}
GET  /v1/video/jobs/<id>               {kind: "story", story, stage, status, phase, keyframe, keyframes,
                                        beat, beats, lang, review, hd, error, position?}
GET  /v1/video/jobs/<id>/keyframes     {shots: [{id, chars, keyframe, action, narration, narration_en, control,
                                        camera, ready, url}], contact}
GET  /v1/video/jobs/<id>/keyframes/NN  image/jpeg
GET  /v1/video/jobs/<id>/contact       image/jpeg (kontaktní arch)
POST /v1/video/jobs/<id>/approve       {} → render; {redo: ["03"], keyframe: {"07": "nový popis"}} → přegenerovat
GET  /v1/video/jobs/<id>/result        video/mp4 s vypravěčem a hudbou; ?variant=sub | 16x9
```

Životní cyklus: `queued → running (phase keyframes) → review` (jen s
`review: true`) `→ /approve → queued → running (phase compile, render,
assemble, rife, voice, music, mix) → done`. Bez `review` jede job rovnou až do
`done`. `redo` vrátí job do `running` jen pro vybrané záběry (nový seed, nebo
nový popis z `keyframe`) a pak znovu do `review`. `status: "review"` je pro
scény neznámý stav — `VideoService` v Ol1nLLM ho neumí, StoryStudio ho musí
obsloužit. Postup: `keyframe/keyframes` ve fázi keyframes, `beat/beats` při
renderu; celý job je v minutách, klient polluje stejně jako u scén.

## chain.py — dlouhý klip z jednoho obrázku

Wan 2.2 umí najednou nejvýš **81 snímků** (5.06 s @ 16 fps) a `length` musí být
**4n+1**, protože VAE komprimuje čas 4:1. Není to limit stroje — 49 snímků
(3.06 s) v Kiran spotu byla volba délky záběru, ne strop. Delší stopáž se dělá
návazně: poslední snímek segmentu N je vstupním obrázkem segmentu N+1.
3 segmenty = 81 + 80 + 80 = **241 snímků = 15.06 s**.

```bash
./chain.py chains/idle01.json --plan       # časová osa a odhad, bez GPU
./chain.py chains/idle01.json --validate   # kontrola proti /object_info
./chain.py chains/idle01.json --materialize # zapiš beatNN.json k ruční úpravě
./chain.py chains/idle01.json              # render všech beatů
./chain.py chains/idle01.json --from 02    # od beatu (nebo scény) dál
./chain.py chains/idle01.json --until idle # skonči po beatu/scéně — náhled
./chain.py chains/idle01.json --resume     # od prvního nehotového beatu
./chain.py chains/idle01.json --hd         # ~1 Mpx místo ~0.44 Mpx
./chain.py chains/idle01.json --assemble   # slepení (ffmpeg, bez GPU)
./chain.py chains/idle01.json --smooth     # RIFE 16 -> 32 fps po scénách
```

Zdroj pravdy je manifest `chains/<jméno>.json` — jeden beat = jeden segment,
vlastní prompt a seed. Delší scéna se píše jako `scenes`: pojmenované skupiny
beatů, `id` a `seed` se dopočítají, scéna může přepsat `style_tail`/`negative`.
Řetěz jede přes hranice scén beze změny (každý beat startuje z posledního
snímku předchozího); scény jsou adresy pro `--from`/`--until` a hranice, po
kterých se dělí RIFE, aby dlouhý klip nešel do paměti naráz. Základní workflow
(`base`) se čte z `workflows/` beze změny, chain.py do něj jen dosazuje a
přidává tři nody.

### Kde je workflow

`chain.py` staví graf v paměti a posílá ho rovnou na `/prompt` — na disku sám
od sebe nic nenechá. `--materialize` zapíše plnohodnotná API workflow
s **dosazenými** hodnotami do `workflows/chain-<name>/beatNN.json`; ta se dají
otevřít v ComfyUI přetažením na plátno, doladit ručně, a `chain.py` je pak při
renderu **použije beze změny** (`--hd` si je vyrobí znovu z manifestu).

Pro sidebar je potřeba UI formát — API formát ComfyUI ze sidebaru nekonvertuje:

```bash
./api2ui.py workflows/chain-idle01/*.json \
  -o ~/Code/ComfyUI/user/default/workflows/video-stack/chain-idle01
```

### Co drží řetěz pohromadě

Dvě věci, bez kterých se to po třech skocích rozpadne:

| | |
|---|---|
| **PNG, ne vp9** | Předávaný snímek jde přes `ImageFromBatch` → `SaveImage` (node 20 a 22). Kdyby se seed bral z uloženého webm, každý skok by přidal ztrátovou kompresi a třetí segment vyjde měkký a zašuměný. |
| **ColorMatchV2 proti originálu** | Node 21 srovnává předávaný snímek s **původním** obrázkem, ne s předchozím seedem — jinak by se drift jen kopíroval dál místo aby se opravoval. `mkl`, strength 0.6. |

Segmenty se ukládají s `crf 18` (ne 32 jako jinde) — při montáži se překódovávají
a na spojích by crf 32 bylo vidět.

### Rozlišení se počítá ze zdroje

`WanImageToVideo` dělá **center-crop** na zadaný poměr, takže poměr obrázku se
má trefit, ne ohnout do 9:16. `chain.py` přečte poměr zdroje a dopočítá rozměr
násobný 16 na daný rozpočet pixelů (draft ~0.44 Mpx, `--hd` ~1 Mpx) — z 896×1152
tedy vyleze 592×752. Odhad času vychází z matice ve `phase3_bench.md`
(26. 8. přeměřeno: ~333 s na megapixel a 81 snímků — srpnová matice měřila studené čtení z NVMe, s modely v page cache je to 2× rychleji).

### Ladění množství pohybu

Globální pohyb u Wan 2.2 řídí **high-noise expert**. Negativní prompt je při
`cfg 1.0` neúčinný, takže dial je jinde — volitelné klíče v manifestu:

| klíč | co dělá |
|---|---|
| `motion` | `strength_model` I2V Lightning LoRA na high-noise expertu (node 2). Níž (0.7–0.8) = víc pohybu. |
| `boundary` | hranice mezi experty (node 13 `end_at_step` / 14 `start_at_step`), default 2. Na 3 = víc kroků v high-noise = víc pohybu. |
| `shift` | `ModelSamplingSD3` (node 3 a 6), default 5.0. Výš (6–8) = klidnější, méně deformací obličeje. |

### Montáž

Segment N+1 začíná přesně tím snímkem, kterým segment N končí, takže
`--assemble` ho zahazuje (`select=gte(n\,1)`) — jinak by každý spoj zadrhl
o zdvojený snímek. RIFE se pouští `--smooth` přes **celý** slepený klip, ne po
segmentech, aby vyhladil i spoje.

### Scéna s vlastním obrázkem, `--only` a kamera

Scéna může mít vlastní `source`. Řetěz se na ní přeruší: první beat scény
startuje z jejího obrázku a ten je referencí (colormatch, VACE, `beat_ref`)
pro všechny beaty scény. Z toho se skládají příběhy — záběr = scéna s
keyframem — a drift se nekumuluje přes celou minutu, jen uvnitř záběru.

| klíč | co dělá |
|---|---|
| `scenes[].source` | obrázek scény (cesta, jméno v output/ nebo input/) |
| `cut_transition` | přechod na scénu s vlastním obrázkem: `cut`, `fade` (default), `slices` |
| `cut_crossfade` | délka toho přechodu ve snímcích, default 8 (0,5 s) |
| `camera` | beat přes Wan Fun Camera: `zoom_in`, `zoom_out`, `pan_left/right/up/down`, `cw`, `acw` |
| `camera_speed` | rychlost kamery, default 0.5 |

`--only shot03` přegeneruje jen scénu a to, co na ní visí až do další scény
s vlastním obrázkem; `--resume` přeskakuje hotové úseky za takovým střihem.
Bez scén s vlastním obrázkem se chová všechno jako dřív (stejné otisky ve
`state.json`, stejná montáž). Kamerová báze
(`workflows/camera_beat_14b_lightning_portrait.json`) má stejná ID nodů jako
I2V báze, takže `motion`/`boundary`/`shift` platí beze změny; s `control` ani
s LTX nejde.

## tools/story.py — minutový anime příběh

Z postaviček (vlastní obrázek nebo vygenerovaný) udělá ~65 s příběh: 12 záběrů
s keyframy, pohyb z Wan 2.2, tanec z koster, český nebo anglický vypravěč,
hudba, titulky. Katalog je v `stories/*.json` (deset dětských příběhů),
kontrola bez GPU `tools/check_stories.py`. Běží na SPARKu z venv ComfyUI;
z Macu přes `spark-video story …`.

```bash
spark-video story plan lost_umbrella                 # časová osa, odhad, délka narace
spark-video story cast lost_umbrella mia ~/anime/mia.png   # vlastní postavička
spark-video story sheet lost_umbrella cat             # nebo 4 kandidáti (Illustrious) → arch
spark-video story sheet lost_umbrella cat --pick 2
spark-video story keyframes lost_umbrella             # 12 keyframů (FLUX Kontext) → kontaktní arch
spark-video story keyframes lost_umbrella --shot 03,07 --reroll
spark-video story approve lost_umbrella               # brána: bez ní se nerenderuje
spark-video story run lost_umbrella                   # render → vypravěč → hudba → mix, stáhne videa
spark-video story run lost_umbrella --hd --lang en
spark-video story batch --auto-cast                   # přes noc: všechny příběhy v draftu
```

Jak to drží pohromadě:

- **Záběr = keyframe z obrázku postavy.** FLUX Kontext dostane referenci
  postavy (u dvou postav slepené vedle sebe) a popis záběru; `--method
  ipadapter` jde přes Illustrious + IPAdapter a drží jen hrdinu. Otisk
  (prompt, reference, metoda, seed) je v `kf/NN.json` — změna promptu
  přegeneruje jen ten záběr se stejným seedem, `--reroll` dá nový.
- **Kontaktní arch je brána.** `approve` uloží sha keyframů; když se keyframe
  změní, `compile` odmítne, dokud se neschválí znovu. `--no-review` bránu
  obejde (batch draft, server bez `review`).
- **Manifest se generuje** do `chains/<work>.json` — úpravy dělej ve
  `stories/`, ne tam. Uvnitř záběru prolnutí 4 snímky přes navazovací snímek,
  mezi záběry fade 8 snímků. Tanec startuje kostru od začátku v každém záběru.
- **Oživení tváře (`identity: face`) se nepoužívá** — PuLID/InsightFace je na
  fotky, anime tvář by přemaloval. Identitu drží keyframe každého záběru.
- **Vypravěč**: Piper (`tools/get_piper.sh`, CPU, vlastní venv), hlasy
  `cs_CZ-jirka-medium` a `en_US-lessac-medium`. Věta záběru začíná 0,2 s po
  dokončení prolnutí; delší než záběr se zrychlí až na 1,2×, jinak varování.
  `check_stories.py` hlídá délku textu předem (~2 slova/s česky).
- **Hudba**: ACE-Step přes AiStack `services/audio` (`AUDIO_URL`, default
  `localhost:8093`) na celou délku; když neběží (noční režim 00–07), zaskočí
  LTX dárce z `chain.py` (19 s smyčka), jinak bez hudby. Pod vypravěčem se
  ztlumí sidechainem, na konci `loudnorm` −16 LUFS.
- **Výstup** v `output/<work>/`: `<work>_story_<lang>.mp4` (9:16),
  `_sub.mp4` (vypálené titulky, potřebuje libass), `_16x9.mp4` (rozmazané
  pozadí), `story/<lang>.srt`.

Formát příběhu (zkráceně):

```json
{
  "id": "lost_umbrella", "title": "Ztracený deštník", "title_en": "The Lost Umbrella",
  "desc": "…", "desc_en": "…", "seed": 1001, "lang": "cs",
  "world": "a cozy small town on a rainy spring day, …",
  "music": "gentle playful children's music, ukulele, …, instrumental, 90 bpm",
  "characters": {
    "mia": {"name": "Mia", "name_en": "Mia", "desc": "a cheerful 7-year-old girl … yellow raincoat …"},
    "cat": {"name": "kocour Mourek", "name_en": "Tom the cat", "desc": "a chubby fluffy grey tabby cat …"}
  },
  "shots": [
    {"id": "01", "chars": ["mia"], "keyframe": "Mia kneels on the window seat …",
     "motion": "Mia presses her nose to the rainy window glass …, then she leans back and smiles",
     "narration": "Pršelo. Mia se na déšť moc těšila.", "narration_en": "It was raining. …", "camera": "zoom_in"},
    {"id": "03", "motion": ["první beat …", "druhý beat …"], "…": "…"},
    {"id": "09", "control": "chicken_dance", "beats": 2, "motion": "Mia dances the chicken dance …", "…": "…"}
  ]
}
```

První postava je hrdina (default `chars`). `desc` a `keyframe` jsou anglicky
(prompty), `name`/`narration` česky, `_en` anglicky. Volitelně: `sheet`
u postavy (pevný obrázek), `keyframe_method`, `engine`, `wan` (shift/motion/
boundary pro celý příběh), `music_engine` (`ace`|`ltx`|`none`),
`music_volume`, `voice` ({cs, en} jména hlasů), `length_scale`.

Nasazení na SPARK: `git pull`, `tools/get_piper.sh` (jednou) a
`sudo systemctl restart video-api` (katalog příběhů se čte při startu).

## Druhý engine: LTX-2.3 (experimentální)

`"engine": "ltx"` v kořeni manifestu přepne beaty na LTX-2.3 (22B AV DiT):
video **a synchronní zvuk** jedním průchodem, 25 fps, klip až 481 snímků
(~19 s, `length` 8n+1), nativní vertikála. Řetězení, colormatch i oživení
tváře jedou stejnou cestou jako u Wan; navíc vzniká `seg<id>_av.mp4` se
zvukem. RIFE se u LTX přeskakuje. Váhy stáhne `tools/get_ltx.sh` (~41 GB),
stav a omezení viz `reports/phase5_ltx.md`.

Licence: LTX-2 Community License — do $10M ročního obratu zdarma, výstupy
patří nám, ale je nutné **označovat AI výstup**; zákaz deepfake/impersonace.

### Držení identity u LTX

| klíč | co dělá |
|---|---|
| `identity: "face"` | oživení tváře z originálu na navazovacím snímku. U LTX běží jako **samostatný prompt** (do jednoho grafu se FLUX+PuLID vedle 27GB checkpointu nevejde) — stojí ~75 s na střih. |
| `beat_ref: "original"` | beat startuje z **původní fotky** místo z navazovacího snímku, takže drift se nekumuluje. Cena: skok do výchozí pózy na střihu (prolínačka `slices` ho schová). Oživení tváře pak není potřeba. |

`soundtrack: "<popis hudby>"` složí jednu skladbu přes celé video (jeden 19s
LTX klip jako dárce, stopa se zacyklí a namuxuje). Bez něj má každý beat
vlastní hudbu — u víc beatů to zní jako přeskakující rádio.

**Stav po A/B (31. 8. 2026, celá čísla v `reports/phase5_ltx.md`):** LTX je
~6× rychlejší než Wan na jednotku práce, umí zvuk a klipy do 19 s, obraz je
ostřejší. **Identitu ale drží hůř** (0.61 hd / 0.43 draft proti Wan 0.70) a
v dlouhém klipu se postava změní na jiného člověka. Control z kostry na
ComfyUI 0.19.3 **nefunguje** (chybí `GetICLoRAParameters` — model kostru
překreslí). Proto: LTX na krátké ozvučené klipy, Wan na vše, kde jde o
identitu a na control beaty.
