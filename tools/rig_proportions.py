"""rig_proportions.py — proporce postavy z fotky vůči Mixamo figuríně, pro mixamo_pose.py --proportions.

    blender -b --factory-startup -noaudio -P tools/rig_proportions.py -- postava_mia.fbx drive/src/<id>_prop.json

Vstup je rig z UGCFactory (MIA na Sparku, 3D model postavy z fotky → mixamorig kostra,
viz UGCFactory nas/docs/FANTASYCHARACTER_PLAN.md §13). Výstup jsou poměry segmentů,
kterými mixamo_pose.py přestaví Mixamo kostru, než z ní vzorkuje klouby.

Proč vůbec: kostra tance má vždycky proporce Mixamo figuríny a Wan pak tělo z fotky
přetváří na ně. reports/phase4_identity.md to jmenuje jako další krok („fit kostry
na postavu ve fotce per job").

Proč kalibrace: délky kostí z MIA nejdou brát naivně. MIA staví klouby jinde než
Mixamo — na samotné Mixamo figuríně (mesh z Zombie Walk.fbx) jí vyšla stehna 0.79,
trup 1.22 a ramena 1.15 Mixamo kostry. Bez odečtení by každá postava dostala
zkrácená stehna a dlouhý trup. Poměr se proto bere MIA(postava) / MIA(figurína);
čísla figuríny jsou níž (CALIBRATION), naměřená 2026-09-15.

Segmenty se berou jen tam, kde znamenají totéž v obou kostrách: kost ruky má MIA
až po špičky prstů (bez prstů) a kost hlavy jinak dlouhou, takže ruce, hlava
a chodidla zůstávají Mixamo. Vše je normované vzdáleností kotníky → krk, takže
výška postavy nerozhoduje (mixamo_pose.py stejně rámuje podle --fill).
"""
import json
import os
import sys

import bpy

# MIA rig na Mixamo figuríně (Beta mesh ze Zombie Walk.fbx), segmenty / (kotníky → krk)
CALIBRATION = {"thigh": 0.2490, "shin": 0.3141, "arm": 0.2001, "forearm": 0.1898,
               "torso": 0.3989, "hips": 0.1230, "shoulders": 0.2456}
CLAMP = (0.8, 1.25)   # za tím je spíš chyba odhadu (ruka za zády, podpatky) než tělo


def segments(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=os.path.abspath(path))
    arm = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
    mw = arm.matrix_world
    head = {b.name.split(":")[-1]: mw @ b.head_local for b in arm.data.bones}

    def side(fn):
        return (fn("Left") + fn("Right")) / 2

    ankle = (head["LeftFoot"] + head["RightFoot"]) / 2
    norm = (head["Neck"] - ankle).length
    return {
        "thigh": side(lambda s: (head[s + "Leg"] - head[s + "UpLeg"]).length) / norm,
        "shin": side(lambda s: (head[s + "Foot"] - head[s + "Leg"]).length) / norm,
        "arm": side(lambda s: (head[s + "ForeArm"] - head[s + "Arm"]).length) / norm,
        "forearm": side(lambda s: (head[s + "Hand"] - head[s + "ForeArm"]).length) / norm,
        "torso": (head["Neck"] - head["Hips"]).length / norm,
        "hips": (head["LeftUpLeg"] - head["RightUpLeg"]).length / norm,
        "shoulders": (head["LeftArm"] - head["RightArm"]).length / norm,
    }


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    if len(argv) != 2:
        sys.exit("použití: rig_proportions.py -- <rig.fbx> <výstup.json>")
    rig, out = argv
    seg = segments(rig)
    ratios = {k: round(min(max(seg[k] / CALIBRATION[k], CLAMP[0]), CLAMP[1]), 3) for k in CALIBRATION}
    data = {"source": os.path.basename(rig), "ratios": ratios,
            "raw": {k: round(v / CALIBRATION[k], 3) for k, v in seg.items()}}
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(data, fh, indent=2)
    print("PROPORTIONS", json.dumps(data), flush=True)


main()
