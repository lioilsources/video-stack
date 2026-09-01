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
import argparse, glob, hashlib, json, math, os, shutil, subprocess, sys, threading, time
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "http://localhost:8188"
COMFY = os.path.expanduser("~/Code/ComfyUI")
IN, OUT = os.path.join(COMFY, "input"), os.path.join(COMFY, "output")

# Rozpočty pixelů podle naměřené matice (reports/phase3_bench.md):
# 832x480 = 399k -> 290 s/81f, 1280x704 = 901k -> 806 s/81f.
BUDGET = {"draft": 442368, "hd": 995328}
SEC_PER_MPX = 333  # naměřeno 26. 8.: 140–155 s @ 0.445 Mpx / 81 snímků
CACHE_EVERY = int(os.environ.get("CHAIN_CACHE_EVERY", 15))  # s, viz submit()
# Načítání 27GB checkpointu trvá jednotky sekund; při 15 s stihne janitor jeden
# průchod a cache mezitím sežere paměť pro váhy. Přes CHAIN_CACHE_EVERY se dá
# interval stáhnout (průchod models/ stojí ~2 s).

# Control beat: pohyb z kostry řídicího klipu (drive/<id>_pose.webm), vzhled z
# navazovacího snímku — pro pohyby, které Wan z textu neumí (moonwalk…).
CONTROL_BASE = {"fun": "control_beat_14b_lightning_portrait",     # fun_control: ref_image = vzhled
                "vace": "vace_control_14b_lightning_portrait"}    # VACE: reference_image = vzhled, silnější kotva
DRIVE = os.path.join(HERE, "drive")

# LTX-2.3: druhý engine — 22B AV DiT, video + synchronní zvuk jedním průchodem,
# 25 fps, length 8n+1 do 481 (~19 s). Řetězení beatů (seedNN.png), colormatch
# i oživení tváře jedou stejnou cestou (stejná ID nodů 8-16); navíc jde ven
# seg<id>_av.mp4 se zvukem (nody 67-69). RIFE se přeskakuje — 25 fps nativně.
# Union control (kostra) je experiment: 0.19.3 nemá GetICLoRAParameters,
# reference se enkóduje s downscale 1 místo trénovaných 0.5 (viz
# reports/phase5_ltx.md); pořádné IC-LoRA zapojení chce novější ComfyUI.
LTX_BASE = {"i2v": "ltx_i2v_portrait", "control": "ltx_control_portrait"}


def pose_path(cid):
    return os.path.join(DRIVE, "%s_pose.webm" % cid)


# Trénované pásmo poměru stran Wan 2.2 I2V: 9:16 až 16:9. Mimo něj model kreslí,
# ale kompozice ujíždí (zdvojené končetiny, protažený obličej).
ASPECT_BAND = (9 / 16, 16 / 9)


def die(msg):
    sys.exit("chain.py: " + msg)


# ---------------------------------------------------------------- manifest

def load(path):
    return load_dict(json.load(open(path)))


def load_dict(m):
    """Manifest už načtený z JSONu → doplněné defaulty a zploštěné scény.
    Oddělené od load(), ať jde katalog scén zkontrolovat bez souboru
    (tools/check_scenes.py)."""
    m.setdefault("engine", "wan")
    if m["engine"] not in ("wan", "ltx"):
        die("engine %r: čekám wan nebo ltx" % m["engine"])
    ltx = m["engine"] == "ltx"
    m.setdefault("fps", 25 if ltx else 16)
    m.setdefault("length", 121 if ltx else 81)
    m.setdefault("seed", 42)
    m.setdefault("negative", "blurry, static, low quality, watermark, text")
    m.setdefault("style_tail", "")
    m.setdefault("colormatch", {"method": "mkl", "strength": 0.6})
    m.setdefault("base", "i2v_final_14b_lightning_portrait")
    m.setdefault("crossfade", 1)
    m.setdefault("transition", "fade")
    m.setdefault("bands", 12)
    check_length(m["length"], "manifest", m["engine"])
    if not isinstance(m["crossfade"], int) or not 1 <= m["crossfade"] <= 16:
        die("crossfade %r: čekám 1–16 snímků (1 = tvrdý střih)" % m["crossfade"])
    if m["transition"] not in ("cut", "fade", "slices"):
        die("transition %r: čekám cut, fade nebo slices" % m["transition"])
    if m["transition"] == "cut":
        m["crossfade"] = 1
    elif m["crossfade"] < 2:
        m["transition"] = "cut"                      # 1 snímek se prolnout nedá

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
            for k in ("length", "motion", "boundary", "shift", "sharpen", "identity", "face_denoise",
                      "control_ref", "control_model", "beat_ref"):
                if k in s or k in m:
                    b.setdefault(k, s.get(k, m.get(k)))
            b.setdefault("length", 121 if ltx else 81)
            check_length(b["length"], "beat %s" % b["id"], m["engine"])
            if b.get("control"):
                if not os.path.exists(pose_path(b["control"])):
                    die("beat %s: control %r — chybí %s (tools/drive.py pose)"
                        % (b["id"], b["control"], os.path.relpath(pose_path(b["control"]), HERE)))
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


