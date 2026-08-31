#!/usr/bin/env python3
"""serve.py — HTTP job server nad chain.py pro Ol1nLLM Image Studio („Rozhýbat").

    GET  /health
    GET  /v1/video/scenes                  katalog scén ze scenes/*.json
    POST /v1/video/jobs                    {scene, image: base64, seed?} → 202 {job_id, …}
    GET  /v1/video/jobs/<id>               {status, position, beat, beats, phase, error}
    GET  /v1/video/jobs/<id>/result        video/mp4

Render trvá minuty a je to sekvence ComfyUI promptů s předáváním snímku, ffmpeg
a RIFE — to nemá řídit telefon. Orchestraci dělá chain.py jako subprocess,
tenhle server jen drží frontu a stav. Jeden worker: GPU je sériová. Stav jobu
je v jobs/<id>.json a výsledek je durable soubor v ComfyUI output/, takže
appka může job sledovat i po restartu serveru nebo telefonu.

Bez závislostí mimo stdlib + PIL (kvůli chain.py se stejně jede z ComfyUI venv).
Přístup řeší CF Access na hostname, server sám nic neautentizuje — nesmí
poslouchat na veřejné adrese bez tunelu.
"""
import base64, glob, io, json, os, re, subprocess, sys, threading, time, uuid
import urllib.request
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
SEC_PER_FRAME_LTX = 0.8
MAX_BODY = 32 << 20


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
            # klient podle toho scénu odliší: zvuk umí zatím jen LTX engine
            "audio": t.get("engine") == "ltx",
            # control beat (VACE + reference) je ~1.5× I2V (190–205 s vs 130 s)
            "minutes_est": max(1, round(
                (SEC_LOAD_LTX + sum(SEC_PER_FRAME_LTX * L for L in lens)) / 60
                if t.get("engine") == "ltx" else
                sum(SEC_PER_BEAT * L / 81 * (1.5 if c else 1.0)
                    for L, c in zip(lens, beat_controls(t))) / 60)),
            "_tpl": t,
        }
    return out


def public(scene):
    return {k: v for k, v in scene.items() if not k.startswith("_")}


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
        requeue = []
        for p in sorted(glob.glob(os.path.join(JOBS, "*.json")), key=os.path.getmtime):
            j = json.load(open(p))
            if j["status"] == "running":
                # chain.py je samostatný proces — restart serveru ho nezabije.
                # Když ještě běží, jen se k němu znovu připojíme a hlídáme ho;
                # teprve když není, je to opravdu utržený job.
                pid = chain_pid(j["id"])
                if pid:
                    log("job", j["id"], "běží dál (pid %d), připojuji se" % pid)
                    threading.Thread(target=self._watch, args=(j["id"], pid), daemon=True).start()
                elif os.path.exists(self.result_path(j["id"])):
                    j.update(status="done", phase=None, finished=time.time()); self._write(j)
                else:
                    # chain.py umřel (typicky s rourou na stdout starého serveru);
                    # state.json a navazovací snímky jsou na disku → --resume
                    self.resume.add(j["id"]); requeue.insert(0, j["id"])
                    j.update(status="queued", phase=None); self._write(j)
            elif j["status"] == "queued":
                requeue.append(j["id"])                # manifest i obrázek jsou na disku
            self.jobs[j["id"]] = j
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

    def _watch(self, jid, pid):
        """Osiřelý chain.py po restartu serveru: postup podle souborů, konec podle pid."""
        while True:
            segs = glob.glob(os.path.join(OUT, jid, "seg[0-9][0-9]_*.webm"))
            done = os.path.exists(self.result_path(jid))
            full = os.path.exists(os.path.join(OUT, jid, "%s_full.mp4" % jid))
            self.update(jid, beat=len({os.path.basename(s)[:5] for s in segs}),
                        phase="rife" if full else ("assemble" if len(segs) >= self.jobs[jid]["beats"] else "render"))
            if not pid_alive(pid):
                break
            time.sleep(10)
        if os.path.exists(self.result_path(jid)):
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
            if j["status"] != "done" and os.path.exists(self.result_path(jid)):
                j.update(status="done", phase=None, error=None, finished=time.time()); self._write(j)
            v = {k: j[k] for k in ("id", "scene", "status", "beat", "beats", "phase", "error")}
            if j["status"] == "queued":
                v["position"] = self.queue.index(jid)
            return v

    def result_path(self, jid):
        """Finální soubor jobu. Wan končí RIFE (`_32fps.mp4`), LTX je plynulé
        nativně a končí slepením (`_full.mp4`) — vrací se ten, který existuje."""
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
        self.update(jid, status="running", phase="render", started=time.time())
        resume = jid in self.resume
        self.resume.discard(jid)
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
                if m:
                    self.update(jid, beat=int(m.group(1)))
                elif "hotovo — slep" in line or "nic k renderu" in line:
                    self.update(jid, phase="assemble")
                elif "_full.mp4" in line:
                    self.update(jid, phase="rife")
            if rc is not None:
                break
            time.sleep(5)
        if rc == 0 and os.path.exists(self.result_path(jid)):
            log("job", jid, "hotovo")
            self.update(jid, status="done", phase=None, finished=time.time())
        else:
            err = next((l for l in reversed(tail) if l.startswith("chain.py:")), None) \
                or (tail[-1] if tail else "chain.py skončil s kódem %d" % rc)
            log("job", jid, "chyba:", err)
            self.update(jid, status="error", error=err[:300], finished=time.time())


