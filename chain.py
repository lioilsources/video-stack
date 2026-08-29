#!/usr/bin/env python3
"""chain.py — dlouhé video z jednoho obrázku řetězením I2V segmentů.

Wan 2.2 umí najednou nejvýš 81 snímků (5.06 s @ 16 fps, konec trénovacího okna)
a `length` musí být 4n+1, protože VAE komprimuje čas 4:1. Delší stopáž se dělá
návazně: poslední snímek segmentu N je vstupním obrázkem segmentu N+1. Střih je
tím pádem neviditelný — sdílený snímek se při montáži zahodí.

Dvě věci, bez kterých se řetěz rozpadne:

1. Předávaný snímek se ukládá jako **PNG** (node 22), ne z vp9. Seed z lossy
   videa se po třech skocích projeví jako měkký, zašuměný obraz.
2. Na předávaný snímek jde **ColorMatchV2** proti původnímu obrázku (node 21).
   Bez toho každý skok posune barvy a expozici a drift se kumuluje.

Manifest je buď plochý seznam `beats`, nebo `scenes` — pojmenované skupiny
beatů. `id` a `seed` se dopočítají (seed = kořenový `seed` + pořadí beatu),
scéna může přepsat `style_tail` a `negative`. Číslování segNN/seedNN je
globální přes celý řetěz, scény ho nemění.

    ./chain.py chains/idle01.json --plan       # časová osa a odhad, bez GPU
    ./chain.py chains/idle01.json --validate   # kontrola proti /object_info
    ./chain.py chains/idle01.json --materialize # zapiš beatNN.json k ruční úpravě
    ./chain.py chains/idle01.json --all        # render + slepení + RIFE naráz
    ./chain.py chains/idle01.json              # jen render všech beatů
    ./chain.py chains/idle01.json --from 02    # od beatu (nebo scény) dál
    ./chain.py chains/idle01.json --until idle # skonči po beatu/scéně — náhled
    ./chain.py chains/idle01.json --resume     # od prvního nehotového beatu
    ./chain.py chains/idle01.json --hd         # větší rozlišení
    ./chain.py chains/idle01.json --assemble   # slepení (ffmpeg, bez GPU)
    ./chain.py chains/idle01.json --smooth     # RIFE 16 -> 32 fps po scénách
"""
import argparse, glob, hashlib, json, math, os, shutil, subprocess, sys, time
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "http://localhost:8188"
COMFY = os.path.expanduser("~/Code/ComfyUI")
IN, OUT = os.path.join(COMFY, "input"), os.path.join(COMFY, "output")

# Rozpočty pixelů podle naměřené matice (reports/phase3_bench.md):
# 832x480 = 399k -> 290 s/81f, 1280x704 = 901k -> 806 s/81f.
BUDGET = {"draft": 442368, "hd": 995328}
SEC_PER_MPX = 333  # naměřeno 26. 8.: 140–155 s @ 0.445 Mpx / 81 snímků

# Trénované pásmo poměru stran Wan 2.2 I2V: 9:16 až 16:9. Mimo něj model kreslí,
# ale kompozice ujíždí (zdvojené končetiny, protažený obličej).
ASPECT_BAND = (9 / 16, 16 / 9)


def die(msg):
    sys.exit("chain.py: " + msg)


# ---------------------------------------------------------------- manifest