def check_length(L, what, engine="wan"):
    if engine == "ltx":
        if not isinstance(L, int) or L < 9 or (L - 1) % 8:
            die("%s: length %r není 8n+1 (LTX VAE komprimuje čas 8:1)" % (what, L))
        if L > 481:
            die("%s: length %d > 481 — přes ~19 s na klip LTX-2.3 neumí" % (what, L))
        return
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
    q = 32 if m.get("engine") == "ltx" else 16
    w, h = max(q, round(w / q) * q), max(q, round(h / q) * q)
    if w > w0 or h > h0:
        print("  ! zdroj %d×%d je menší než render %d×%d — upscaluje se, "
              "detaily v obličeji tím trpí" % (w0, h0, w, h))
    return w, h


# ---------------------------------------------------------------- graf

def build(m, beat, idx, seed_img, orig_img, w, h):
    """Základní I2V workflow + tři nody navíc pro předání snímku dál."""
    control = beat.get("control")
    ltx = m["engine"] == "ltx"
    if ltx:
        g = json.load(open(os.path.join(HERE, "workflows", LTX_BASE["control" if control else "i2v"] + ".json")))
    else:
        cmodel = beat.get("control_model") or "vace"
        if cmodel not in CONTROL_BASE:
            die("control_model %r: čekám fun nebo vace" % cmodel)
        g = json.load(open(os.path.join(HERE, "workflows", (CONTROL_BASE[cmodel] if control else m["base"]) + ".json")))
    L, name = beat["length"], m["name"]
    cm = m["colormatch"]
    if control and ltx:
        # core LoadVideo neumí frame_load_cap — délku ořeže ImageFromBatch (72)
        g["60"]["inputs"]["file"] = os.path.basename(pose_path(control))
        g["72"]["inputs"]["length"] = L
        g["61"]["inputs"].update(width=w, height=h)
    elif control:
        # kostra do input/ kopíruje cmd_render; tady jen jméno, délka a měřítko
        g["60"]["inputs"].update(video=os.path.basename(pose_path(control)), frame_load_cap=L)
        g["61"]["inputs"].update(width=w, height=h)

    g["8"]["inputs"]["text"] = beat["prompt"] + beat["style_tail"]
    g["9"]["inputs"]["text"] = beat["negative"]
    # Vzhled control beatu: "original" (default) = úvodní fotka — postava, oblečení,
    # boty i místnost se v každém klipu vrátí k originálu (A/B na gangnamu: VACE
    # + originál tvář 0.66 a nejvěrnější tělo; fun_control + handoff 0.53 a bosá).
    # "handoff" = navazovací snímek: kontinuita, ale každý klip zdědí drift.
    # Vstupní snímek beatu. Default je navazovací snímek předchozího beatu —
    # plynulá návaznost, ale beat dědí i jeho drift. `beat_ref: "original"`
    # startuje každý beat z PŮVODNÍ fotky: identita se nekumuluje, za cenu
    # skoku do výchozí pózy (u tance na místě to prolínačka schová). Totéž
    # dělá u Wan control beatů `control_ref: "original"` přes VACE reference.
    from_original = (control and (beat.get("control_ref") or "original") == "original") \
        or beat.get("beat_ref") == "original"
    g["10"]["inputs"]["image"] = orig_img if from_original else seed_img
    g["12"]["inputs"].update(width=w, height=h, length=L)
    if g["12"]["class_type"] == "WanVaceToVideo" and "50" in g:
        # VACE I2V báze: control_video = [navazovací snímek, šedé×(L−1)], masky [0, 1×(L−1)];
        # reference_image = originál (node 30 níže) — identita v každém snímku
        for n in ("50", "52", "53"):
            g[n]["inputs"].update(width=w, height=h)
        g["50"]["inputs"]["batch_size"] = g["53"]["inputs"]["batch_size"] = L - 1
    if ltx:
        g["13"]["inputs"]["seed"] = beat["seed"]
        # zvuková větev: délka a tempo drží krok s videem
        g["63"]["inputs"].update(frames_number=L, frame_rate=m["fps"])
        g["65"]["inputs"]["frame_rate"] = float(m["fps"])
        g["68"]["inputs"]["fps"] = float(m["fps"])
        g["69"]["inputs"]["filename_prefix"] = "%s/seg%s_av" % (name, beat["id"])
        for knob in ("motion", "boundary", "shift"):
            if beat.get(knob) is not None:
                print("  ! beat %s: %s je knob Wan — LTX ho ignoruje" % (beat["id"], knob))
    else:
        for n in ("13", "14"):
            g[n]["inputs"]["noise_seed"] = beat["seed"]
        if beat.get("boundary") is not None:  # víc kroků v high-noise expertu = víc pohybu
            g["13"]["inputs"]["end_at_step"] = beat["boundary"]
            g["14"]["inputs"]["start_at_step"] = beat["boundary"]
        if beat.get("motion") is not None and not control:  # u control beatu pohyb určuje kostra
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
    # U Wan se oživení tváře přilepí rovnou do grafu. U LTX ne: 27GB checkpoint
    # a FLUX+PuLID (~10 GB) se do paměti najednou nevejdou (ComfyUI zaloguje
    # `loaded partially` a beat se plazí na CPU), takže tam běží jako samostatný
    # prompt po dokončení beatu — viz refresh_prompt().
    if beat.get("identity") == "face" and not ltx:
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


