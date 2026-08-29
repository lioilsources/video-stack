# Fáze 4 — identita, tempo, smyčky, délka (2026-08-29)

Měřicí sada: `chains/bench_identity.json` — zdroj `leather_shorts_src.png`, seed 42,
6 beatů po 81 snímcích (2 klidné `calm`, 4 taneční `dance`), draft 464×944.
Metrika: `tools/face_drift.py` = ArcFace (insightface antelopev2) kosinová
podobnost navazovacího snímku `seedNN.png` k originálu `seed00.png`.
1.0 = táž tvář, ~0.6 = pořád ona, < 0.4 = jiný člověk. Varianty mění jen
`base`/knoby, prompty a seed zůstávají.

## Výsledky

| varianta | 01 | 02 | 03 | 04 | 05 | 06 | průměr | min | s/beat |
|---|---|---|---|---|---|---|---|---|---|
| baseline (I2V, bez oprav) | 0.554 | 0.456 | 0.372 | 0.312 | 0.221 | 0.147 | 0.344 | 0.147 | 105–185 |
| `identity: "face"` (oživení navazovacího snímku) | 0.585 | 0.646 | 0.621 | 0.658 | 0.623 | 0.649 | **0.630** | **0.585** | 110–125 |
| face s `face_denoise 0.3` (default 0.4) | 0.573 | 0.611 | 0.667 | 0.619 | 0.525 | 0.586 | 0.597 | 0.525 | 90–130 |
| face + `sharpen 0.3` + `shift 6` | 0.648 | 0.616 | 0.668 | 0.608 | 0.479 | 0.495 | 0.586 | 0.479 | 115–120 |
| VACE s `reference_image` = originál (T2V Lightning, bez oživení) | 0.780 | 0.597 | 0.552 | 0.414 | 0.514 | 0.440 | 0.550 | 0.414 | 135 |

Baseline potvrzuje diagnózu: podobnost klesá monotónně s každou generací,
na 6. beatu (30 s) je to podle metriky jiný člověk. Klidné beaty ztrácejí
~0.1 na skok, taneční (otočka, ruce nad hlavou) ~0.06–0.09 navíc k tomu.

**Oživení tváře drift zastaví.** Podobnost už neklesá s počtem generací —
drží ~0.63 od 1. do 6. beatu (baseline 0.15 na šestém), tj. každý beat
startuje z tváře, která je originálu stejně blízko jako první. Cena je
nulová: beat trvá stejně jako bez oživení (SDXL Lightning FaceDetailer se
schová do rozptylu). Baseline časy 175–185 s u tanečních beatů byly
paměťová past unified paměti (viz níže), ne složitost promptu.

Jednosnímkový test oživení (`identity: "face"`, FaceDetailer SDXL Lightning +
IPAdapter FaceID PlusV2 s referencí = originál) na `30deb5e8_seed03`:
0.418 → **0.661**, vizuálně tvar obličeje a oči zpět u originálu, vlasy a tělo
beze změny.

**Nižší denoise (0.3) identitu mírně oslabí** (0.60 < 0.63) a skok na střihu
neřeší (3.4 — ten řeší crossfade); default zůstává 0.4.

**Doostření a shift 6 nepomohly** (0.586 < 0.630, beaty 5–6 padají) — vyřazeno.

**VACE s referencí** injektuje identitu silně do prvního beatu (0.78, nejlepší
jednotlivé číslo), ale pak drží jen jako I2V a hlavně **tlumí pohyb**:
v kontaktním archu taneční beaty skoro stojí, otočka je náznak. T2V Lightning
LoRA na fun_vace funguje (4 kroky, žádný rozpad), beat 135 s. Jako default ne;
kandidát na „klidný režim" (portrét, mluvící hlava), kde je malý pohyb
žádoucí — a v kombinaci s oživením by identita byla nejvyšší.

## Skok na střihu (`tools/seam_pop.py`)

Rozdíl pixelů přes střih / medián rozdílů sousedů (1 = nerozeznatelné, > 2
viditelné), horní třetina obrazu, klip 16 fps před RIFE:

