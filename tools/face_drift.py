#!/usr/bin/env python3
"""face_drift.py — jak moc se tvář na navazovacích snímcích vzdaluje originálu.

    ./tools/face_drift.py <name> [<name> …]     # input/<name>_seed00.png = originál

Pro každý input/<name>_seedNN.png spočítá ArcFace embedding (insightface —
stejný balík, který používá InstantID/FaceID v ComfyUI) a vypíše kosinovou
podobnost k seed00. 1.0 = tatáž tvář, ~0.6 = pořád ona, < 0.4 = jiný člověk.
Jedno číslo na variantu (průměr) je to, čím se porovnávají iterace v
reports/phase4_identity.md. Bez GPU (onnxruntime na CPU stačí, ~1 s/snímek).
"""
import glob, os, sys

import numpy as np

COMFY = os.path.expanduser("~/Code/ComfyUI")
IN = os.path.join(COMFY, "input")
ROOT = os.path.join(COMFY, "models", "insightface")


def analyser():
    from insightface.app import FaceAnalysis
    for pack in ("antelopev2", "buffalo_l"):
        if os.path.isdir(os.path.join(ROOT, "models", pack)):
            app = FaceAnalysis(name=pack, root=ROOT, providers=["CPUExecutionProvider"])
            app.prepare(ctx_id=-1, det_size=(640, 640))
            return app
    sys.exit("face_drift: v %s/models není antelopev2 ani buffalo_l" % ROOT)


def embedding(app, path):
    import cv2
    img = cv2.imread(path)
    faces = app.get(img)
    if not faces:
        return None
    f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    e = f.normed_embedding
    return e / np.linalg.norm(e)


def main(names):
    app = analyser()
    for name in names:
        paths = sorted(glob.glob(os.path.join(IN, "%s_seed[0-9][0-9].png" % name)))
        if not paths:
            print("%s: žádné seedNN.png v %s" % (name, IN)); continue
        ref = embedding(app, paths[0])
        if ref is None:
            print("%s: v seed00 není tvář" % name); continue
        sims = []
        print("%s" % name)
        for p in paths[1:]:
            e = embedding(app, p)
            s = float(ref @ e) if e is not None else float("nan")
            sims.append(s)
            print("  %-22s %s" % (os.path.basename(p), "—  tvář nenalezena" if e is None else "%.3f" % s))
        ok = [s for s in sims if s == s]
        if ok:
            print("  průměr %.3f  min %.3f  (n=%d)" % (sum(ok) / len(ok), min(ok), len(ok)))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
