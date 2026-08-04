#!/usr/bin/env python3
"""Kontrola workflow proti běžícímu ComfyUI.

Chytá to, co ComfyUI odhalí až při zařazení do fronty — a co nás dvakrát
zastavilo uprostřed práce (RIFE VFI bez dtype/torch_compile/batch_size,
MMAudioFeatureUtilsLoader bez mode/precision).

  ./validate.py                    # všechna workflow v repu
  ./validate.py workflows/*.json   # jen vybraná
  ./validate.py --quiet            # jen chyby a souhrn (pro CI)

Návratový kód 1, pokud něco neprošlo.
"""
import argparse, glob, json, os, sys, urllib.error, urllib.request

API = os.environ.get("COMFY_API", "http://localhost:8188")
ROOT = os.path.dirname(os.path.abspath(__file__))
PATTERNS = ["workflows/*.json", "workflows/*/*.json", "ads/*/workflows/*.json"]


def load_graph(path):
    """Vrátí mapu nodů. Snese i tvar {"prompt": {...}} (tělo POST požadavku)."""
    d = json.load(open(path))
    if set(d) == {"prompt"} and isinstance(d["prompt"], dict):
        return d["prompt"], True
    return d, False


def check(path, info):
    """[(závažnost, zpráva)] pro jedno workflow."""
    out = []
    try:
        g, wrapped = load_graph(path)
    except json.JSONDecodeError as e:
        return [("chyba", "nevalidní JSON: %s" % e)]
    if wrapped:
        out.append(("varování", "obalené v {\"prompt\": ...} — UI import to nepřečte"))

    ids = set(g)
    for nid in sorted(g, key=lambda x: (len(x), x)):
        node = g[nid]
        ct = node.get("class_type")
        if not ct:
            out.append(("chyba", "node %s: chybí class_type" % nid)); continue
        if ct not in info:
            out.append(("chyba", "node %s: neznámý typ %s" % (nid, ct))); continue

        spec = info[ct]["input"]
        required = set(spec.get("required", {}))
        known = required | set(spec.get("optional", {}))
        inputs = node.get("inputs", {})

        missing = required - set(inputs)
        if missing:
            out.append(("chyba", "node %s (%s): chybí povinné %s" % (nid, ct, sorted(missing))))
        unknown = set(inputs) - known
        if unknown:
            out.append(("varování", "node %s (%s): neznámé vstupy %s" % (nid, ct, sorted(unknown))))

        for name, val in inputs.items():
            # odkaz na jiný node: ["<id>", <output_index>]
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                src, idx = val
                if src not in ids:
                    out.append(("chyba", "node %s.%s: odkaz na neexistující node %s" % (nid, name, src)))
                elif g[src].get("class_type") in info:
                    n_out = len(info[g[src]["class_type"]]["output"])
                    if not isinstance(idx, int) or idx >= n_out:
                        out.append(("chyba", "node %s.%s: node %s (%s) nemá výstup %s"
                                    % (nid, name, src, g[src]["class_type"], idx)))
            # hodnota z comba: ověř, že je v nabídce
            elif name in spec.get("required", {}) or name in spec.get("optional", {}):
                s = spec.get("required", {}).get(name) or spec["optional"][name]
                opts = None
                if isinstance(s[0], list):
                    opts = s[0]
                elif s[0] == "COMBO" and len(s) > 1 and isinstance(s[1], dict):
                    opts = s[1].get("options")
                if opts and isinstance(val, str) and val not in opts:
                    shown = ", ".join(map(str, opts[:4])) + ("…" if len(opts) > 4 else "")
                    out.append(("chyba", "node %s (%s).%s: %r není v nabídce (%s)"
                                % (nid, ct, name, val, shown)))

        # placeholder zůstal v souboru
        for name, val in inputs.items():
            if isinstance(val, str) and "PLACEHOLDER" in val:
                out.append(("varování", "node %s.%s: zůstal placeholder %r" % (nid, name, val)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="konkrétní soubory (jinak celé repo)")
    ap.add_argument("--quiet", "-q", action="store_true", help="jen chyby a souhrn")
    a = ap.parse_args()

    try:
        info = json.load(urllib.request.urlopen(API + "/object_info", timeout=30))
    except (urllib.error.URLError, OSError) as e:
        sys.exit("ComfyUI na %s neodpovídá (%s) — validace potřebuje běžící instanci" % (API, e))

    paths = a.paths or sorted(p for pat in PATTERNS for p in glob.glob(os.path.join(ROOT, pat)))
    if not paths:
        sys.exit("žádná workflow k ověření")

    errors = warnings = 0
    for p in paths:
        rel = os.path.relpath(p, ROOT)
        probs = check(p, info)
        errs = [m for s, m in probs if s == "chyba"]
        warns = [m for s, m in probs if s == "varování"]
        errors += len(errs); warnings += len(warns)
        if errs:
            print("CHYBA  %s" % rel)
        elif warns and not a.quiet:
            print("varuje %s" % rel)
        elif not a.quiet:
            print("ok     %s" % rel)
        for m in errs:
            print("         %s" % m)
        for m in warns:
            if not a.quiet or errs:
                print("         (varování) %s" % m)

    print("\n%d workflow — %d chyb, %d varování" % (len(paths), errors, warnings))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
