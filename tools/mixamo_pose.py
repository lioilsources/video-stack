"""mixamo_pose.py — Mixamo FBX → OpenPose kostra přímo z kloubů, bez odhadu pózy.

    blender -b --factory-startup -noaudio -P tools/mixamo_pose.py -- Flair.fbx drive/src/flair.json \\
        [--length 81] [--speed 0.65] [--loop] [--zoom 1.5] [--preview drive/src/flair_preview.mp4]
        [--proportions drive/src/<postava>_prop.json]   # kostra s proporcemi postavy z fotky

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

S velikostí postavy jde i velikost tváře, a tím identita: salsa z obálky
(postava 66 % výšky) držela tvář ve videu 0.11, s postavou na 90 % 0.30 —
stejný beat, seed i fotka. `--fill 0.9` proto rámuje podle výšky postavy
**ve stoje** místo obálky pohybu: každý tanec má stejně velkou tvář bez
ohledu na to, kam sahají ruce (zdvižené paže a široké výpady z rámu vyjedou).
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
    ap.add_argument("--fill", type=float,
                    help="výška postavy ve stoje jako podíl výšky záběru (0.9); místo --zoom")
    ap.add_argument("--proportions", help="JSON z tools/rig_proportions.py: kostra s proporcemi postavy z fotky")
    ap.add_argument("--preview", help="mp4 s renderem postavy ve stejných snímcích (kontrola kostry)")
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--height", type=int, default=832)
    return ap.parse_args(sys.argv[sys.argv.index("--") + 1:])


def action_fcurves(arm):
    """F-křivky aktivní akce. Blender 4.4+ má vrstvené akce a `Action.fcurves`
    v 5.x už neexistuje — křivky jsou v channelbagu slotu."""
    action = arm.animation_data.action
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    from bpy_extras import anim_utils
    bag = anim_utils.action_get_channelbag_for_slot(action, arm.animation_data.action_slot)
    return list(bag.fcurves) if bag else []


def reshape(arm, ratios):
    """Přestaví klidovou kostru na proporce postavy: délky a šířky segmentů podle
    poměrů z rig_proportions.py, směry kostí zůstanou.

    Animace je v rotacích vůči klidové póze, takže tanec se nezmění — jen ho tančí
    tělo s kratšími pažemi, širšími boky apod. Kosti bez poměru (ruce, prsty, hlava,
    chodidla) se jen posunou s rodičem. Posun boků se škáluje délkou nohou, jinak
    by kratší nohy visely nad podlahou a delší se do ní bořily.

    Počítá se ve světě: FBX armatura má rotaci 90° kolem X a měřítko 0.01, takže
    "do šířky" je světové X, ne lokální osa kosti."""
    r = ratios
    W = arm.matrix_world.copy()
    Wi = W.inverted()
    unit = Vector((1.0, 1.0, 1.0))

    def rules(short):
        """(škála offsetu hlavy od rodiče po osách světa, škála délky kosti)"""
        side = short.replace("Left", "").replace("Right", "")
        if short in ("Spine", "Spine1", "Spine2", "Neck"):
            return unit * r["torso"], r["torso"]
        if short == "Head":
            return unit * r["torso"], 1.0
        if side == "Shoulder":
            return Vector((r["shoulders"], 1.0, r["torso"])), r["shoulders"]
        if side == "Arm":
            return Vector((r["shoulders"], 1.0, 1.0)), r["arm"]
        if side == "ForeArm":
            return unit * r["arm"], r["forearm"]
        if side == "Hand":
            return unit * r["forearm"], 1.0
        if side == "UpLeg":
            return Vector((r["hips"], 1.0, 1.0)), r["thigh"]
        if side == "Leg":
            return unit * r["thigh"], r["shin"]
        if side == "Foot":
            return unit * r["shin"], 1.0
        return unit, 1.0

    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones
    old = {b.name: (W @ b.head, W @ b.tail, b.roll) for b in eb}
    floor = min(min(h.z, t.z) for h, t, _ in old.values())
    thigh = (old["mixamorig:LeftLeg"][0] - old["mixamorig:LeftUpLeg"][0]).length
    shin = (old["mixamorig:LeftFoot"][0] - old["mixamorig:LeftLeg"][0]).length
    leg = (thigh * r["thigh"] + shin * r["shin"]) / (thigh + shin)

    new_head = {}
    for b in sorted(eb, key=lambda b: len(b.parent_recursive)):
        h, t, roll = old[b.name]
        off_scale, len_scale = rules(b.name.split(":")[-1])
        if b.parent is None:
            nh = Vector((h.x, h.y, floor + (h.z - floor) * leg))
            len_scale = r["torso"]
        else:
            nh = new_head[b.parent.name] + (h - old[b.parent.name][0]) * off_scale
        new_head[b.name] = nh
        b.use_connect = False
        b.head, b.tail = Wi @ nh, Wi @ (nh + (t - h) * len_scale)
        b.roll = roll
    bpy.ops.object.mode_set(mode="OBJECT")

    root = next(b.name for b in arm.data.bones if b.parent is None)
    for fc in action_fcurves(arm):
        if fc.data_path == 'pose.bones["%s"].location' % root:
            for kp in fc.keyframe_points:
                for p in (kp.co, kp.handle_left, kp.handle_right):
                    p.y *= leg
            fc.update()
    return leg


def clip_lowest(arm, meshes=()):
    """Nejnižší bod přes celou animaci (celé snímky), ve světě: (kosti, mesh).
    Mesh je None, když žádný není."""
    sc = bpy.context.scene
    f0, f1 = (int(round(x)) for x in arm.animation_data.action.frame_range)
    bone_low = mesh_low = None
    for f in range(f0, f1 + 1):
        sc.frame_set(f)
        for pb in arm.pose.bones:
            for p in (pb.head, pb.tail):
                z = (arm.matrix_world @ p).z
                bone_low = z if bone_low is None else min(bone_low, z)
        if meshes:
            dg = bpy.context.evaluated_depsgraph_get()
            for o in meshes:
                ev = o.evaluated_get(dg)
                m = ev.to_mesh()
                co = np.empty(len(m.vertices) * 3, dtype=np.float64)
                m.vertices.foreach_get("co", co)
                M = np.array(o.matrix_world)
                z = float((co.reshape(-1, 3) @ M[2, :3] + M[2, 3]).min())
                ev.to_mesh_clear()
                mesh_low = z if mesh_low is None else min(mesh_low, z)
    return bone_low, mesh_low


def main():
    a = parse()
    if a.fill is not None and (a.zoom != 1.0 or not 0 < a.fill <= 1):
        sys.exit("mixamo_pose.py: --fill čeká podíl 0–1 a nejde spolu se --zoom")
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=os.path.abspath(a.fbx))
    sc = bpy.context.scene
    arm = next(o for o in sc.objects if o.type == "ARMATURE")
    meshes = [o for o in sc.objects if o.type == "MESH"]
    proportions, floor_pad = None, 0.05
    if a.proportions:
        if a.fill is None:
            sys.exit("mixamo_pose.py: --proportions jen s --fill (rámování podle výšky ve stoje)")
        with open(a.proportions) as fh:
            proportions = json.load(fh)["ratios"]
        # Záběr stojí na nejnižším bodu klipu. A/B proti Mixamo kostře má měnit
        # jen proporce, ne rámování, takže:
        # - kratší nohy s chodidlem Mixamo velikosti by nejnižší bod posunuly
        #   (salsa: špička o 4.7 cm níž) → armatura se posune, aby zůstal;
        # - skin figuríny se maže (k přestavěné kostře nesedí), a obálka z kostí
        #   by jinak dostala pevnou rezervu 5 cm místo skutečné podrážky — salsa
        #   pak stála v rámu o 11 cm výš. Rezerva se proto změří na původní
        #   kostře jako rozdíl mesh − kosti a použije místo pevné.
        bone_low, mesh_low = clip_lowest(arm, meshes)
        floor_pad = (bone_low - mesh_low) if mesh_low is not None else 0.05
        for o in meshes:
            bpy.data.objects.remove(o, do_unlink=True)
        meshes = []
        leg = reshape(arm, proportions)
        arm.location.z += bone_low - clip_lowest(arm)[0]
        print("mixamo_pose: proporce %s, nohy ×%.3f" % (proportions, leg))
    B = {pb.name.split(":")[-1]: pb for pb in arm.pose.bones}
    W = arm.matrix_world.copy()                          # klidová matice — jen pro směry, posun smyčky ji mění

    f0, f1 = (int(round(x)) for x in arm.animation_data.action.frame_range)
    span = f1 - f0                                       # u Mixamo smyčky je f1 == f0
    src_fps = sc.render.fps / sc.render.fps_base

    # Smyčka z animace, která není na místě (salsa skončí 0.26 m vedle startu), by
    # na každém švu poskočila do strany. Posun kořene se proto rozpustí lineárně
    # přes cyklus — konec cyklu navazuje na začátek. Jen do strany: hloubku kamera
    # zepředu nevidí a výška patří póze (dřep na konci není posun).
    home = arm.location.x
    drift = 0.0
    if a.loop and span:
        sc.frame_set(f0)
        x0 = (arm.matrix_world @ B["Hips"].head).x
        sc.frame_set(f1)
        drift = (arm.matrix_world @ B["Hips"].head).x - x0
        drift = drift if abs(drift) > 0.01 else 0.0     # smyčky na místě nechat beze změny

    def goto(i):
        t = i / FPS * a.speed * src_fps
        fr = f0 + (t % span if a.loop else min(t, span))
        if drift:
            arm.location.x = home - drift * (fr - f0) / span
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
        return arm.matrix_world @ B[name].head

    def turn(name, v):
        return ((arm.matrix_world @ B[name].matrix).to_3x3() @ v).normalized()

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
    hips = []
    for i in range(a.length):
        goto(i)
        hips.append(pos("Hips").x)
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
                for p in (arm.matrix_world @ pb.head, arm.matrix_world @ pb.tail):
                    lo, hi = np.minimum(lo, (p.x, p.z)), np.maximum(hi, (p.x, p.z))
    if not meshes:                                       # kosti vedou středem těla, tělo je širší
        lo, hi = lo - (0.1, floor_pad), hi + (0.1, 0.1)

    w, h = hi - lo
    # výška ve stoje z klidové pózy (temeno − špičky), nezávislá na tom, co tanec dělá
    stand = rest("HeadTop_End").z - min(rest("LeftToe_End").z, rest("RightToe_End").z)
    if a.fill:
        frame_h = stand / a.fill
    else:
        frame_h = max(h / FILL_H, w / FILL_W * a.height / a.width) / a.zoom
    frame_w = frame_h * a.width / a.height
    # Z obálky je střed rámu i s rozmáchnutou paží — při --fill, kdy se končetiny
    # ořezávají, by tělo stálo mimo střed (snake hip hop u pravého okraje). Tam
    # se centruje rozsah pohybu boků a končetiny vyjíždějí na obě strany.
    cx = (min(hips) + max(hips)) / 2 if a.fill else (lo[0] + hi[0]) / 2
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
                   "zoom": a.zoom, "fill": a.fill, "proportions": proportions, "frames": frames}, fh)

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

    print("POSE %s  %d snímků @ %d fps  (animace %d snímků @ %g fps, tempo %g×%s, %s)  obálka %.2f×%.2f m  "
          "postava %d %% výšky%s"
          % (a.out, a.length, FPS, span, src_fps, a.speed,
             (", smyčka (posun %.2f m rozpuštěn)" % drift if drift else ", smyčka") if a.loop else "",
             "ve stoje %d %% výšky" % round(100 * stand / frame_h) if a.fill else "zoom %g" % a.zoom, w, h,
             round(100 * h / frame_h), "" if w <= frame_w else ", krajní polohy %.2f m za rámem" % (w - frame_w)))


main()
