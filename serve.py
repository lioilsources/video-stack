#!/usr/bin/env python3
"""serve.py — HTTP job server nad chain.py pro Ol1nLLM Image Studio („Rozhýbat").

    GET  /health
    GET  /v1/video/scenes                  katalog scén ze scenes/*.json (+ custom: meze vlastního promptu)
    POST /v1/video/jobs                    {scene, image: base64, seed?} → 202 {job_id, …}
                                           {prompt, beats?, image, seed?} = vlastní pohyb místo scény
    GET  /v1/video/jobs/<id>               {status, position, beat, beats, phase, error}
    GET  /v1/video/jobs/<id>/result        video/mp4

Příběhy (StoryStudio; tools/story.py — minutový anime příběh ze záběrů):

    GET  /v1/video/stories                 katalog ze stories/*.json, role postav
    POST /v1/video/stories/jobs            {story, characters: {role: base64}, seed?, lang?, review?, hd?}
    GET  /v1/video/jobs/<id>               + kind, story, stage, keyframe/keyframes, status "review"
    GET  /v1/video/jobs/<id>/keyframes     záběry s texty a URL keyframů (po fázi keyframes)
    GET  /v1/video/jobs/<id>/keyframes/NN  image/jpeg
    GET  /v1/video/jobs/<id>/contact       image/jpeg — kontaktní arch
    POST /v1/video/jobs/<id>/approve       {} = render; {redo: ["03"], keyframe: {"07": "nový popis"}} = přegenerovat
    GET  /v1/video/jobs/<id>/result?variant=sub|16x9   video/mp4 (bez variant = s hlasem a hudbou)

Render trvá minuty a je to sekvence ComfyUI promptů s předáváním snímku, ffmpeg
a RIFE — to nemá řídit telefon. Orchestraci dělá chain.py jako subprocess,
tenhle server jen drží frontu a stav. Jeden worker: GPU je sériová. Stav jobu
je v jobs/<id>.json a výsledek je durable soubor v ComfyUI output/, takže
appka může job sledovat i po restartu serveru nebo telefonu.

Bez závislostí mimo stdlib + PIL (kvůli chain.py se stejně jede z ComfyUI venv).
Přístup řeší CF Access na hostname, server sám nic neautentizuje — nesmí
poslouchat na veřejné adrese bez tunelu.
"""
import base64, glob, io, json, os, re, shutil, subprocess, sys, threading, time, uuid
import urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
COMFY = os.path.expanduser("~/Code/ComfyUI")
IN, OUT = os.path.join(COMFY, "input"), os.path.join(COMFY, "output")
SCENES, CHAINS, JOBS = (os.path.join(HERE, d) for d in ("scenes", "chains", "jobs"))
API = "http://localhost:8188"
PORT = int(os.environ.get("VIDEO_API_PORT", "8096"))
PREFIX = "/v1/video"
MIN_FREE_GB = 12       # pod tím ComfyUI (reserve-vram 8) odloží model na CPU a jede hodinu
SEC_PER_BEAT = 150     # Wan, draft 0.44 Mpx / 81 snímků, naměřeno
# LTX-2.3 jede v hd (0.99 Mpx) a i tak je ~6x rychlejší než Wan na jednotku
# práce. Naměřeno 31. 8. 2026: 121 snímků v hd 90 s SE ZAHŘÁTÝM modelem, ale
# 173 s ze studena — 27GB checkpoint se načítá ~70 s a u krátkého klipu je to
# většina času, takže se počítá zvlášť. Radši nadhodnotit: uživatel čeká.
SEC_LOAD_LTX = 70
# 0.8 bylo podle izolovaných benchů; v minutové scéně, kde se střídá LTX
# s FLUXem, vyšly beaty na 205–310 s / 185 snímků (31. 8., job 4c6b25aa) —
# tedy ~1.0 a rostoucí, jak docházela paměť. Po explicitním uvolňování modelů
# (chain.free_models) se to vrací k ~0.9; 1.0 je bezpečný kompromis.
SEC_PER_FRAME_LTX = 1.2   # hd beat 121 sn = 145 s (1. 9., bench_id_original)
# Oživení tváře je u LTX samostatný prompt (FLUX+PuLID se do grafu s 27GB
# checkpointem nevejde), takže na každém handoffu ComfyUI modely vymění.
SEC_REFRESH_LTX = 75
# hudební dárce (soundtrack): 481 sn LTX v malém rozlišení + mux
SEC_TRACK = 300
# příběh: keyframe FLUX Kontext ~50 s, vypravěč + hudba + mix ~3 min
SEC_KEYFRAME = 50
SEC_STORY_AUDIO = 180
SEC_PER_BEAT_HD = 330     # Wan hd ~1 Mpx / 81 snímků
MAX_BODY = 32 << 20

