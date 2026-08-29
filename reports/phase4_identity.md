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

**Rozhodnutí pro presety: `identity: "face"` + `crossfade: 4`** ve všech.

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
