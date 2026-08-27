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

    ./chain.py chains/idle01.json --plan       # časová osa a odhad, bez GPU
    ./chain.py chains/idle01.json --validate   # kontrola proti /object_info
    ./chain.py chains/idle01.json --materialize # zapiš beatNN.json k ruční úpravě
    ./chain.py chains/idle01.json --all        # render + slepení + RIFE naráz
    ./chain.py chains/idle01.json              # jen render všech beatů
    ./chain.py chains/idle01.json --beats 02   # beat 02 a všechny následující
    ./chain.py chains/idle01.json --hd         # větší rozlišení
    ./chain.py chains/idle01.json --assemble   # slepení (ffmpeg, bez GPU)
    ./chain.py chains/idle01.json --smooth     # RIFE 16 -> 32 fps přes celek
"""
import argparse, glob, json, math, os, shutil, subprocess, sys, time
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "http://localhost:8188"
COMFY = os.path.expanduser("~/Code/ComfyUI")
IN, OUT = os.path.join(COMFY, "input"), os.path.join(COMFY, "output")

# Rozpočty pixelů podle naměřené matice (reports/phase3_bench.md):
# 832x480 = 399k -> 290 s/81f, 1280x704 = 901k -> 806 s/81f.
BUDGET = {"draft": 442368, "hd": 995328}
SEC_PER_MPX = 333  # naměřeno 26. 8.: 140–155 s @ 0.445 Mpx / 81 snímků


def die(msg):
    sys.exit("chain.py: " + msg)


# ---------------------------------------------------------------- manifest

def load(path):
    m = json.load(open(path))
    m.setdefault("fps", 16)
    m.setdefault("length", 81)
    m.setdefault("negative", "blurry, static, low quality, watermark, text")
    m.setdefault("style_tail", "")
    m.setdefault("colormatch", {"method": "mkl", "strength": 0.6})
    m.setdefault("base", "i2v_final_14b_lightning_portrait")
    if (m["length"] - 1) % 4:
        die("length %d není 4n+1 (Wan VAE komprimuje čas 4:1)" % m["length"])
    if m["length"] > 81:
        die("length %d > 81 — za trénovacím oknem Wan 2.2, klip driftuje" % m["length"])
    if not m.get("beats"):
        die("manifest nemá žádné beats")
    return m


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
    """Rozměr násobný 16 se zachovaným poměrem stran zdroje.

    WanImageToVideo dělá center-crop na zadaný poměr, takže poměr zdroje se má
    trefit, ne ohnout do 9:16 — jinak přijdeš o okraje kompozice."""
    if m.get("width") and m.get("height") and tier == "draft":
        return m["width"], m["height"]
    from PIL import Image
    w0, h0 = Image.open(src).size
    aspect = w0 / h0
    h = math.sqrt(BUDGET[tier] / aspect)
    w = aspect * h
    return max(16, round(w / 16) * 16), max(16, round(h / 16) * 16)


# ---------------------------------------------------------------- graf

def build(m, beat, idx, seed_img, orig_img, w, h):
    """Základní I2V workflow + tři nody navíc pro předání snímku dál."""
    g = json.load(open(os.path.join(HERE, "workflows", m["base"] + ".json")))
    L, name = m["length"], m["name"]
    cm = m["colormatch"]

    g["8"]["inputs"]["text"] = beat["prompt"] + m["style_tail"]
    g["9"]["inputs"]["text"] = m.get("negative")
    g["10"]["inputs"]["image"] = seed_img
    g["12"]["inputs"].update(width=w, height=h, length=L)
    for n in ("13", "14"):
        g[n]["inputs"]["noise_seed"] = beat.get("seed", 42)
    if "boundary" in m:  # víc kroků v high-noise expertu = víc pohybu
        g["13"]["inputs"]["end_at_step"] = m["boundary"]
        g["14"]["inputs"]["start_at_step"] = m["boundary"]
    if "motion" in m:  # síla I2V Lightning LoRA na high-noise expertu
        g["2"]["inputs"]["strength_model"] = m["motion"]
    if "shift" in m:
        for n in ("3", "6"):
            g[n]["inputs"]["shift"] = m["shift"]
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
    g["22"] = {"class_type": "SaveImage",
               "inputs": {"images": ["21", 0],
                          "filename_prefix": "%s/seed%02d" % (name, idx + 1)}}
    return g


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


# ---------------------------------------------------------------- příkazy

def cmd_plan(m, src, w, h):
    L, fps = m["length"], m["fps"]
    seg = L / fps
    total = seg + (len(m["beats"]) - 1) * (L - 1) / fps
    est = SEC_PER_MPX * (w * h / 1e6)
    print("zdroj      %s" % src)
    print("rozlišení  %d×%d  (%.2f Mpx)" % (w, h, w * h / 1e6))
    print("segment    %d snímků = %.2f s @ %d fps, odhad %.0f s GPU" % (L, seg, fps, est))
    print()
    for i, b in enumerate(m["beats"]):
        start = 0 if i == 0 else seg + (i - 1) * (L - 1) / fps
        print("  beat %-4s %6.2f–%5.2f s  seed%02d → seed%02d  %s"
              % (b["id"], start, start + (seg if i == 0 else (L - 1) / fps),
                 i, i + 1, b["prompt"][:58]))
    frames = L + (len(m["beats"]) - 1) * (L - 1)
    print("\ncelkem     %d snímků = %.2f s @ %d fps" % (frames, frames / fps, fps))
    print("odhad GPU  %.0f min (bez fronty; --cache-none načítá modely znovu)"
          % (est * len(m["beats"]) / 60))


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


def cmd_render(m, src, w, h, only, hd):
    name = m["name"]
    orig = "%s_seed00.png" % name
    shutil.copy(src, os.path.join(IN, orig))
    start = 0
    if only:
        ids = [b["id"] for b in m["beats"]]
        if only not in ids:
            die("beat %r není v manifestu (%s)" % (only, ", ".join(ids)))
        start = ids.index(only)
        need = os.path.join(IN, "%s_seed%02d.png" % (name, start))
        if not os.path.exists(need):
            die("chybí %s — beat %s navazuje na předchozí, spusť napřed ty"
                % (os.path.basename(need), only))
        if start:
            print("  pozn.: beaty za %s se přegenerují taky (mění se navazující snímek)" % only)

    for i in range(start, len(m["beats"])):
        b = m["beats"][i]
        seed_img = "%s_seed%02d.png" % (name, i)
        path = beat_path(m, b)
        if os.path.exists(path) and not hd:
            g = json.load(open(path))       # ručně doladěná verze má přednost
            note = " (z %s)" % os.path.relpath(path, HERE)
        else:
            g = build(m, b, i, seed_img, orig, w, h)
            note = ""
        outputs = submit(g, "beat %s%s" % (b["id"], note))
        nxt = os.path.join(IN, "%s_seed%02d.png" % (name, i + 1))
        shutil.copy(outfile(outputs, "22", ".png"), nxt)
        print("     %s  →  %s" % (os.path.basename(outfile(outputs, "16", ".webm")),
                                  os.path.basename(nxt)))
    print("hotovo — slep to přes --assemble")


def segments(m):
    out = []
    for b in m["beats"]:
        hits = sorted(glob.glob(os.path.join(OUT, m["name"], "seg%s_*.webm" % b["id"])))
        if not hits:
            die("chybí vyrenderovaný segment pro beat %s" % b["id"])
        out.append(max(hits, key=os.path.getmtime))
    return out


def cmd_assemble(m):
    """Concat s zahozením prvního snímku každého navazujícího segmentu.

    Segment N+1 začíná přesně tím snímkem, kterým segment N končí (je to jeho
    vstupní obrázek), takže bez select=gte(n,1) by ve výsledku každý spoj
    zadrhl o jeden zdvojený snímek."""
    segs, fps = segments(m), m["fps"]
    dst = os.path.join(OUT, m["name"], "%s_full.mp4" % m["name"])
    parts, labels = [], []
    for i, _ in enumerate(segs):
        sel = "" if i == 0 else "select=gte(n\\,1),"
        parts.append("[%d:v]%ssetpts=N/%d/TB[v%d];" % (i, sel, fps, i))
        labels.append("[v%d]" % i)
    fc = "".join(parts) + "".join(labels) + "concat=n=%d:v=1:a=0[out]" % len(segs)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for s in segs:
        cmd += ["-i", s]
    cmd += ["-filter_complex", fc, "-map", "[out]", "-r", str(fps),
            "-c:v", "libx264", "-crf", "16", "-preset", "slow", "-pix_fmt", "yuv420p", dst]
    subprocess.run(cmd, check=True)
    n = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", dst],
                       capture_output=True, text=True).stdout.strip()
    print("  %s  %s snímků = %.2f s @ %d fps" % (dst, n, int(n) / fps, fps))
    return dst


def cmd_smooth(m):
    """RIFE přes celý slepený klip, ne po segmentech — vyhladí i spoje."""
    full = os.path.join(OUT, m["name"], "%s_full.mp4" % m["name"])
    if not os.path.exists(full):
        die("nejdřív --assemble")
    staged = "%s_full.mp4" % m["name"]
    shutil.copy(full, os.path.join(IN, staged))
    g = json.load(open(os.path.join(HERE, "workflows", "upscale_interp.json")))
    g["1"]["inputs"]["video"] = staged
    g["2"]["inputs"]["scale_by"] = float(m.get("upscale", 1.0))
    g["4"]["inputs"].update(filename_prefix="%s/final_%dfps" % (m["name"], m["fps"] * 2),
                            fps=float(m["fps"] * 2), crf=20.0)
    outputs = submit(g, "RIFE %d→%d fps" % (m["fps"], m["fps"] * 2))
    final = outfile(outputs, "4", ".webm")
    print("  %s" % final)
    return final


def cmd_all(m, src, w, h, only, hd):
    """Render → slepení → RIFE na jeden zátah. Co si obvykle přeješ."""
    cmd_render(m, src, w, h, only, hd)
    full = cmd_assemble(m)
    final = cmd_smooth(m)
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
    ap.add_argument("--beats", help="přegeneruj od tohoto beatu dál, např. 02")
    ap.add_argument("--hd", action="store_true", help="~1 Mpx místo ~0.44 Mpx")
    ap.add_argument("--assemble", action="store_true", help="slepení, bez GPU")
    ap.add_argument("--smooth", action="store_true", help="RIFE 2× fps přes celek")
    ap.add_argument("--all", action="store_true",
                    help="render + slepení + RIFE naráz")
    a = ap.parse_args()

    m = load(a.manifest)
    if a.assemble:
        cmd_assemble(m); sys.exit(0)
    if a.smooth:
        cmd_smooth(m); sys.exit(0)
    src = source_path(m)
    w, h = resolution(m, src, "hd" if a.hd else "draft")
    if a.plan:
        cmd_plan(m, src, w, h)
    elif a.materialize:
        cmd_materialize(m, src, w, h)
    elif a.validate:
        sys.exit(0 if cmd_validate(m, src, w, h) else 1)
    elif a.all:
        cmd_all(m, src, w, h, a.beats, a.hd)
    else:
        cmd_render(m, src, w, h, a.beats, a.hd)
