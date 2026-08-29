#!/usr/bin/env python3
"""seam_pop.py — jak moc „skočí" obraz na střihu mezi beaty.

    ./tools/seam_pop.py <name> [<name> …]      # čte chains/<name>.json a output/<name>/<name>_full.mp4

Pro každý střih (poslední snímek beatu N → první snímek beatu N+1, 16 fps klip
před RIFE) spočítá průměrný absolutní rozdíl pixelů v horní třetině obrazu
(obličej) a vydělí ho mediánem rozdílů sousedních snímků ±8 kolem střihu.
1.0 = střih nerozeznatelný od běžného pohybu, > 2 = viditelný skok.
Doplněk k face_drift.py: oživení tváře drift zastaví, ale může skočit na střihu.
"""
import json, os, subprocess, sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.expanduser("~/Code/ComfyUI/output")
sys.path.insert(0, HERE)
import chain  # noqa: E402  (load + frames_upto)


def frames(path, w=232, h=472):
    """Všechny snímky jako uint8 [n, h, w, 3] přes ffmpeg — zmenšené, na diff stačí."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", "scale=%d:%d" % (w, h),
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)


def main(names):
    for name in names:
        m = chain.load(os.path.join(HERE, "chains", name + ".json"))
        path = os.path.join(OUT, name, "%s_full.mp4" % name)
        if not os.path.exists(path):
            print("%s: chybí %s" % (name, path)); continue
        f = frames(path).astype(np.int16)
        top = f[:, : f.shape[1] // 3]                       # horní třetina = hlava
        d = np.abs(np.diff(top, axis=0)).mean(axis=(1, 2, 3))   # d[i] = rozdíl snímků i, i+1
        print(name)
        ratios = []
        for i in range(1, len(m["beats"])):
            cut = chain.frames_upto(m, i)                  # první snímek beatu i
            j = cut - 1                                    # dvojice (cut-1, cut)
            lo, hi = max(0, j - 8), min(len(d), j + 9)
            neigh = np.median(np.concatenate([d[lo:j], d[j + 1:hi]]))
            r = d[j] / max(neigh, 1e-6)
            ratios.append(r)
            print("  střih %s→%s  snímek %4d  diff %5.2f  okolí %5.2f  poměr %.2f"
                  % (m["beats"][i - 1]["id"], m["beats"][i]["id"], cut, d[j], neigh, r))
        print("  průměr poměru %.2f  max %.2f" % (sum(ratios) / len(ratios), max(ratios)))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