def load(path):
    m = json.load(open(path))
    m.setdefault("fps", 16)
    m.setdefault("length", 81)
    m.setdefault("seed", 42)
    m.setdefault("negative", "blurry, static, low quality, watermark, text")
    m.setdefault("style_tail", "")
    m.setdefault("colormatch", {"method": "mkl", "strength": 0.6})
    m.setdefault("base", "i2v_final_14b_lightning_portrait")
    m.setdefault("crossfade", 1)
    check_length(m["length"], "manifest")
    if not isinstance(m["crossfade"], int) or not 1 <= m["crossfade"] <= 16:
        die("crossfade %r: čekám 1–16 snímků (1 = tvrdý střih)" % m["crossfade"])

    # Scény se zploští do m["beats"]; plochý manifest je jedna bezejmenná scéna,
    # takže dál jede všechno jednou cestou. Co beat nemá, zdědí ze scény, pak
    # z kořene manifestu.
    scenes = m.get("scenes")
    if not scenes:
        if not m.get("beats"):
            die("manifest nemá žádné beats ani scenes")
        scenes = [{"name": "", "beats": m["beats"]}]
    beats, m["scenes"] = [], []
    for k, s in enumerate(scenes):
        name = s.get("name") or ("scene%d" % (k + 1) if m.get("scenes") else "")
        if not s.get("beats"):
            die("scéna %r nemá žádné beats" % name)
        first = len(beats)
        for b in s["beats"]:
            i = len(beats)
            b.setdefault("id", "%02d" % (i + 1))
            b.setdefault("seed", m["seed"] + i)
            b.setdefault("style_tail", s.get("style_tail", m["style_tail"]))
            b.setdefault("negative", s.get("negative", m["negative"]))
            # tempo se ladí po beatech: délka a knoby pohybu dědí beat ← scéna ← kořen
            for k in ("length", "motion", "boundary", "shift", "sharpen", "identity", "face_denoise"):
                if k in s or k in m:
                    b.setdefault(k, s.get(k, m.get(k)))
            b.setdefault("length", 81)
            check_length(b["length"], "beat %s" % b["id"])
            b["scene"] = name
            if not (b.get("prompt") or "").strip():
                die("beat %s%s nemá prompt" % (b["id"], " (%s)" % name if name else ""))
            beats.append(b)
        m["scenes"].append({"name": name, "first": first, "last": len(beats) - 1})
    ids = [b["id"] for b in beats]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        die("duplicitní id beatů: %s" % ", ".join(dup))
    m["beats"] = beats
    return m


def check_length(L, what):
    if not isinstance(L, int) or L < 5 or (L - 1) % 4:
        die("%s: length %r není 4n+1 (Wan VAE komprimuje čas 4:1)" % (what, L))
    if L > 81:
        die("%s: length %d > 81 — za trénovacím oknem Wan 2.2, klip driftuje" % (what, L))


def frames_upto(m, upto):
    """Počet snímků slepeného klipu po prvních `upto` beatech: první celý,
    každý další kratší o překryv střihu — 1 snímek (sdílený navazovací) při
    tvrdém střihu, `crossfade` snímků při prolínačce."""
    k = m["crossfade"]
    n = 0
    for i, b in enumerate(m["beats"][:upto]):
        n += b["length"] if i == 0 else b["length"] - k
    return n


def resolve(m, token, end=False):
    """Beat id nebo jméno scény → index beatu. U scény první beat, s end=True poslední."""
    ids = [b["id"] for b in m["beats"]]
    if token in ids:
        return ids.index(token)
    for s in m["scenes"]:
        if s["name"] and s["name"] == token:
            return s["last"] if end else s["first"]
    names = ", ".join(s["name"] for s in m["scenes"] if s["name"]) or "—"
    die("%r není beat ani scéna (beaty: %s; scény: %s)" % (token, ", ".join(ids), names))


def source_path(m):
    """Zdrojový obrázek: 'latest:<glob>' se hledá v output/, jinak cesta/jméno."""
    s = m["source"]
    if s.startswith("latest:"):
        hits = glob.glob(os.path.join(OUT, s[7:]))
        if not hits:
            die("žádný soubor nesedí na %r v %s" % (s[7:], OUT))
        return max(hits, key=os.path.getmtime)
    for cand in (s, os.path.join(OUT, s), os.path.join(IN, s)):
        if os.path.exists(cand):
            return cand
    die("zdrojový obrázek %r nenalezen" % s)


