
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
`bin/spark-video`: nahraje obrázek a manifest, spustí `chain.py --all`, převede
výsledek a stáhne ho.

```bash
spark-video new  mojevideo ~/Pictures/fotka.png
spark-video edit mojevideo
spark-video plan mojevideo
spark-video run  mojevideo
```

Nic se neinstaluje a nebuilduje — je to bash a git drží bit spustitelnosti.
Skript si najde kořen repa sám, takže `./bin/spark-video ls` funguje odkudkoliv;
kdo ho chce mít pod rukou, přidá si `bin/` do PATH.

Manifesty jsou v `chains/` (stejné, které čte `chain.py`) a výstupy v `out/`,
což je v `.gitignore`. V home nezůstává nic. Podrobnosti v [bin/README.md](bin/README.md).

## chain.py — 15s klip z jednoho obrázku

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
./chain.py chains/idle01.json --beats 02   # beat 02 a všechny následující
./chain.py chains/idle01.json --hd         # ~1 Mpx místo ~0.44 Mpx
./chain.py chains/idle01.json --assemble   # slepení (ffmpeg, bez GPU)
./chain.py chains/idle01.json --smooth     # RIFE 16 -> 32 fps přes celek
```

Zdroj pravdy je manifest `chains/<jméno>.json` — jeden beat = jeden segment,
vlastní prompt a seed. Základní workflow (`base`) se čte z `workflows/`
beze změny, chain.py do něj jen dosazuje a přidává tři nody.

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
