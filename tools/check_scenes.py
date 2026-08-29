#!/usr/bin/env python3
"""check_scenes.py — kontrola katalogu scén před nasazením.

    ./tools/check_scenes.py

Hlídá to, co se dá rozbít hromadnou úpravou presetů: chybějící dvojjazyčné
popisky (label/desc česky pro Ol1nLLM, label_en/desc_en anglicky pro
TsumikiBot — klient si vybere, nikdo nemusí releasovat), neplatné délky beatů
a prázdné prompty. Nenahrazuje --validate proti ComfyUI, ten kontroluje graf.
"""
import glob, json, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import chain  # noqa: E402


def main():
    bad = 0
    files = sorted(glob.glob(os.path.join(HERE, "scenes", "*.json")))
    for p in files:
        t = json.load(open(p))
        name = os.path.basename(p)
        for k in ("id", "label", "desc", "label_en", "desc_en"):
            if not (t.get(k) or "").strip():
                print("  x %s: chybí %s" % (name, k)); bad += 1
        if t.get("id") != os.path.splitext(name)[0]:
            print("  x %s: id %r nesedí na jméno souboru" % (name, t.get("id"))); bad += 1
        # projde stejnou cestou jako render: délky, prázdné prompty, knoby
        m = dict(t, name="check", source="x.png")
        try:
            chain.load_dict(m)
        except SystemExit as e:
            print("  x %s: %s" % (name, e)); bad += 1
    print("  ok — %d scén" % len(files) if not bad else "  %d problém(ů)" % bad)
    return bad == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