# Vlastní pohyb („Rozhýbat promptem"): scéna poskládaná z promptu uživatele.
# Bez `photorealistic` a bez oživení tváře — obrázek může být anime a PuLID by
# tvář přemaloval na fotku. Stejný prompt na každý beat; Wan ho roztáhne na
# délku beatu, takže víc beatů = pohyb se zopakuje nebo pokračuje.
CUSTOM_MAX_BEATS = 3
CUSTOM_MAX_PROMPT = 500
CUSTOM_TPL = {
    "length": 81, "fps": 16, "crf": 18, "transition": "fade", "crossfade": 6, "bands": 12,
    "style_tail": ", static camera, one slow continuous movement at a calm steady tempo, highly detailed",
    "negative": "blurry, low quality, watermark, text, distorted face, extra fingers, extra limbs, "
                "deformed hands, morphing",
    "colormatch": {"method": "mkl", "strength": 0.6},
}
# Český (i jiný) prompt → anglický popis pohybu ve tvaru, který Wan drží:
# oblouk s koncovou pózou, bez kamery a bez popisu obrázku (bin/README.md).
# LLM na gateway AiStacku; když neběží, jde prompt dál tak, jak ho uživatel napsal.
LLM_GATEWAY = os.environ.get("LLM_GATEWAY", "http://localhost:8080/v1/chat/completions")
PROMPT_MODEL = os.environ.get("VIDEO_PROMPT_MODEL", "shop")
# Příklady drží i malý model gateway (mimo denní režim odpovídá `fallback`):
# bez nich nechával češtinu nebo měnil slovesa („zívne" → „sneezes").
MOTION_SYSTEM = (
    "You write English prompts for an image-to-video model that animates a still image. "
    "Step 1: translate the request into English (it is often Czech) — keep the exact meaning of every verb. "
    "Step 2: describe only the motion as one continuous arc that ends in a still pose. "
    "Never describe the image, style, lighting or camera. Answer with one English sentence, "
    "at most 40 words, nothing else."
)
MOTION_SHOTS = [
    ("pes zavrtí ocasem a vyskočí", "The dog wags its tail, jumps up once, lands and stands still."),
    ("dívka se otočí a usměje",
     "The girl slowly turns her head toward the viewer, smiles warmly and holds the smile."),
    ("drak roztáhne křídla", "The dragon slowly spreads its wings wide, holds them open, then stays still."),
]
_CZECH = re.compile("[ěščřžůňťď]", re.I)


def motion_prompt(text):
    """Prompt uživatele → anglický prompt pohybu; při chybě LLM (nebo když
    odpověď zůstala česky) jde dál původní text — umT5 ve Wanu je vícejazyčný."""
    msgs = [{"role": "system", "content": MOTION_SYSTEM}]
    for u, a in MOTION_SHOTS:
        msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    msgs.append({"role": "user", "content": text})
    body = {"model": PROMPT_MODEL, "max_tokens": 120, "temperature": 0.1, "messages": msgs}
    try:
        req = urllib.request.Request(LLM_GATEWAY, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=20))
        out = d["choices"][0]["message"]["content"].strip().strip('"\u201e\u201c').strip()
        if out and len(out) <= 600 and not _CZECH.search(out):
            return out
        log("prompt: LLM vrátilo %r, beru text uživatele" % out[:80])
    except Exception as e:                                 # noqa: BLE001 — bez LLM jede původní text
        log("prompt: LLM nedostupné (%s), beru text uživatele" % e)
    return text


def custom_scene(beats):
    """Pseudoscéna pro frontu a odhad — stejný tvar jako položka SCENE_CATALOG."""
    k = CUSTOM_TPL["crossfade"]
    frames = 81 * beats - k * (beats - 1)
    return {"id": "custom", "beats": beats, "seconds": round(frames / CUSTOM_TPL["fps"], 1),
            "minutes_est": max(1, round(SEC_PER_BEAT * beats / 60))}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- scény

def beat_lengths(t):
    """Délky beatů se stejným děděním jako v chain.py: beat ← scéna ← kořen ← 81."""
    return [b.get("length", s.get("length", t.get("length", 81)))
            for s in t["scenes"] for b in s["beats"]]


def beat_controls(t):
    return [bool(b.get("control")) for s in t["scenes"] for b in s["beats"]]


def load_scenes():
    """scenes/<id>.json = kus manifestu (style_tail, negative, scenes[]) + id/label/desc.

    label/desc jsou česky (Ol1nLLM), label_en/desc_en anglicky (TsumikiBot pro
    globální trh). Klient si vybere; kdo _en nezná, dostane češtinu jako dřív —
    proto se _en posílá vždycky, i když je to jen kopie češtiny."""
    out = {}
    for p in sorted(glob.glob(os.path.join(SCENES, "*.json"))):
        t = json.load(open(p))
        fps = t.get("fps", 16)
        lens = beat_lengths(t)
        k = t.get("crossfade", 1)                            # překryv na střihu (1 = sdílený snímek)
        frames = lens[0] + sum(L - k for L in lens[1:])
        out[t["id"]] = {
            "id": t["id"], "label": t["label"], "desc": t.get("desc", ""),
            "label_en": t.get("label_en", t["label"]),
            "desc_en": t.get("desc_en", t.get("desc", "")),
            "beats": len(lens), "seconds": round(frames / fps, 1),
            # klient podle toho scénu odliší: zvuk = LTX engine nebo hudební dárce
            "audio": t.get("engine") == "ltx" or bool(t.get("soundtrack")),
            # control beat (VACE + reference) je ~1.5× I2V (190–205 s vs 130 s)
            "minutes_est": max(1, round(
                ((SEC_LOAD_LTX + sum(SEC_PER_FRAME_LTX * L for L in lens)
                  + (SEC_REFRESH_LTX * (len(lens) - 1) if t.get("identity") == "face" else 0))
                 if t.get("engine") == "ltx" else
                 sum(SEC_PER_BEAT * L / 81 * (1.5 if c else 1.0)
                     for L, c in zip(lens, beat_controls(t))))
                / 60 + (SEC_TRACK / 60 if t.get("soundtrack") else 0))),
            "_tpl": t,
        }
    return out