# Oživení tváře: FaceDetailer s PuLID na FLUX.1-dev — stejný recept, kterým
# face inpaint v Ol1nLLM drží identitu z reference (flux_fill_inpaint_face
# asset). Nahradil SDXL + IPAdapter FaceID: rovnováha identity přes beaty byla
# ~0.63 ArcFace, PuLID na dev dává ~0.72 (viz reports/phase4_identity.md).
# fp8, protože dev fp16 se vedle Wan neve­jde a lowvram režim shodí PuLID na
# cuda/cpu mismatch; loadery PuLID/EVA/InsightFace jsou vlastní ze stejného
# důvodu (sdílené končí po samplingu offloadnuté na CPU).
FACE = {
    "unet": "flux1-dev.safetensors", "dtype": "fp8_e4m3fn",
    "clip1": "t5xxl_fp16.safetensors", "clip2": "clip_l.safetensors",
    "pulid": "pulid_flux_v0.9.1.safetensors",
    "bbox": "bbox/face_yolov8m.pt",
    "steps": 20, "guidance": 3.5, "denoise": 0.55, "weight": 1.2,
    "positive": "close-up of the same person's face, photorealistic, natural skin texture, "
                "sharp detailed eyes, consistent identity",
}


def face_refresh(g, image, ref, seed, denoise=None):
    """Přidá do grafu nody 40–48 + 57/58: obličej v `image` přemaluje
    FaceDetailer na FLUX dev s identitou z `ref` (PuLID). Vrací odkaz na
    výsledný obrázek."""
    g["40"] = {"class_type": "UNETLoader", "inputs": {"unet_name": FACE["unet"], "weight_dtype": FACE["dtype"]}}
    g["41"] = {"class_type": "DualCLIPLoader",
               "inputs": {"clip_name1": FACE["clip1"], "clip_name2": FACE["clip2"], "type": "flux"}}
    g["42"] = {"class_type": "PulidFluxModelLoader", "inputs": {"pulid_file": FACE["pulid"]}}
    g["43"] = {"class_type": "PulidFluxEvaClipLoader", "inputs": {}}
    g["44"] = {"class_type": "PulidFluxInsightFaceLoader", "inputs": {"provider": "CPU"}}
    g["45"] = {"class_type": "ApplyPulidFlux",
               "inputs": {"model": ["40", 0], "pulid_flux": ["42", 0], "eva_clip": ["43", 0],
                          "face_analysis": ["44", 0], "image": ref, "weight": FACE["weight"],
                          "start_at": 0.0, "end_at": 1.0}}
    g["46"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["41", 0], "text": FACE["positive"]}}
    g["47"] = {"class_type": "FluxGuidance", "inputs": {"conditioning": ["46", 0], "guidance": FACE["guidance"]}}
    g["48"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": ["41", 0], "text": ""}}
    # FLUX má vlastní VAE — node 11 v Wan grafu je wan_2.1_vae a detailer by s ním
    # dekódoval nesmysly
    g["49"] = {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}}
    g["57"] = {"class_type": "UltralyticsDetectorProvider", "inputs": {"model_name": FACE["bbox"]}}
    g["58"] = {"class_type": "FaceDetailer",
               "inputs": {"image": image, "model": ["45", 0], "clip": ["41", 0], "vae": ["49", 0],
                          "guide_size": 1024.0, "guide_size_for": True, "max_size": 1024.0,
                          "seed": seed, "steps": FACE["steps"], "cfg": 1.0,
                          "sampler_name": "euler", "scheduler": "simple",
                          "positive": ["47", 0], "negative": ["48", 0],
                          "denoise": denoise or FACE["denoise"], "feather": 8, "noise_mask": True,
                          "force_inpaint": True, "bbox_threshold": 0.5, "bbox_dilation": 24,
                          "bbox_crop_factor": 2.0, "sam_detection_hint": "center-1",
                          "sam_dilation": 0, "sam_threshold": 0.93, "sam_bbox_expansion": 0,
                          "sam_mask_hint_threshold": 0.7, "sam_mask_hint_use_negative": "False",
                          "drop_size": 10, "bbox_detector": ["57", 0], "wildcard": "", "cycle": 1}}
    return ["58", 0]


