#!/usr/bin/env python3
"""story.py — minutový anime příběh ze záběrů: postavy → keyframy → řetěz → vypravěč, hudba, titulky.

    ./tools/story.py ls                                     # katalog a stav rozpracovaných příběhů
    ./tools/story.py plan      <story>                      # časová osa, odhad, délka narace — bez GPU
    ./tools/story.py sheet     <story> <role> [--n 4]       # kandidáti referenčního obrázku postavy
    ./tools/story.py sheet     <story> <role> --pick 2      # vybraný kandidát = postava
    ./tools/story.py cast      <story> <role> <obrázek>     # vlastní postavička místo generované
    ./tools/story.py keyframes <story> [--shot 03,07] [--reroll|--force] [--method kontext|ipadapter]
    ./tools/story.py contact   <story>                      # kontaktní arch keyframů
    ./tools/story.py approve   <story>                      # brána: keyframy schválené
    ./tools/story.py compile   <story> [--no-review]        # → chains/<work>.json + narration.json
    ./tools/story.py voice     <story> [--lang cs|en]       # vypravěč (Piper)
    ./tools/story.py music     <story>                      # hudba (ACE-Step, záloha LTX dárce)
    ./tools/story.py mix       <story> [--lang cs|en]       # zvuk + titulky + 16:9
    ./tools/story.py run       <story> [--hd] [--resume] [--only shot03] [--no-review] [--until review]
    ./tools/story.py batch     [<story>…] [--hd] [--auto-cast]

<story> je id ze stories/ (pracovní adresář output/story_<id>/story/), nebo
cesta k JSON s --work <jméno> (serve.py: output/<job>/story/story.json).

Záběr = scéna chain.py s vlastním keyframem (`scenes[].source`), takže řetěz
driftu se na každém záběru přeruší a postava se vrací k obrázku, který prošel
kontaktním archem. Keyframy vznikají z referenčního obrázku postavy přes FLUX
Kontext (default) nebo Illustrious + IPAdapter — ne z textu, jinak by postava
byla v každém záběru jiná. Běží na SPARKu z venv ComfyUI (PIL), jako drive.py.
"""
import argparse, glob, hashlib, json, math, os, re, shutil, subprocess, sys, textwrap, time, wave
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import chain  # noqa: E402

STORIES = os.path.join(HERE, "stories")
CHAINS = os.path.join(HERE, "chains")
WORKFLOWS = os.path.join(HERE, "workflows")
PIPER_HOME = os.environ.get("PIPER_HOME", os.path.expanduser("~/.local/share/video-stack/piper"))
AUDIO_URL = os.environ.get("AUDIO_URL", "http://localhost:8093")    # AiStack services/audio
TAGGER_URL = os.environ.get("TAGGER_URL", "http://127.0.0.1:8097/tag")   # AiStack finetune-wd14
LLM_GATEWAY = os.environ.get("LLM_GATEWAY", "http://localhost:8080/v1/chat/completions")
PROMPT_MODEL = os.environ.get("VIDEO_PROMPT_MODEL", "shop")
# Tagy, které popisují obrázek, ne postavu: kompozici, pozadí, náladu, médium.
# Do promptu keyframu nepatří — ten si kompozici i prostředí určuje sám.
TAG_DROP = {
    "solo", "full body", "upper body", "lower body", "cowboy shot", "portrait", "close-up",
    "looking at viewer", "looking away", "looking back", "looking down", "looking up",
    "standing", "sitting", "kneeling", "lying", "walking", "running", "jumping", "arms up",
    "simple background", "white background", "grey background", "gradient background",
    "transparent background", "outdoors", "indoors", "day", "night", "sky", "cloud", "water",
    "tree", "grass", "flower", "wall", "window", "blurry", "blurry background", "depth of field",
    "signature", "artist name", "dated", "watermark", "web address", "english text", "text",
    "border", "letterboxed", "traditional media", "photo (medium)", "realistic", "monochrome",
    "greyscale", "sketch", "painting (medium)", "watercolor (medium)", "no humans",
    "smile", "blush", "open mouth", "closed mouth", "closed eyes", "parted lips", "teeth",
    "holding", "holding weapon", "weapon", "sparkle", "light particles", "motion blur",
}
# Tagy, které do dětského příběhu nepatří; reference může být z jiné tvorby.
TAG_BLOCK = {
    "breasts", "large breasts", "medium breasts", "small breasts", "huge breasts", "cleavage",
    "nipples", "areolae", "nude", "nudity", "topless", "bottomless", "completely nude",
    "panties", "underwear", "bra", "lingerie", "thong", "pantyshot", "upskirt", "ass",
    "sideboob", "underboob", "navel", "pussy", "penis", "sex", "spread legs", "bondage",
    "covered nipples", "see-through", "wet clothes", "bikini", "swimsuit", "lactation",
}
TAG_MAX = 14                       # delší výčet Kontext stejně neudrží
# Co dělá postavu postavou: tohle jde do popisu dřív než cokoli jiného, i když
# má nižší jistotu. Bez toho vypadla „brown hair" za limitem a Kontext barvil
# vlasy v každém záběru jinak.
TAG_FIRST = re.compile(
    r"\b(hair|eyes|dress|shirt|skirt|jacket|coat|hoodie|sweater|uniform|shorts|pants|trousers|"
    r"footwear|shoes|boots|socks|hat|cap|ribbon|bow|glasses|ornament|braid|ponytail|twintails|"
    r"bangs|ahoge|horns|ears|tail|wings|fur|whiskers|beak|scales|freckles|apron|scarf|gloves|belt)\b")

KF_W, KF_H = 768, 1344            # SDXL/Kontext bucket blízko 9:16; Wan z něj dopočítá 496×880 / 752×1312
LANGS = ("cs", "en")
# Vypravěčka: kasandra je jediný ženský český hlas, který Piper má
# (vedle jirky); en lessac je taky ženský. Příběh si hlas může přebít
# polem `voice` ({"cs": "...", "en": "..."}).
VOICES = {"cs": "cs_CZ-kasandra-medium", "en": "en_US-lessac-medium"}
WORDS_PER_S = {"cs": 2.2, "en": 2.5}    # Piper při length_scale 1.0; pomalejší čtení to dál dělí
LENGTH_SCALE = 1.1                       # dětský vypravěč: o desetinu pomaleji
SHEET_CKPT = "Illustrious-XL-v2.0.safetensors"

STYLE = ("anime style, children's picture book illustration, soft cel shading, clean lineart, "
         "bright pastel colors, cute rounded character design")
# Wan: anime slovník na začátek ocasu, kamera se dosadí podle záběru. `static camera`
# je pravidlo z bin/README.md — bez něj Wan najíždí a odjíždí sám od sebe.
WAN_TAIL = (", anime style, 2D cel animation, clean lineart, flat pastel colors, {camera}, "
            "one continuous gentle movement at a calm steady tempo")
WAN_NEG = ("blurry, low quality, watermark, text, subtitles, 3d render, realistic, photo, photorealistic, "
           "deformed hands, extra limbs, extra fingers, distorted face, morphing, flickering")
SHEET_PREFIX = "masterpiece, best quality, amazing quality"
SHEET_NEG = ("lowres, bad anatomy, bad hands, worst quality, low quality, jpeg artifacts, blurry, "
             "watermark, signature, text, multiple views, character sheet, cropped")
CAMERA_WORDS = {"Zoom In": "slow camera push in", "Zoom Out": "slow camera pull back",
                "Pan Left": "slow camera pan to the left", "Pan Right": "slow camera pan to the right",
                "Pan Up": "slow camera tilt up", "Pan Down": "slow camera tilt down",
                "Anti Clockwise (ACW)": "slow camera roll", "ClockWise (CW)": "slow camera roll",
                "Static": "static camera"}
PHASES = ("keyframes", "review", "compile", "render", "voice", "music", "mix")


def die(msg):
    sys.exit("story.py: " + msg)


def phase(name):
    """Značka pro serve.py (fáze jobu) a pro čtenáře logu."""
    print("== fáze: %s" % name, flush=True)


