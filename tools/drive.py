#!/usr/bin/env python3
"""drive.py — řídicí klipy pro control beaty (pohyb z kostry místo z textu).

    ./tools/drive.py t2v  <id> "<prompt>" [--seed 42] [--length 81]   # Wan T2V → drive/src/<id>.webm
    ./tools/drive.py pose <id> [--src cesta] [--start 0] [--length 81] # DWPose kostra → drive/<id>_pose.webm

Do repa jde jen kostra (černé pozadí, pár set kB); zdrojový klip zůstává v
drive/src/ (gitignore). Jede na SPARKu přes ComfyUI jako chain.py (stejný
submit, stejné čekání, drop_page_cache). Kostra: tělo + ruce, obličej ne —
identitu drží ref_image, ne klip.
"""
import argparse, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import chain  # noqa: E402

DRIVE = os.path.join(HERE, "drive")
W, H = 480, 832          # trénované portrétní rozlišení Wan 2.2; kostra se při renderu škáluje na beat


def t2v(a):
    import json
    g = json.load(open(os.path.join(HERE, "workflows", "t2v_final_14b_lightning_portrait.json")))
    g["8"]["inputs"]["text"] = a.prompt
    g["10"]["inputs"].update(width=W, height=H, length=a.length)
    for n in ("11", "12"):
        g[n]["inputs"]["noise_seed"] = a.seed
    g["15"]["inputs"].update(filename_prefix="drive/%s_src" % a.id, fps=16.0, crf=16.0)
    out = chain.outfile(chain.submit(g, "t2v %s" % a.id), "15", ".webm")
    os.makedirs(os.path.join(DRIVE, "src"), exist_ok=True)
    dst = os.path.join(DRIVE, "src", a.id + ".webm")
    shutil.copy(out, dst)
    print("  %s  (%d snímků)" % (os.path.relpath(dst, HERE), nframes(dst)))


def pose(a):
    src = a.src or os.path.join(DRIVE, "src", a.id + ".webm")
    if not os.path.exists(src):
        chain.die("zdroj %s neexistuje (t2v, Mixamo render, vlastní natočení…)" % src)
    staged = "drive_%s%s" % (a.id, os.path.splitext(src)[1])
    if a.loop:
        # Mixamo animace jsou smyčky: krátký klip zopakovat, ať vyplní celý beat
        staged = "drive_%s_loop.mp4" % a.id
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-ss", str(a.start), "-i", src,
                        "-t", "%.3f" % (a.length / 16 + 0.5), "-an", "-r", "16", "-pix_fmt", "yuv420p",
                        os.path.join(chain.IN, staged)], check=True)
        a.start = 0.0
    else:
        shutil.copy(src, os.path.join(chain.IN, staged))
    def load(cap):
        return {
            "1": {"class_type": "VHS_LoadVideo",
                  "inputs": {"video": staged, "force_rate": 16, "custom_width": 0, "custom_height": 0,
                             "frame_load_cap": cap, "skip_first_frames": int(a.start * 16),
                             "select_every_nth": 1}},
            "2": {"class_type": "ImageScale",
                  "inputs": {"image": ["1", 0], "upscale_method": "lanczos", "width": W, "height": H,
                             "crop": "center"}}}

    def dwpose(cap, bbox):
        g = load(cap)
        g["3"] = {"class_type": "DWPreprocessor",
                  "inputs": {"image": ["2", 0], "detect_hand": "enable", "detect_body": "enable",
                             "detect_face": "disable", "resolution": 512,
                             "bbox_detector": bbox, "pose_estimator": "dw-ll_ucoco_384.onnx"}}
        return g

    def sapiens(cap):
        # Sapiens2 (Meta) čte i stylizované postavy (Mixamo panáčci), kde yolox/DWPose
        # nevidí člověka; výstup kreslí v OpenPose barvách, které fun_control zná
        g = load(cap)
        g["9"] = {"class_type": "Sapiens2Loader", "inputs": {"checkpoint": "sapiens2_1b_pose.safetensors"}}
        g["8"] = {"class_type": "Sapiens2Pose",
                  "inputs": {"image": ["2", 0], "sapiens2_model": ["9", 0], "output_format": "openpose",
                             "include_face": False, "frames_per_batch": 4}}
        g["3"] = {"class_type": "Sapiens2DrawPose",
                  "inputs": {"keypoints": ["8", 0], "draw_skeleton": True, "draw_points": True,
                             "draw_face": False, "point_radius": 3, "stick_width": 3,
                             "score_threshold": 0.3}}
        return g

    def with_save(g, save):
        g["4"] = save
        return g

    probe_save = {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "drive/%s_probe" % a.id}}
    final_save = {"class_type": "SaveWEBM", "inputs": {"images": ["3", 0], "filename_prefix": "drive/%s_pose" % a.id,
                                                       "codec": "vp9", "fps": 16.0, "crf": 10.0}}
    if a.dwpose:
        # DWPose: detektor osob nevidí stylizované postavy → sonda, případně bez bbox
        bbox = "yolox_l.onnx"
        probe = chain.outfile(chain.submit(with_save(dwpose(3, bbox), probe_save), "pose probe %s" % a.id), "4", ".png")
        if not skeleton_visible(probe):
            print("  yolox postavu nenašel → DWPose bez bbox detektoru")
            bbox = "None"
        g = dwpose(a.length, bbox)
    else:
        g = sapiens(a.length)
    out = chain.outfile(chain.submit(with_save(g, final_save), "pose %s" % a.id), "4", ".webm")
    dst = os.path.join(DRIVE, a.id + "_pose.webm")
    shutil.copy(out, dst)
    n = nframes(dst)
    print("  %s  (%d snímků @ 16 fps%s)" % (os.path.relpath(dst, HERE), n,
                                            "" if n >= a.length else " — MÉNĚ než %d, klip je krátký" % a.length))
    if os.path.getsize(dst) < 100_000:
        print("  ! kostra je skoro prázdná (%d kB) — odhad pózy postavu nečte; zkus %s, nebo lidsky "
              "vypadající postavu a postavu přes 70 %% výšky záběru"
              % (os.path.getsize(dst) // 1000, "Sapiens2 (bez --dwpose)" if a.dwpose else "--dwpose"))


def skeleton_visible(png):
    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(png).convert("L"))
    return (a > 20).mean() > 0.01          # čistá kostra má ~3 %, pár bodů rukou < 0.5 %


def nframes(path):
    try:
        return chain.nframes(path)
    except Exception:                                  # noqa: BLE001
        return -1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("t2v"); p.add_argument("id"); p.add_argument("prompt")
    p.add_argument("--seed", type=int, default=42); p.add_argument("--length", type=int, default=81)
    p = sub.add_parser("pose"); p.add_argument("id"); p.add_argument("--src")
    p.add_argument("--start", type=float, default=0.0, help="odkud v sekundách")
    p.add_argument("--length", type=int, default=81)
    p.add_argument("--loop", action="store_true", help="klip opakovat, dokud nevyplní --length (Mixamo smyčky)")
    p.add_argument("--dwpose", action="store_true", help="DWPose místo Sapiens2 (rychlejší, ale stylizované postavy nečte)")
    a = ap.parse_args()
    {"t2v": t2v, "pose": pose}[a.cmd](a)