def refresh_prompt(beat, seed_png, orig_png, prefix):
    """Samostatný graf: oživí tvář na už uloženém navazovacím snímku.

    Existuje kvůli LTX — do jeho grafu se FLUX+PuLID nevejde. Tím, že jde o
    druhý prompt, ComfyUI mezi nimi model vymění (LTX ven, FLUX dovnitř), takže
    obojí má paměť pro sebe. Cena je to přehození, ~1 min na beat."""
    g = {"30": {"class_type": "LoadImage", "inputs": {"image": orig_png}},
         "31": {"class_type": "LoadImage", "inputs": {"image": seed_png}}}
    out = face_refresh(g, ["31", 0], ["30", 0], beat["seed"],
                       denoise=beat.get("face_denoise") or FACE["denoise"])
    g["22"] = {"class_type": "SaveImage",
               "inputs": {"images": out, "filename_prefix": prefix}}
    return g


# ---------------------------------------------------------------- paměť

def drop_page_cache():
    """GB10 má unified paměť a CUDA hlásí jako volné jen RAM bez page cache.
    Po pár rendrech drží cache ze safetensors desítky GB, ComfyUI vidí ~7 GB
    volno, model offloaduje na CPU a beat trvá hodinu. posix_fadvise DONTNEED
    cache pustí bez roota (naměřeno: 7 → 53 GB volno za 2 s)."""
    n = 0
    for root in (os.path.join(COMFY, "models"), OUT, IN):
        for dp, _, fs in os.walk(root):
            for f in fs:
                try:
                    fd = os.open(os.path.join(dp, f), os.O_RDONLY)
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    os.close(fd)
                    n += 1
                except OSError:
                    pass
    return n


# ---------------------------------------------------------------- API

