#!/usr/bin/env python3
"""API workflow -> UI (LiteGraph) formát, aby šla otevřít ze sidebaru ComfyUI.

Sidebar načítá soubor rovnou jako UI graf; API formát detekuje jen file-reader
(drag & drop), proto ze sidebaru vyskočí prázdné plátno. Tenhle převod z API
mapy nodů dopočítá `nodes`, `links`, pozice a `widgets_values`.

Zrádnost, kvůli které se to nedá napsat naslepo: widget se seedem má v UI
navíc hodnotu `control_after_generate` ("randomize"), která v API formátu
nemá protějšek. Pozná se podle příznaku control_after_generate v /object_info.

  ./api2ui.py workflows/*.json -o ~/ComfyUI/user/default/workflows/video-stack
  ./api2ui.py --selftest      # round-trip proti skutečnému UI workflow
"""
import argparse, glob, json, os, sys, urllib.request

API = os.environ.get("COMFY_API", "http://localhost:8188")


def widget_order(spec):
    """Jména widgetů v pořadí, v jakém je frontend vytváří (+ control_after_generate)."""
    order = []
    for section in ("required", "optional"):
        for name, s in (spec.get(section) or {}).items():
            order.append(name)
            opts = s[1] if len(s) > 1 and isinstance(s[1], dict) else {}
            # widgety, které frontend přidává navíc a v API formátu nemají protějšek
            if opts.get("control_after_generate"):
                order.append("__extra__ctrl_" + name)     # "fixed" / "randomize" / ...
            if opts.get("image_upload") or opts.get("animated_image_upload"):
                order.append("__extra__upload_" + name)   # jméno vstupu, typicky "image"
    return order


