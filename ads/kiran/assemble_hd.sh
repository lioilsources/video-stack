#!/usr/bin/env bash
# Kiran ad — finální 1080p montáž.
# Vstup: shots_hd/*.webm (1280×704 @ 16 fps, 49 snímků, po RIFE 32 fps).
# Výstup: 1920×1080 @ 32 fps, hudba + SFX z geometry_wars, titulky.
#
# 1280×704 * 1.5 = 1920×1056 → doplněno na 1080 (12 px pruh nahoře i dole).
# Časová osa je stejná jako v draftu (3.0625 s/záběr), jen fps je dvojnásobné.
set -euo pipefail
D="$(dirname "$0")"
A=/Volumes/YOTTA/Dev/Kiran/tyrian_mobile/assets/skins/geometry_wars
S="$D/shots_hd"

ffmpeg -y -loglevel error \
  -i "$S/shot01.webm" -i "$S/shot02.webm" -i "$S/shot03.webm" -i "$S/shot04.webm" \
  -i "$S/shot05.webm" -i "$S/shot06.webm" -i "$S/shot07.webm" -i "$S/shot08.webm" \
  -i "$S/shot09.webm" -i "$S/shot10.webm" \
  -i "$A/music/intro.ogg" -i "$A/music/theme_3.ogg" -i "$A/music/theme_5.ogg" \
  -i "$A/sfx/sector_complete.ogg" -i "$A/sfx/explosion_large.ogg" \
  -i "$A/sfx/fire_beam.ogg" -i "$A/sfx/explosion_small.ogg" \
  -loop 1 -i "$D/titles_hd/t1.png" -loop 1 -i "$D/titles_hd/t2.png" -loop 1 -i "$D/titles_hd/t3.png" \
  -filter_complex "
    [0:v][1:v][2:v][3:v][4:v][5:v][6:v][7:v][8:v][9:v]concat=n=10:v=1:a=0[cat];
    [cat]scale=1920:1056:flags=lanczos,pad=1920:1080:0:12:color=black[up];
    [up][17:v]overlay=0:0:enable='between(t,0.5,2.9)'[v1];
    [v1][18:v]overlay=0:0:enable='between(t,27.7,29.2)'[v2];
    [v2][19:v]overlay=0:0:enable='between(t,29.3,30.6)'[v];
    [10:a]atrim=0:9.4,afade=t=in:d=0.3,afade=t=out:st=8.5:d=0.8,volume=0.85,aformat=channel_layouts=stereo[m1];
    [11:a]atrim=0:6.8,afade=t=in:d=0.25,afade=t=out:st=6.0:d=0.7,volume=0.85,adelay=9190:all=1,aformat=channel_layouts=stereo[m2];
    [12:a]atrim=0:12.8,afade=t=in:d=0.2,afade=t=out:st=11.5:d=1.2,volume=0.9,adelay=15310:all=1,aformat=channel_layouts=stereo[m3];
    [13:a]volume=0.8,adelay=27600:all=1,aformat=channel_layouts=stereo[s1];
    [14:a]asplit[e1][e2];
    [e1]volume=0.7,adelay=13500:all=1,aformat=channel_layouts=stereo[s2];
    [e2]volume=0.65,adelay=24600:all=1,aformat=channel_layouts=stereo[s3];
    [15:a]volume=0.55,adelay=21500:all=1,aformat=channel_layouts=stereo[s4];
    [16:a]volume=0.6,adelay=25600:all=1,aformat=channel_layouts=stereo[s5];
    [m1][m2][m3][s1][s2][s3][s4][s5]amix=inputs=8:normalize=0,alimiter=limit=0.95,
      apad=whole_dur=30.63[a]
  " \
  -map "[v]" -map "[a]" -t 30.63 \
  -c:v libx264 -crf 17 -preset slow -pix_fmt yuv420p -c:a aac -b:a 192k \
  -movflags +faststart "$D/kiran_ad_1080p.mp4"

ffprobe -v error -show_entries format=duration:stream=width,height,r_frame_rate,codec_name \
  -of csv=p=0 "$D/kiran_ad_1080p.mp4"