def chain_pid(jid):
    """PID běžícího chain.py pro tenhle job (podle manifestu v argv), nebo None."""
    needle = "chains/%s.json" % jid
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            argv = open("/proc/%s/cmdline" % d, "rb").read().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(b"chain.py") for a in argv) and needle.encode() in argv:
            return int(d)
    return None


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
            return self._json(200, {"scenes": [public(s) for s in items]})
        m = re.fullmatch(PREFIX + r"/jobs/([0-9a-f]{8})(/result)?", path)
        if not m:
            return self._err(404, "neznámá cesta")
        jid, want_result = m.group(1), bool(m.group(2))
        v = JOBSTORE.view(jid)
        if v is None:
            return self._err(404, "job neexistuje")
        if not want_result:
            return self._json(200, v)
        if v["status"] != "done":
            return self._err(409, "job není hotový (%s)" % v["status"])
        p = JOBSTORE.result_path(jid)
        size = os.path.getsize(p)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(p, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != PREFIX + "/jobs":
            return self._err(404, "neznámá cesta")
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= MAX_BODY:
            return self._err(413 if n > MAX_BODY else 400, "tělo požadavku chybí nebo je moc velké")
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            return self._err(400, "tělo není JSON")
        scene = SCENE_CATALOG.get(body.get("scene"))
        if not scene:
            return self._err(400, "neznámá scéna %r (znám: %s)"
                             % (body.get("scene"), ", ".join(SCENE_CATALOG)))
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
        m = {k: v for k, v in scene["_tpl"].items() if k not in ("id", "label", "desc")}
        m.update(name=jid, source="%s_src.png" % jid, seed=seed, _scene=scene["id"])
        os.makedirs(CHAINS, exist_ok=True)
        json.dump(m, open(os.path.join(CHAINS, jid + ".json"), "w"), indent=2, ensure_ascii=False)
        JOBSTORE.submit(jid, scene, seed)
        log("job", jid, "přijat: scéna", scene["id"], "%dx%d" % im.size, "seed", seed)
        self._json(202, {"job_id": jid, "beats": scene["beats"], "seconds": scene["seconds"],
                         "minutes_est": scene["minutes_est"]})


if __name__ == "__main__":
    SCENE_CATALOG = load_scenes()
    if not SCENE_CATALOG:
        sys.exit("serve.py: žádné scény v %s" % SCENES)
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
