# spark-video — 15s klip z jednoho obrázku, bez klikání v ComfyUI

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
další generace navíc, takže se pomalu ztrácí podobnost s originálem. Nad ~6
beatů je lepší udělat víc kratších klipů a sestříhat je.

### Prompty

`prompt` + `style_tail` se slepí dohromady. Dvě věci, které dělají práci:

- **`static camera`** ve `style_tail` — bez toho si Wan rád začne najíždět a odjíždět.
- **Popisuj oblouk, ne stav.** „raises one hand… **then lowers it**" je důvod,
  proč se ruka na konci vrátí dolů a další segment nezačíná uprostřed gesta.

Negativní prompt je při `cfg 1.0` **neúčinný** — na množství pohybu se sahá jinde.

### Když je pohybu málo nebo moc

Množství pohybu řídí *high-noise expert*. Volitelné klíče v manifestu:

| klíč | default | co dělá |
|---|---|---|
| `motion` | 1.0 | síla Lightning LoRA na high-noise expertu. **Níž (0.8) = víc pohybu.** |
| `boundary` | 2 | hranice mezi experty ze 4 kroků. **Na 3 = víc pohybu.** |
| `shift` | 5.0 | **Výš (6–8) = klidnější**, míň deformací obličeje. |

## Ostatní

**Rozlišení** se počítá z poměru stran zdroje (Wan dělá center-crop, takže se
poměr trefuje, neohýbá). Default ~0.45 Mpx, `spark-video run <jméno> --hd`
zhruba 1 Mpx — ale je to ~3× pomalejší.

**Čas:** ~150 s na segment, 15s klip ~7 min GPU. Na SPARKu ale běží fronta, takže
když tam někdo pouští SDXL dávku, wall-clock je delší.

**Přegenerovat jeden beat** (typicky když třetí segment nesedí) jde jen přímo
na SPARKu, kvůli navazujícím snímkům:

```bash
ssh spark 'cd Code/video-stack && ./chain.py chains/mojevideo.json --beats 02'
```

Přegeneruje beat 02 **a všechny následující** — navazují na sebe.

**Ručně doladit workflow v ComfyUI** (když už se klikat chce):

```bash
ssh spark 'cd Code/video-stack && ./chain.py chains/mojevideo.json --materialize'
```

Zapíše `workflows/chain-mojevideo/beatNN.json`. Ty se dají otevřít v ComfyUI
**přetažením na plátno** (ze sidebaru ne — API formát ComfyUI nekonvertuje, na to
je `api2ui.py`) a `chain.py` je pak při renderu použije beze změny.

**Jiný stroj:** `SPARK_HOST=jinyhost spark-video run …`, jiná datová složka:
`SPARK_VIDEO_DIR=~/jinde`.