def resolution(m, src, tier):
    """Rozměr násobný 16. Klíč "fit" v manifestu řídí poměr stran.

    WanImageToVideo dělá center-crop na zadaný poměr, takže "source" (default)
    poměr zdroje zachová a neuřízne nic — za cenu renderu mimo distribuci
    modelu. "9:16" poměr srovná do trénovaného pásma a nechá node oříznout.

    Wan 2.2 I2V je trénovaný na 480×832 a 720×1280, tedy kolem 9:16. Telefonní
    fotka bývá 19.5:9 = 0.462, což je 18 % pod spodní hranicí, a u řetězených
    beatů se odchylka sčítá — každý beat startuje z výstupu předchozího."""
    if m.get("width") and m.get("height"):
        return m["width"], m["height"]
    from PIL import Image
    w0, h0 = Image.open(src).size
    src_aspect = w0 / h0
    lo, hi = ASPECT_BAND
    fit = m.get("fit", "source")
    if fit not in ("source", "9:16"):
        die("fit: čekám \"source\" nebo \"9:16\", dostal jsem %r" % fit)
    aspect = min(max(src_aspect, lo), hi) if fit == "9:16" else src_aspect

    if not lo <= src_aspect <= hi:
        cut = (100 * (1 - src_aspect / lo) if src_aspect < lo
               else 100 * (1 - hi / src_aspect))
        if fit == "source":
            print("  ! poměr zdroje %.3f je mimo trénované pásmo %.3f-%.3f — "
                  "kompozice může ujíždět. \"fit\": \"9:16\" to srovná, "
                  "ořízne ale %.0f %% (center-crop)." % (src_aspect, lo, hi, cut))
        else:
            print("  ! srovnávám poměr %.3f -> %.3f, center-crop ubere %.0f %%"
                  % (src_aspect, aspect, cut))

    h = math.sqrt(BUDGET[tier] / aspect)
    w = aspect * h
    w, h = max(16, round(w / 16) * 16), max(16, round(h / 16) * 16)
    if w > w0 or h > h0:
        print("  ! zdroj %d×%d je menší než render %d×%d — upscaluje se, "
              "detaily v obličeji tím trpí" % (w0, h0, w, h))
    return w, h


# ---------------------------------------------------------------- graf

def build(m, beat, idx, seed_img, orig_img, w, h):
    """Základní I2V workflow + tři nody navíc pro předání snímku dál."""
    g = json.load(open(os.path.join(HERE, "workflows", m["base"] + ".json")))
    L, name = beat["length"], m["name"]
    cm = m["colormatch"]

    g["8"]["inputs"]["text"] = beat["prompt"] + beat["style_tail"]
    g["9"]["inputs"]["text"] = beat["negative"]
    g["10"]["inputs"]["image"] = seed_img
    g["12"]["inputs"].update(width=w, height=h, length=L)
    if g["12"]["class_type"] == "WanVaceToVideo":
        # VACE báze: control_video = [navazovací snímek, šedé×(L−1)], masky [0, 1×(L−1)];
        # reference_image = originál (node 30 níže) — identita v každém snímku
        for n in ("50", "52", "53"):
            g[n]["inputs"].update(width=w, height=h)
        g["50"]["inputs"]["batch_size"] = g["53"]["inputs"]["batch_size"] = L - 1
    for n in ("13", "14"):
        g[n]["inputs"]["noise_seed"] = beat["seed"]
    if beat.get("boundary") is not None:  # víc kroků v high-noise expertu = víc pohybu
        g["13"]["inputs"]["end_at_step"] = beat["boundary"]
        g["14"]["inputs"]["start_at_step"] = beat["boundary"]
    if beat.get("motion") is not None:  # síla Lightning LoRA na high-noise expertu
        g["2"]["inputs"]["strength_model"] = beat["motion"]
    if beat.get("shift") is not None:
        for n in ("3", "6"):
            g[n]["inputs"]["shift"] = beat["shift"]
    g["16"]["inputs"].update(filename_prefix="%s/seg%s" % (name, beat["id"]),
                             fps=float(m["fps"]), crf=float(m.get("crf", 18)))

    # 30 = původní obrázek jako barevná reference (ne předchozí seed — jinak
    # by se drift jen kopíroval dál místo aby se opravoval)
    g["30"] = {"class_type": "LoadImage", "inputs": {"image": orig_img}}
    g["20"] = {"class_type": "ImageFromBatch",
               "inputs": {"image": ["15", 0], "batch_index": L - 1, "length": 1}}
    g["21"] = {"class_type": "ColorMatchV2",
               "inputs": {"image_target": ["20", 0], "image_ref": ["30", 0],
                          "method": cm["method"], "strength": cm["strength"],
                          "multithread": True}}
    handoff = ["21", 0]
    # Identita: každý beat vidí jen svůj první snímek, který je už o generaci
    # dál — tvář driftuje. Oživení přemaluje jen obličej navazovacího snímku
    # s identitou z ORIGINÁLU (node 30), tělo a pozadí zůstanou.
    if beat.get("identity") == "face":
        handoff = face_refresh(g, handoff, ["30", 0], beat["seed"],
                               denoise=beat.get("face_denoise") or FACE["denoise"])
    # Navazovací snímek je VAE-dekódovaný, tedy měkčí než originál, a další beat
    # z něj startuje — měkkost se sčítá. Doostření je levná protiváha.
    if beat.get("sharpen"):
        g["23"] = {"class_type": "ImageSharpen",
                   "inputs": {"image": handoff, "sharpen_radius": 1,
                              "sigma": 1.0, "alpha": float(beat["sharpen"])}}
        handoff = ["23", 0]
    g["22"] = {"class_type": "SaveImage",
               "inputs": {"images": handoff,
                          "filename_prefix": "%s/seed%02d" % (name, idx + 1)}}
    return g