def public(scene):
    return {k: v for k, v in scene.items() if not k.startswith("_")}


def story_mod():
    """tools/story.py jako modul (normalizace, časová osa, brána) — líně, jako chain."""
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import story
    return story


def load_stories():
    """stories/<id>.json → katalog. Rozbitý příběh se zaloguje a vynechá,
    server kvůli němu nespadne (scény jedou dál)."""
    story = story_mod()
    out = {}
    for p in sorted(glob.glob(os.path.join(HERE, "stories", "*.json"))):
        try:
            raw = json.load(open(p))
            st = story.normalize(json.loads(json.dumps(raw)), p)
            m = story.load_manifest_dict(story.manifest_dict(st, story.Work("catalog")))
        except (SystemExit, ValueError, KeyError) as e:
            log("příběh", os.path.basename(p), "vynechán:", e)
            continue
        tl = story.timeline(st, m)
        units = sum(b["length"] / 81 * (1.5 if b.get("control") else 1.0) for b in m["beats"])
        est = lambda per: max(1, round((len(st["shots"]) * SEC_KEYFRAME + units * per + SEC_STORY_AUDIO) / 60))
        out[st["id"]] = {
            "id": st["id"], "title": st.get("title", st["id"]), "title_en": st.get("title_en", st.get("title", "")),
            "desc": st.get("desc", ""), "desc_en": st.get("desc_en", st.get("desc", "")),
            "setting": st.get("setting", ""),
            "shots": len(st["shots"]), "beats": len(m["beats"]), "seconds": round(tl["total"], 1),
            "minutes_est": est(SEC_PER_BEAT), "minutes_est_hd": est(SEC_PER_BEAT_HD),
            "audio": True, "languages": list(story.LANGS),
            "_order": raw.get("order", 100), "_raw": raw, "_st": st,
        }
    return out


def public_story(entry):
    """Katalogová položka pro klienta. `default` = server má pro roli obrázek
    (vygenerovaný nebo obsazený přes story.py), takže ji appka nemusí posílat."""
    story = story_mod()
    st = entry["_st"]
    work = story.Work("story_" + st["id"])
    v = {k: v for k, v in entry.items() if not k.startswith("_")}
    v["characters"] = [{"role": r, "name": c["name"], "name_en": c["name_en"], "desc": c["desc"],
                        "look": c.get("look", ""), "hero": n == 0,
                        "default": bool(story.char_path(st, work, r))}
                       for n, (r, c) in enumerate(st["characters"].items())]
    # scénář k přečtení před spuštěním: děj česky, vypravěč v obou jazycích
    v["script"] = [{"id": sh["id"], "chars": sh["chars"], "action": sh.get("action", ""),
                    "narration": sh.get("narration", ""), "narration_en": sh.get("narration_en", ""),
                    "control": sh.get("control"), "camera": sh.get("camera"), "beats": sh["beats"]}
                   for sh in st["shots"]]
    return v


def story_dir(jid):
    return os.path.join(OUT, jid, "story")


def decode_image(b64):
    """base64 (i data URI) → PIL obrázek, průhlednost na bílé; ValueError při nesmyslu."""
    from PIL import Image
    if "," in b64[:40] and b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        im = Image.open(io.BytesIO(base64.b64decode(b64)))
        im.load()
    except Exception as e:                                 # noqa: BLE001
        raise ValueError("obrázek musí být base64 PNG/JPEG") from e
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    return im.convert("RGB")


# ---------------------------------------------------------------- joby