def ensure(path):
    """Cesta k zápisu: založí adresář. Čtecí cesty (Work.p) nic nezakládají,
    aby kontrola katalogu na Macu nesypala prázdné složky."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def sha(obj):
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- příběh

class Work:
    """Pracovní adresář příběhu: output/<jméno>/story/. chain.py píše segmenty
    do output/<jméno>/ vedle, takže render, keyframy i výsledek leží pohromadě
    a serve.py ví, kde co hledat jen podle id jobu."""

    def __init__(self, name):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            die("jméno pracovního adresáře %r smí být jen [A-Za-z0-9_-]" % name)
        self.name = name
        self.root = os.path.join(chain.OUT, name)
        self.dir = os.path.join(self.root, "story")

    def p(self, *parts):
        return os.path.join(self.dir, *parts)

    def char(self, role):
        return self.p("chars", role + ".png")

    def kf(self, shot):
        return self.p("kf", shot + ".png")

    def kf_meta(self, shot):
        return self.p("kf", shot + ".json")

    def voice(self, shot, lang):
        return self.p("voice", "%s_%s.wav" % (shot, lang))

    @property
    def manifest(self):
        return os.path.join(CHAINS, self.name + ".json")

    def final(self, lang, variant=""):
        return os.path.join(self.root, "%s_story_%s%s.mp4" % (self.name, lang, variant and "_" + variant))


def story_path(ref):
    if ref.endswith(".json") or os.sep in ref:
        return ref
    return os.path.join(STORIES, ref + ".json")


def load_story(ref, work=None):
    """stories/<id>.json → (story, Work). Doplní defaulty a zkontroluje, co
    se dá bez GPU: role, záběry, délky, kostry, kamery."""
    path = story_path(ref)
    if not os.path.exists(path):
        die("příběh %s neexistuje (katalog: %s)" % (ref, ", ".join(catalog_ids()) or "prázdný"))
    st = json.load(open(path))
    return normalize(st, path), Work(work or "story_%s" % st["id"])


def normalize(st, path="?"):
    where = os.path.basename(path)
    for k in ("id", "world", "characters", "shots"):
        if not st.get(k):
            die("%s: chybí %s" % (where, k))
    st.setdefault("seed", 42)
    st.setdefault("style", STYLE)
    st.setdefault("lang", "cs")
    st.setdefault("engine", "wan")
    chars = st["characters"]
    for role, c in chars.items():
        if not re.fullmatch(r"[a-z0-9_]+", role):
            die("%s: role %r smí být jen [a-z0-9_]" % (where, role))
        if not (c.get("desc") or "").strip():
            die("%s: postava %s nemá desc (vzhled pro prompty)" % (where, role))
        c.setdefault("name", role)
        c.setdefault("name_en", c["name"])
        c.setdefault("tag", c["name_en"])          # jak se postava jmenuje v anglických promptech
    hero = next(iter(chars))
    ids = set()
    for n, sh in enumerate(st["shots"]):
        sh.setdefault("id", "%02d" % (n + 1))
        if not re.fullmatch(r"\d\d", sh["id"]) or sh["id"] in ids:
            die("%s: záběr %r — id čekám jako dvě číslice a jedinečné" % (where, sh["id"]))
        ids.add(sh["id"])
        sh.setdefault("chars", [hero])
        bad = [c for c in sh["chars"] if c not in chars]
        if bad:
            die("%s: záběr %s zná neznámé postavy %s" % (where, sh["id"], bad))
        if not 1 <= len(sh["chars"]) <= 2:
            die("%s: záběr %s má %d postav — Kontext drží nejvýš dvě (hrdina + pomocník)"
                % (where, sh["id"], len(sh["chars"])))
        if not (sh.get("keyframe") or "").strip():
            die("%s: záběr %s nemá keyframe" % (where, sh["id"]))
        mo = sh.get("motion")
        if isinstance(mo, str):
            mo = [mo] * int(sh.get("beats", 1))
        if not mo or not all((x or "").strip() for x in mo):
            die("%s: záběr %s nemá motion (prompt pohybu, u tance popis tance)" % (where, sh["id"]))
        if sh.get("beats") and int(sh["beats"]) != len(mo):
            die("%s: záběr %s — beats %s nesedí na %d promptů v motion" % (where, sh["id"], sh["beats"], len(mo)))
        sh["prompts"], sh["beats"] = mo, len(mo)
        if sh.get("control") and not os.path.exists(chain.pose_path(sh["control"])):
            die("%s: záběr %s — kostra %r chybí (drive/%s_pose.webm)"
                % (where, sh["id"], sh["control"], sh["control"]))
        if sh.get("camera"):
            if sh.get("control"):
                die("%s: záběr %s — camera a control najednou nejde" % (where, sh["id"]))
            cam = chain.CAMERA_POSES.get(sh["camera"])
            if not cam:
                die("%s: záběr %s — camera %r, znám %s"
                    % (where, sh["id"], sh["camera"], ", ".join(chain.CAMERA_POSES)))
            sh["_camera"] = cam
    return st


def catalog_ids():
    return sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(STORIES, "*.json")))


def narration(sh, lang):
    return (sh.get("narration" if lang == "cs" else "narration_" + lang) or "").strip()


def char_path(st, work, role):
    """Obrázek postavy: explicitní `sheet` v příběhu (serve.py tam dává nahraný),
    jinak vygenerovaný / obsazený v pracovním adresáři. None = chybí."""
    c = st["characters"][role]
    if c.get("sheet"):
        p = c["sheet"] if os.path.isabs(c["sheet"]) else os.path.join(STORIES, c["sheet"])
        return p if os.path.exists(p) else None
    p = os.path.join(work.dir, "chars", role + ".png")
    return p if os.path.exists(p) else None


def need_chars(st, work, roles=None):
    missing = [r for r in (roles or st["characters"]) if not char_path(st, work, r)]
    if missing:
        die("chybí obrázky postav %s — vygeneruj (story.py sheet %s <role>) nebo obsaď vlastní "
            "(story.py cast %s <role> <obrázek>)" % (", ".join(missing), st["id"], st["id"]))


# ---------------------------------------------------------------- ComfyUI

def template(name):
    return json.load(open(os.path.join(WORKFLOWS, name + ".json")))


def stage(src, name):
    """Obrázek do ComfyUI input/ (LoadImage čte jen odtamtud), vrací jméno."""
    shutil.copy(src, os.path.join(chain.IN, name))
    return name


def images(outputs, node):
    return [os.path.join(chain.OUT, im.get("subfolder", ""), im["filename"])
            for im in outputs.get(node, {}).get("images", []) if im["filename"].endswith(".png")]


# ---------------------------------------------------------------- postavy

def sheet_prompt(st, role):
    c = st["characters"][role]
    return ("%s, solo, full body, standing, front view, looking at viewer, arms at sides, "
            "simple background, white background, %s, %s" % (SHEET_PREFIX, c["desc"], st["style"]))


def cmd_sheet(st, work, role, n=4, seed=None, pick=None):
    if role not in st["characters"]:
        die("postava %r v příběhu není (znám %s)" % (role, ", ".join(st["characters"])))
    if pick is not None:
        src = work.p("cand", "%s_%d.png" % (role, pick))
        if not os.path.exists(src):
            die("kandidát %d pro %s neexistuje — napřed story.py sheet %s %s" % (pick, role, st["id"], role))
        shutil.copy(src, ensure(work.char(role)))
        print("  %s = kandidát %d  →  %s" % (role, pick, work.char(role)))
        return work.char(role)
    seed = st["seed"] + 1000 + list(st["characters"]).index(role) if seed is None else seed
    g = template("story_sheet_illustrious")
    g["1"]["inputs"]["ckpt_name"] = st.get("sheet_ckpt", SHEET_CKPT)
    g["2"]["inputs"]["text"] = sheet_prompt(st, role)
    g["3"]["inputs"]["text"] = SHEET_NEG
    g["4"]["inputs"]["batch_size"] = n
    g["5"]["inputs"]["seed"] = seed
    g["7"]["inputs"]["filename_prefix"] = "%s/story/_gen/sheet_%s" % (work.name, role)
    outs = images(chain.submit(g, "postava %s (%d kandidátů)" % (role, n)), "7")
    if not outs:
        die("generování postavy %s nevrátilo obrázky" % role)
    cands = []
    for k, src in enumerate(outs, 1):
        dst = ensure(work.p("cand", "%s_%d.png" % (role, k)))
        shutil.copy(src, dst)
        cands.append((dst, "%s %d" % (role, k), ""))
    arch = contact_sheet(cands, work.p("cand", "%s_contact.jpg" % role), cols=len(cands))
    print("  kandidáti: %s\n  vyber: story.py sheet %s %s --pick <1–%d>" % (arch, st["id"], role, len(outs)))
    return arch


def cmd_cast(st, work, role, image):
    """Vlastní postavička (tvoje anime postava) místo generované."""
    from PIL import Image
    if role not in st["characters"]:
        die("postava %r v příběhu není (znám %s)" % (role, ", ".join(st["characters"])))
    im = Image.open(image)
    im.load()
    if im.mode in ("RGBA", "LA", "P"):
        # průhledné pozadí → bílé; Kontext i IPAdapter čtou alfu jako černou
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    im.convert("RGB").save(ensure(work.char(role)))
    open(work.char(role) + ".cast", "w").write(os.path.basename(image))
    print("  %s ← %s (%d×%d)" % (role, image, *im.size))
    return work.char(role)


# ---------------------------------------------------------------- keyframy

def llm(system, shots, user, max_tokens=120):
    """Gateway AiStacku s few-shotem; None, když neodpoví. Volá se z workeru,
    nikdy z obsluhy HTTP — model sdílí GPU s ComfyUI a umí mlčet i půl minuty."""
    msgs = [{"role": "system", "content": system}]
    for u, a in shots:
        msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    msgs.append({"role": "user", "content": user})
    body = {"model": PROMPT_MODEL, "max_tokens": max_tokens, "temperature": 0.1, "messages": msgs}
    try:
        req = urllib.request.Request(LLM_GATEWAY, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        d = json.load(urllib.request.urlopen(req, timeout=60))
        return d["choices"][0]["message"]["content"].strip()
    except Exception as e:                                 # noqa: BLE001
        print("  ! LLM nedostupné (%s)" % str(e)[:60], flush=True)
        return None


WHO_SYSTEM = ("You split a Czech character label into JSON. Answer with one JSON object and nothing else: "
              '{"name": "<the proper name, or empty>", "species_cs": "<the Czech species in nominative>", '
              '"species_en": "<the English species>"}.')
WHO_SHOTS = [
    ("ježek Bodlinka", '{"name": "Bodlinka", "species_cs": "ježek", "species_en": "hedgehog"}'),
    ("veverka", '{"name": "", "species_cs": "veverka", "species_en": "squirrel"}'),
    ("dráček Pip", '{"name": "Pip", "species_cs": "dráček", "species_en": "little dragon"}'),
    # vzhled do jména nepatří, popisek si ho uživatel občas přibalí
    ("Kyklop Bručoun s jedním okem a žlutými vlasy",
     '{"name": "Bručoun", "species_cs": "kyklop", "species_en": "cyclops"}'),
]


def who_info(who):
    """„ježek Bodlinka" → jméno, český a anglický druh. Bez LLM heuristika:
    velké písmeno = jméno, zbytek druh (anglicky pak nic, vzhled nese obrázek)."""
    raw = llm(WHO_SYSTEM, WHO_SHOTS, who.strip(), max_tokens=80) or ""
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and (d.get("species_cs") or d.get("name")):
                return {"name": (d.get("name") or "").strip(),
                        "species_cs": (d.get("species_cs") or "").strip(),
                        "species_en": (d.get("species_en") or "").strip()}
        except ValueError:
            pass
    words = who.split()
    name = next((w for w in words if w[:1].isupper()), "")
    return {"name": name, "species_cs": " ".join(w for w in words if w != name), "species_en": ""}


def label_parts(label):
    """„kocour Mourek" → („Mourek", „kocour"); „Tom the cat" → („Tom", „cat")."""
    words = re.sub(r"\bthe\b", " ", label).split()
    name = next((w for w in words if w[:1].isupper()), "")
    return name, " ".join(w for w in words if w != name).strip()


LOOK_SYSTEM = ("You name what makes an animal or creature recognizable in a drawing. "
               "Answer with one short English noun phrase, at most 12 words, nothing else.")
LOOK_SHOTS = [
    ("cyclops", "a single large eye in the middle of its face, no second eye"),
    ("hedgehog", "a round body covered in spines, a small pointed snout, tiny paws"),
    ("squirrel", "a bushy upright tail, large front teeth, small tufted ears"),
]


def species_look(kind):
    """Druh → čím se pozná. Tagy z reference tohle neřeknou: fotka jednooké
    příšerky dala „red eyes, sharp teeth" a Kontext podle svého zvyku nakreslil
    oči dvě. Kešuje se v postavě (`species_look` ve story.json jobu)."""
    out = llm(LOOK_SYSTEM, LOOK_SHOTS, kind, max_tokens=40)
    out = (out or "").strip().strip('".').strip()
    return out if out and len(out) <= 120 and "\n" not in out else ""


def recast(st, work, path=None):
    """Obsazená role může být úplně jiné zvíře, než co má scénář — `who`
    („ježek Bodlinka") přijde z appky vedle obrázku. Přepíše postavu v
    promptech i ve vyprávění a zapíše se do story.json jobu, aby to viděla
    i vypravěčka a titulky. Jednou: `_recast` drží, co už proběhlo."""
    done = []
    for role, c in st["characters"].items():
        who = (c.get("who") or "").strip()
        if not who or c.get("_recast") == who:
            continue
        info = who_info(who)
        # Do vět patří jméno a druh, ne celý popisek: uživatel do pole často
        # napíše i vzhled („Mia, blonďatá holka v modrých šatech") a vypravěčka
        # to pak předčítala celé. Vzhled stejně nese obrázek, ne text.
        species = info["species_cs"] if len(info["species_cs"]) <= 24 else ""
        new_name = (("%s %s" % (species, info["name"])).strip()
                    if info["name"] and species else (info["name"] or species or who))
        new_tag = (("%s the %s" % (info["name"], info["species_en"])).strip()
                   if info["name"] and info["species_en"]
                   else (info["name"] or info["species_en"] or who))
        # delší dřív, ať se „kocour Mourek" nerozpadne na „kocour" + „Mourek"
        old_cs_name, old_cs_species = label_parts(c["name"])
        old_en_name, old_en_species = label_parts(c["tag"])
        swap = [(c["tag"], new_tag), (c["name_en"], new_tag), (c["name"], new_name),
                (old_cs_name, info["name"] or new_name), (old_en_name, info["name"] or new_tag),
                (old_cs_species, species or new_name),
                (old_en_species, info["species_en"] or new_tag)]
        swap = [(a, b) for a, b in swap if a and b]
        swap.sort(key=lambda ab: -len(ab[0]))
        for sh in st["shots"]:
            for k in ("keyframe", "action", "narration", "narration_en"):
                if sh.get(k):
                    sh[k] = swap_words(sh[k], swap)
            if isinstance(sh.get("motion"), list):
                sh["motion"] = [swap_words(t, swap) for t in sh["motion"]]
            elif sh.get("motion"):
                sh["motion"] = swap_words(sh["motion"], swap)
            if isinstance(sh.get("prompts"), list):
                sh["prompts"] = [swap_words(t, swap) for t in sh["prompts"]]
        c.update(name=new_name, name_en=new_tag, tag=new_tag, species_en=info["species_en"],
                 species_look=species_look(info["species_en"]) if info["species_en"] else "",
                 _recast=who)
        done.append("%s → %s" % (role, who))
    if not done:
        return
    for c in st["characters"].values():
        if c.get("_recast"):
            fix_czech(st, c["name"])
            fix_english(st, c["tag"])
    print("  obsazení: %s" % ", ".join(done), flush=True)
    if path and os.path.exists(path):
        json.dump(st, open(path, "w"), indent=2, ensure_ascii=False)


def swap_words(text, pairs):
    """Záměna postavy v textu. Bez ohledu na velikost písmen — na začátku věty
    stojí „Medvídek Bručoun" a jinde „medvídek". Velké písmeno se doplní jen
    na začátku věty: uprostřed patří „leží holčička Mia", ne „leží Holčička"."""
    for old, new in pairs:
        if not old or not new or old.lower() == new.lower():
            continue

        def cap(m, new=new):
            head = m.string[:m.start()].rstrip()
            return new[:1].upper() + new[1:] if not head or head[-1] in ".!?:\u201e\"" else new

        text = re.sub(r"\b%s\b" % re.escape(old), cap, text, flags=re.I)
    return text


CZ_SYSTEM = ("Dostaneš postavu a větu, ve které se ta postava vyměnila za jinou. Oprav shodu: "
             "sloveso v minulém čase, přídavná jména i zájmena se musí řídit rodem nové postavy "
             "(ježek = mužský, veverka = ženský, kotě = střední). Nic nepřidávej ani neubírej, "
             "vrať jen opravenou větu.")
CZ_SHOTS = [
    ("Postava: ježek Bodlinka\nVěta: Na plotě seděl ježek Bodlinka. S jejím deštníkem!",
     "Na plotě seděl ježek Bodlinka. S jejím deštníkem!"),
    ("Postava: veverka Zrzka\nVěta: Na plotě seděl veverka Zrzka. S jejím deštníkem!",
     "Na plotě seděla veverka Zrzka. S jejím deštníkem!"),
    ("Postava: veverka Zrzka\nVěta: Zrzka jí deštník vrátil. Hodný veverka.",
     "Zrzka jí deštník vrátila. Hodná veverka."),
    ("Postava: holčička Mia\nVěta: Holčička Mia sklouzne pod peřinu a koukají mu jen oči.",
     "Holčička Mia sklouzne pod peřinu a koukají jí jen oči."),
]


# „Bručoun the cyclops" → slovo „the" sedí skoro v každé anglické větě a
# průchod pak přepisoval věty o jiné postavě.
STOP = {"the", "and", "with", "her", "his", "its", "their", "little", "small", "big",
        "malý", "malá", "velký", "velká", "můj", "moje"}


def stems(label):
    """Kmeny slov popisku pro hledání ve skloňovaném textu („kyklopa
    Bručouna" pozná podle „kyklo" a „bruča"... tedy prvních pět písmen)."""
    return [w[:5].lower() for w in re.sub(r"[^\w\s]", " ", label).split()
            if len(w) > 2 and w.lower() not in STOP]


def mentions(text, label):
    low = text.lower()
    return any(st_ in low for st_ in stems(label))


def keeps_names(src, out):
    """Věta po opravě musí nést tatáž vlastní jména. Model jinak ochotně
    udělal z věty o dvou postavách větu o té jedné, na kterou se ptáme
    („Mia zvedne Bručouna" → „Bručoun zvedne")."""
    def names(t):
        # velké písmeno uprostřed věty = jméno; první slovo věty se nepočítá
        return {w[:5].lower() for w in re.findall(r"(?<![.!?]\s)(?<!^)\b[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\w]+",
                                                  t, re.M)}
    return names(src) <= names(out)


def fix_czech(st, label=""):
    """Po záměně sedí slova, ale ne vždy rod („seděl veverka", „koukají mu jen
    oči"). Gateway větu srovná; bez ní zůstane, jak vyšla ze záměny — význam je
    správný. Kromě vyprávění i `action`, ten appka ukazuje v kontrole záběrů.

    Jen věty, kde ta postava opravdu je: model jinak poslušně přepsal větu o
    někom jiném na zadanou postavu („Holčička Mia sklouzne pod peřinu" →
    „Kyklop Bručoun sklouzne pod peřinu"). Výsledek musí postavu pořád nést,
    jinak se zahodí."""
    for sh in st["shots"]:
        for k in ("narration", "action"):
            t = (sh.get(k) or "").strip()
            if not t or not mentions(t, label):
                continue
            out = llm(CZ_SYSTEM, CZ_SHOTS, "Postava: %s\nVěta: %s" % (label, t), max_tokens=220)
            if not out:
                return                                  # LLM je dole, nemá smysl zkoušet dál
            out = out.strip().strip('"\u201e\u201c').strip()
            if (out and 0.6 * len(t) < len(out) < 1.6 * len(t)
                    and re.search("[ěščřžýáíéúůň]", out, re.I)
                    and mentions(out, label) and keeps_names(t, out)):
                sh[k] = out


EN_SYSTEM = ("You fix pronoun agreement in one English sentence after a character was swapped. "
             "Keep every other word, only make the pronouns match the named character. "
             "Answer with the corrected sentence and nothing else.")
EN_SHOTS = [
    ("Character: Mia the girl\nSentence: Mia the girl hides under his star quilt, only his eyes peeking out.",
     "Mia the girl hides under her star quilt, only her eyes peeking out."),
    ("Character: Bručoun the cyclops\nSentence: Bručoun the cyclops falls from the shelf onto the carpet.",
     "Bručoun the cyclops falls from the shelf onto the carpet."),
]


def fix_english(st, label=""):
    """Totéž anglicky: prompt keyframu i pohybu drží zájmena původní postavy
    („Mia … under his quilt") a FLUX podle nich kreslí. Jen věty s tou
    postavou, výsledek ji musí pořád nést."""
    for sh in st["shots"]:
        for k in ("keyframe", "narration_en"):
            t = (sh.get(k) or "").strip()
            if not t or not mentions(t, label):
                continue
            out = llm(EN_SYSTEM, EN_SHOTS, "Character: %s\nSentence: %s" % (label, t), max_tokens=220)
            if not out:
                return
            out = out.strip().strip('"').strip()
            if (out and 0.6 * len(t) < len(out) < 1.6 * len(t)
                    and mentions(out, label) and keeps_names(t, out)):
                sh[k] = out


def ref_tags(path):
    """WD14 sidecar AiStacku: obrázek → booru tagy podle jistoty. Když neběží,
    prázdný seznam a prompt jede bez popisu (jako předtím)."""
    try:
        data = open(path, "rb").read()
        req = urllib.request.Request(TAGGER_URL, data, {"Content-Type": "application/octet-stream"})
        d = json.load(urllib.request.urlopen(req, timeout=120))
    except Exception as e:                                 # noqa: BLE001
        print("  ! tagger nedostupný (%s) — reference zůstane bez popisu" % str(e)[:60], flush=True)
        return []
    keep = [(t.replace("_", " "), c) for t, c in d.get("general", {}).items() if c >= 0.4]
    keep = [(t, c) for t, c in keep if t not in TAG_DROP and t not in TAG_BLOCK]
    # identita napřed (a uvnitř podle jistoty), zbytek doplní zbývající místa
    keep.sort(key=lambda tc: (not TAG_FIRST.search(tc[0]), -tc[1]))
    return [t for t, _ in keep[:TAG_MAX]]


# Pozor na slovník: „keep the hairstyle and the outfit" předpokládá člověka a
# Kontext podle toho z jednooké příšerky udělal chlapečka se zelenými vlasy.
# Druh jde dopředu, rysy z tagů reference hned za něj a teprve pak styl.
CAST_SHEET = ("Redraw the %(kind)s shown in the reference image as one full-body character standing and "
              "facing the viewer on a plain white background, friendly expression. "
              "Keep exactly what the reference shows: %(tags)sthe face and its features, the same number "
              "of eyes, limbs, horns and ears, the body shape and proportions, and all colors. It stays "
              "the same creature — do not turn it into a human or a child. %(style)s. "
              "No text, no frame, no scenery.")


def cast_sheet(st, work, role):
    """Nahraná reference → JEDEN referenční list ve stylu příběhu; z něj pak
    jedou všechny keyframy.

    Nahraná fotka je mimo doménu (jiný styl, jiné světlo, často i rám a pozadí)
    a Kontext z ní pokaždé přečte něco jiného — Mie se mezi záběry měnily vlasy
    i šaty. Když se jednou překreslí do stylu příběhu a všech dvanáct záběrů pak
    vychází z TÉHOŽ obrázku, identita drží. Není to řetěz: reference je pořád ta
    samá, takže se drift nekumuluje."""
    src = char_path(st, work, role)
    if not src or not is_cast(st, work, role):
        return src
    dst = work.p("chars", role + "_book.png")
    kind = (st["characters"][role].get("species_en") or "").strip() or "character"
    tags = ref_tags(src)
    c = st["characters"][role]
    # Job obsazený starší verzí popis druhu nemá; dopočítá se tady, aby z
    # opravy něco měl i rozdělaný příběh, když si necháš záběr překreslit.
    if c.get("species_en") and not c.get("species_look"):
        c["species_look"] = species_look(c["species_en"])
    look = (c.get("species_look") or "").strip()
    marks = ([look] if look else []) + tags[:8]
    prompt = CAST_SHEET % {"kind": kind, "style": st["style"],
                           "tags": ("its distinctive features (%s), " % ", ".join(marks)) if marks else ""}
    seed = st["seed"] + 500 + sum(ord(ch) for ch in role)
    key = sha([chain.file_sha(src), prompt, seed, KF_W, KF_H])
    if os.path.exists(dst) and os.path.exists(dst + ".key") and open(dst + ".key").read() == key:
        return dst
    g = template("story_keyframe_kontext")
    g["4"]["inputs"]["image"] = stage(src, "%s_cast_%s.png" % (work.name, role))
    del g["5"], g["6"]
    g["7"]["inputs"]["image"] = ["4", 0]
    g["9"]["inputs"]["text"] = prompt
    g["13"]["inputs"].update(width=KF_W, height=KF_H)
    g["14"]["inputs"]["seed"] = seed
    g["16"]["inputs"]["filename_prefix"] = "%s/story/_gen/cast_%s" % (work.name, role)
    outs = images(chain.submit(g, "referenční list %s" % role), "16")
    if not outs:
        die("referenční list %s: ComfyUI nevrátil obrázek" % role)
    shutil.copy(outs[0], ensure(dst))
    open(dst + ".key", "w").write(key)
    print("  ok referenční list %s" % role, flush=True)
    return dst


def ref_image(st, work, role):
    """Obrázek, který jde do keyframu: u obsazené role list ve stylu příběhu,
    u výchozí postavy její vygenerovaný sheet."""
    return cast_sheet(st, work, role) if is_cast(st, work, role) else char_path(st, work, role)


def cast_desc(st, work, role):
    """Popis obsazené role Z JEJÍ REFERENCE (WD14), ne ze scénáře. Drží dvě věci
    najednou: prompt říká totéž, co je na obrázku, a říká to u všech dvanácti
    keyframů stejně — bez popisu si Kontext u každého záběru vymyslel jinou
    postavu, zvlášť když jsou reference dvě slepené vedle sebe.
    Vedle obrázku se to cachuje (.desc), takže se tagger ptá jednou."""
    img = char_path(st, work, role)      # tagy z nahrané reference: ta je pravda
    if not img:
        return ""
    cache = img + ".desc"
    if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(img):
        return open(cache).read().strip()
    c = st["characters"][role]
    kind = (c.get("species_en") or "").strip()
    look = (c.get("species_look") or "").strip()
    tags = ref_tags(img)
    desc = ", ".join(([kind] if kind else []) + ([look] if look else [])
                     + [t for t in tags if t != kind])
    if tags:
        open(cache, "w").write(desc)        # prázdno necachovat: tagger byl dole
        print("  %s podle reference: %s" % (role, desc), flush=True)
    return desc


def is_cast(st, work, role):
    """Roli obsadil uživatel vlastním obrázkem — z appky (`cast` v story.json,
    doplní serve.py) nebo přes `story.py cast` (soubor .cast u obrázku).
    U obsazené role rozhoduje o vzhledu reference, ne `desc` ze scénáře."""
    return bool(st["characters"][role].get("cast")) or os.path.exists(work.char(role) + ".cast")


def look_of(st, work, role):
    """Závorka s popisem postavy do promptu. Obsazená role: popis z vlastní
    reference (tagy), ne `desc` ze scénáře — ten popisuje kanonickou postavu a
    Kontext text poslechne víc než obrázek, takže by nahranou referenci přebil."""
    if not is_cast(st, work, role):
        return " (%s)" % st["characters"][role]["desc"]
    d = cast_desc(st, work, role)
    return " (%s)" % d if d else ""


def who(st, work, roles):
    """Věta, která Kontextu řekne, kdo je kdo na referenci (vedle sebe slepené)."""
    ch = st["characters"]
    if len(roles) == 1:
        return "%s is the character shown in the reference image%s." \
            % (ch[roles[0]]["tag"], look_of(st, work, roles[0]))
    a, b = ch[roles[0]], ch[roles[1]]
    return ("The reference image shows two characters side by side: on the left %s%s, "
            "on the right %s%s." % (a["tag"], look_of(st, work, roles[0]),
                                    b["tag"], look_of(st, work, roles[1])))


def full_body(st, sh):
    """Taneční záběr: kostra je celá postava zepředu, keyframe se jí musí podobat."""
    if not sh.get("control"):
        return ""
    return (" %s stands in full body in the middle of the frame, facing the viewer, filling most of the "
            "frame height, arms relaxed, feet visible." % st["characters"][sh["chars"][0]]["tag"])


def kf_prompt(st, work, sh, method):
    cast = [r for r in sh["chars"] if is_cast(st, work, r)]
    if method == "kontext":
        # U obsazené role ještě jednou a natvrdo: vzhled je z reference. Samotné
        # „keep … as in the reference" nestačilo, dokud vedle stál popis ze scénáře.
        hard = (" Take %s appearance only from the reference image: face, hairstyle, hair color, eye "
                "color, outfit and body proportions. Do not invent %s."
                % (("the characters'", "different characters") if len(cast) > 1
                   else (st["characters"][cast[0]]["tag"] + "'s", "a different character"))) \
            if cast else ""
        return ("%s Create a new single illustration, not a character sheet: %s%s Setting: %s. "
                "Keep every character's face, hairstyle, outfit, body proportions and colors exactly as in "
                "the reference.%s %s. Vertical composition, the whole scene with background, no text, "
                "no speech bubbles." % (who(st, work, sh["chars"]), sh["keyframe"].rstrip(". ") + ".",
                                        full_body(st, sh), st["world"], hard, st["style"]))
    ch = st["characters"]
    # IPAdapter: popis obsazené role taky ven, vzhled nese reference přes adaptér
    return ", ".join([SHEET_PREFIX] + [ch[r]["desc"] for r in sh["chars"] if r not in cast]
                     + [sh["keyframe"].rstrip(". "), st["world"], st["style"]]
                     + (["full body, standing, facing viewer"] if sh.get("control") else []))


def kf_key(st, work, sh, method, seed):
    refs = [chain.file_sha(ref_image(st, work, r)) for r in sh["chars"]]
    return sha([kf_prompt(st, work, sh, method), method, seed, refs, KF_W, KF_H,
                st.get("sheet_ckpt") if method == "ipadapter" else None])


def kf_graph(st, work, sh, method, seed):
    refs = [stage(ref_image(st, work, r), "%s_char_%s.png" % (work.name, r)) for r in sh["chars"]]
    prefix = "%s/story/_gen/kf%s" % (work.name, sh["id"])
    if method == "kontext":
        g = template("story_keyframe_kontext")
        g["4"]["inputs"]["image"] = refs[0]
        if len(refs) == 2:
            g["5"]["inputs"]["image"] = refs[1]
        else:
            del g["5"], g["6"]
            g["7"]["inputs"]["image"] = ["4", 0]
        g["9"]["inputs"]["text"] = kf_prompt(st, work, sh, method)
        g["13"]["inputs"].update(width=KF_W, height=KF_H)
        g["14"]["inputs"]["seed"] = seed
        g["16"]["inputs"]["filename_prefix"] = prefix
        return g, "16"
    # IPAdapter drží jen hrdinu záběru: slepená dvojice by se v CLIP ořezu na
    # čtverec rozpůlila; pomocník jde jen textem
    g = template("story_keyframe_ipadapter")
    g["1"]["inputs"]["ckpt_name"] = st.get("sheet_ckpt", SHEET_CKPT)
    g["2"]["inputs"]["text"] = kf_prompt(st, work, sh, method)
    g["4"]["inputs"]["image"] = refs[0]
    g["9"]["inputs"].update(width=KF_W, height=KF_H)
    g["10"]["inputs"]["seed"] = seed
    g["12"]["inputs"]["filename_prefix"] = prefix
    return g, "12"


def select_shots(st, shots):
    if not shots:
        return st["shots"]
    want = {s.strip().zfill(2) for s in shots.split(",") if s.strip()}
    bad = want - {sh["id"] for sh in st["shots"]}
    if bad:
        die("záběry %s v příběhu nejsou" % ", ".join(sorted(bad)))
    return [sh for sh in st["shots"] if sh["id"] in want]


def cmd_keyframes(st, work, shots=None, method=None, seed=None, force=False, reroll=False):
    """Keyframe záběru z obrázků postav. Bez přepínačů jen chybějící a ty,
    u kterých se od minula změnilo zadání (prompt, postava, metoda) — se
    stejným seedem, ať je změna přičitatelná. --reroll = nový seed.
    Metoda: přepínač > ta, kterou keyframe vznikl > `keyframe_method` příběhu —
    `run` bez přepínače tak nepřegeneruje záběry udělané ručně jinou metodou."""
    if method not in (None, "kontext", "ipadapter"):
        die("metoda %r: čekám kontext nebo ipadapter" % method)
    recast(st, work, work.p("story.json"))
    sel = select_shots(st, shots)
    need_chars(st, work, sorted({r for sh in sel for r in sh["chars"]}))
    done = 0
    for sh in sel:
        meta_path = work.kf_meta(sh["id"])
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
        how = method or meta.get("method") or st.get("keyframe_method", "kontext")
        s = seed if seed is not None else meta.get("seed", st["seed"] + 100 + int(sh["id"]))
        if reroll and seed is None and meta:
            s = meta["seed"] + 1
        key = kf_key(st, work, sh, how, s)
        if not (force or reroll) and os.path.exists(work.kf(sh["id"])) and meta.get("key") == key:
            continue
        g, node = kf_graph(st, work, sh, how, s)
        outs = images(chain.submit(g, "keyframe %s" % sh["id"]), node)
        if not outs:
            die("keyframe %s: ComfyUI nevrátil obrázek" % sh["id"])
        shutil.copy(outs[0], ensure(work.kf(sh["id"])))
        json.dump({"seed": s, "key": key, "method": how, "prompt": kf_prompt(st, work, sh, how),
                   "chars": sh["chars"], "time": time.time()},
                  open(meta_path, "w"), indent=1, ensure_ascii=False)
        done += 1
    print("  keyframy: %d nových, %d záběrů celkem" % (done, len(st["shots"])), flush=True)
    return done


# ---------------------------------------------------------------- kontaktní arch

def font(size):
    from PIL import ImageFont
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial Unicode.ttf"):
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def contact_sheet(items, dst, cols=4, tw=256):
    """[(obrázek, titulek, text)] → mřížka JPEG s popisky. Chybějící obrázek = šedé pole."""
    from PIL import Image, ImageDraw
    th = int(tw * KF_H / KF_W)
    lab = 96
    rows = max(1, math.ceil(len(items) / cols))
    sheet = Image.new("RGB", (cols * tw + (cols + 1) * 8, rows * (th + lab) + (rows + 1) * 8), (24, 24, 28))
    d = ImageDraw.Draw(sheet)
    f1, f2 = font(18), font(13)
    for n, (path, title, text) in enumerate(items):
        x, y = 8 + (n % cols) * (tw + 8), 8 + (n // cols) * (th + lab + 8)
        if path and os.path.exists(path):
            im = Image.open(path).convert("RGB")
            im.thumbnail((tw, th))
            sheet.paste(im, (x + (tw - im.width) // 2, y + (th - im.height) // 2))
        else:
            d.rectangle([x, y, x + tw, y + th], fill=(60, 60, 66))
            d.text((x + 10, y + th // 2), "chybí", font=f1, fill=(200, 120, 120))
        d.text((x + 4, y + th + 4), title, font=f1, fill=(240, 240, 240))
        for k, line in enumerate(textwrap.wrap(text, 34)[:4]):
            d.text((x + 4, y + th + 28 + k * 16), line, font=f2, fill=(180, 180, 190))
    sheet.save(ensure(dst), quality=85)
    return dst


def cmd_contact(st, work, lang=None):
    lang = lang or st["lang"]
    items = []
    for sh in st["shots"]:
        tags = " ".join(t for t in ("[tanec %s]" % sh["control"] if sh.get("control") else "",
                                    "[kamera %s]" % sh["camera"] if sh.get("camera") else "",
                                    "×%d" % sh["beats"] if sh["beats"] > 1 else "") if t)
        items.append((work.kf(sh["id"]), "%s %s" % (sh["id"], tags), narration(sh, lang) or sh["keyframe"]))
    dst = contact_sheet(items, work.p("contact.jpg"))
    print("  kontaktní arch: %s" % dst, flush=True)
    return dst


# ---------------------------------------------------------------- brána

def cmd_approve(st, work):
    missing = [sh["id"] for sh in st["shots"] if not os.path.exists(work.kf(sh["id"]))]
    if missing:
        die("chybí keyframy %s — napřed story.py keyframes %s" % (", ".join(missing), st["id"]))
    ok = {sh["id"]: chain.file_sha(work.kf(sh["id"])) for sh in st["shots"]}
    json.dump({"shots": ok, "time": time.time()}, open(ensure(work.p("approved.json")), "w"), indent=1)
    print("  schváleno %d keyframů — story.py run %s" % (len(ok), st["id"]))
    return ok


def gate(st, work):
    """Nesedí-li schválený arch na aktuální keyframy, vrátí seznam záběrů; jinak []."""
    try:
        ok = json.load(open(os.path.join(work.dir, "approved.json")))["shots"]
    except (OSError, ValueError, KeyError):
        return [sh["id"] for sh in st["shots"]]
    return [sh["id"] for sh in st["shots"]
            if not os.path.exists(work.kf(sh["id"])) or ok.get(sh["id"]) != chain.file_sha(work.kf(sh["id"]))]


# ---------------------------------------------------------------- manifest

def tail(st, sh):
    return WAN_TAIL.format(camera=CAMERA_WORDS[sh["_camera"]] if sh.get("_camera") else "static camera")


def manifest_dict(st, work):
    """Příběh → manifest chain.py: záběr = scéna s vlastním keyframem."""
    m = {
        "name": work.name,
        "source": work.kf(st["shots"][0]["id"]),
        "seed": st["seed"],
        "engine": st["engine"],
        "base": "i2v_final_14b_lightning_portrait",
        "fps": 16 if st["engine"] == "wan" else 25,
        "crf": 18,
        "negative": st.get("negative", WAN_NEG),
        "colormatch": {"method": "mkl", "strength": 0.6},
        # uvnitř záběru neznatelný šev přes navazovací snímek, mezi záběry měkké prolnutí
        "transition": "fade", "crossfade": 4,
        "cut_transition": st.get("cut_transition", "fade"),
        "cut_crossfade": st.get("cut_crossfade", 8),
        "_story": st["id"],
        "_pozor": "generováno z příběhu (tools/story.py compile) — úpravy dělej ve stories/, tady se přepíšou",
        "scenes": [],
    }
    for knob, val in (st.get("wan") or {}).items():         # shift / motion / boundary pro celý příběh
        m[knob] = val
    for sh in st["shots"]:
        beats = []
        for j, prompt in enumerate(sh["prompts"]):
            b = {"prompt": prompt, "style_tail": tail(st, sh)}
            if sh.get("control"):
                # kostra řídí pohyb, vzhled drží keyframe (control_ref original = VACE reference)
                b.update(control=sh["control"], boundary=3)
                if j == 0:
                    b["control_start"] = int(sh.get("control_start", 0))
            if sh.get("_camera"):
                b.update(camera=sh["_camera"], camera_speed=float(sh.get("camera_speed", 0.5)))
            # POZOR: `motion` je v příběhu TEXT pohybu (→ prompt), kdežto
            # v manifestu chain.py je to síla LoRA (float). Sílu proto bere
            # záběr z `motion_strength`, jinak by text skončil ve
            # strength_model a ComfyUI graf odmítne.
            for knob in ("shift", "boundary"):
                if knob in sh:
                    b[knob] = sh[knob]
            if "motion_strength" in sh:
                b["motion"] = sh["motion_strength"]
            beats.append(b)
        m["scenes"].append({"name": "shot" + sh["id"], "source": work.kf(sh["id"]), "beats": beats})
    return m


def timeline(st, m):
    """Časy záběrů ve slepeném videu (s) z manifestu po chain.load_dict.
    start = první snímek záběru (začíná prolínání), clear = prolnutí dokončené,
    end = poslední snímek; vypravěč mluví v [clear + 0.2, end − 0.1]."""
    fps, out = m["fps"], []
    for s, sh in zip(m["scenes"], st["shots"]):
        f0 = chain.frames_upto(m, s["first"]) - chain.overlap(m, s["first"])
        f1 = chain.frames_upto(m, s["last"] + 1)
        t0, t1 = f0 / fps, f1 / fps
        clear = t0 + chain.overlap(m, s["first"]) / fps
        out.append({"shot": sh["id"], "start": round(t0, 3), "clear": round(clear, 3), "end": round(t1, 3),
                    "voice_at": round(clear + 0.2, 3), "avail": round(t1 - 0.1 - clear - 0.2, 3),
                    "text": {lang: narration(sh, lang) for lang in LANGS}})
    total = chain.frames_upto(m, len(m["beats"])) / fps
    return {"fps": fps, "total": round(total, 3), "shots": out}


def load_manifest_dict(m):
    return chain.load_dict(json.loads(json.dumps(m)))       # load_dict mění vstup — kopie


def speech_warnings(tl, lang, scale=LENGTH_SCALE):
    """Záběry, kde odhad délky čtení (slova / rychlost) přesahuje prostor."""
    out = []
    rate = WORDS_PER_S.get(lang, 2.3) / scale
    for s in tl["shots"]:
        words = len(s["text"].get(lang, "").split())
        need = words / rate
        if need > s["avail"]:
            out.append((s["shot"], words, need, s["avail"]))
    return out


def cmd_compile(st, work, review=True):
    if review:
        bad = gate(st, work)
        if bad:
            die("keyframy %s nejsou schválené (nebo se od schválení změnily) — prohlédni kontaktní "
                "arch a story.py approve %s; bez brány --no-review" % (", ".join(bad), st["id"]))
    m = manifest_dict(st, work)
    os.makedirs(CHAINS, exist_ok=True)
    json.dump(m, open(work.manifest, "w"), indent=2, ensure_ascii=False)
    tl = timeline(st, load_manifest_dict(m))
    json.dump(tl, open(ensure(work.p("narration.json")), "w"), indent=1, ensure_ascii=False)
    for lang in LANGS:
        for shot, words, need, avail in speech_warnings(tl, lang):
            print("  ! záběr %s (%s): %d slov ≈ %.1f s čtení, prostor %.1f s — zkrať text nebo přidej beat"
                  % (shot, lang, words, need, avail))
    print("  manifest: %s  (%d záběrů, %.1f s)" % (os.path.relpath(work.manifest, HERE),
                                                  len(st["shots"]), tl["total"]), flush=True)
    return work.manifest


def cmd_plan(st, work):
    m = load_manifest_dict(manifest_dict(st, work))
    tl = timeline(st, m)
    hd_px, dr_px = 752 * 1312, 496 * 880
    units = sum(b["length"] / 81 * (1.5 if b.get("control") else 1.0) for b in m["beats"])
    print("%s — %s  (%d záběrů, %d beatů, %.1f s)" % (st["id"], st.get("title", ""), len(st["shots"]),
                                                     len(m["beats"]), tl["total"]))
    for s, sh in zip(tl["shots"], st["shots"]):
        tags = ("[tanec %s] " % sh["control"] if sh.get("control") else "") + \
               ("[kamera %s] " % sh["camera"] if sh.get("camera") else "")
        print("  %s  %5.1f–%5.1f s  ×%d  %-6s %s%s" % (sh["id"], s["start"], s["end"], sh["beats"],
                                                   "+".join(sh["chars"]), tags, narration(sh, st["lang"])[:60]))
    for lang in LANGS:
        for shot, words, need, avail in speech_warnings(tl, lang):
            print("  ! %s %s: %d slov ≈ %.1f s, prostor %.1f s" % (shot, lang, words, need, avail))
    per = chain.SEC_PER_MPX / 1e6 / 60
    print("odhad GPU  keyframy ~%d min, render draft ~%d min / hd ~%d min, zvuk ~3 min"
          % (round(len(st["shots"]) * 50 / 60), round(units * dr_px * per), round(units * hd_px * per)))
    chars = {r: bool(char_path(st, work, r)) for r in st["characters"]}
    print("postavy    %s" % ", ".join("%s %s" % (r, "ok" if v else "CHYBÍ") for r, v in chars.items()))


# ---------------------------------------------------------------- vypravěč

def piper_bin():
    p = os.path.join(PIPER_HOME, "venv", "bin", "piper")
    if not os.path.exists(p):
        die("Piper chybí v %s — nainstaluj tools/get_piper.sh (nebo nastav PIPER_HOME)" % PIPER_HOME)
    return p


def voice_model(st, lang):
    name = (st.get("voice") or {}).get(lang) or VOICES.get(lang)
    if not name:
        die("pro jazyk %r nemám hlas (VOICES / story.voice)" % lang)
    p = os.path.join(PIPER_HOME, "voices", name + ".onnx")
    if not os.path.exists(p):
        die("hlas %s chybí (%s) — tools/get_piper.sh %s" % (name, p, name))
    return p


def wav_dur(path):
    with wave.open(path) as w:
        return w.getnframes() / float(w.getframerate())


# Piper jede na doraz: špičky sedí na ±1.0 a v hlasitějších větách je i dvě
# stě vzorků uříznutých rovně — ve výsledku to chrastí, zvlášť pod hudbou.
# adeclip špičky dopočítá, limiter je pak drží pod −1 dB. Při stejném průchodu
# se srovná i lichý datový blok, na který si ffmpeg v mixu stěžoval
# („Invalid PCM packet, data has size 1").
VOICE_FIX = "adeclip,alimiter=limit=0.891:level=false"
MUSIC_XF = 2.0                    # překryv smyčky hudby (s)
# Generovaná hudba (ACE-Step i LTX dárce) sype v tichých místech fizz nad
# 6 kHz — naměřeno na hotovém příběhu: v šumivých oknech leželo 60 % energie
# nad 6 kHz proti 20 % v čistých. Je tichý, ale mezi větami vypravěčky je ho
# slyšet. Shelf ho srazí na polovinu a hlasitost skladby se nezmění.
MUSIC_TAME = "highshelf=f=6000:g=-9"


def cmd_voice(st, work, lang):
    """Věta záběru → WAV. Otisk textu a hlasu vedle, takže se přečte jen změna."""
    model, scale = voice_model(st, lang), float(st.get("length_scale", LENGTH_SCALE))
    n = 0
    for sh in st["shots"]:
        text = narration(sh, lang)
        dst = work.voice(sh["id"], lang)
        if not text:
            if os.path.exists(dst):
                os.remove(dst)
            continue
        key = sha([text, os.path.basename(model), scale, VOICE_FIX])
        if os.path.exists(dst) and os.path.exists(dst + ".key") and open(dst + ".key").read() == key:
            continue
        raw = dst + ".piper.wav"
        subprocess.run([piper_bin(), "-m", model, "-f", ensure(raw), "--length-scale", str(scale),
                        "--sentence-silence", "0.25"], input=text.replace("\n", " ").encode(),
                       check=True, capture_output=True)
        ffmpeg("-i", raw, "-af", VOICE_FIX, "-c:a", "pcm_s16le", dst)
        os.remove(raw)
        open(dst + ".key", "w").write(key)
        print("  hlas %s  %.1f s  %s" % (sh["id"], wav_dur(dst), text[:50]), flush=True)
        n += 1
    print("  vypravěč (%s): %d nových vět" % (lang, n), flush=True)


# ---------------------------------------------------------------- hudba

def ace_music(prompt, dur, seed, dst):
    """AiStack services/audio (ACE-Step 1.5): job → poll → stažení."""
    hdr = {"Content-Type": "application/json"}
    if os.environ.get("AUDIO_API_KEY"):
        hdr["Authorization"] = "Bearer " + os.environ["AUDIO_API_KEY"]
    body = {"prompt": prompt, "duration_s": round(min(dur, 600), 1), "seed": seed, "instrumental": True,
            "loop": False, "format": "ogg", "variations": 1}
    req = urllib.request.Request(AUDIO_URL + "/v1/audio/music", json.dumps(body).encode(), hdr)
    jid = json.load(urllib.request.urlopen(req, timeout=30))["job_id"]
    t0 = time.time()
    while True:
        time.sleep(5)
        j = json.load(urllib.request.urlopen(urllib.request.Request(
            AUDIO_URL + "/v1/audio/jobs/" + jid, headers=hdr), timeout=30))
        if j["status"] == "done":
            break
        if j["status"] == "error":
            raise RuntimeError(j.get("error") or "hudba spadla")
        if time.time() - t0 > 1200:          # první hudba po startu kontejneru ~8 min (váhy)
            raise RuntimeError("hudba přes 20 min, vzdávám")
    url = j["outputs"][0]["url"]
    url = url if url.startswith("http") else AUDIO_URL + url
    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=120) as r, open(dst, "wb") as fh:
        shutil.copyfileobj(r, fh)
    print("  hudba ACE-Step  %.0f s  (%s)" % (time.time() - t0, prompt[:50]), flush=True)
    return dst


def cmd_music(st, work):
    """Hudba přes celé video. ACE-Step (hotová služba v AiStacku) složí celou
    délku; když neběží (noční režim 00–07, kontejner dole), zaskočí LTX dárce
    z chain.py (19 s smyčka). Bez `music` v příběhu nic."""
    prompt, engine = st.get("music"), st.get("music_engine", "ace")
    if not prompt or engine == "none":
        return None
    # délka z časové osy (ne z mp4): klíč cache se nemění jen tím, že se video přeslepilo
    total = json.load(open(work.p("narration.json")))["total"]
    key = sha([prompt, st["seed"], round(total), engine])
    for ext in ("ogg", "m4a"):
        p = work.p("music." + ext)
        if os.path.exists(p) and os.path.exists(p + ".key") and open(p + ".key").read() == key:
            return p
    chain.free_models("před hudbou")          # unified paměť: ať má ACE-Step kam
    dst = None
    if engine == "ace":
        try:
            dst = ace_music(prompt, total + 2, st["seed"], ensure(work.p("music.ogg")))
        except (urllib.error.URLError, OSError, RuntimeError, KeyError, ValueError) as e:
            print("  ! ACE-Step nedostupný (%s) — zkouším LTX dárce" % e, flush=True)
    if dst is None:
        m = chain.load(work.manifest)
        m["soundtrack"] = prompt
        stage(chain.source_path(m), "%s_seed00.png" % m["name"])
        try:
            dst = ensure(work.p("music.m4a"))
            shutil.copy(chain.soundtrack_track(m), dst)
        except SystemExit as e:
            print("  ! ani LTX dárce (%s) — video bude bez hudby" % e, flush=True)
            return None
    open(dst + ".key", "w").write(key)
    return dst


# ---------------------------------------------------------------- mix

def final_video(st, work):
    """Slepené video z chain.py: Wan po RIFE (32 fps), LTX rovnou (25 fps)."""
    m = chain.load(work.manifest)
    rife = os.path.join(work.root, "%s_%dfps.mp4" % (work.name, m["fps"] * 2))
    for p in ([chain.full_path(m)] if m["engine"] == "ltx" else [rife, chain.full_path(m)]):
        if os.path.exists(p):
            return p
    die("video %s ještě není — napřed story.py run %s" % (work.name, st["id"]))


def srt_time(t):
    ms = int(round(t * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + list(args), check=True)


LOUD = "I=-16:TP=-1.5:LRA=11"


def loudnorm(inputs, filt, total):
    """Nastavení loudnorm pro druhý průchod. Jednoprůchodový loudnorm cíl jen
    odhaduje a u příběhu s tichou hudbou pod řečí podstřelil na −19 LUFS;
    první průchod proto jen měří a druhý si nese naměřené hodnoty, takže
    výsledek na −16 LUFS opravdu sedí. Když měření selže, jede jako dřív."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats"] + list(inputs) + [
                "-filter_complex", filt + ";[pre]loudnorm=%s:print_format=json[aout]" % LOUD,
                "-map", "[aout]", "-t", "%.3f" % total, "-vn", "-f", "null", "-"],
            capture_output=True, text=True, check=True).stderr
        d = json.loads(out[out.rindex("{"):out.rindex("}") + 1])
        m = {k: float(d[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh")}
        if any(v == float("-inf") or v != v for v in m.values()):
            raise ValueError("ticho")
        return ("loudnorm=%s:measured_I=%.2f:measured_TP=%.2f:measured_LRA=%.2f:"
                "measured_thresh=%.2f:linear=true" % (LOUD, m["input_i"], m["input_tp"],
                                                      m["input_lra"], m["input_thresh"]))
    except (subprocess.CalledProcessError, ValueError, KeyError) as e:
        print("  ! měření hlasitosti selhalo (%s) — jedu jednoprůchodově" % str(e)[:60], flush=True)
        return "loudnorm=" + LOUD


def music_bed(st, src, total, work):
    """Hudební podklad na celou délku: zkrocené výšky a, když je stopa kratší
    (typicky 19s LTX dárce), složená přes acrossfade. Natvrdo `-stream_loop`
    tu byl dřív a šev lupal."""
    dur = chain.duration_s(src)
    tame = st.get("music_tame", MUSIC_TAME)
    n = 1 if dur >= total - 0.2 or dur <= MUSIC_XF * 2 \
        else max(2, int(math.ceil((total - dur) / (dur - MUSIC_XF))) + 1)
    dst = work.p("music_bed.m4a")
    key = sha([os.path.basename(src), chain.file_sha(src), round(total, 1), n, MUSIC_XF, tame])
    if os.path.exists(dst) and os.path.exists(dst + ".key") and open(dst + ".key").read() == key:
        return dst
    args, filt, prev = [], [], "[0:a]"
    for i in range(n):
        args += ["-i", src]
    for i in range(1, n):
        out = "[x%d]" % i
        filt.append("%s[%d:a]acrossfade=d=%.2f:c1=tri:c2=tri%s" % (prev, i, MUSIC_XF, out))
        prev = out
    filt.append("%s%s[bed]" % (prev, tame or "anull"))
    ffmpeg(*args, "-filter_complex", ";".join(filt), "-map", "[bed]", "-t", "%.3f" % total,
           "-c:a", "aac", "-b:a", "192k", ensure(dst))
    open(dst + ".key", "w").write(key)
    print("  hudba na %.1f s: %s, výšky %s" % (total, "%d× smyčka s překryvem %.1f s" % (n, MUSIC_XF)
                                               if n > 1 else "jedním kusem", tame or "beze změny"), flush=True)
    return dst


def cmd_mix(st, work, lang, music=False):
    """Vypravěč na začátky záběrů, hudba pod něj se ztlumením (sidechain),
    titulky (SRT + vypálená varianta) a 16:9 s rozmazaným pozadím.
    `music` = výsledek cmd_music z téhož běhu (None = bez hudby, nezkoušet
    znovu); False = zjistit teď."""
    tl = json.load(open(work.p("narration.json")))
    video = final_video(st, work)
    total = chain.duration_s(video)
    if music is False:
        music = cmd_music(st, work)
    voices, subs, prev_end = [], [], 0.0
    for s in tl["shots"]:
        wav, text = work.voice(s["shot"], lang), s["text"].get(lang, "")
        if not text or not os.path.exists(wav):
            continue
        d = wav_dur(wav)
        at = max(s["voice_at"], prev_end + 0.15)
        avail = s["end"] - 0.1 - at
        tempo = min(1.2, d / avail) if avail > 0 and d > avail else 1.0
        spoken = d / tempo
        if spoken > avail + 0.3:
            print("  ! záběr %s: vypravěč přesahuje o %.1f s i zrychlený — zkrať text"
                  % (s["shot"], spoken - avail), flush=True)
        voices.append((wav, at, tempo))
        subs.append([at, at + spoken + 0.3, text])
        prev_end = at + spoken
    for a, b in zip(subs, subs[1:]):                      # titulek nepřekryje další
        a[1] = min(a[1], b[0] - 0.05)
    srt = ensure(work.p("%s.srt" % lang))
    with open(srt, "w") as fh:
        for n, (a, b, text) in enumerate(subs, 1):
            fh.write("%d\n%s --> %s\n%s\n\n" % (n, srt_time(a), srt_time(min(b, total)),
                                                "\n".join(textwrap.wrap(text, 32))))

    inputs, filt = ["-i", video], []
    for j, (wav, at, tempo) in enumerate(voices):
        inputs += ["-i", wav]
        t = ",atempo=%.3f" % tempo if tempo > 1.001 else ""
        ms = int(at * 1000)
        filt.append("[%d:a]aresample=44100,aformat=channel_layouts=stereo%s,adelay=%d|%d[n%d]"
                    % (j + 1, t, ms, ms, j))
    if voices:
        filt.append("%samix=inputs=%d:normalize=0:duration=longest,apad,atrim=0:%.3f[voice]"
                    % ("".join("[n%d]" % j for j in range(len(voices))), len(voices), total))
    if music:
        idx = len(voices) + 1
        inputs += ["-i", music_bed(st, music, total, work)]  # zkrocené výšky, případně slepená smyčka
        filt.append("[%d:a]aresample=44100,aformat=channel_layouts=stereo,atrim=0:%.3f,"
                    "afade=t=in:d=1.5,afade=t=out:st=%.3f:d=2.5,volume=%.2f[mus]"
                    % (idx, total, max(0.0, total - 2.5), float(st.get("music_volume", 0.45))))
    if voices and music:
        filt += ["[voice]asplit=2[vo][vsc]",
                 "[mus][vsc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=600[duck]",
                 "[duck][vo]amix=inputs=2:normalize=0:duration=first[pre]"]
    elif voices:
        filt.append("[voice]anull[pre]")
    elif music:
        filt.append("[mus]anull[pre]")
    else:
        filt.append("[0:a]anull[pre]")
    if st["engine"] == "ltx" and (voices or music):
        # LTX nese vlastní zvuk scény — nechat ho tiše pod vším jako atmosféru
        filt[-1] = filt[-1].replace("[pre]", "[pre0]")
        filt.append("[0:a]volume=0.35[amb];[pre0][amb]amix=inputs=2:normalize=0:duration=first[pre]")
    main = work.final(lang)
    norm = loudnorm(inputs, ";".join(filt), total)
    ffmpeg(*inputs, "-filter_complex", ";".join(filt + ["[pre]%s,aresample=44100[aout]" % norm]),
           "-map", "0:v", "-map", "[aout]", "-t", "%.3f" % total, "-c:v", "copy", "-c:a", "aac",
           "-b:a", "160k", "-movflags", "+faststart", main)

    enc = ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p", "-profile:v", "high",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
           "-c:a", "copy", "-movflags", "+faststart"]
    style = ("FontName=DejaVu Sans,FontSize=13,PrimaryColour=&H00FFFFFF,OutlineColour=&H00202020,"
             "BorderStyle=1,Outline=1.6,Shadow=0,Alignment=2,MarginV=28,MarginL=14,MarginR=14")
    if subs:
        # vypálené titulky jsou bonus: bez libass (filtr subtitles) zůstane SRT vedle a mix běží dál
        try:
            ffmpeg("-i", main, "-vf", "subtitles=filename='%s':force_style='%s'" % (srt, style), *enc,
                   work.final(lang, "sub"))
        except subprocess.CalledProcessError:
            print("  ! vypálení titulků selhalo (ffmpeg bez libass?) — zůstává %s" % srt, flush=True)
    ffmpeg("-i", main, "-filter_complex",
           "[0:v]split=2[a][b];[a]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
           "gblur=sigma=24,eq=brightness=-0.06[bg];[b]scale=-2:1080[fg];[bg][fg]overlay=(W-w)/2:0[v]",
           "-map", "[v]", "-map", "0:a", *enc, work.final(lang, "16x9"))
    print("  mix: %d vět vypravěče, hudba %s, %.1f s" % (len(voices), os.path.basename(music) if music else "ne",
                                                       total), flush=True)
    for p in (main, work.final(lang, "sub"), work.final(lang, "16x9"), srt):
        if os.path.exists(p):
            print("  %s" % p)
    return main


# ---------------------------------------------------------------- celý běh

def cmd_run(st, work, hd=False, resume=False, only=None, review=True, until=None, lang=None, method=None):
    lang = lang or st["lang"]
    need_chars(st, work)
    phase("keyframes")
    cmd_keyframes(st, work, method=method)
    cmd_contact(st, work, lang)
    if until == "review":
        phase("review")
        print("  keyframy čekají na schválení: story.py approve %s" % st["id"], flush=True)
        return None
    phase("compile")
    path = cmd_compile(st, work, review=review)
    chain.free_models("po keyframech")       # FLUX Kontext ven, ať má Wan 14B paměť
    phase("render")
    cmd = [sys.executable, os.path.join(HERE, "chain.py"), path, "--all"]
    cmd += (["--hd"] if hd else []) + (["--resume"] if resume else []) + (["--only", only] if only else [])
    subprocess.run(cmd, cwd=HERE, check=True)
    phase("voice")
    cmd_voice(st, work, lang)
    phase("music")
    music = cmd_music(st, work)
    phase("mix")
    out = cmd_mix(st, work, lang, music=music)
    print("\nHOTOVO  %s" % out, flush=True)
    return out


def cmd_batch(ids, hd=False, auto_cast=False, lang=None):
    """Přes noc: keyframy + arch všech příběhů, draft rovnou (bez brány),
    hd jen u schválených. Chyba jednoho příběhu ostatní nezastaví."""
    report = []
    for sid in ids or catalog_ids():
        st, work = load_story(sid)
        try:
            missing = [r for r in st["characters"] if not char_path(st, work, r)]
            if missing and auto_cast:
                for r in missing:
                    cmd_sheet(st, work, r, n=1)
                    cmd_sheet(st, work, r, pick=1)
            elif missing:
                report.append((sid, "přeskočeno — chybí postavy %s (--auto-cast)" % ", ".join(missing)))
                continue
            if hd and gate(st, work):
                cmd_keyframes(st, work)
                cmd_contact(st, work)
                report.append((sid, "hd přeskočeno — keyframy neschválené"))
                continue
            out = cmd_run(st, work, hd=hd, resume=True, review=hd, lang=lang)
            report.append((sid, out))
        except (SystemExit, subprocess.CalledProcessError) as e:
            report.append((sid, "CHYBA %s" % e))
            print("  ! %s: %s" % (sid, e), flush=True)
    print("\nBATCH")
    for sid, what in report:
        print("  %-24s %s" % (sid, what))


def cmd_ls():
    print("%-22s %-30s %5s %8s %9s %9s %s" % ("příběh", "název", "záběr", "postavy", "keyframy", "schváleno", "hotovo"))
    for sid in catalog_ids():
        try:
            st, work = load_story(sid)
        except SystemExit as e:
            print("%-22s CHYBA %s" % (sid, e))
            continue
        chars = sum(bool(char_path(st, work, r)) for r in st["characters"])
        kfs = sum(os.path.exists(work.kf(sh["id"])) for sh in st["shots"])
        done = [l for l in LANGS if os.path.exists(work.final(l))]
        print("%-22s %-30s %5d %4d/%-3d %4d/%-4d %9s %s" % (
            sid, st.get("title", "")[:30], len(st["shots"]), chars, len(st["characters"]), kfs,
            len(st["shots"]), "ano" if kfs and not gate(st, work) else "ne", ",".join(done) or "—"))


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)     # serve.py čte log průběžně
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def story_arg(p):
        p.add_argument("story", help="id ze stories/ nebo cesta k JSON")
        p.add_argument("--work", help="pracovní adresář v output/ (default story_<id>)")
        return p

    sub.add_parser("ls")
    story_arg(sub.add_parser("plan"))
    p = story_arg(sub.add_parser("sheet")); p.add_argument("role")  # noqa: E702
    p.add_argument("--n", type=int, default=4); p.add_argument("--seed", type=int)  # noqa: E702
    p.add_argument("--pick", type=int)
    p = story_arg(sub.add_parser("cast")); p.add_argument("role"); p.add_argument("image")  # noqa: E702
    p = story_arg(sub.add_parser("keyframes"))
    p.add_argument("--shot", help="jen tyhle záběry, např. 03,07")
    p.add_argument("--method", choices=("kontext", "ipadapter"))
    p.add_argument("--seed", type=int)
    p.add_argument("--force", action="store_true", help="přegenerovat se stejným seedem")
    p.add_argument("--reroll", action="store_true", help="přegenerovat s novým seedem")
    p = story_arg(sub.add_parser("contact")); p.add_argument("--lang", choices=LANGS)  # noqa: E702
    story_arg(sub.add_parser("approve"))
    p = story_arg(sub.add_parser("compile")); p.add_argument("--no-review", action="store_true")  # noqa: E702
    p = story_arg(sub.add_parser("voice")); p.add_argument("--lang", choices=LANGS)  # noqa: E702
    story_arg(sub.add_parser("music"))
    p = story_arg(sub.add_parser("mix")); p.add_argument("--lang", choices=LANGS)  # noqa: E702
    p = story_arg(sub.add_parser("run"))
    p.add_argument("--hd", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--only", metavar="shotNN", help="jen záběr (a co na něm visí)")
    p.add_argument("--no-review", action="store_true", help="bez brány kontaktního archu")
    p.add_argument("--until", choices=("review",), help="skonči po keyframech a archu")
    p.add_argument("--lang", choices=LANGS)
    p.add_argument("--method", choices=("kontext", "ipadapter"))
    p = sub.add_parser("batch"); p.add_argument("stories", nargs="*")  # noqa: E702
    p.add_argument("--hd", action="store_true")
    p.add_argument("--auto-cast", action="store_true", help="chybějící postavy vygenerovat (1. kandidát)")
    p.add_argument("--lang", choices=LANGS)
    a = ap.parse_args()

    if a.cmd == "ls":
        cmd_ls(); sys.exit(0)  # noqa: E702
    if a.cmd == "batch":
        cmd_batch(a.stories, hd=a.hd, auto_cast=a.auto_cast, lang=a.lang); sys.exit(0)  # noqa: E702
    st, work = load_story(a.story, a.work)
    if a.cmd == "plan":
        cmd_plan(st, work)
    elif a.cmd == "sheet":
        cmd_sheet(st, work, a.role, n=a.n, seed=a.seed, pick=a.pick)
    elif a.cmd == "cast":
        cmd_cast(st, work, a.role, a.image)
    elif a.cmd == "keyframes":
        cmd_keyframes(st, work, a.shot, a.method, a.seed, a.force, a.reroll)
        cmd_contact(st, work)
    elif a.cmd == "contact":
        cmd_contact(st, work, a.lang)
    elif a.cmd == "approve":
        cmd_approve(st, work)
    elif a.cmd == "compile":
        cmd_compile(st, work, review=not a.no_review)
    elif a.cmd == "voice":
        cmd_voice(st, work, a.lang or st["lang"])
    elif a.cmd == "music":
        cmd_music(st, work)
    elif a.cmd == "mix":
        cmd_mix(st, work, a.lang or st["lang"])
    elif a.cmd == "run":
        cmd_run(st, work, hd=a.hd, resume=a.resume, only=a.only, review=not a.no_review,
                until=a.until, lang=a.lang, method=a.method)