# Oživení tváře: SDXL FaceDetailer s IPAdapter FaceID PlusV2 — stejný recept,
# kterým Ol1nLLM repose drží tvář z předlohy (comfyui_service.dart). Lightning
# checkpoint, ať to na beat stojí sekundy, ne minuty.
FACE = {
    "ckpt": "Juggernaut-XL-Lightning_4Steps.safetensors",
    "bbox": "bbox/face_yolov8m.pt",
    "steps": 6, "cfg": 1.5, "sampler": "euler", "scheduler": "sgm_uniform",
    "denoise": 0.4, "lora": 0.6, "weight": 0.8, "weight_v2": 1.0,
    "positive": "close-up of the same person's face, photorealistic, natural skin texture, "
                "sharp detailed eyes, consistent identity",
    "negative": "blurry, deformed face, cartoon, painting, extra eyes, text, watermark",
}


def face_refresh(g, image, ref, seed, base=40, denoise=None):
    """Přidá do grafu nody 40–46: obličej v `image` přemaluje FaceDetailer
    s identitou z `ref`. Vrací odkaz na výsledný obrázek."""
    n = lambda k: str(base + k)
    g[n(0)] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": FACE["ckpt"]}}
    g[n(1)] = {"class_type": "IPAdapterUnifiedLoaderFaceID",
               "inputs": {"model": [n(0), 0], "preset": "FACEID PLUS V2",
                          "lora_strength": FACE["lora"], "provider": "CPU"}}
    g[n(2)] = {"class_type": "IPAdapterFaceID",
               "inputs": {"model": [n(1), 0], "ipadapter": [n(1), 1], "image": ref,
                          "weight": FACE["weight"], "weight_faceidv2": FACE["weight_v2"],
                          "weight_type": "linear", "combine_embeds": "concat",
                          "start_at": 0.0, "end_at": 1.0, "embeds_scaling": "V only"}}
    g[n(3)] = {"class_type": "CLIPTextEncode", "inputs": {"clip": [n(0), 1], "text": FACE["positive"]}}
    g[n(4)] = {"class_type": "CLIPTextEncode", "inputs": {"clip": [n(0), 1], "text": FACE["negative"]}}
    g[n(5)] = {"class_type": "UltralyticsDetectorProvider", "inputs": {"model_name": FACE["bbox"]}}
    g[n(6)] = {"class_type": "FaceDetailer",
               "inputs": {"image": image, "model": [n(2), 0], "clip": [n(0), 1], "vae": [n(0), 2],
                          "guide_size": 512.0, "guide_size_for": True, "max_size": 1024.0,
                          "seed": seed, "steps": FACE["steps"], "cfg": FACE["cfg"],
                          "sampler_name": FACE["sampler"], "scheduler": FACE["scheduler"],
                          "positive": [n(3), 0], "negative": [n(4), 0],
                          "denoise": denoise or FACE["denoise"], "feather": 5, "noise_mask": True,
                          "force_inpaint": True, "bbox_threshold": 0.5, "bbox_dilation": 10,
                          "bbox_crop_factor": 3.0, "sam_detection_hint": "center-1",
                          "sam_dilation": 0, "sam_threshold": 0.93, "sam_bbox_expansion": 0,
                          "sam_mask_hint_threshold": 0.7, "sam_mask_hint_use_negative": "False",
                          "drop_size": 10, "bbox_detector": [n(5), 0], "wildcard": "", "cycle": 1}}
    return [n(6), 0]


# ---------------------------------------------------------------- API

