
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
