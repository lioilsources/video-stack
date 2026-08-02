#!/usr/bin/env python3
"""Kiran ad — montáž řízená manifestem.

Časová osa se POČÍTÁ z `length` jednotlivých záběrů; v shots.json se nikde
neuvádí. Změna délky záběru je tedy jediná editace a všechno ostatní
(pozice záběrů, timing hudby a SFX, titulky, celková stopáž) se dopočítá.

  ./assemble.py --plan            # vypíše časovou osu, nic nerenderuje
  ./assemble.py --titles          # vyrenderuje titulkové PNG
  ./assemble.py                   # 1080p finále z shots_hd/
  ./assemble.py --draft           # 480p z shots/ (bez upscalu)

Audio se v manifestu kotví symbolicky: "at": {"shot": "06"} = začátek záběru 6,
{"shot": "09", "offset": 0.6} = 0.6 s po jeho začátku, {"end": true} = konec
spotu. Absolutní čas ("at": 13.5) taky funguje.
"""
import argparse, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
M = json.load(open(os.path.join(HERE, "shots.json")))
FONT = "/System/Library/Fonts/Helvetica.ttc"


def shot_trim(shot):
    """(offset, dur) vybraného úseku záběru. Bez trimu = celý záběr."""
    full = shot.get("length", M["defaults"]["length"]) / M["fps_render"]
    tr = shot.get("trim")
    if not tr:
        return 0.0, full
    a = float(tr.get("from", 0.0))
    b = float(tr.get("to", full))
    if not (0.0 <= a < b <= full + 1e-6):
        raise SystemExit("shot%s: neplatný trim %r (záběr má %.3f s)" % (shot["id"], tr, full))
    return a, min(b, full) - a


def timeline():
    """[(shot, start, dur, src_offset)] + celková délka.

    Délka záběru ve spotu = jeho trim, nebo celá stopáž. Časy se skládají
    kumulativně, takže zkrácení jednoho záběru posune všechny následující
    i s navázaným audiem a titulky."""
    out, t = [], 0.0
    for shot in M["shots"]:
        off, dur = shot_trim(shot)
        out.append((shot, t, dur, off))
        t += dur
    return out, t


def resolve(anchor, tl, total):
    """Symbolická kotva -> absolutní čas v sekundách."""
    if isinstance(anchor, (int, float)):
        return float(anchor)
    if anchor.get("end"):
        return total + float(anchor.get("offset", 0))
    for s, start, dur, _off in tl:
        if s["id"] == anchor["shot"]:
            return start + float(anchor.get("offset", 0))
    raise SystemExit("neznámá kotva: %r" % (anchor,))


def plan():
    tl, total = timeline()
    print("Časová osa (fps render %d, finální %d)" % (M["fps_render"], M["fps_final"]))
    for s, start, dur, off in tl:
        full = s.get("length", M["defaults"]["length"]) / M["fps_render"]
        mark = "  [trim %.2f–%.2f z %.2f s]" % (off, off + dur, full) if s.get("trim") else ""
        print("  shot%s  %6.2f–%6.2f s  (%4.2f s, %d snímků)  %s%s"
              % (s["id"], start, start + dur, dur,
                 s.get("length", M["defaults"]["length"]), s["title"], mark))
    print("  celkem: %.2f s\n" % total)
    print("Hudba:")
    for m in M["audio"]["music"]:
        at = resolve(m["at"], tl, total)
        print("  %6.2f s  %-22s trim %.1f s, gain %.2f" % (at, m["file"], m["trim"], m["gain"]))
    print("SFX:")
    for x in M["audio"]["sfx"]:
        print("  %6.2f s  %-22s gain %.2f" % (resolve(x["at"], tl, total), x["file"], x["gain"]))
    print("Titulky:")
    for t in M["titles"]:
        print("  %6.2f–%6.2f s  %r" % (resolve(t["from"], tl, total),
                                       resolve(t["to"], tl, total), t["text"]))
    return tl, total


def make_titles(hd=True):
    w, h = (1920, 1080) if hd else (832, 480)
    scale = 1.0 if hd else 832 / 1920
    d = os.path.join(HERE, "titles_hd" if hd else "titles")
    os.makedirs(d, exist_ok=True)
    for i, t in enumerate(M["titles"], 1):
        size = max(12, int(t["size"] * scale))
        off = int(t["offset"] * scale)
        out = os.path.join(d, "t%d.png" % i)
        subprocess.run(["magick", "-size", "%dx%d" % (w, h), "xc:none",
                        "-font", FONT, "-pointsize", str(size), "-gravity", t["gravity"],
                        "-fill", "black", "-annotate", "+%d+%d" % (max(2, int(3 * scale)), off + int(4 * scale)), t["text"],
                        "-fill", "white", "-annotate", "+0+%d" % off, t["text"], out], check=True)
        print("  %s  %r" % (os.path.relpath(out, HERE), t["text"]))


