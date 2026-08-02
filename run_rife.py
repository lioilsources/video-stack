import json, time, urllib.request, os
API="http://localhost:8188"
wf=json.load(open("/home/ol1n/Code/video-stack/workflows/upscale_interp.json"))
for i in range(1,11):
    num=f"{i:02d}"
    d=json.loads(json.dumps(wf))
    d["1"]["inputs"]["video"]=f"kiran_ad_hd/shot{num}_00001_.webm"
    d["2"]["inputs"]["scale_by"]=1.0          # bez upscalu, ten udela ffmpeg lanczos
    d["4"]["inputs"]["filename_prefix"]=f"kiran_ad_32/shot{num}"
    d["4"]["inputs"]["fps"]=32.0
    r=urllib.request.Request(API+"/prompt",json.dumps({"prompt":d}).encode(),{"Content-Type":"application/json"})
    pid=json.load(urllib.request.urlopen(r))["prompt_id"]; t0=time.time()
    while True:
        time.sleep(4)
        h=json.load(urllib.request.urlopen(API+"/history/"+pid))
        if pid in h and h[pid]["status"].get("completed"): print(f"RIFE {num} OK {time.time()-t0:.0f}s",flush=True); break
        if pid in h and h[pid]["status"].get("status_str")=="error":
            msgs=[m for m in h[pid]["status"].get("messages",[]) if m[0]=="execution_error"]
            print(f"RIFE {num} ERROR", msgs[0][1]["exception_message"][:150] if msgs else "", flush=True); break
        if time.time()-t0>1200: print(f"RIFE {num} TIMEOUT",flush=True); break
print("RIFE DONE",flush=True)