def submit(g, label):
    req = urllib.request.Request(API + "/prompt", json.dumps({"prompt": g}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        pid = json.load(urllib.request.urlopen(req))["prompt_id"]
    except urllib.error.HTTPError as e:
        body = json.load(e)
        die("%s odmítnut: %s" % (label, json.dumps(body.get("node_errors") or body)[:900]))
    t0 = time.time()
    while True:
        time.sleep(5)
        hist = json.load(urllib.request.urlopen(API + "/history/" + pid))
        if pid in hist:
            st = hist[pid]["status"]
            if st.get("completed"):
                print("  ok %s  %.0f s" % (label, time.time() - t0), flush=True)
                return hist[pid]["outputs"]
            if st.get("status_str") == "error":
                die("%s spadl: %s" % (label, json.dumps(st.get("messages"))[:900]))
        if time.time() - t0 > 5400:
            die("%s běží přes 90 min, přestávám čekat" % label)


def outfile(outputs, node, ext):
    for im in outputs.get(node, {}).get("images", []):
        if im["filename"].endswith(ext):
            return os.path.join(OUT, im.get("subfolder", ""), im["filename"])
    die("node %s nevrátil žádný %s" % (node, ext))


# ---------------------------------------------------------------- stav řetězu

def state_path(m):
    return os.path.join(OUT, m["name"], "state.json")


def beat_hash(m, b, w, h):
    """Otisk všeho, co ovlivní segment i navazovací snímek. Změna = přegenerovat."""
    key = (b["prompt"], b["style_tail"], b["negative"], b["seed"], w, h, b["length"],
           m["base"], b.get("motion"), b.get("boundary"), b.get("shift"), b.get("sharpen"),
           b.get("identity"), b.get("face_denoise"), m["colormatch"])
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


def load_state(m):
    try:
        return json.load(open(state_path(m)))
    except (OSError, ValueError):
        return {}


def save_state(m, st):
    os.makedirs(os.path.dirname(state_path(m)), exist_ok=True)
    json.dump(st, open(state_path(m), "w"), indent=1)


def resume_index(m, w, h):
    """První beat, který není hotový: chybí segment, chybí navazovací snímek,
    nebo se od renderu změnilo zadání (otisk ve state.json)."""
    st = load_state(m)
    for i, b in enumerate(m["beats"]):
        seg = glob.glob(os.path.join(OUT, m["name"], "seg%s_*.webm" % b["id"]))
        nxt = os.path.join(IN, "%s_seed%02d.png" % (m["name"], i + 1))
        if not seg or not os.path.exists(nxt) or st.get(b["id"]) != beat_hash(m, b, w, h):
            return i
    return len(m["beats"])


# ---------------------------------------------------------------- příkazy

def cmd_plan(m, src, w, h):
    fps = m["fps"]
    per_frame = SEC_PER_MPX * (w * h / 1e6) / 81   # naměřeno na 81 snímcích, škáluje lineárně
    est = lambda beats: sum(per_frame * b["length"] for b in beats)
    print("zdroj      %s" % src)
    print("rozlišení  %d×%d  (%.2f Mpx)" % (w, h, w * h / 1e6))
    print("beat       %s snímků @ %d fps, ~%.0f s GPU na 81 snímků"
          % ("/".join(sorted({str(b["length"]) for b in m["beats"]})), fps, per_frame * 81))
    for s in m["scenes"]:
        beats = m["beats"][s["first"]:s["last"] + 1]
        if s["name"]:
            t0, t1 = frames_upto(m, s["first"]) / fps, frames_upto(m, s["last"] + 1) / fps
            print("\n%-10s %s–%s  %.1f–%.1f s  ~%.0f min GPU"
                  % (s["name"], beats[0]["id"], beats[-1]["id"], t0, t1, est(beats) / 60))
        else:
            print()
        for i in range(s["first"], s["last"] + 1):
            b = m["beats"][i]
            start = frames_upto(m, i) / fps
            print("  beat %-4s %6.2f–%5.2f s  %2d sn  seed%02d → seed%02d  %s"
                  % (b["id"], start, frames_upto(m, i + 1) / fps, b["length"],
                     i, i + 1, b["prompt"][:52]))
    n = len(m["beats"])
    frames = frames_upto(m, n)
    print("\ncelkem     %d snímků = %.2f s @ %d fps" % (frames, frames / fps, fps))
    print("odhad GPU  %.0f min (bez fronty; --cache-none načítá modely znovu)"
          % (est(m["beats"]) / 60))
    if n > 6:
        print("  ! %d beatů = %d generací za sebou. Colormatch drží barvy, ne identitu — "
              "po renderu porovnej input/%s_seed%02d.png s originálem." % (n, n, m["name"], n))


def beat_path(m, beat):
    return os.path.join(HERE, "workflows", "chain-" + m["name"], "beat%s.json" % beat["id"])


def cmd_materialize(m, src, w, h):
    """Zapíše workflows/chain-<name>/beatNN.json — plnohodnotné API workflow
    s DOSAZENÝMI hodnotami, žádné placeholdery.

    Otevře se v ComfyUI přetažením na plátno (sidebar API formát nekonvertuje,
    na to je api2ui.py). Když soubor existuje, render ho použije beze změny —
    takže co si v ComfyUI doladíš ručně, to se pak i vyrenderuje."""
    d = os.path.dirname(beat_path(m, m["beats"][0]))
    os.makedirs(d, exist_ok=True)
    orig = "%s_seed00.png" % m["name"]
    for i, b in enumerate(m["beats"]):
        g = build(m, b, i, "%s_seed%02d.png" % (m["name"], i), orig, w, h)
        path = beat_path(m, b)
        with open(path, "w") as fh:
            json.dump(g, fh, indent=1, ensure_ascii=False)
        print("  %-34s %d×%d  %s" % (os.path.relpath(path, HERE), w, h, b["prompt"][:40]))
    print("  vstupní obrázek se čeká v ComfyUI input/ jako %s" % orig)


def cmd_validate(m, src, w, h):
    info = json.load(urllib.request.urlopen(API + "/object_info"))
    bad = 0
    for i, b in enumerate(m["beats"]):
        g = build(m, b, i, "x.png", "y.png", w, h)
        for nid, node in g.items():
            ct = node["class_type"]
            if ct not in info:
                print("  x beat %s node %s: neznámý typ %s" % (b["id"], nid, ct)); bad += 1; continue
            missing = set(info[ct]["input"].get("required", {})) - set(node["inputs"])
            if missing:
                print("  x beat %s node %s (%s): chybí %s"
                      % (b["id"], nid, ct, sorted(missing))); bad += 1
    print("  ok — všechny beaty validní" if not bad else "  %d problém(ů)" % bad)
    return bad == 0


def cmd_render(m, src, w, h, start, stop, hd):
    """Beaty [start, stop). Každý startuje z navazovacího snímku předchozího."""
    name, beats = m["name"], m["beats"]
    orig = "%s_seed00.png" % name
    shutil.copy(src, os.path.join(IN, orig))
    if start >= stop:
        print("  nic k renderu — beaty %s–%s jsou hotové" % (beats[0]["id"], beats[stop - 1]["id"]))
        return
    if start:
        need = os.path.join(IN, "%s_seed%02d.png" % (name, start))
        if not os.path.exists(need):
            die("chybí %s — beat %s navazuje na předchozí, spusť napřed ty"
                % (os.path.basename(need), beats[start]["id"]))
        print("  od beatu %s (%s hotové); co je za ním se přegeneruje taky — mění se navazující snímek"
              % (beats[start]["id"], "%s–%s" % (beats[0]["id"], beats[start - 1]["id"])))

    st = load_state(m)
    for i in range(start, stop):
        b = beats[i]
        seed_img = "%s_seed%02d.png" % (name, i)
        path = beat_path(m, b)
        if os.path.exists(path) and not hd:
            g = json.load(open(path))       # ručně doladěná verze má přednost
            note = " (z %s)" % os.path.relpath(path, HERE)
        else:
            g = build(m, b, i, seed_img, orig, w, h)
            note = ""
        label = "beat %s%s%s" % (b["id"], " %s" % b["scene"] if b["scene"] else "", note)
        outputs = submit(g, label)
        nxt = os.path.join(IN, "%s_seed%02d.png" % (name, i + 1))
        shutil.copy(outfile(outputs, "22", ".png"), nxt)
        print("     %s  →  %s" % (os.path.basename(outfile(outputs, "16", ".webm")),
                                  os.path.basename(nxt)))
        st[b["id"]] = beat_hash(m, b, w, h)
        # co je za právě vyrenderovaným beatem, stojí na starém navazovacím
        # snímku — pro --resume už neplatí
        for later in beats[i + 1:]:
            st.pop(later["id"], None)
        save_state(m, st)
    print("hotovo — slep to přes --assemble")


def segments(m, upto):
    out = []
    for b in m["beats"][:upto]:
        hits = sorted(glob.glob(os.path.join(OUT, m["name"], "seg%s_*.webm" % b["id"])))
        if not hits:
            die("chybí vyrenderovaný segment pro beat %s" % b["id"])
        out.append(max(hits, key=os.path.getmtime))
    return out


def concat(files, fps, dst, xfade=1, lengths=None):
    """ffmpeg spojení vstupů, které sdílejí hraniční snímek.

    Vstup N+1 začíná přesně tím snímkem, kterým vstup N končí (sdílený
    navazovací snímek). xfade=1: tvrdý střih, první snímek každého dalšího
    vstupu se zahodí (select=gte(n,1)), jinak by spoj zadrhl o zdvojený
    snímek. xfade>1: posledních k snímků N se prolne s prvními k snímky N+1
    (ffmpeg xfade) — skok z re-encode navazovacího snímku a resetu pohybu se
    rozloží do k snímků; `lengths` = počty snímků vstupů, kvůli offsetům.
    Platí pro segmenty i pro RIFE chunky."""
    if xfade > 1:
        assert lengths and len(lengths) == len(files)
        parts = ["[%d:v]setpts=PTS-STARTPTS,fps=%d[v%d];" % (i, fps, i) for i in range(len(files))]
        prev, out_frames = "[v0]", lengths[0]
        for i in range(1, len(files)):
            off = (out_frames - xfade) / fps
            parts.append("%s[v%d]xfade=transition=fade:duration=%.4f:offset=%.4f[x%d];"
                         % (prev, i, xfade / fps, off, i))
            prev, out_frames = "[x%d]" % i, out_frames + lengths[i] - xfade
        fc = "".join(parts)[:-1].replace("[x%d]" % (len(files) - 1), "[out]") \
            if len(files) > 1 else "[0:v]setpts=PTS-STARTPTS[out]"
    else:
        parts, labels = [], []
        for i, _ in enumerate(files):
            sel = "" if i == 0 else "select=gte(n\\,1),"
            parts.append("[%d:v]%ssetpts=N/%d/TB[v%d];" % (i, sel, fps, i))
            labels.append("[v%d]" % i)
        fc = "".join(parts) + "".join(labels) + "concat=n=%d:v=1:a=0[out]" % len(files)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for f in files:
        cmd += ["-i", f]
    cmd += ["-filter_complex", fc, "-map", "[out]", "-r", str(fps),
            "-c:v", "libx264", "-crf", "16", "-preset", "slow", "-pix_fmt", "yuv420p", dst]
    subprocess.run(cmd, check=True)
    return dst


def nframes(path):
    return int(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                               "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
                              capture_output=True, text=True).stdout.strip())


def full_path(m):
    return os.path.join(OUT, m["name"], "%s_full.mp4" % m["name"])


def cmd_assemble(m, upto):
    segs, fps = segments(m, upto), m["fps"]
    dst = concat(segs, fps, full_path(m), xfade=m["crossfade"],
                 lengths=[b["length"] for b in m["beats"][:upto]])
    n = nframes(dst)
    print("  %s  %d snímků = %.2f s @ %d fps" % (dst, n, n / fps, fps))
    return dst


def cmd_smooth(m, upto):
    """RIFE po scénách s překryvem jednoho snímku, pak concat.

    Celý klip naráz by u dlouhé scény neprošel pamětí — VHS_LoadVideo drží
    všechny snímky v RAM a RIFE k tomu dvojnásobek. Chunk končí snímkem, kterým
    další začíná, takže dvojice přes hranici scén se interpoluje taky. RIFE ×2
    vrací 2F−1 snímků (poslední originál jednou), po zahození prvního snímku
    každého dalšího chunku vyjde 2N−1 — totéž co jednorázový průchod."""
    full = full_path(m)
    if not os.path.exists(full):
        die("nejdřív --assemble")
    fps = m["fps"]
    staged = "%s_full.mp4" % m["name"]
    shutil.copy(full, os.path.join(IN, staged))
    chunks = []
    for s in m["scenes"]:
        a, b = s["first"], min(s["last"], upto - 1)
        if a > b:
            break                                   # scéna celá za --until
        # chunk začíná posledním snímkem předchozího beatu (sdílený) a končí
        # posledním snímkem beatu b
        skip = 0 if a == 0 else frames_upto(m, a) - 1
        cap = frames_upto(m, b + 1) - skip
        g = json.load(open(os.path.join(HERE, "workflows", "upscale_interp.json")))
        g["1"]["inputs"].update(video=staged, skip_first_frames=skip, frame_load_cap=cap)
        g["2"]["inputs"]["scale_by"] = float(m.get("upscale", 1.0))
        g["4"]["inputs"].update(filename_prefix="%s/rife_%s" % (m["name"], s["name"] or "all"),
                                fps=float(fps * 2), crf=20.0)
        label = "RIFE %d→%d fps%s" % (fps, fps * 2, " " + s["name"] if s["name"] else "")
        chunks.append(outfile(submit(g, label), "4", ".webm"))
    dst = concat(chunks, fps * 2, os.path.join(OUT, m["name"], "%s_%dfps.mp4" % (m["name"], fps * 2)))
    n = nframes(dst)
    print("  %s  %d snímků = %.2f s @ %d fps" % (dst, n, n / (fps * 2), fps * 2))
    return dst


def cmd_all(m, src, w, h, start, stop, hd):
    """Render → slepení → RIFE na jeden zátah. Co si obvykle přeješ."""
    cmd_render(m, src, w, h, start, stop, hd)
    full = cmd_assemble(m, stop)
    final = cmd_smooth(m, stop)
    print("\nHOTOVO")
    print("  %d fps  %s" % (m["fps"], full))
    print("  %d fps  %s" % (m["fps"] * 2, final))
    return final


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--plan", action="store_true", help="časová osa a odhad, bez GPU")
    ap.add_argument("--validate", action="store_true", help="kontrola proti /object_info")
    ap.add_argument("--materialize", action="store_true",
                    help="zapiš workflows/chain-<name>/beatNN.json, bez renderu")
    ap.add_argument("--from", dest="start", metavar="BEAT|SCÉNA",
                    help="renderuj od beatu / scény dál (vše za ním se přegeneruje)")
    ap.add_argument("--beats", dest="start", help=argparse.SUPPRESS)      # starší jméno
    ap.add_argument("--until", metavar="BEAT|SCÉNA",
                    help="skonči po beatu / scéně; slepení a RIFE jen přes tenhle prefix")
    ap.add_argument("--resume", action="store_true",
                    help="pokračuj od prvního nehotového beatu (output/<name>/state.json)")
    ap.add_argument("--hd", action="store_true", help="~1 Mpx místo ~0.44 Mpx")
    ap.add_argument("--assemble", action="store_true", help="slepení, bez GPU")
    ap.add_argument("--smooth", action="store_true", help="RIFE 2× fps po scénách")
    ap.add_argument("--all", action="store_true",
                    help="render + slepení + RIFE naráz")
    a = ap.parse_args()

    m = load(a.manifest)
    stop = resolve(m, a.until, end=True) + 1 if a.until else len(m["beats"])
    if a.assemble:
        cmd_assemble(m, stop); sys.exit(0)
    if a.smooth:
        cmd_smooth(m, stop); sys.exit(0)
    src = source_path(m)
    w, h = resolution(m, src, "hd" if a.hd else "draft")
    if a.start and a.resume:
        die("--from a --resume se vylučují")
    start = 0
    if a.start:
        start = resolve(m, a.start)
        if start >= stop:
            die("--from %s je za --until %s" % (a.start, a.until))
    elif a.resume:
        start = resume_index(m, w, h)
    if a.plan:
        cmd_plan(m, src, w, h)
    elif a.materialize:
        cmd_materialize(m, src, w, h)
    elif a.validate:
        sys.exit(0 if cmd_validate(m, src, w, h) else 1)
    elif a.all:
        cmd_all(m, src, w, h, start, stop, a.hd)
    else:
        cmd_render(m, src, w, h, start, stop, a.hd)
