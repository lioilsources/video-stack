#!/usr/bin/env python3
"""Kiran ad — 10 zaberu pres i2v_final_14b_lightning, 480p, 49 snimku."""
import json, time, urllib.request

API = "http://localhost:8188"
TAIL = ", neon vector graphics, glowing wireframe outlines, deep space, vibrant cyan and magenta, high contrast"
SHOTS = [
 ("01", "camera slowly orbits around the glowing vessel, engine flame flickering"),
 ("02", "the vessel drifts weightlessly through space, slow camera follow, stars parallax"),
 ("03", "the vessel weaves between tumbling asteroids, camera tracking, near miss"),
 ("04", "falcon fighters glide into V formation, menacing approach toward camera"),
 ("05", "dogfight: lasers firing, fighters strafing, explosion flash, fast motion"),
 ("06", "massive boss ship descends from above, small vessel holds position, dread"),
 ("07", "boss unleashes barrage, vessel takes hits and tumbles, sparks and debris"),
 ("08", "vessel fires relentless stream, boss hull glowing hotter, orange heat building"),
 ("09", "boss ship breaks apart in chain explosions, fragments scattering, blinding flash"),
 ("10", "vessel accelerates away toward distant bright gate, engine trail, calm after battle"),
]

base = json.load(open("/home/ol1n/Code/video-stack/workflows/i2v_final_14b_lightning.json"))

for num, prompt in SHOTS:
    d = json.loads(json.dumps(base))
    d["10"]["inputs"]["image"] = f"kiran_ad_shot{num}.png"
    d["8"]["inputs"]["text"] = prompt + TAIL
    d["12"]["inputs"].update(width=832, height=480, length=49)
    d["16"]["inputs"]["filename_prefix"] = f"kiran_ad/shot{num}"
    req = urllib.request.Request(API + "/prompt", json.dumps({"prompt": d}).encode(),
                                 {"Content-Type": "application/json"})
    pid = json.load(urllib.request.urlopen(req))["prompt_id"]
    t0 = time.time()
    while True:
        time.sleep(5)
        h = json.load(urllib.request.urlopen(API + "/history/" + pid))
        if pid in h and h[pid]["status"].get("completed"):
            print(f"SHOT {num} OK {time.time()-t0:.0f}s", flush=True); break
        if pid in h and h[pid]["status"].get("status_str") == "error":
            print(f"SHOT {num} ERROR", flush=True); break
        if time.time() - t0 > 900:
            print(f"SHOT {num} TIMEOUT", flush=True); break
print("AD SHOTS DONE", flush=True)