def convert(api, info):
    nodes, links, link_id = [], [], 0
    # cílové sloty: kam který link vede
    order_map = {nid: i for i, nid in enumerate(sorted(api, key=lambda x: int(x)))}

    # rozvržení: sloupce podle hloubky v grafu
    depth = {}

    def calc(nid, seen=()):
        if nid in depth:
            return depth[nid]
        if nid in seen:
            return 0
        d = 0
        for v in api[nid]["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] in api:
                d = max(d, calc(v[0], seen + (nid,)) + 1)
        depth[nid] = d
        return d

    for nid in api:
        calc(nid)
    col_count = {}

    for nid in sorted(api, key=lambda x: int(x)):
        node = api[nid]
        ct = node["class_type"]
        spec = info[ct]["input"]
        d = depth[nid]
        row = col_count.get(d, 0)
        col_count[d] = row + 1

        inputs_ui, widgets = [], []
        linked = {k: v for k, v in node["inputs"].items()
                  if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and v[0] in api}

        for name in widget_order(spec):
            if name.startswith("__extra__"):
                widgets.append(name.split("_")[-1] if "upload_" in name else "fixed")
                continue
            if name in linked:
                continue
            if name in node["inputs"]:
                widgets.append(node["inputs"][name])

        for name, val in node["inputs"].items():
            if name not in linked:
                continue
            src, slot = val
            src_type = info[api[src]["class_type"]]["output"][slot]
            link_id += 1
            inputs_ui.append({"name": name, "type": src_type, "link": link_id})
            links.append([link_id, int(src), slot, int(nid), len(inputs_ui) - 1, src_type])

        outputs_ui = []
        for i, (otype, oname) in enumerate(zip(info[ct]["output"], info[ct]["output_name"])):
            outputs_ui.append({"name": oname, "type": otype, "links": [], "shape": 3, "slot_index": i})

        nodes.append({
            "id": int(nid), "type": ct,
            "pos": [d * 400 - 200, row * 260 + 60],
            "size": {"0": 340, "1": max(60, 30 + 26 * (len(widgets) + len(inputs_ui)))},
            "flags": {}, "order": order_map[nid], "mode": 0,
            "inputs": inputs_ui, "outputs": outputs_ui,
            "properties": {"Node name for S&R": ct},
            "widgets_values": widgets,
        })

    # doplnit odchozí linky do outputs
    by_id = {n["id"]: n for n in nodes}
    for lid, src, slot, dst, dslot, _t in links:
        by_id[src]["outputs"][slot]["links"].append(lid)

    return {"last_node_id": max(int(k) for k in api), "last_link_id": link_id,
            "nodes": nodes, "links": links, "groups": [], "config": {},
            "extra": {}, "version": 0.4}


def ui_to_api(ui, info):
    """Opačný směr — jen pro selftest (co dělá graphToPrompt)."""
    api = {}
    link_src = {l[0]: (l[1], l[2]) for l in ui["links"]}
    for n in ui["nodes"]:
        if n.get("mode") in (2, 4):
            continue
        ct = n["type"]
        if ct not in info:
            continue
        spec = info[ct]["input"]
        ins, wv = {}, list(n.get("widgets_values") or [])
        linked = {i["name"] for i in n.get("inputs", []) if i.get("link") is not None}
        for name in widget_order(spec):
            if name.startswith("__extra__"):
                if wv: wv.pop(0)
                continue
            if name in linked:
                continue
            if wv:
                ins[name] = wv.pop(0)
        for i in n.get("inputs", []):
            if i.get("link") is not None and i["link"] in link_src:
                src, slot = link_src[i["link"]]
                ins[i["name"]] = [str(src), slot]
        api[str(n["id"])] = {"class_type": ct, "inputs": ins}
    return api


def selftest(info):
    """Vezme skutečné UI workflow, udělá z něj API, převede zpět a porovná."""
    refs = glob.glob(os.path.expanduser("~/Code/ComfyUI/user/default/workflows/*.json"))
    ok = fail = 0
    for f in refs:
        try:
            ui = json.load(open(f))
        except Exception:
            continue
        if not (isinstance(ui, dict) and "nodes" in ui and "links" in ui):
            continue
        if any(n["type"] not in info for n in ui["nodes"]):
            continue
        api = ui_to_api(ui, info)
        if not api:
            continue
        back = convert(api, info)
        orig_w = {n["id"]: n.get("widgets_values") or [] for n in ui["nodes"] if n["type"] in info}
        new_w = {n["id"]: n["widgets_values"] for n in back["nodes"]}
        diffs = [i for i in orig_w if i in new_w and orig_w[i] != new_w[i]
                 and [x for x in orig_w[i] if x not in ("fixed", "randomize", "increment", "decrement")]
                     != [x for x in new_w[i] if x not in ("fixed", "randomize", "increment", "decrement")]]
        if diffs:
            fail += 1
            print("  x %s — widgets_values nesedí u nodů %s" % (os.path.basename(f), diffs[:3]))
            for i in diffs[:1]:
                print("      původní: %s" % (orig_w[i],))
                print("      převod : %s" % (new_w[i],))
        else:
            ok += 1
    print("  selftest: %d workflow sedí, %d neshod" % (ok, fail))
    return fail == 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*")
    ap.add_argument("-o", "--out", help="cílový adresář")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    info = json.load(urllib.request.urlopen(API + "/object_info", timeout=30))

    if a.selftest:
        sys.exit(0 if selftest(info) else 1)
    if not a.paths or not a.out:
        sys.exit("použití: ./api2ui.py <soubory> -o <adresář>   |   --selftest")

    os.makedirs(a.out, exist_ok=True)
    for p in a.paths:
        api = json.load(open(p))
        if set(api) == {"prompt"}:
            api = api["prompt"]
        if not all(isinstance(v, dict) and "class_type" in v for v in api.values()):
            print("  přeskakuji %s (není API formát)" % p); continue
        dst = os.path.join(a.out, os.path.basename(p))
        json.dump(convert(api, info), open(dst, "w"), indent=1, ensure_ascii=False)
        print("  %s -> %s" % (os.path.basename(p), os.path.relpath(dst)))
