#!/usr/bin/env python3
"""Kiran ad — materializace a render jednotlivých záběrů ze shots.json.

  ./render.py --materialize          # zapíše workflows/shotNN.json (bez renderu)
  ./render.py --validate             # zkontroluje je proti /object_info
  ./render.py --shots 05,09          # přerenderuje jen tyhle záběry
  ./render.py --all --draft          # všechny v 480p
  ./render.py --shots 03 --seed 7    # jiný seed

Každý workflows/shotNN.json je plnohodnotný API workflow s DOSAZENÝMI hodnotami
(prompt, keyframe, rozlišení, seed) — jde otevřít v ComfyUI přetažením na plátno
a upravit ručně. Pokud soubor existuje, render.py ho použije beze změny; s
--draft nebo --seed si workflow vyrobí z manifestu znovu.
"""
import argparse, json, os, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
API = "http://localhost:8188"
M = json.load(open(os.path.join(HERE, "shots.json")))
WF_DIR = os.path.join(HERE, "workflows")


def base_workflow(name):
    return json.load(open(os.path.join(ROOT, "workflows", name + ".json")))


def build(shot, draft=False, seed=None):
    """Manifest -> hotový API workflow s dosazenými hodnotami."""
    d = M["defaults"]
    wf_name = shot.get("workflow", d["workflow"])
    w, h = (d["draft"]["width"], d["draft"]["height"]) if draft else (d["width"], d["height"])
    length = shot.get("length", d["length"])
    seed = seed if seed is not None else shot.get("seed", d["seed"])
    prompt = shot["prompt"] + M["style_tail"]
    g = base_workflow(wf_name)
    prefix = "kiran_ad_%s/shot%s" % ("draft" if draft else "hd", shot["id"])

    if wf_name == "camera_control":
        g["9"]["inputs"]["image"] = shot["keyframe"]
        g["10"]["inputs"].update(camera_pose=shot["camera_pose"], width=w, height=h, length=length)
        g["6"]["inputs"]["text"] = prompt
        g["7"]["inputs"]["text"] = M["negative"]
        for n in ("12", "13"):
            g[n]["inputs"]["noise_seed"] = seed
        g["15"]["inputs"]["filename_prefix"] = prefix
    else:  # i2v_final_14b_lightning
        g["10"]["inputs"]["image"] = shot["keyframe"]
        g["8"]["inputs"]["text"] = prompt
        g["9"]["inputs"]["text"] = M["negative"]
        g["12"]["inputs"].update(width=w, height=h, length=length)
        for n in ("13", "14"):
            g[n]["inputs"]["noise_seed"] = seed
        g["16"]["inputs"]["filename_prefix"] = prefix
    return g


def materialize():
    os.makedirs(WF_DIR, exist_ok=True)
    for s in M["shots"]:
        path = os.path.join(WF_DIR, "shot%s.json" % s["id"])
        with open(path, "w") as fh:
            json.dump(build(s), fh, indent=1, ensure_ascii=False)
        wfn = s.get("workflow", M["defaults"]["workflow"])
        print("  %-28s %-26s %s" % (os.path.relpath(path, HERE), wfn, s["title"]))


def validate():
    """Ověří class_type a povinné vstupy proti běžícímu ComfyUI."""
    info = json.load(urllib.request.urlopen(API + "/object_info"))
    bad = 0
    for s in M["shots"]:
        path = os.path.join(WF_DIR, "shot%s.json" % s["id"])
        g = json.load(open(path)) if os.path.exists(path) else build(s)
        for nid, node in g.items():
            ct = node["class_type"]
            if ct not in info:
                print("  x shot%s node %s: neznámý typ %s" % (s["id"], nid, ct)); bad += 1; continue
            missing = set(info[ct]["input"].get("required", {})) - set(node["inputs"])
            if missing:
                print("  x shot%s node %s (%s): chybí %s" % (s["id"], nid, ct, sorted(missing))); bad += 1
    print("  ok — všechny workflow validní" if not bad else "  %d problém(ů)" % bad)
    return bad == 0


def run(shot, draft, seed):
    path = os.path.join(WF_DIR, "shot%s.json" % shot["id"])
    use_file = os.path.exists(path) and not draft and seed is None
    g = json.load(open(path)) if use_file else build(shot, draft, seed)
    req = urllib.request.Request(API + "/prompt", json.dumps({"prompt": g}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        pid = json.load(urllib.request.urlopen(req))["prompt_id"]
    except urllib.error.HTTPError as e:
        print("  x shot%s odmítnut: %s" % (shot["id"], json.load(e).get("node_errors")))
        return
    t0 = time.time()
    while True:
        time.sleep(5)
        hist = json.load(urllib.request.urlopen(API + "/history/" + pid))
        if pid in hist:
            st = hist[pid]["status"]
            if st.get("completed"):
                print("  ok shot%s  %.0fs" % (shot["id"], time.time() - t0), flush=True); return
            if st.get("status_str") == "error":
                print("  x shot%s chyba při běhu" % shot["id"], flush=True); return
        if time.time() - t0 > 3600:
            print("  ! shot%s stále běží (přestávám čekat)" % shot["id"], flush=True); return


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--materialize", action="store_true", help="zapiš workflows/shotNN.json")
    ap.add_argument("--validate", action="store_true", help="ověř proti /object_info")
    ap.add_argument("--shots", help="čárkou oddělená ID, např. 05,09")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--draft", action="store_true", help="480p místo 720p")
    ap.add_argument("--seed", type=int)
    a = ap.parse_args()

    if a.materialize:
        materialize()
    if a.validate:
        sys.exit(0 if validate() else 1)
    if a.all or a.shots:
        ids = [x["id"] for x in M["shots"]] if a.all else a.shots.split(",")
        for shot in [x for x in M["shots"] if x["id"] in ids]:
            print("-> shot%s - %s" % (shot["id"], shot["title"]), flush=True)
            run(shot, a.draft, a.seed)
        print("HOTOVO", flush=True)
    if not any([a.materialize, a.validate, a.all, a.shots]):
        ap.print_help()