def free_models(label=""):
    """Řekni ComfyUI, ať pustí načtené modely z paměti.

    Nutné, když se v jednom řetězu střídají dva velké modely — u LTX beatu
    (27 GB) a oživení tváře na FLUXu (12 GB) si je ComfyUI s `--cache-lru 2`
    drží oba, unified paměť dojde a při dalším přepnutí se model vystěhuje na
    CPU (`0.00 MB usable … 23836 MB offloaded, lowvram patches: 1660`,
    naměřeno 31. 8.) — beat pak trvá dvojnásobek. Uvolnění stojí jen znovu-
    načtení z page cache, což je řádově levnější."""
    try:
        req = urllib.request.Request(
            API + "/free", json.dumps({"unload_models": True, "free_memory": True}).encode(),
            {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
    except (urllib.error.URLError, OSError) as e:      # starší ComfyUI /free nemá
        print("  ! uvolnění modelů %s neprošlo: %s" % (label, e))


def submit(g, label):
    """Pošli graf a čekej na výsledek; po celou dobu drž page cache dole.

    Jedno dropnutí před promptem nestačí. Načítání checkpointu je na unified
    paměti dvojí zátěž — bajty jdou jednou do page cache a jednou do vah — a
    než se 27GB LTX dočte, cache spolkne přesně tu paměť, kterou model
    potřebuje: ComfyUI zaloguje `loaded partially … offloaded, lowvram
    patches` a beat se plazí na CPU (naměřeno 31. 8.: vram_free 67 → 9 GB
    během načítání, 10.8 GB odloženo). Vlákno na pozadí proto pouští cache i
    během běhu."""
    drop_page_cache()
    req = urllib.request.Request(API + "/prompt", json.dumps({"prompt": g}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        pid = json.load(urllib.request.urlopen(req))["prompt_id"]
    except urllib.error.HTTPError as e:
        body = json.load(e)
        die("%s odmítnut: %s" % (label, json.dumps(body.get("node_errors") or body)[:900]))
    t0 = time.time()
    stop = threading.Event()

    def janitor():
        while not stop.wait(CACHE_EVERY):
            drop_page_cache()

    threading.Thread(target=janitor, daemon=True).start()
    try:
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
    finally:
        stop.set()


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
           m["engine"], m["fps"],
           m["base"], b.get("motion"), b.get("boundary"), b.get("shift"), b.get("sharpen"),
           b.get("identity"), b.get("face_denoise"), m["colormatch"], b.get("control"),
           b.get("control_ref"), b.get("control_model"), b.get("beat_ref"),
           os.path.getmtime(pose_path(b["control"])) if b.get("control") else None)
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
            print("  beat %-4s %6.2f–%5.2f s  %2d sn  seed%02d → seed%02d  %s%s"
                  % (b["id"], start, frames_upto(m, i + 1) / fps, b["length"],
                     i, i + 1, "[control %s] " % b["control"] if b.get("control") else "",
                     b["prompt"][:52]))
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
        if b.get("control"):
            shutil.copy(pose_path(b["control"]), os.path.join(IN, os.path.basename(pose_path(b["control"]))))
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
        # LTX: oživení tváře až teď, druhým promptem (do jednoho grafu se nevejde)
        # beat_ref: original → další beat handoff ignoruje, oživovat ho je jen ztráta času
        if (m["engine"] == "ltx" and b.get("identity") == "face"
                and b.get("beat_ref") != "original" and i + 1 < len(beats)):
            staged = "%s_pre%02d.png" % (name, i + 1)
            shutil.copy(nxt, os.path.join(IN, staged))
            rg = refresh_prompt(b, staged, orig, "%s/face%02d" % (name, i + 1))
            free_models("před oživením")     # ven s LTX, ať má FLUX kam
            shutil.copy(outfile(submit(rg, "oživení tváře %s" % b["id"]), "22", ".png"), nxt)
            free_models("po oživení")        # ven s FLUXem, ať má LTX kam
            print("     tvář oživena z originálu  →  %s" % os.path.basename(nxt))
        st[b["id"]] = beat_hash(m, b, w, h)
        # co je za právě vyrenderovaným beatem, stojí na starém navazovacím
        # snímku — pro --resume už neplatí
        for later in beats[i + 1:]:
            st.pop(later["id"], None)
        save_state(m, st)
    print("hotovo — slep to přes --assemble")


def segments(m, upto):
    """Soubory segmentů k montáži. U LTX to jsou `seg<id>_av_*.mp4` — nesou i
    vygenerovaný zvuk, kvůli kterému se do LTX jde; němé `seg<id>_*.webm`
    vznikají taky (sleduje je serve.py kvůli postupu), ale hudbu by zahodily."""
    pat = "seg%s_av_*.mp4" if m["engine"] == "ltx" else "seg%s_*.webm"
    out = []
    for b in m["beats"][:upto]:
        hits = sorted(glob.glob(os.path.join(OUT, m["name"], pat % b["id"])))
        if not hits:
            die("chybí vyrenderovaný segment pro beat %s" % b["id"])
        out.append(max(hits, key=os.path.getmtime))
    return out


def slices_expr(bands):
    """xfade custom: obraz ve vodorovných pásech, liché pásy nový obraz vtlačí
    zleva (starý vytlačí doprava), sudé zrcadlově zprava. Střih jako záměr —
    krátký a čitelný, ne neznatelná prolínačka. P jde v xfade od 1 (starý) k 0
    (nový); a0..a3/b0..b3 jsou pixely vstupů per plane, pás se počítá z Y/H
    plane, takže sedí i pro chroma v yuv420p."""
    def px(src, dx):
        return ("if(eq(PLANE,0),{s}0({x},Y),if(eq(PLANE,1),{s}1({x},Y),"
                "if(eq(PLANE,2),{s}2({x},Y),{s}3({x},Y))))").format(s=src, x=dx)
    s = "(W*(1-P))"
    odd = "if(lt(X,%s),%s,%s)" % (s, px("b", "X+W-%s" % s), px("a", "X-%s" % s))
    even = "if(gte(X,W-%s),%s,%s)" % (s, px("b", "X-W+%s" % s), px("a", "X+%s" % s))
    return "if(eq(mod(floor(Y*%d/H),2),0),%s,%s)" % (bands, odd, even)


def concat(files, fps, dst, xfade=1, lengths=None, transition="fade", bands=12,
           with_audio=False):
    """ffmpeg spojení vstupů, které sdílejí hraniční snímek.

    Vstup N+1 začíná přesně tím snímkem, kterým vstup N končí (sdílený
    navazovací snímek). xfade=1: tvrdý střih, první snímek každého dalšího
    vstupu se zahodí (select=gte(n,1)), jinak by spoj zadrhl o zdvojený
    snímek. xfade>1: posledních k snímků N se prolne s prvními k snímky N+1
    (ffmpeg xfade; `transition` fade = prolínačka, slices = pásová
    přejížďka) — skok z re-encode navazovacího snímku a resetu pohybu se
    rozloží do k snímků; `lengths` = počty snímků vstupů, kvůli offsetům.
    Platí pro segmenty i pro RIFE chunky."""
    if xfade > 1:
        assert lengths and len(lengths) == len(files)
        kind = "custom:expr='%s'" % slices_expr(bands) if transition == "slices" else "fade"
        parts = ["[%d:v]setpts=PTS-STARTPTS,fps=%d[v%d];" % (i, fps, i) for i in range(len(files))]
        prev, out_frames = "[v0]", lengths[0]
        for i in range(1, len(files)):
            off = (out_frames - xfade) / fps
            parts.append("%s[v%d]xfade=transition=%s:duration=%.4f:offset=%.4f[x%d];"
                         % (prev, i, kind, xfade / fps, off, i))
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
    # Zvuk: `with_audio` = vstupy ho nesou (LTX), jinak se dolepí ticho. Prolínačka
    # obrazu má svůj protějšek v acrossfade, aby hudba na střihu nelupla; u tvrdého
    # střihu se stopy jen navážou.
    afilter, amap = "", None
    if with_audio:
        if len(files) == 1:
            amap = "0:a"
        elif xfade > 1:
            prev = "[0:a]"
            for i in range(1, len(files)):
                out = "[a%d]" % i
                afilter += "%s[%d:a]acrossfade=d=%.4f:c1=tri:c2=tri%s;" % (prev, i, xfade / fps, out)
                prev = out
            afilter = afilter[:-1].replace("[a%d]" % (len(files) - 1), "[aout]") + ";"
            amap = "[aout]"
        else:
            afilter = "".join("[%d:a]" % i for i in range(len(files))) \
                      + "concat=n=%d:v=0:a=1[aout];" % len(files)
            amap = "[aout]"
        fc = afilter + fc

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for f in files:
        cmd += ["-i", f]
    # Fotky na iOS import odmítaly ("unsupported format"): mp4 bez zvuku, s moov
    # atomem až za daty a bez barevných tagů. Tichá AAC stopa, +faststart a
    # explicitní BT.709 z toho udělají soubor, který Photos přijme bez konverze.
    # iOS Fotky si u nestandardní snímkové frekvence vynutí konverzi, a ta padá
    # („unsupported format" i na jinak korektním H.264+AAC). 32 fps z RIFE proto
    # jde ven jako 30; rozdíl je 6 % snímků, na oko neznatelný.
    out_fps = 30 if fps not in (24, 25, 30, 60) else fps
    if not with_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        amap = "%d:a" % len(files)
    cmd += ["-filter_complex", fc, "-map", "[out]", "-map", amap, "-shortest",
            "-r", str(out_fps), "-vsync", "cfr", "-c:v", "libx264", "-crf", "16", "-preset", "slow",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.0",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", dst]
    subprocess.run(cmd, check=True)
    return dst


def nframes(path):
    return int(subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                               "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
                              capture_output=True, text=True).stdout.strip())


def full_path(m):
    return os.path.join(OUT, m["name"], "%s_full.mp4" % m["name"])


def duration_s(path):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip())


def soundtrack_track(m):
    """Vyrenderuje (nebo vrátí z cache) hudební stopu manifestu: jeden 19s LTX
    AV klip, jehož video se zahodí. Jedna generace = jedna skladba — na rozdíl
    od per-beat audia, kde každý beat složí něco jiného."""
    track = os.path.join(OUT, m["name"], "track.m4a")
    key = hashlib.sha1(json.dumps([m["soundtrack"], m["seed"]]).encode()).hexdigest()[:12]
    st = load_state(m)
    if os.path.exists(track) and st.get("_track") == key:
        return track
    g = json.load(open(os.path.join(HERE, "workflows", LTX_BASE["i2v"] + ".json")))
    g["8"]["inputs"]["text"] = m["soundtrack"]
    g["10"]["inputs"]["image"] = "%s_seed00.png" % m["name"]
    # malé rozlišení: video dárce nikdo neuvidí, jde jen o zvuk
    g["12"]["inputs"].update(width=448, height=896, length=481)
    g["13"]["inputs"]["seed"] = m["seed"]
    g["63"]["inputs"].update(frames_number=481, frame_rate=25)
    g["65"]["inputs"]["frame_rate"] = 25.0
    g["68"]["inputs"]["fps"] = 25.0
    g["16"]["inputs"].update(filename_prefix="%s/track_v" % m["name"], fps=25.0)
    g["69"]["inputs"]["filename_prefix"] = "%s/track" % m["name"]
    free_models("před hudebním dárcem")
    submit(g, "hudební dárce (19 s LTX)")
    free_models("po hudebním dárci")
    hits = glob.glob(os.path.join(OUT, m["name"], "track_0*.mp4"))
    if not hits:
        die("hudební dárce nevrátil mp4 (%s/track_0*.mp4)" % m["name"])
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", max(hits, key=os.path.getmtime), "-vn", "-c:a", "aac", "-b:a", "192k",
                    track], check=True)
    st["_track"] = key
    save_state(m, st)
    return track