| varianta | 1→2 | 2→3 | 3→4 | 4→5 | 5→6 | průměr | max |
|---|---|---|---|---|---|---|---|
| baseline, tvrdý střih | 1.89 | 6.35 | 2.22 | 4.97 | 2.28 | 3.54 | 6.35 |
| face, tvrdý střih | 1.85 | 4.28 | 2.67 | 5.47 | 1.60 | 3.17 | 5.47 |
| face, `crossfade: 4` | 1.72 | 0.94 | 1.41 | 1.32 | 1.06 | **1.29** | **1.72** |
| face, `crossfade: 8` | 1.42 | 1.60 | 0.98 | 1.53 | 1.08 | 1.32 | 1.60 |

Skok na střihu má **i baseline** — není to oživení tváře (to ho nezhoršuje),
ale re-encode navazovacího snímku (colormatch + VAE) a reset rychlosti pohybu
na začátku beatu. Prolínačka 4 snímků (0.25 s) ho srovná na úroveň běžného
pohybu; 8 snímků už nic nepřidá. Cena: 3 snímky na střih.

Prolínačka byla měřitelně čistá, ale vizuálně „neznatelná, a proto matoucí".
Nahrazena střihem jako záměrem: **`transition: "slices"`** — 12 vodorovných
pásů, liché pásy nový obraz vtlačí zleva a starý vytlačí doprava, sudé
zrcadlově, 6 snímků (0,375 s). ffmpeg `xfade=transition=custom` s vlastním
výrazem (`chain.slices_expr`), ověřeno na syntetických klipech i na
`bench_identity_face` (re-assemble bez GPU). Otevřené: chování RIFE přes
pásy v pohybu — kdyby dělal duchy, přesune se střih až za RIFE.

**Rozhodnutí pro presety: `identity: "face"` + `transition: "slices"`, `crossfade: 6`** ve všech (29 scén, z toho 11 ikonických tanců).

## Poznámky k tempu a smyčkám

Kontaktní archy (1 fps) přepsaných `tap_dance_rhythm` a `melbourne_shuffle`
po iteraci 1: pohyb postupuje beat od beatu (zvednutí nohy, ruka ke klobouku,
kopy; T-step, otočka, široký postoj), žádný beat nevypadá jako smyčka téže
akce. Tempo je v rámci scény vyrovnané — kratší beaty (49) pro úderné akce
sedí. Vedlejší jevy I2V: postava se během 5 beatů posune od zdi do místnosti
a u stepu zmizí boty a objeví se klobouk (prompt „tips an imaginary hat brim"
si klobouk vyrobil) — prompty pro rekvizity psát bez rekvizit.


(doplní se po renderu přepsaných scén — `tap_dance_rhythm`, `melbourne_shuffle`)

## Provozní nález: past unified paměti

Baseline i první pokus o face variantu jely část času na CPU (`X MB usable,
Y MB offloaded, lowvram patches`): po sérii renderů drží page cache ze
safetensors ~80 GB a ComfyUI sám ~53 GB RSS offloadovaných modelů, takže CUDA
hlásí ~15 GB volno i s vypnutými LLM kontejnery. Restart ComfyUI pomůže
(37 GB volno), ale po jednom beatu je cache zpátky (7 GB volno).

**Řešení bez roota:** `posix_fadvise(DONTNEED)` na soubory v `models/`,
`input/`, `output/` — page cache 84 → 34 GB, `vram_free` 7.3 → **52.9 GB**
za 2 s. `chain.drop_page_cache()` běží před každým promptem (beat i RIFE)
a `serve.py` před kontrolou `vram_free`.

## Moonwalk: proč není podobný (2026-08-29, referenční obrázek)

Sedm renderů na `leather_shorts_src.png` (ruka opřená o zárubeň, rozkročené
nohy, podpatky), vždy seed 42, `boundary 3`, `motion 0.8`:

| test | výchozí póza | prompt | výsledek |
|---|---|---|---|
| a | reference | preset („glides backwards while the feet appear to walk forward") | zvedne koleno a položí, ruka na zárubni, nohy na místě |
| b | reference | mechanika (flat foot slides back, other on toes, swap…) | pustí zárubeň, přešlápne na místě |
| c | reference | pojmenování („does the moonwalk dance move") | stojí, přenáší váhu |
| d | **přípravný beat** (pustí zárubeň, doprostřed, nohy u sebe) → b | **jde prostorem** — kroky, přenášení váhy, posun po místnosti; ne klouzání |
| e | přípravný beat → klouzání do strany | stojí uprostřed, minimální posun |
| f | reference → klouzání do strany | pustí zárubeň, houpá se, mírný posun vpravo |
| preset celý | reference | 6 beatů | beat 2 nic; otočka skončila zády ke kameře; stoj na špičkách = sepnuté ruce + výskok; ruka k čelu ✓; náklon = předklon v pase |

Závěr: **oba faktory, ale póza je silnější.** Z opřené, rozkročené pózy
nevznikne lokomoce žádným promptem — ruka na zárubni a nohy v rozkroku jsou
pro I2V kotvy, které model ctí víc než text. Přípravný beat je odemkne.
Samotný moonwalk (iluze klouzání) ale Wan 2.2 I2V (Lightning, cfg 1) z textu
neumí ani z neutrální pózy — udělá obyčejné kroky. Totéž platí pro „klouzání
do strany". Ikonické pohyby s lokomocí (moonwalk, cval, kroky Thrilleru)
potřebují jiný nástroj než text: **řídicí video** (fun_control /
`v2v_control_14b_lightning_portrait.json`, v benchi „pohyb 1:1 dle control
videa") — jeden krátký driving klip na preset. To je další iterace.

Opatření teď: `icon_moonwalk`, `icon_thriller`, `icon_gangnam`,
`icon_charleston` začínají přípravným beatem (pustit, doprostřed, nohy u sebe);
moonwalk beat používá mechanický popis (jediný, který dává pohyb prostorem);
otočka výslovně „comes all the way back around to face the camera".

## Control beat: pohyb z kostry (2026-08-29)

`"control": "<id>"` v beatu = Wan 2.2 fun_control + T2V Lightning, `ref_image`
= navazovací snímek, `control_video` = DWPose kostra `drive/<id>_pose.webm`.
Test `chains/mw_ctl.json` (přípravný beat I2V → control beat) na referenci:
postava **kopíruje kostru snímek po snímku** (paže, závěrečné zvednutí rukou),
vzhled a místnost z reference, tvář 0.53–0.69 (oživení funguje i tady),
beat 170 s (I2V 105–130 s). Mechanismus drží.

Bootstrap kostra z Wan T2V („a dancer performs the moonwalk…") ale moonwalk
**není** — T2V tancuje na místě a zvedne ruce; klouzání z textu neumí ani
T2V. Kostra pro moonwalk musí přijít z Mixamo (má Moonwalk) nebo z vlastního
natočení: `spark-video drive moonwalk klip.mp4` → `drive/moonwalk_pose.webm`.

**RIFE přes pásovou přejížďku dělá rozmazané bloky** (riziko z plánu se
potvrdilo). Proto RIFE jede po beatech (segment 81 → 161 snímků, sousední
segmenty sdílejí hraniční snímek) a střih se dělá až na 32 fps s překryvem
2k−1 snímků = stejná doba. `_full.mp4` (16 fps) zůstává jako dřív.

**Mixamo kostra (2026-08-29 11:40):** nahrávka obrazovky z Mixamo prohlížeče
(358×562, 4 s, smyčka na 81 snímků přes `--loop`). yolox detektor osob na
červeném panáčkovi nenašel nic (kostra černá) — DWPose bez bbox detektoru
přes celý snímek funguje; `drive.py` to sonduje automaticky. Control beat na
referenci: nohy a váha kopírují kostru snímek po snímku (křížení, náklon),
postava se pohybuje prostorem mimo zárubeň; tvář na handoffu 0.44 (postava je
v control beatu menší). `icon_moonwalk` přepnut na `"control": "moonwalk"`.
Otevřené: kostra z nahrávky má kameru mírně shora (Mixamo výchozí pohled) —
render z Blenderu zepředu by byl přesnější.
