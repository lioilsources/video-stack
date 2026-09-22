#!/usr/bin/env python3
"""check_stories.py — kontrola katalogu příběhů před nasazením (bez GPU).

    ./tools/check_stories.py            # celý stories/
    ./tools/check_stories.py -v         # i časová osa a prostor pro vypravěče

Hlídá, co se rozbije ruční úpravou stories/*.json: chybějící dvojjazyčné
texty (title/desc/narration česky pro Ol1nLLM, _en pro TsumikiBot), záběry
bez keyframe/motion, víc než dvě postavy v záběru, chybějící kostry tance,
délky beatů přes stejnou cestu jako render (chain.load_dict), a hlavně
naraci, která se do záběru nevejde — to je chyba, kterou jinak odhalí až mix.
"""
import glob, json, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))
import story  # noqa: E402


def main(verbose=False):
    bad = warn = 0
    files = sorted(glob.glob(os.path.join(HERE, "stories", "*.json")))
    for p in files:
        name = os.path.basename(p)
        raw = json.load(open(p))
        for k in ("id", "title", "title_en", "desc", "desc_en", "music", "world"):
            if not (raw.get(k) or "").strip():
                print("  x %s: chybí %s" % (name, k)); bad += 1
        if raw.get("id") != os.path.splitext(name)[0]:
            print("  x %s: id %r nesedí na jméno souboru" % (name, raw.get("id"))); bad += 1
        for sh in raw.get("shots", []):
            for k in ("narration", "narration_en"):
                if not (sh.get(k) or "").strip():
                    print("  x %s záběr %s: chybí %s" % (name, sh.get("id", "?"), k)); bad += 1
        try:
            st = story.normalize(raw, p)
            work = story.Work("check_" + st["id"])
            m = story.load_manifest_dict(story.manifest_dict(st, work))
        except SystemExit as e:
            print("  x %s: %s" % (name, e)); bad += 1
            continue
        tl = story.timeline(st, m)
        if not 45 <= tl["total"] <= 90:
            print("  ! %s: %.0f s — mimo 45–90 s" % (name, tl["total"])); warn += 1
        for lang in story.LANGS:
            for shot, words, need, avail in story.speech_warnings(tl, lang):
                print("  ! %s záběr %s (%s): %d slov ≈ %.1f s, prostor %.1f s"
                      % (name, shot, lang, words, need, avail)); warn += 1
        if verbose:
            print("  %s  %d záběrů, %d beatů, %.1f s" % (name, len(st["shots"]), len(m["beats"]), tl["total"]))
    print("  ok — %d příběhů%s" % (len(files), ", %d varování" % warn if warn else "")
          if not bad else "  %d problém(ů), %d varování" % (bad, warn))
    return bad == 0


if __name__ == "__main__":
    sys.exit(0 if main("-v" in sys.argv[1:]) else 1)
