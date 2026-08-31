#!/usr/bin/env python3
"""face_drift.py — jak moc se tvář vzdaluje originálu.

    ./tools/face_drift.py <name> [<name> …]      # input/<name>_seed00.png = originál
    ./tools/face_drift.py --video klip.webm --ref orig.png [--frames 8]

Pro každý input/<name>_seedNN.png spočítá ArcFace embedding (insightface —
stejný balík, který používá InstantID/FaceID v ComfyUI) a vypíše kosinovou
podobnost k seed00. 1.0 = tatáž tvář, ~0.6 = pořád ona, < 0.4 = jiný člověk.
Jedno číslo na variantu (průměr) je to, čím se porovnávají iterace v
reports/phase4_identity.md. Bez GPU (onnxruntime na CPU stačí, ~1 s/snímek).

Režim `--video` měří drift **uvnitř** klipu, ne mezi beaty: vytáhne
rovnoměrně `--frames` snímků a porovná je s `--ref`. Navazovací snímek
(seedNN.png) je totiž po oživení tváře, takže měří strop refreshe — tohle
měří, jak identitu drží samotný model. Nutné pro A/B enginů (phase5_ltx.md).
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


def frames_from_video(path, n):
    """n snímků rovnoměrně po klipu → dočasné PNG. Poslední snímek se bere
    o jeden dřív: u vp9 bývá poslední snímek občas nedekódovatelný."""
    import subprocess, tempfile
    total = int(subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
        capture_output=True, text=True).stdout.strip() or 0)
    if total < 2:
        sys.exit("face_drift: %s nemá čitelné snímky" % path)
    idx = [round(i * (total - 2) / max(1, n - 1)) for i in range(n)]
    d = tempfile.mkdtemp(prefix="face_drift_")
    out = []
    for k, i in enumerate(idx):
        dst = os.path.join(d, "f%03d.png" % k)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", path, "-vf", r"select=eq(n\,%d)" % i, "-vsync", "0",
                        "-frames:v", "1", dst], check=True)
        if os.path.exists(dst):
            out.append((i, dst))
    return total, out


def main_video(video, ref_path, n):
    app = analyser()
    ref = embedding(app, ref_path)
    if ref is None:
        sys.exit("face_drift: v referenci %s není tvář" % ref_path)
    total, frames = frames_from_video(video, n)
    print("%s  (%d snímků, měřeno %d)" % (os.path.basename(video), total, len(frames)))
    sims = []
    for i, f in frames:
        e = embedding(app, f)
        s = float(ref @ e) if e is not None else float("nan")
        sims.append(s)
        print("  snímek %-5d %s" % (i, "—  tvář nenalezena" if e is None else "%.3f" % s))
    ok = [s for s in sims if s == s]
    if ok:
        print("  průměr %.3f  min %.3f  první %.3f  poslední %.3f  (n=%d)"
              % (sum(ok) / len(ok), min(ok), ok[0], ok[-1], len(ok)))


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "--video":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--video", required=True)
        ap.add_argument("--ref", required=True)
        ap.add_argument("--frames", type=int, default=8)
        p = ap.parse_args(a)
        main_video(p.video, p.ref, p.frames)
    else:
        main(a)
