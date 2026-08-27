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
SEC_PER_BEAT = 150     # draft 0.44 Mpx / 81 snímků, naměřeno
MAX_BODY = 32 << 20


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- scény

def load_scenes():
    """scenes/<id>.json = kus manifestu (style_tail, negative, scenes[]) + id/label/desc."""
    out = {}
    for p in sorted(glob.glob(os.path.join(SCENES, "*.json"))):
        t = json.load(open(p))
        L, fps = t.get("length", 81), t.get("fps", 16)
        n = sum(len(s["beats"]) for s in t["scenes"])
        out[t["id"]] = {
            "id": t["id"], "label": t["label"], "desc": t.get("desc", ""),
            "beats": n, "seconds": round((L + (n - 1) * (L - 1)) / fps, 1),
            "minutes_est": round(n * SEC_PER_BEAT * L / 81 / 60),
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
        self.wake = threading.Event()
        os.makedirs(JOBS, exist_ok=True)
        for p in glob.glob(os.path.join(JOBS, "*.json")):
            j = json.load(open(p))
            if j["status"] in ("queued", "running"):
                # subprocess umřel se serverem; navazovací snímky sice zůstaly,
                # ale appka čeká na jasnou odpověď, ne na věčné "running"
                j.update(status="error", error="server restartován během renderu — zadej znovu",
                         finished=time.time())
                self._write(j)
            self.jobs[j["id"]] = j
        threading.Thread(target=self._worker, daemon=True).start()

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
            v = {k: j[k] for k in ("id", "scene", "status", "beat", "beats", "phase", "error")}
            if j["status"] == "queued":
                v["position"] = self.queue.index(jid)
            return v

    def result_path(self, jid):
        return os.path.join(OUT, jid, "%s_32fps.mp4" % jid)

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
        free = vram_free_gb()
        if free is not None and free < MIN_FREE_GB:
            self.update(jid, status="error", finished=time.time(),
                        error="GPU paměť obsazená (%.0f GB volno, potřeba %d) — uvolni LLM"
                              % (free, MIN_FREE_GB))
            return
        self.update(jid, status="running", phase="render", started=time.time())
        cmd = [sys.executable, os.path.join(HERE, "chain.py"),
               os.path.join("chains", jid + ".json"), "--all"]
        log("job", jid, "start:", " ".join(cmd[1:]))
        p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
        tail = []
        for line in p.stdout:
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
        rc = p.wait()
        if rc == 0 and os.path.exists(self.result_path(jid)):
            log("job", jid, "hotovo")
            self.update(jid, status="done", phase=None, finished=time.time())
        else:
            err = next((l for l in reversed(tail) if l.startswith("chain.py:")), None) \
                or (tail[-1] if tail else "chain.py skončil s kódem %d" % rc)
            log("job", jid, "chyba:", err)
            self.update(jid, status="error", error=err[:300], finished=time.time())


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
            return self._json(200, {"scenes": [public(s) for s in SCENE_CATALOG.values()]})
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
    log("video-api :%d, scény: %s" % (PORT, ", ".join(SCENE_CATALOG)))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