def assemble(hd=True):
    tl, total = timeline()
    shots_dir = os.path.join(HERE, "shots_hd" if hd else "shots")
    if not os.path.isdir(shots_dir):
        sys.exit("chybí %s" % shots_dir)
    skin = M["audio"]["skin_dir"]
    tdir = os.path.join(HERE, "titles_hd" if hd else "titles")
    if not os.path.isdir(tdir):
        make_titles(hd)

    inputs, filt = [], []
    for s, _, dur, off in tl:
        p = os.path.join(shots_dir, "shot%s.webm" % s["id"])
        if not os.path.exists(p):  # tolerantně i k pojmenování z ComfyUI
            alt = os.path.join(shots_dir, "shot%s_00001_.webm" % s["id"])
            p = alt if os.path.exists(alt) else sys.exit("chybí %s" % p)
        if s.get("trim"):
            inputs += ["-ss", "%.3f" % off, "-t", "%.3f" % dur, "-i", p]
        else:
            inputs += ["-i", p]
    n = len(tl)

    for i in range(n):
        filt.append("[%d:v]fps=%d,setpts=PTS-STARTPTS[c%d];" % (i, M["fps_final"], i))
    filt.append("".join("[c%d]" % i for i in range(n)) + "concat=n=%d:v=1:a=0[cat];" % n)
    if hd:
        filt.append("[cat]scale=1920:1056:flags=lanczos,pad=1920:1080:0:12:color=black[base];")
    else:
        filt.append("[cat]null[base];")

    ai = n  # index dalšího vstupu
    amix = []
    for m in M["audio"]["music"]:
        inputs += ["-i", os.path.join(skin, m["file"])]
        at = resolve(m["at"], tl, total)
        lbl = "m%d" % len(amix)
        fo = max(0.0, m["trim"] - 0.8)
        filt.append("[%d:a]atrim=0:%.3f,afade=t=in:d=0.3,afade=t=out:st=%.3f:d=0.8,"
                    "volume=%.2f,adelay=%d:all=1,aformat=channel_layouts=stereo[%s];"
                    % (ai, m["trim"], fo, m["gain"], int(at * 1000), lbl))
        amix.append(lbl); ai += 1
    for x in M["audio"]["sfx"]:
        inputs += ["-i", os.path.join(skin, x["file"])]
        at = resolve(x["at"], tl, total)
        lbl = "s%d" % len(amix)
        filt.append("[%d:a]volume=%.2f,adelay=%d:all=1,aformat=channel_layouts=stereo[%s];"
                    % (ai, x["gain"], int(at * 1000), lbl))
        amix.append(lbl); ai += 1

    prev = "base"
    for i, t in enumerate(M["titles"], 1):
        inputs += ["-loop", "1", "-i", os.path.join(tdir, "t%d.png" % i)]
        nxt = "v%d" % i
        filt.append("[%s][%d:v]overlay=0:0:enable='between(t,%.3f,%.3f)'[%s];"
                    % (prev, ai, resolve(t["from"], tl, total), resolve(t["to"], tl, total), nxt))
        prev = nxt; ai += 1

    filt.append("".join("[%s]" % l for l in amix) +
                "amix=inputs=%d:normalize=0,alimiter=limit=0.95,apad=whole_dur=%.3f[a]"
                % (len(amix), total))

    out = os.path.join(HERE, "kiran_ad_1080p.mp4" if hd else "kiran_ad_draft.mp4")
    cmd = (["ffmpeg", "-y", "-loglevel", "error"] + inputs +
           ["-filter_complex", "".join(filt), "-map", "[%s]" % prev, "-map", "[a]",
            "-t", "%.3f" % total, "-c:v", "libx264", "-crf", "17" if hd else "20",
            "-preset", "slow" if hd else "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out])
    subprocess.run(cmd, check=True)
    print("  -> %s (%.2f s)" % (os.path.relpath(out, HERE), total))
    subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                    "stream=width,height,r_frame_rate,codec_name", "-of", "csv=p=0", out])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--titles", action="store_true")
    ap.add_argument("--draft", action="store_true")
    a = ap.parse_args()
    if a.plan:
        plan()
    elif a.titles:
        make_titles(not a.draft)
    else:
        assemble(not a.draft)
