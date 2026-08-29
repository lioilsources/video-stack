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

Baseline potvrzuje diagnózu: podobnost klesá monotónně s každou generací,
na 6. beatu (30 s) je to podle metriky jiný člověk. Klidné beaty ztrácejí
~0.1 na skok, taneční (otočka, ruce nad hlavou) ~0.06–0.09 navíc k tomu.

Jednosnímkový test oživení (`identity: "face"`, FaceDetailer SDXL Lightning +
IPAdapter FaceID PlusV2 s referencí = originál) na `30deb5e8_seed03`:
0.418 → **0.661**, vizuálně tvar obličeje a oči zpět u originálu, vlasy a tělo
beze změny.

## Poznámky k tempu a smyčkám

(doplní se po renderu přepsaných scén — `tap_dance_rhythm`, `melbourne_shuffle`)