class Jobs:
    """Stav jobů v paměti + write-through do jobs/<id>.json, FIFO fronta, jeden worker."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}
        self.queue = []                       # ids ve stavu queued, v pořadí
        self.resume = set()                   # ids, které se mají spustit s --resume (navázat na state.json)
        self.wake = threading.Event()
        os.makedirs(JOBS, exist_ok=True)
        requeue, watch = [], []
        for p in sorted(glob.glob(os.path.join(JOBS, "*.json")), key=os.path.getmtime):
            j = json.load(open(p))
            if j["status"] == "running":
                # chain.py je samostatný proces — restart serveru ho nezabije.
                # Když ještě běží, jen se k němu znovu připojíme a hlídáme ho;
                # teprve když není, je to opravdu utržený job.
                pid = job_pid(j["id"])
                if pid:
                    log("job", j["id"], "běží dál (pid %d), připojuji se" % pid)
                    watch.append((j["id"], pid))       # vlákno až po načtení — _watch čte self.jobs
                elif j.get("kind") == "story" and j.get("stage") in ("keyframes", "redo") \
                        and self._keyframes_ready(j):
                    j.update(status="review", phase="review"); self._write(j)
                elif j.get("stage") not in ("keyframes", "redo") and os.path.exists(self.result_path(j["id"], j)):
                    j.update(status="done", phase=None, finished=time.time()); self._write(j)
                else:
                    # chain.py umřel (typicky s rourou na stdout starého serveru);
                    # state.json a navazovací snímky jsou na disku → --resume
                    self.resume.add(j["id"]); requeue.insert(0, j["id"])
                    j.update(status="queued", phase=None); self._write(j)
            elif j["status"] == "queued":
                requeue.append(j["id"])                # manifest i obrázek jsou na disku
            self.jobs[j["id"]] = j
        for jid, pid in watch:
            threading.Thread(target=self._watch, args=(jid, pid), daemon=True).start()
        self.queue.extend(requeue)
        if requeue:
            log("znovu ve frontě:", ", ".join(requeue))
            self.wake.set()
        threading.Thread(target=self._worker, daemon=True).start()

    def _resume_later(self, jid):
        """Utržený job (chain.py umřel bez výsledku) dopředu fronty s --resume."""
        with self.lock:
            self.resume.add(jid)
            if jid not in self.queue:
                self.queue.insert(0, jid)
            self.jobs[jid].update(status="queued", phase=None, error=None)
            self._write(self.jobs[jid])
        self.wake.set()

    def _keyframes_ready(self, j):
        return all(os.path.exists(os.path.join(story_dir(j["id"]), "kf", "%02d.png" % (n + 1)))
                   for n in range(j.get("shots", 0)))

    def _watch(self, jid, pid):
        """Osiřelý chain.py / story.py po restartu serveru: postup podle souborů, konec podle pid."""
        story_job = self.jobs[jid].get("kind") == "story"
        while True:
            segs = glob.glob(os.path.join(OUT, jid, "seg[0-9][0-9]_*.webm"))
            full = os.path.exists(os.path.join(OUT, jid, "%s_full.mp4" % jid))
            beat = len({os.path.basename(s)[:5] for s in segs})
            if story_job:                      # fáze příběhu se ze souborů nepoznají — jen postup renderu
                self.update(jid, beat=beat)
            else:
                self.update(jid, beat=beat,
                            phase="rife" if full else ("assemble" if len(segs) >= self.jobs[jid]["beats"] else "render"))
            if not pid_alive(pid):
                break
            time.sleep(10)
        j = self.jobs[jid]
        if story_job and j.get("stage") in ("keyframes", "redo"):
            if self._keyframes_ready(j):
                log("job", jid, "keyframy hotové (po restartu serveru)")
                self.update(jid, status="review", phase="review")
            else:
                self._resume_later(jid)
        elif os.path.exists(self.result_path(jid)):
            log("job", jid, "hotovo (po restartu serveru)")
            self.update(jid, status="done", phase=None, finished=time.time())
        else:
            log("job", jid, "chain.py skončil bez výsledku → --resume")
            self._resume_later(jid)

    def _write(self, j):
        tmp = os.path.join(JOBS, j["id"] + ".json.tmp")
        json.dump(j, open(tmp, "w"), indent=1)
        os.replace(tmp, os.path.join(JOBS, j["id"] + ".json"))

    def update(self, jid, **kw):
        with self.lock:
            j = self.jobs[jid]
            j.update(kw)
            self._write(j)

    def new_id(self):
        return uuid.uuid4().hex[:8]

    def submit(self, jid, scene, seed):
        """Zařadí job. Vstupy (obrázek, manifest) už musí ležet na disku — worker
        může sáhnout po manifestu hned, jak se job objeví ve frontě."""
        j = {"id": jid, "scene": scene["id"], "status": "queued", "beat": 0,
             "beats": scene["beats"], "phase": None, "error": None, "seed": seed,
             "created": time.time(), "started": None, "finished": None}
        return self.enqueue(j)

    def submit_story(self, jid, entry, seed, lang, review, hd):
        """Příběh: `stage` keyframes (review=true → po keyframech stav review a
        čeká na /approve) nebo all (rovnou až do hotového videa)."""
        j = {"id": jid, "kind": "story", "story": entry["id"], "status": "queued",
             "stage": "keyframes" if review else "all", "review": review, "hd": hd, "lang": lang,
             "shots": entry["shots"], "keyframe": 0, "keyframes": entry["shots"], "redo": [],
             "beat": 0, "beats": entry["beats"], "phase": None, "error": None, "seed": seed,
             "created": time.time(), "started": None, "finished": None}
        return self.enqueue(j)

    def requeue(self, jid, **kw):
        """Příběh po review: znovu do fronty s novou fází (render nebo přegenerování)."""
        with self.lock:
            j = self.jobs[jid]
            j.update(kw, status="queued", phase=None, error=None, finished=None)
            if jid not in self.queue:
                self.queue.append(jid)
            self._write(j)
        self.wake.set()
        return j

    def enqueue(self, j):
        jid = j["id"]
        with self.lock:
            self.jobs[jid] = j
            self.queue.append(jid)
            self._write(j)
        self.wake.set()
        return j

    def view(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                return None
            if j["status"] not in ("done", "review") and j.get("stage") not in ("keyframes", "redo") \
                    and os.path.exists(self.result_path(jid, j)):
                j.update(status="done", phase=None, error=None, finished=time.time()); self._write(j)
            keys = ("id", "scene", "status", "beat", "beats", "phase", "error")
            if j.get("kind") == "story":
                keys = ("id", "kind", "story", "stage", "status", "keyframe", "keyframes", "shots",
                        "beat", "beats", "phase", "error", "lang", "review", "hd")
            v = {k: j.get(k) for k in keys}
            if j["status"] == "queued" and jid in self.queue:
                v["position"] = self.queue.index(jid)
            return v

    def result_path(self, jid, j=None, variant=""):
        """Finální soubor jobu. Wan končí RIFE (`_32fps.mp4`), LTX je plynulé
        nativně a končí slepením (`_full.mp4`) — vrací se ten, který existuje.
        Příběh končí mixem: `<id>_story_<lang>[_sub|_16x9].mp4`."""
        j = j or self.jobs.get(jid) or {}
        if j.get("kind") == "story":
            return os.path.join(OUT, jid, "%s_story_%s%s.mp4" % (jid, j.get("lang", "cs"),
                                                                  "_" + variant if variant else ""))
        d = os.path.join(OUT, jid)
        rife = os.path.join(d, "%s_32fps.mp4" % jid)
        if os.path.exists(rife):
            return rife
        full = os.path.join(d, "%s_full.mp4" % jid)
        return full if os.path.exists(full) else rife

    # ---- worker

    def _worker(self):
        while True:
            self.wake.wait()
            with self.lock:
                if not self.queue:
                    self.wake.clear()
                    continue
                jid = self.queue.pop(0)
            try:
                self._run(jid)
            except Exception as e:                         # noqa: BLE001 — job nesmí shodit worker
                log("job", jid, "spadl:", repr(e))
                self.update(jid, status="error", error=str(e)[:300], finished=time.time())

    def _run(self, jid):
        import chain
        chain.drop_page_cache()
        free = vram_free_gb()
        if free is not None and free < MIN_FREE_GB:
            self.update(jid, status="error", finished=time.time(),
                        error="GPU paměť obsazená (%.0f GB volno, potřeba %d) — uvolni LLM"
                              % (free, MIN_FREE_GB))
            return
        j = self.jobs[jid]
        story_job = j.get("kind") == "story"
        self.update(jid, status="running", phase=None if story_job else "render", started=time.time())
        resume = jid in self.resume
        self.resume.discard(jid)
        if story_job:
            sj = os.path.join(story_dir(jid), "story.json")
            base = [sys.executable, os.path.join(HERE, "tools", "story.py")]
            if j["stage"] == "redo":
                cmd = base + ["keyframes", sj, "--work", jid, "--shot", ",".join(j["redo"]), "--reroll"]
            else:
                cmd = base + ["run", sj, "--work", jid, "--lang", j["lang"]]
                cmd += (["--until", "review"] if j["stage"] == "keyframes" else [])
                cmd += (["--hd"] if j.get("hd") else []) + ([] if j.get("review") else ["--no-review"])
                cmd += (["--resume"] if resume else [])
            self.update(jid, keyframe=0)
        else:
            cmd = [sys.executable, os.path.join(HERE, "chain.py"),
                   os.path.join("chains", jid + ".json"), "--all"] + (["--resume"] if resume else [])
        log("job", jid, "start:", " ".join(cmd[1:]))
        # stdout do souboru, ne do roury: chain.py musí přežít restart serveru
        # (s rourou umře na BrokenPipe při prvním printu po smrti čtenáře)
        logpath = os.path.join(JOBS, jid + ".log")
        with open(logpath, "a") as fh:
            p = subprocess.Popen(cmd, cwd=HERE, stdout=fh, stderr=subprocess.STDOUT, text=True)
        tail, pos = [], 0
        while True:
            rc = p.poll()
            with open(logpath) as fh:
                fh.seek(pos); chunk = fh.read(); pos = fh.tell()
            for line in chunk.splitlines():
                line = line.rstrip()
                if line:
                    tail = (tail + [line])[-20:]
                m = re.match(r"\s*ok beat (\d+)", line)
                f = re.match(r"== fáze: (\w+)", line)
                if f:
                    ph = f.group(1)
                    kw = {"keyframe": self.jobs[jid]["keyframes"]} if ph not in ("keyframes",) else {}
                    self.update(jid, phase=ph, **kw)
                elif re.match(r"\s*ok keyframe \d+", line):
                    self.update(jid, keyframe=self.jobs[jid].get("keyframe", 0) + 1)
                elif m:
                    self.update(jid, beat=int(m.group(1)))
                elif "hotovo — slep" in line or "nic k renderu" in line:
                    self.update(jid, phase="assemble")
                elif "_full.mp4" in line:
                    self.update(jid, phase="rife")
            if rc is not None:
                break
            time.sleep(5)
        if rc == 0 and story_job and j["stage"] in ("keyframes", "redo"):
            log("job", jid, "keyframy hotové, čeká na schválení")
            self.update(jid, status="review", phase="review", keyframe=j["keyframes"])
        elif rc == 0 and os.path.exists(self.result_path(jid)):
            log("job", jid, "hotovo")
            self.update(jid, status="done", phase=None, finished=time.time())
        else:
            err = next((l for l in reversed(tail) if l.startswith(("chain.py:", "story.py:"))), None) \
                or (tail[-1] if tail else "chain.py skončil s kódem %d" % rc)
            log("job", jid, "chyba:", err)
            self.update(jid, status="error", error=err[:300], finished=time.time())


def job_pid(jid):
    """PID běžícího procesu jobu, nebo None: story.py (--work <id>) má přednost
    před chain.py, který spouští jako dítě; u scény chain.py podle manifestu."""
    needle = ("chains/%s.json" % jid).encode()
    found = None
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            argv = open("/proc/%s/cmdline" % d, "rb").read().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"story.py") for a in argv) and jid.encode() in argv:
            return int(d)
        if any(a.endswith(b"chain.py") for a in argv) and any(a.endswith(needle) for a in argv):
            found = int(d)
    return found


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return os.path.exists("/proc/%d" % pid) and "zombie" not in open("/proc/%d/status" % pid).read().lower()
    except OSError:
        return False


def vram_free_gb():
    try:
        d = json.load(urllib.request.urlopen(API + "/system_stats", timeout=5))
        return d["devices"][0]["vram_free"] / 1e9
    except Exception:                                      # noqa: BLE001 — bez ComfyUI to spadne v chain.py
        return None


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "video-api/1"

    def log_message(self, fmt, *args):                     # vlastní log, ne do stderr per request
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json(code, {"error": msg})

    def _file(self, path, ctype):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _jpeg(self, path):
        from PIL import Image
        buf = io.BytesIO()
        Image.open(path).convert("RGB").save(buf, "JPEG", quality=88)
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= MAX_BODY:
            raise ValueError("tělo požadavku chybí nebo je moc velké")
        return json.loads(self.rfile.read(n))

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/health":
            return self._json(200, {"ok": True, "queued": len(JOBSTORE.queue)})
        if path == PREFIX + "/scenes":
            # Pořadí katalogu = pořadí v appce (klient nic netřídí). Volitelné
            # "order" ve scéně tedy rozhoduje, co uživatel uvidí první; bez něj
            # 100 a pak abecedně jako dřív.
            items = sorted(SCENE_CATALOG.values(),
                           key=lambda s: (s["_tpl"].get("order", 100), s["id"]))
            return self._json(200, {"scenes": [public(s) for s in items],
                                    "custom": {"max_beats": CUSTOM_MAX_BEATS,
                                               "max_prompt": CUSTOM_MAX_PROMPT,
                                               "seconds_per_beat": custom_scene(1)["seconds"],
                                               "minutes_per_beat": custom_scene(1)["minutes_est"]}})
        if path == PREFIX + "/stories":
            items = sorted(STORY_CATALOG.values(), key=lambda s: (s["_order"], s["id"]))
            return self._json(200, {"stories": [public_story(s) for s in items]})
        m = re.fullmatch(PREFIX + r"/jobs/([0-9a-f]{8})(?:/(result|keyframes|contact)(?:/(\d\d))?)?", path)
        if not m:
            return self._err(404, "neznámá cesta")
        jid, what, shot = m.group(1), m.group(2), m.group(3)
        v = JOBSTORE.view(jid)
        if v is None:
            return self._err(404, "job neexistuje")
        if not what:
            return self._json(200, v)
        if what in ("keyframes", "contact"):
            if v.get("kind") != "story":
                return self._err(404, "keyframy má jen příběh")
            d = story_dir(jid)
            if what == "contact":
                p = os.path.join(d, "contact.jpg")
                return self._file(p, "image/jpeg") if os.path.exists(p) else self._err(409, "arch ještě není")
            if shot:
                p = os.path.join(d, "kf", shot + ".png")
                return self._jpeg(p) if os.path.exists(p) else self._err(409, "keyframe %s ještě není" % shot)
            st = json.load(open(os.path.join(d, "story.json")))
            return self._json(200, {"shots": [
                {"id": sh["id"], "chars": sh.get("chars"), "keyframe": sh.get("keyframe"),
                 "action": sh.get("action"),
                 "narration": sh.get("narration"), "narration_en": sh.get("narration_en"),
                 "control": sh.get("control"), "camera": sh.get("camera"),
                 "ready": os.path.exists(os.path.join(d, "kf", sh["id"] + ".png")),
                 "url": "%s/jobs/%s/keyframes/%s" % (PREFIX, jid, sh["id"])} for sh in st["shots"]],
                "contact": "%s/jobs/%s/contact" % (PREFIX, jid)})
        if v["status"] != "done":
            return self._err(409, "job není hotový (%s)" % v["status"])
        variant = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("variant") or [""])[0]
        if variant not in ("", "sub", "16x9"):
            return self._err(400, "variant: sub nebo 16x9")
        p = JOBSTORE.result_path(jid, variant=variant if v.get("kind") == "story" else "")
        if not os.path.exists(p):
            return self._err(404, "varianta %s neexistuje" % (variant or "hlavní"))
        self._file(p, "video/mp4")

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == PREFIX + "/stories/jobs":
            return self._post_story()
        m = re.fullmatch(PREFIX + r"/jobs/([0-9a-f]{8})/approve", path)
        if m:
            return self._approve(m.group(1))
        if path != PREFIX + "/jobs":
            return self._err(404, "neznámá cesta")
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= MAX_BODY:
            return self._err(413 if n > MAX_BODY else 400, "tělo požadavku chybí nebo je moc velké")
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            return self._err(400, "tělo není JSON")
        prompt = (body.get("prompt") or "").strip() if isinstance(body.get("prompt"), str) else ""
        scene = SCENE_CATALOG.get(body.get("scene"))
        if not scene and not prompt:
            return self._err(400, "neznámá scéna %r (znám: %s) a chybí prompt"
                             % (body.get("scene"), ", ".join(SCENE_CATALOG)))
        if not scene and len(prompt) > CUSTOM_MAX_PROMPT:
            return self._err(400, "prompt je delší než %d znaků" % CUSTOM_MAX_PROMPT)
        img = body.get("image") or ""
        if "," in img[:40] and img.startswith("data:"):
            img = img.split(",", 1)[1]                    # data URI → holý base64
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(base64.b64decode(img)))
            im.load()
        except Exception:                                  # noqa: BLE001
            return self._err(400, "image musí být base64 PNG/JPEG")
        seed = body.get("seed")
        seed = int(seed) if isinstance(seed, int) and seed >= 0 else int.from_bytes(os.urandom(4), "big") >> 1

        jid = JOBSTORE.new_id()
        # zdroj jako PNG bez ohledu na to, co přišlo — chain.py source nezávisí na příponě
        im.convert("RGB").save(os.path.join(IN, "%s_src.png" % jid))
        used = None
        if scene:
            m = {k: v for k, v in scene["_tpl"].items() if k not in ("id", "label", "desc")}
            m.update(name=jid, source="%s_src.png" % jid, seed=seed, _scene=scene["id"])
        else:
            beats = body.get("beats")
            beats = min(max(beats if isinstance(beats, int) else 1, 1), CUSTOM_MAX_BEATS)
            used = motion_prompt(prompt)
            scene = custom_scene(beats)
            m = dict(json.loads(json.dumps(CUSTOM_TPL)), name=jid, source="%s_src.png" % jid, seed=seed,
                     _scene="custom", _prompt=prompt,
                     scenes=[{"name": "custom", "beats": [{"prompt": used} for _ in range(beats)]}])
        os.makedirs(CHAINS, exist_ok=True)
        json.dump(m, open(os.path.join(CHAINS, jid + ".json"), "w"), indent=2, ensure_ascii=False)
        JOBSTORE.submit(jid, scene, seed)
        log("job", jid, "přijat: scéna", scene["id"], "%dx%d" % im.size, "seed", seed,
            "prompt %r → %r" % (prompt, used) if used else "")
        self._json(202, {"job_id": jid, "beats": scene["beats"], "seconds": scene["seconds"],
                         "minutes_est": scene["minutes_est"], "prompt": used})

    def _post_story(self):
        """Nový příběh. Postavy: base64 obrázky podle rolí; co chybí, doplní
        server z výchozích (story.py sheet/cast), jinak 400. Všechno se zkopíruje
        do output/<job>/story/, takže job je soběstačný i po změně katalogu."""
        try:
            body = self._body()
        except ValueError as e:
            return self._err(400, "tělo: %s" % e)
        entry = STORY_CATALOG.get(body.get("story"))
        if not entry:
            return self._err(400, "neznámý příběh %r (znám: %s)" % (body.get("story"), ", ".join(STORY_CATALOG)))
        lang = body.get("lang") or entry["_st"].get("lang", "cs")
        if lang not in entry["languages"]:
            return self._err(400, "lang: %s" % " nebo ".join(entry["languages"]))
        story = story_mod()
        st, sent = entry["_st"], body.get("characters") or {}
        unknown = set(sent) - set(st["characters"])
        if unknown:
            return self._err(400, "neznámé role %s (příběh má %s)" % (", ".join(sorted(unknown)), ", ".join(st["characters"])))
        jid = JOBSTORE.new_id()
        chars_dir = os.path.join(story_dir(jid), "chars")
        os.makedirs(chars_dir, exist_ok=True)
        raw = json.loads(json.dumps(entry["_raw"]))
        default_work = story.Work("story_" + st["id"])
        for role, c in st["characters"].items():
            dst = os.path.join(chars_dir, role + ".png")
            if role in sent:
                try:
                    decode_image(sent[role]).save(dst)
                except ValueError as e:
                    shutil.rmtree(os.path.join(OUT, jid), ignore_errors=True)
                    return self._err(400, "postava %s: %s" % (role, e))
            else:
                src = story.char_path(st, default_work, role)
                if not src:
                    shutil.rmtree(os.path.join(OUT, jid), ignore_errors=True)
                    return self._err(400, "chybí postava %s (%s) — pošli obrázek v characters.%s"
                                     % (role, c["name"], role))
                shutil.copy(src, dst)
            raw["characters"][role]["sheet"] = dst
        seed = body.get("seed")
        raw["seed"] = int(seed) if isinstance(seed, int) and seed >= 0 else raw.get("seed", 42)
        raw["lang"] = lang
        json.dump(raw, open(os.path.join(story_dir(jid), "story.json"), "w"), indent=2, ensure_ascii=False)
        review, hd = bool(body.get("review")), bool(body.get("hd"))
        JOBSTORE.submit_story(jid, entry, raw["seed"], lang, review, hd)
        log("job", jid, "přijat: příběh", st["id"], "role", ",".join(sorted(sent)) or "výchozí",
            "lang", lang, "review" if review else "rovnou", "hd" if hd else "draft")
        self._json(202, {"job_id": jid, "shots": entry["shots"], "beats": entry["beats"],
                         "seconds": entry["seconds"],
                         "minutes_est": entry["minutes_est_hd" if hd else "minutes_est"]})

    def _approve(self, jid):
        """Po review: {} = schválit a renderovat; redo/keyframe = přegenerovat
        vybrané záběry (keyframe = nový popis záběru) a znovu do review."""
        j = JOBSTORE.jobs.get(jid)
        if not j or j.get("kind") != "story":
            return self._err(404, "příběh s tímhle id neexistuje")
        if j["status"] != "review":
            return self._err(409, "job není ve stavu review (%s)" % j["status"])
        try:
            body = self._body() if int(self.headers.get("Content-Length") or 0) else {}
        except ValueError as e:
            return self._err(400, str(e))
        sj = os.path.join(story_dir(jid), "story.json")
        raw = json.load(open(sj))
        ids = {sh["id"] for sh in raw["shots"]}
        edits = body.get("keyframe") or {}
        redo = sorted(set(body.get("redo") or []) | set(edits))
        bad = [s for s in redo if s not in ids]
        if bad:
            return self._err(400, "neznámé záběry %s" % ", ".join(bad))
        if edits:
            for sh in raw["shots"]:
                if sh["id"] in edits:
                    if not (edits[sh["id"]] or "").strip():
                        return self._err(400, "záběr %s: prázdný popis" % sh["id"])
                    sh["keyframe"] = edits[sh["id"]].strip()
            json.dump(raw, open(sj, "w"), indent=2, ensure_ascii=False)
        if redo:
            JOBSTORE.requeue(jid, stage="redo", redo=redo, keyframes=len(redo), keyframe=0)
            log("job", jid, "přegenerovat záběry", ",".join(redo))
            return self._json(202, {"status": "queued", "stage": "redo", "redo": redo})
        story = story_mod()
        try:
            st, work = story.load_story(sj, jid)
            story.cmd_approve(st, work)
        except SystemExit as e:
            return self._err(409, str(e))
        JOBSTORE.requeue(jid, stage="all", review=True, keyframe=j["shots"], keyframes=j["shots"])
        log("job", jid, "keyframy schválené → render")
        return self._json(202, {"status": "queued", "stage": "all"})


if __name__ == "__main__":
    SCENE_CATALOG = load_scenes()
    if not SCENE_CATALOG:
        sys.exit("serve.py: žádné scény v %s" % SCENES)
    STORY_CATALOG = load_stories()
    log("katalog: %d scén, %d příběhů" % (len(SCENE_CATALOG), len(STORY_CATALOG)))
    JOBSTORE = Jobs()
    # Page cache ze safetensors vytlačuje CUDA "volnou" paměť i jobům, které jdou
    # do ComfyUI mimo tenhle server (appka, bench). Pouštět ji periodicky je
    # levné (~2 s) a drží GPU render na GPU pro všechny.
    def _cache_janitor():
        import chain
        while True:
            time.sleep(120)
            try:
                chain.drop_page_cache()
            except Exception:                          # noqa: BLE001 — úklid nesmí shodit server
                pass
    threading.Thread(target=_cache_janitor, daemon=True).start()
    log("video-api :%d, scény: %s" % (PORT, ", ".join(SCENE_CATALOG)))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
