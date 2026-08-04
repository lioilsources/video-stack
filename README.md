
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
