"""mixamo_pose.py — Mixamo FBX → OpenPose kostra přímo z kloubů, bez odhadu pózy.

    blender -b --factory-startup -noaudio -P tools/mixamo_pose.py -- Flair.fbx drive/src/flair.json \\
        [--length 81] [--speed 0.65] [--loop] [--zoom 1.5] [--preview drive/src/flair_preview.mp4]

Výstup je JSON ve formátu POSE_KEYPOINT (canvas_width/height, people[] s
pose/foot/hand body v pixelech), který `drive.py draw` nakreslí tímtéž
kresličem ComfyUI, jakým Sapiens2DrawPose kreslí ostatní kostry — control beat
tak dostane stejný vizuální jazyk.

Proč ne render + Sapiens2: odhad pózy je naučený na lidech hlavou nahoru. Na
flairu (tělo vodorovně, nohy nad hlavou) vrátil 23 z 81 snímků prázdných a
zbytek v úlomcích. FBX nese přesné klouby v každém snímku, takže nic nevypadne
a levá/pravá strana sedí i hlavou dolů.

Mixamo nemá kosti pro nos, oči, uši, palce a paty chodidel — dopočítají se
z hlavy (Head → HeadTop_End) a chodidla v poměrech délky kosti. Body obličeje
dostanou skóre 0, když míří od kamery: OpenPose kostra zezadu nemá nos ani oči
a právě podle toho model pozná, že je postava otočená zády.

Kamera: ortografická, přesně zepředu (postava v Mixamu kouká na −Y), podlaha
kousek nad spodním okrajem, záběr z obálky postavy přes všechny vzorkované
snímky, takže nikdy neuteče z rámu. 480×832 = portrét Wan 2.2, drive.py nic
neořízne. Tempo a smyčka se vzorkují přímo z animace (sub-snímky), ne
zpomalením hotového videa.

Velikost postavy v kostře = velikost postavy ve videu: control beat záběr
oddálí nebo přiblíží podle ní. Široký pohyb (flair, rozpětí nohou 2 m) se do
portrétu vejde jen malý a model pak ukáže víc místnosti než fotka. `--zoom`
záběr zúží — postava větší, krajní polohy končetin vyjedou z rámu.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

import bpy
import numpy as np
from mathutils import Vector

FPS = 16
FILL_H = 0.8          # postava do 80 % výšky (pravidla pro nahrávky, reports/phase4_identity.md)
FILL_W = 0.92         # do šířky těsněji — široké pohyby (flair) limituje šířka portrétu
FLOOR_MARGIN = 0.05   # podlaha kousek nad spodním okrajem, jako chodidla na fotce
TO_CAMERA = Vector((0, -1, 0))

# OpenPose-18 bez hlavy (0, 14–17) a krku (1 = střed ramen, tak ho počítá DWPose)
LIMBS = {2: "RightArm", 3: "RightForeArm", 4: "RightHand", 5: "LeftArm", 6: "LeftForeArm", 7: "LeftHand",
         8: "RightUpLeg", 9: "RightLeg", 10: "RightFoot", 11: "LeftUpLeg", 12: "LeftLeg", 13: "LeftFoot"}
FINGERS = ("Thumb", "Index", "Middle", "Ring", "Pinky")


def parse():
    ap = argparse.ArgumentParser(prog="mixamo_pose.py")
    ap.add_argument("fbx")
    ap.add_argument("out", help="JSON s body pro drive.py draw")
    ap.add_argument("--length", type=int, default=81, help="snímků výstupu @ 16 fps")
    ap.add_argument("--speed", type=float, default=1.0, help="tempo: 0.65 = pomaleji (míň rozmazaných končetin)")
    ap.add_argument("--loop", action="store_true", help="animaci opakovat (Mixamo smyčky); jinak drží poslední pózu")
    ap.add_argument("--zoom", type=float, default=1.0,
                    help="1 = celý pohyb v záběru; víc = postava větší, krajní polohy končetin z rámu ven")
    ap.add_argument("--preview", help="mp4 s renderem postavy ve stejných snímcích (kontrola kostry)")
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--height", type=int, default=832)
    return ap.parse_args(sys.argv[sys.argv.index("--") + 1:])


def main():
    a = parse()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=os.path.abspath(a.fbx))
    sc = bpy.context.scene
    arm = next(o for o in sc.objects if o.type == "ARMATURE")
    meshes = [o for o in sc.objects if o.type == "MESH"]
    B = {pb.name.split(":")[-1]: pb for pb in arm.pose.bones}
    W = arm.matrix_world

    f0, f1 = (int(round(x)) for x in arm.animation_data.action.frame_range)
    span = f1 - f0                                       # u Mixamo smyčky je f1 == f0
    src_fps = sc.render.fps / sc.render.fps_base

    def goto(i):
        t = i / FPS * a.speed * src_fps
        fr = f0 + (t % span if a.loop else min(t, span))
        sc.frame_set(int(fr), subframe=fr - int(fr))

    # směry z klidové pózy: dopředu podle chodidla, doprava podle ramen
    def rest(name):
        return W @ B[name].bone.head_local

    fwd_w = rest("LeftToe_End") - rest("LeftFoot")
    fwd_w.z = 0
    fwd_w.normalize()
    right_w = (rest("RightArm") - rest("LeftArm")).normalized()
    down_w = Vector((0, 0, -1))

    def local(name, v):
        """Světový směr v klidu → lokál kosti; per snímek ho pak otočí pose matice."""
        return ((W @ B[name].bone.matrix_local).to_3x3().inverted() @ v).normalized()

    head_ax = {"fwd": local("Head", fwd_w), "right": local("Head", right_w)}
    foot_ax = {s: {"fwd": local(s + "Foot", fwd_w), "right": local(s + "Foot", right_w),
                   "down": local(s + "Foot", down_w)} for s in ("Left", "Right")}

    def pos(name):
        return W @ B[name].head

    def turn(name, v):
        return ((W @ B[name].matrix).to_3x3() @ v).normalized()

    def keypoints():
        body = [None] * 18
        for k, n in LIMBS.items():
            body[k] = (pos(n), True)
        body[1] = ((pos("RightArm") + pos("LeftArm")) / 2, True)

        h0, h1 = pos("Head"), pos("HeadTop_End")
        L, up = (h1 - h0).length, (h1 - h0).normalized()
        fwd, right = turn("Head", head_ax["fwd"]), turn("Head", head_ax["right"])

        def face(u, f, s, normal, limit):
            return (h0 + up * (u * L) + fwd * (f * L) + right * (s * L),
                    normal.normalized().dot(TO_CAMERA) > limit)

        body[0] = face(0.30, 0.55, 0.0, fwd, -0.1)                        # nos
        body[14] = face(0.48, 0.45, 0.17, fwd + right * 0.6, -0.2)        # pravé oko
        body[15] = face(0.48, 0.45, -0.17, fwd - right * 0.6, -0.2)       # levé oko
        body[16] = face(0.40, 0.0, 0.40, right - fwd * 0.2, -0.3)         # pravé ucho
        body[17] = face(0.40, 0.0, -0.40, -right - fwd * 0.2, -0.3)       # levé ucho

        feet = []                                        # COCO-WholeBody: palec, malík, pata; levá, pak pravá
        for side in ("Left", "Right"):
            ankle, ball, tip = pos(side + "Foot"), pos(side + "ToeBase"), pos(side + "Toe_End")
            Lf = (ball - ankle).length
            ax = {k: turn(side + "Foot", v) for k, v in foot_ax[side].items()}
            medial = ax["right"] if side == "Left" else -ax["right"]
            feet += [(tip + medial * (0.25 * Lf), True),
                     (ball.lerp(tip, 0.5) - medial * (0.35 * Lf), True),
                     (ankle + ax["down"] * (0.5 * Lf) - ax["fwd"] * (0.35 * Lf), True)]

        def hand(side):                                  # zápěstí, pak každý prst od kořene ke špičce
            return [(pos(side + "Hand"), True)] + [(pos("%sHand%s%d" % (side, f, k)), True)
                                                   for f in FINGERS for k in (1, 2, 3, 4)]

        return body, feet, hand("Right"), hand("Left")

    # obálka postavy přes všechny vzorkované snímky
    lo, hi = np.full(2, np.inf), np.full(2, -np.inf)
    for i in range(a.length):
        goto(i)
        if meshes:
            dg = bpy.context.evaluated_depsgraph_get()
            for o in meshes:
                ev = o.evaluated_get(dg)
                m = ev.to_mesh()
                co = np.empty(len(m.vertices) * 3, dtype=np.float64)
                m.vertices.foreach_get("co", co)
                M = np.array(o.matrix_world)
                wc = co.reshape(-1, 3) @ M[:3, :3].T + M[:3, 3]
                lo = np.minimum(lo, wc[:, [0, 2]].min(0))
                hi = np.maximum(hi, wc[:, [0, 2]].max(0))
                ev.to_mesh_clear()
        else:
            for pb in arm.pose.bones:
                for p in (W @ pb.head, W @ pb.tail):
                    lo, hi = np.minimum(lo, (p.x, p.z)), np.maximum(hi, (p.x, p.z))
    if not meshes:                                       # kosti vedou středem těla, tělo je širší
        lo, hi = lo - (0.1, 0.05), hi + (0.1, 0.1)

    w, h = hi - lo
    frame_h = max(h / FILL_H, w / FILL_W * a.height / a.width) / a.zoom
    frame_w = frame_h * a.width / a.height
    cx = (lo[0] + hi[0]) / 2
    bottom = lo[1] - FLOOR_MARGIN * frame_h
    left, top = cx - frame_w / 2, bottom + frame_h

    def flat(pts):
        out = []
        for p, visible in pts:
            out += ([round((p.x - left) / frame_w * a.width, 2), round((top - p.z) / frame_h * a.height, 2), 1.0]
                    if visible else [0.0, 0.0, 0.0])
        return out

    frames = []
    for i in range(a.length):
        goto(i)
        body, feet, rh, lh = keypoints()
        frames.append({"canvas_width": a.width, "canvas_height": a.height, "people": [{
            "pose_keypoints_2d": flat(body), "foot_keypoints_2d": flat(feet),
            "face_keypoints_2d": [0.0] * 210,            # kreslič klíč vyžaduje; obličej se nekreslí
            "hand_right_keypoints_2d": flat(rh), "hand_left_keypoints_2d": flat(lh)}]})
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump({"source": os.path.basename(a.fbx), "fps": FPS, "speed": a.speed, "loop": a.loop,
                   "zoom": a.zoom, "frames": frames}, fh)

    if a.preview:
        cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
        sc.collection.objects.link(cam)
        cam.data.type, cam.data.sensor_fit, cam.data.ortho_scale = "ORTHO", "VERTICAL", frame_h
        cam.data.clip_end = 100
        cam.location = (cx, -20, bottom + frame_h / 2)
        cam.rotation_euler = (1.5708, 0, 0)              # dívá se na +Y, postava kouká na −Y
        sc.camera = cam
        r = sc.render
        r.engine = "BLENDER_WORKBENCH"
        r.resolution_x, r.resolution_y, r.resolution_percentage = a.width, a.height, 100
        sh = sc.display.shading
        sh.light, sh.color_type, sh.show_cavity, sh.show_object_outline = "STUDIO", "MATERIAL", True, True
        sc.world = bpy.data.worlds.new("w")
        sc.world.color = (0.75, 0.75, 0.75)
        tmp = tempfile.mkdtemp(prefix="mixamo_")
        try:
            for i in range(a.length):
                goto(i)
                r.filepath = os.path.join(tmp, "%04d.png" % i)
                bpy.ops.render.render(write_still=True)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-framerate", str(FPS), "-i", os.path.join(tmp, "%04d.png"),
                            "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", a.preview], check=True)
        finally:
            shutil.rmtree(tmp)

    print("POSE %s  %d snímků @ %d fps  (animace %d snímků @ %g fps, tempo %g×%s, zoom %g)  obálka %.2f×%.2f m  "
          "postava %d %% výšky%s"
          % (a.out, a.length, FPS, span, src_fps, a.speed, ", smyčka" if a.loop else "", a.zoom, w, h,
             round(100 * h / frame_h), "" if w <= frame_w else ", krajní polohy %.2f m za rámem" % (w - frame_w)))


main()
