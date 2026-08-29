# spark-video — dlouhý klip z jednoho obrázku, bez klikání v ComfyUI

Skript: `bin/spark-video` v tomhle repu.
Manifesty: `chains/<jméno>.json` — verzované, čte je i `chain.py` na SPARKu.
Výsledky: `out/<jméno>_{16,32}fps.mp4` — v `.gitignore`.

Nic z toho neleží v home a nepotřebuje symlink: skript si najde kořen repa sám,
takže funguje odkudkoliv.

## Instalace

Žádná — je to bash, nic se nebuilduje a git drží bit spustitelnosti.

```bash
./bin/spark-video ls
```

Když ho chceš mít pod rukou i mimo repo, přidej si `bin/` do PATH:

```bash
echo 'export PATH="/cesta/k/video-stack/bin:$PATH"' >> ~/.zshrc
```

## Celý postup

```bash
spark-video new  mojevideo ~/Pictures/fotka.png   # založí manifest, nahraje obrázek
spark-video edit mojevideo                        # uprav prompty
spark-video plan mojevideo                        # časová osa a odhad, bez GPU
spark-video run  mojevideo                        # render + slepení + RIFE + stažení
```

`run` vypíše na konci cestu k hotovým souborům a v terminálu je rovnou přehraje.
`spark-video ls` ukáže, co všechno už je hotové. ComfyUI se nemusí ani otevřít.

Na SPARKu to jede přes `chain.py`, který řetězí Wan 2.2 I2V segmenty. Jeden
segment = 81 snímků = 5.06 s (víc Wan 2.2 najednou neumí), poslední snímek
segmentu je vstupním obrázkem toho dalšího.

## Manifest

Zadání celého klipu na jednom místě. Jeden **beat** = jeden 5s segment.

```json
{
  "name": "mojevideo",
  "source": "mojevideo_src.png",
  "length": 81, "fps": 16,
  "style_tail": ", static camera, soft natural window light, photorealistic",
  "colormatch": { "method": "mkl", "strength": 0.6 },
  "beats": [
    { "id": "01", "seed": 42, "prompt": "she breathes gently and blinks slowly" },
    { "id": "02", "seed": 43, "prompt": "a warm smile slowly spreads across her face" },
    { "id": "03", "seed": 44, "prompt": "she raises one hand in a small friendly wave, then lowers it" }
  ]
}
```

**Delší klip = víc beatů.** 3 beaty = 15 s, 5 beatů = 25 s. Každý skok je ale
další generace navíc, takže se pomalu ztrácí podobnost s originálem —
colormatch drží barvy, ne identitu. Po dlouhém běhu porovnej poslední
`input/<jméno>_seedNN.png` s originálem, `plan` na to nad 6 beatů upozorní.

### Dlouhá scéna: `scenes`

Pro N×5 beatů se manifest píše po scénách. `id` a `seed` se dopočítají
(seed = kořenový `seed` + pořadí beatu), scéna může přepsat `style_tail`
a `negative`, beat zase cokoliv ze scény:

```json
{
  "name": "mojevideo",
  "source": "mojevideo_src.png",
  "seed": 42,
  "style_tail": ", static camera, soft natural window light, photorealistic",
  "scenes": [
    { "name": "intro",
      "beats": [ { "prompt": "she breathes gently and blinks slowly" },
                 { "prompt": "a warm smile slowly spreads across her face" } ] },
    { "name": "turn", "style_tail": ", static camera, golden hour",
      "beats": [ { "prompt": "she shifts her weight onto the other leg" },
                 { "prompt": "her hand comes to rest on her hip", "seed": 99 } ] }
  ]
}
```

Kostru vyrobí `spark-video new mojevideo fotka.png --scenes 4` — 4 scény × 5
prázdných beatů; prázdný prompt render odmítne, takže se nedá pustit
nevyplněná. Řetěz jde přes hranice scén beze změny (každý beat startuje
z posledního snímku předchozího). Scény jsou k trojímu:

```bash
spark-video run mojevideo --until intro   # jen první scéna — podívej se, než pustíš zbytek
spark-video run mojevideo --resume        # pokračuj od prvního nehotového beatu
spark-video run mojevideo --from turn     # přegeneruj od scény (nebo beatu "03") dál
```

`--resume` se řídí `output/<jméno>/state.json` na SPARKu: u každého hotového
beatu je otisk zadání (prompt, seed, rozlišení…). Změníš prompt beatu 07 →
`--resume` přegeneruje 07 a všechno za ním, protože každý další beat stojí na
jeho posledním snímku. Beze změn se render přeskočí a jde se rovnou na slepení.
`--from` a `--resume` se vylučují; `--until` jde kombinovat s oběma.

RIFE (16 → 32 fps) jede **po scénách** s překryvem jednoho snímku a pak se
slepí — celý dlouhý klip naráz by nešel do paměti (VHS_LoadVideo drží všechny
snímky v RAM, RIFE k tomu dvojnásobek). Na výsledku to není poznat: spoje mezi
scénami se interpolují taky a snímků vyjde stejně jako při jednom průchodu.

### Prompty

`prompt` + `style_tail` se slepí dohromady. Beat je nezávislých N snímků: Wan
popsanou akci roztáhne nebo stlačí tak, aby vyplnila celý beat, a co nemá konec,
to opakuje. Z toho plynou pravidla:

