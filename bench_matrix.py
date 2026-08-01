#!/usr/bin/env python3
"""Benchmark matice fáze 3: workflow x rozlišení x délka -> TSV."""
import json, sys, time, urllib.request

API = "http://localhost:8188"
PROMPT = ("aerial dogfight over floating voxel islands at sunset, a small fighter "
          "plane banks between rocky sky islands, cinematic, dynamic camera")

def free_mb():
    with open("/proc/meminfo") as f:
        m = {l.split(":")[0]: int(l.split()[1]) for l in f}
    return (m["MemTotal"] - m["MemAvailable"]) // 1024

def run(wf_path, width, height, length):
    d = {"prompt": json.load(open(wf_path))}
    p = d["prompt"]
    for node in p.values():
        i = node["inputs"]
        if node["class_type"] in ("Wan22ImageToVideoLatent", "EmptyHunyuanLatentVideo",
                                  "WanImageToVideo", "WanFirstLastFrameToVideo"):
            i.update(width=width, height=height, length=length)
        if i.get("text") == "PROMPT_PLACEHOLDER":
            i["text"] = PROMPT
    req = urllib.request.Request(API + "/prompt", json.dumps(d).encode(),
                                 {"Content-Type": "application/json"})
    pid = json.load(urllib.request.urlopen(req))["prompt_id"]
    t0, peak = time.time(), free_mb()
    while True:
        time.sleep(5)
        peak = max(peak, free_mb())
        h = json.load(urllib.request.urlopen(API + "/history/" + pid))
        if pid in h:
            st = h[pid]["status"]
            if st.get("completed"):
                return time.time() - t0, peak, "ok"
            if st.get("status_str") == "error":
                return time.time() - t0, peak, "ERROR"
        if time.time() - t0 > 3600:
            return time.time() - t0, peak, "TIMEOUT"

if __name__ == "__main__":
    wf, w, h, l = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    dur, peak, status = run(wf, w, h, l)
    print(f"{wf.rsplit(chr(47),1)[-1]}\t{w}x{h}\t{l}\t{dur:.0f}\t{peak}\t{status}", flush=True)
