# video-stack

<!-- Max 80 řádků. Co se nevejde, jde do docs/ a sem jeden odkaz. Veřejné repo: žádné LAN IP, domovské cesty, ID. -->

## Účel
Video z obrázku na SPARKu přes ComfyUI (Wan 2.2, experimentálně LTX-2.3): workflow v API formátu,
dlouhé klipy řetězením (`chain.py`), minutové anime příběhy (`tools/story.py`) a job server
`serve.py` pro Ol1nLLM Image Studio a StoryStudio.

## Build a test
Nic se nebuilduje (Python skripty + bash). Kontroly bez GPU, gate před pushem:
```
python3 tools/check_scenes.py
python3 tools/check_stories.py
```
Proti běžícímu ComfyUI (na SPARKu): `./validate.py --quiet`, `./chain.py chains/<x>.json --validate`.
Bez GPU: `./chain.py chains/<x>.json --plan`, `./api2ui.py --selftest`.

## Mapa
- `workflows/` — API workflow pro ComfyUI (zdroj pravdy; UI kopie generuje `api2ui.py`)
- `chains/` — manifesty pro `chain.py` (beat = segment)
- `scenes/`, `stories/` — katalog scén a příběhů pro `serve.py`
- `tools/` — `story.py`, kontroly katalogu, pomocné skripty
- `bin/spark-video` — celý běh z Macu (viz `bin/README.md`)
- `deploy/` — systemd unit `video-api` (na SPARKu jede uživatelská varianta)
- `ads/kiran/` — reklamní spot, `reports/` — audity a benchmarky po fázích
- `out/` — výstupy, v `.gitignore`

## Invarianty
- GPU na SPARKu jen přes `sparkctl` (globální pravidla); `video-api` je user unit.
- Po změně `serve.py`, scény nebo příběhu restart `video-api` (katalog se čte při startu).
- Wan: `length` 4n+1, max 81 snímků na segment; LTX: `length` 8n+1.

## Kam dál
README.md (podrobně ke každému nástroji), `bin/README.md`, `ANIME_STORY_PLAN.md`, `reports/`.