- **`static camera`** ve `style_tail` — bez toho si Wan rád začne najíždět a odjíždět.
- **Oblouk s koncovou pózou, ne stav.** „raises one hand, **then lowers it and
  stands still**" — každý beat končí pózou, na kterou navazuje další. Cyklická
  slovesa bez konce („shimmy", „taps rhythmically", „running-man") model smyčkuje,
  dokud beat neskončí; dej jim počet („stomps three times") nebo „then stops".
- **Jedno tempo na scénu.** Do `style_tail` scény („one continuous movement at a
  steady moderate tempo"), do beatu explicitní pacing („slowly over the whole shot",
  „quick, then holds"). Tři slovesa v jednom beatu = rychlý beat, jedno = pomalý.
- **Délka podle akce** — `length` per beat (4n+1, ≤ 81): úderná akce 49 (3 s),
  střední 65 (4 s), pomalá 81 (5 s). Krátký beat nedává modelu čas smyčkovat.

Negativní prompt je při `cfg 1.0` **neúčinný** — na množství pohybu se sahá jinde.

### Když je pohybu málo nebo moc

Množství pohybu řídí *high-noise expert*. Volitelné klíče, dědí se
**beat ← scéna ← kořen manifestu**, takže taneční scéna může mít jiné než klidná:

| klíč | default | co dělá |
|---|---|---|
| `length` | 81 | snímků v beatu (4n+1, max 81 = 5.06 s) |
| `motion` | 1.0 | síla Lightning LoRA na high-noise expertu. **Níž (0.8) = víc pohybu.** |
| `boundary` | 2 | hranice mezi experty ze 4 kroků. **Na 3 = víc pohybu.** |
| `shift` | 5.0 | **Výš (6–8) = klidnější**, míň deformací obličeje. |
| `identity` | – | `"face"` = oživení tváře navazovacího snímku z originálu (FaceDetailer + FaceID). Drift identity zastaví (0.63 vs 0.15 na 6. beatu), beat trvá stejně. |
| `transition` | `fade` | střih mezi beaty: `cut` (tvrdý), `fade` (prolínačka), `slices` (pásová přejížďka — liché pásy nový obraz zleva, sudé zprava; krátký, čitelný střih jako záměr). |
| `crossfade` | 1 | délka střihu ve snímcích (1 = tvrdý). Presety: `slices` + 6 (0,375 s). Skok na střihu má i tvrdý střih bez oživení — je to re-encode navazovacího snímku a reset pohybu (`tools/seam_pop.py`). |
| `bands` | 12 | počet pásů u `slices`. |
| `control` | – | per beat: `"<id>"` = pohyb z kostry `drive/<id>_pose.webm` (Wan VACE), vzhled z **úvodní fotky** (`control_ref: original`; `handoff` = navazovací snímek). `control_model: fun` přepne na fun_control (rychlejší, slabší kotva vzhledu). Pro pohyby, které Wan z textu neumí (moonwalk). Kostru vyrobí `tools/drive.py pose <id> --src klip.mp4` (DWPose); zdroj klipu: Mixamo render, vlastní natočení, stock — jen kostra jde do repa. |
| `sharpen` | 0 | doostření navazovacího snímku (0–1). Navazovací snímek je VAE-dekódovaný, tedy měkčí, a další beat z něj startuje — měkkost se sčítá. |

Presety v `scenes/` mají taneční scény `boundary 3` + `motion 0.8`, klidné `shift 7`.

Identita tváře: `tools/face_drift.py <jméno>` změří ArcFace podobnost každého
navazovacího snímku k originálu (1.0 = táž tvář, < 0.4 = jiný člověk); u 3 klidných
beatů bez oprav klesá 0.63 → 0.42. Měřicí sada je `chains/bench_identity.json`,
výsledky iterací v `reports/phase4_identity.md`.

## Ostatní

**Rozlišení** se počítá z poměru stran zdroje (Wan dělá center-crop, takže se
poměr trefuje, neohýbá). Default ~0.45 Mpx, `spark-video run <jméno> --hd`
zhruba 1 Mpx — ale je to ~2,3× pomalejší. Wan 2.2 je trénovaný kolem 9:16 až
16:9; telefonní fotka (19.5:9) je pod tím pásmem a `plan` na to upozorní.
`"fit": "9:16"` v manifestu poměr srovná za cenu center-cropu; nebo si fotku
ořízni sám, ať rozhoduješ ty, co odejde.

**Čas:** ~150 s na segment draft, ~330 s HD. 5 beatů draft ~12 min GPU, 25 beatů
~45 min. Na SPARKu ale běží fronta a paměť sdílí s LLM: když je `vram_free`
(`/system_stats`) pod `--reserve-vram`, ComfyUI odloží model na CPU a render se
plazí — před dlouhým během to zkontroluj.

**Ručně doladit workflow v ComfyUI** (když už se klikat chce):

```bash
ssh spark 'cd Code/video-stack && ./chain.py chains/mojevideo.json --materialize'
```

Zapíše `workflows/chain-mojevideo/beatNN.json`. Ty se dají otevřít v ComfyUI
**přetažením na plátno** (ze sidebaru ne — API formát ComfyUI nekonvertuje, na to
je `api2ui.py`) a `chain.py` je pak při renderu použije beze změny.

**Jiný stroj:** `SPARK_HOST=jinyhost spark-video run …`, jiná datová složka:
`SPARK_VIDEO_DIR=~/jinde`.