def cmd_soundtrack(m, video):
    """Jedna skladba přes celé video: stopa dárce se smyčkou (acrossfade na
    švech) natáhne na délku videa a namuxuje místo dosavadní stopy. Video
    stream se jen kopíruje — žádné překódování, žádné riziko iOS formátu."""
    track = soundtrack_track(m)
    dur, tdur = duration_s(video), duration_s(track)
    xf = 1.0
    n = max(1, math.ceil(1 + max(0.0, dur - tdur) / (tdur - xf)))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", video]
    cmd += ["-i", track] * n
    prev, parts = "[1:a]", []
    for i in range(2, n + 1):
        out = "[am%d]" % i
        parts.append("%s[%d:a]acrossfade=d=%.2f%s;" % (prev, i, xf, out))
        prev = out
    fc = "".join(parts) + "%satrim=0:%.3f,afade=t=out:st=%.3f:d=0.8[aout]" % (prev, dur, max(0.0, dur - 0.8))
    tmp = video[:-4] + ".snd.mp4"
    cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-shortest", tmp]
    subprocess.run(cmd, check=True)
    os.replace(tmp, video)
    print("  hudba: %s  (%.1f s ×%d → %.1f s)" % (os.path.basename(track), tdur, n, dur))
    return video


def cmd_assemble(m, upto):
    segs, fps = segments(m, upto), m["fps"]
    dst = concat(segs, fps, full_path(m), xfade=m["crossfade"],
                 lengths=[b["length"] for b in m["beats"][:upto]],
                 transition=m["transition"], bands=m["bands"],
                 with_audio=m["engine"] == "ltx")
    n = nframes(dst)
    print("  %s  %d snímků = %.2f s @ %d fps" % (dst, n, n / fps, fps))
    if m.get("soundtrack"):
        cmd_soundtrack(m, dst)
    return dst


