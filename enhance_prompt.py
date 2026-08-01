#!/usr/bin/env python3
"""Prompt enhancement pro video generaci (fáze 3 plánu).

Expanduje stručný uživatelský prompt na cinematic video prompt přes AiStack
gateway (:8080). Alias modelu je konfigurovatelný — textová expanze funguje
s kterýmkoli LLM aliasem (shop, dev, lab); obrazová varianta (--image pro
I2V) vyžaduje vision backend.
"""
import argparse, base64, json, sys, urllib.request

GATEWAY = "http://localhost:8080/v1/chat/completions"
SYSTEM = (
    "You expand terse user prompts into rich cinematic video-generation prompts. "
    "Describe: subject and action, camera movement (dolly/pan/orbit/handheld), "
    "lighting and atmosphere, style. One paragraph, max 90 words, English, "
    "no lists, no preamble. Preserve every concrete detail the user gave. "
    "If an image is provided, ground the description in it."
)

def enhance(prompt, model="shop", image=None, timeout=120):
    content = [{"type": "text", "text": prompt}]
    if image:
        b64 = base64.b64encode(open(image, "rb").read()).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + b64}})
    body = {"model": model, "max_tokens": 220, "temperature": 0.7,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": content}]}
    req = urllib.request.Request(GATEWAY, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=timeout))
    return d["choices"][0]["message"]["content"].strip()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--model", default="shop")
    ap.add_argument("--image")
    a = ap.parse_args()
    try:
        print(enhance(a.prompt, a.model, a.image))
    except Exception as e:
        # degradace: při nedostupné gateway vrať původní prompt (exit 0,
        # volající pipeline nesmí spadnout kvůli enhancementu)
        print(a.prompt)
        print(f"[enhance failed: {e}]", file=sys.stderr)
