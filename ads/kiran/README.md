# Kiran — 30s reklamní spot (Geometry Wars skin)

Kompletní produkční řetěz, celý z herních assetů.

## Postup

1. **Keyframy** (`build_keyframes.sh`, běží na Macu): 10× 832×480 poskládaných
   z reálných spritů `geometry_wars` — vessel, falconi, bouncer, asteroidy,
   exploze — nad parallax vrstvami `backgrounds/layer_*`. Exploze jsou
   sprite sheety 3×3, vyřezává se prostřední políčko (240×240).
2. **Drafty 480p** (`run_kiran_ad.py`): i2v_final_14b_lightning, 49 snímků
   (3.06 s/záběr). ~165 s/záběr po zahřátí.
3. **Revize**: záběry 5 a 9 měly příliš velké exploze (přebily akci), záběr 1
   nedělal orbit → přepnut na camera_control preset ClockWise.
4. **Finále 720p** (`run_kiran_ad_final.py`): 1280×704, ~400 s/záběr.
   Pozn.: camera_control má 20 kroků, v 720p trvá ~33 min — vhodný kandidát
   na Lightning LoRA (na fun_control ověřeno, 4×).
5. **RIFE** (`run_rife.py` + upscale_interp): 16 → 32 fps, ~35 s/záběr.
6. **Montáž** (`assemble_hd.sh`): lanczos 1.5× → 1920×1056, pad na 1080,
   hudba a SFX ze skinu, titulkové PNG přes overlay.

## Audio (vše ze skinu geometry_wars)

| Čas | Stopa |
|---|---|
| 0–9.4 s | `music/intro.ogg` |
| 9.2–16 s | `music/theme_3.ogg` |
| 15.3–28 s | `music/theme_5.ogg` (boss téma) |
| 27.6 s | `sfx/sector_complete.ogg` |
| akcenty | `explosion_large` @13.5 a @24.6, `fire_beam` @21.5, `explosion_small` @25.6 |

Mix: −18 dB mean / −1.7 dB max.

## Poučení

- **ffmpeg na Macu nemá libfreetype** → `drawtext` neexistuje; titulky se
  renderují jako PNG v ImageMagicku a skládají přes `overlay`.
- **RIFE VFI** vyžaduje `dtype`, `torch_compile` a `batch_size`, které nejsou
  v základním workflow — doplněno do upscale_interp.json.
- Sledovat běh podle logu s timeouty je křehké: timeout zabije čekání, ne
  úlohu, a další úloha se zařadí do fronty za tu běžící. Spolehlivější je
  hlídat počet výstupních souborů.

## Reprodukce a úpravy jednotlivých záběrů

Zdroj pravdy je **`shots.json`** — prompty, keyframy, rozlišení, seedy, časová
osa, hudba, SFX i titulky na jednom místě.

```bash
./render.py --materialize      # shots.json -> workflows/shotNN.json
./render.py --validate         # kontrola proti /object_info běžícího ComfyUI
./render.py --shots 05,09      # přerenderuje jen tyhle dva záběry
./render.py --all --draft      # všech 10 v 480p (rychlá iterace)
./render.py --shots 03 --seed 7
```

`workflows/shotNN.json` jsou plnohodnotné API workflow s **dosazenými**
hodnotami — žádné placeholdery. Otevřou se v ComfyUI přetažením na plátno
(sidebar API formát nekonvertuje), dají se ručně doladit a `render.py` je pak
použije beze změny. Kopie jsou i v ComfyUI pod `workflows/video-stack/kiran-ad/`.

`--validate` kontroluje class_type a povinné vstupy proti běžící instanci —
právě tenhle druh chyby shodil RIFE i MMAudio loader.

### Slepení

```bash
./build_keyframes.sh    # keyframy z herních spritů (běží na Macu)
./assemble.py --plan    # vypíše časovou osu, nic nerenderuje
./assemble.py           # 1080p finále: concat + hudba + SFX + titulky
./assemble.py --draft   # 480p varianta z shots/
```

### Délka záběrů

Časová osa se **počítá** z `length` (počet snímků / `fps_render`), v manifestu
žádné `start` není. Změna délky je jediná editace:

```json
"defaults": { "length": 81 }        // všechny záběry na 5.06 s
"shots": [ { "id": "09", "length": 81, ... } ]   // jen tenhle
```

Wan VAE komprimuje čas 4:1, takže **length musí být 4n+1**: 33 (2.06 s),
49 (3.06 s), 65 (4.06 s), 81 (5.06 s). Nad 81 nechodit — konec trénovacího
okna Wan 2.2, delší klipy driftují.

Audio a titulky se kotví symbolicky, takže se posunou s ním:

```json
"at": {"shot": "06"}                  // začátek záběru 6
"at": {"shot": "09", "offset": 0.6}   // 0.6 s po jeho začátku
"to": {"end": true}                   // konec spotu
```

Ověřeno: výstup nového `assemble.py` je proti původnímu ručnímu skriptu
pixel po pixelu identický (PSNR inf).
