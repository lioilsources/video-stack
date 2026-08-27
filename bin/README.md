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

`prompt` + `style_tail` se slepí dohromady. Dvě věci, které dělají práci:

- **`static camera`** ve `style_tail` — bez toho si Wan rád začne najíždět a odjíždět.
- **Popisuj oblouk, ne stav.** „raises one hand… **then lowers it**" je důvod,
  proč se ruka na konci vrátí dolů a další segment nezačíná uprostřed gesta.

Negativní prompt je při `cfg 1.0` **neúčinný** — na množství pohybu se sahá jinde.

### Když je pohybu málo nebo moc

Množství pohybu řídí *high-noise expert*. Volitelné klíče v manifestu (platí
pro celý řetěz, ne po scénách):

| klíč | default | co dělá |
|---|---|---|
| `motion` | 1.0 | síla Lightning LoRA na high-noise expertu. **Níž (0.8) = víc pohybu.** |
| `boundary` | 2 | hranice mezi experty ze 4 kroků. **Na 3 = víc pohybu.** |
| `shift` | 5.0 | **Výš (6–8) = klidnější**, míň deformací obličeje. |

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