def cmd_smooth(m, upto):
    """RIFE po beatech, střih až potom.

    RIFE přes slepený klip by interpoloval i snímky střihu — u pásové
    přejížďky z toho jsou rozmazané bloky (ověřeno). Proto se každý segment
    zdvojí zvlášť (81 → 161 snímků; sousední segmenty sdílejí hraniční snímek
    a RIFE ×2 vrací 2F−1, takže ho nic nezdvojí) a přechod se udělá až na
    32 fps: překryv 2k−1 snímků = stejná doba jako k snímků na 16 fps.
    Vedlejší zisk: chunky jsou malé, paměť neroste s délkou klipu."""
    if m["engine"] == "ltx":
        # 25 fps nativně; slepení proběhlo v cmd_assemble a druhý průchod by jen
        # znovu překódoval hotový soubor (a zhoršil ho)
        print("  LTX je nativně plynulé (25 fps) — RIFE se přeskakuje")
        return full_path(m)
    segs, fps = segments(m, upto), m["fps"]
    beats = m["beats"][:upto]
    chunks = []
    for b, seg in zip(beats, segs):
        staged = "%s_seg%s.webm" % (m["name"], b["id"])
        shutil.copy(seg, os.path.join(IN, staged))
        g = json.load(open(os.path.join(HERE, "workflows", "upscale_interp.json")))
        g["1"]["inputs"].update(video=staged, skip_first_frames=0, frame_load_cap=0)
        g["2"]["inputs"]["scale_by"] = float(m.get("upscale", 1.0))
        g["4"]["inputs"].update(filename_prefix="%s/rife_%s" % (m["name"], b["id"]),
                                fps=float(fps * 2), crf=20.0)
        chunks.append(outfile(submit(g, "RIFE %d→%d fps beat %s" % (fps, fps * 2, b["id"])), "4", ".webm"))
    k = m["crossfade"]
    dst = concat(chunks, fps * 2, os.path.join(OUT, m["name"], "%s_%dfps.mp4" % (m["name"], fps * 2)),
                 xfade=(2 * k - 1) if k > 1 else 1, lengths=[2 * b["length"] - 1 for b in beats],
                 transition=m["transition"], bands=m["bands"])
    n = nframes(dst)
    print("  %s  %d snímků = %.2f s" % (dst, n, n / 30.0))
    if m.get("soundtrack"):
        cmd_soundtrack(m, dst)
    return dst


def cmd_all(m, src, w, h, start, stop, hd):
    """Render → slepení → RIFE na jeden zátah. Co si obvykle přeješ."""
    cmd_render(m, src, w, h, start, stop, hd)
    full = cmd_assemble(m, stop)
    final = cmd_smooth(m, stop)
    print("\nHOTOVO")
    if final == full:                       # LTX: RIFE se nekoná, je to týž soubor
        print("  %d fps  %s" % (m["fps"], full))
    else:
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
    # U LTX je vyšší rozlišení skoro zadarmo (naměřeno: 2,15x pixelů = 1,2x čas)
    # a identitu znatelně zvedne (0.43 -> 0.61), takže hd je default.
    w, h = resolution(m, src, "hd" if (a.hd or m["engine"] == "ltx") else "draft")
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
