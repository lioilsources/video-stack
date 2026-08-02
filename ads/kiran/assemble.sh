#!/usr/bin/env bash
# Kiran ad — montáž: concat 10 záběrů + hudba/SFX z geometry_wars + titulky.
# Timeline (s): shot k začíná na (k-1)*3.0625; celkem 30.63 s.
#   hudba: intro (0–9.2) → theme_3 (9.2–15.3) → theme_5 boss (15.3–27.6)
#          → sector_complete + dojezd (27.6–30.6)
#   SFX:   explosion_large @13.5 (souboj), fire_beam @21.5 (palba na bosse),
#          explosion_large @24.6 + explosion_small @25.6 (rozpad)
set -euo pipefail
D="$(dirname "$0")"
A=/Volumes/YOTTA/Dev/Kiran/tyrian_mobile/assets/skins/geometry_wars
FONT=/System/Library/Fonts/Helvetica.ttc

ffmpeg -y -loglevel error \
  -i "$D/shots/shot01_00001_.webm" -i "$D/shots/shot02_00001_.webm" \
  -i "$D/shots/shot03_00001_.webm" -i "$D/shots/shot04_00001_.webm" \
  -i "$D/shots/shot05_00001_.webm" -i "$D/shots/shot06_00001_.webm" \
  -i "$D/shots/shot07_00001_.webm" -i "$D/shots/shot08_00001_.webm" \
  -i "$D/shots/shot09_00001_.webm" -i "$D/shots/shot10_00001_.webm" \
  -i "$A/music/intro.ogg" -i "$A/music/theme_3.ogg" -i "$A/music/theme_5.ogg" \
  -i "$A/sfx/sector_complete.ogg" -i "$A/sfx/explosion_large.ogg" \
  -i "$A/sfx/fire_beam.ogg" -i "$A/sfx/explosion_small.ogg" \
  -loop 1 -i "$D/titles/t1.png" -loop 1 -i "$D/titles/t2.png" -loop 1 -i "$D/titles/t3.png" \
  -filter_complex "
    [0:v][1:v][2:v][3:v][4:v][5:v][6:v][7:v][8:v][9:v]concat=n=10:v=1:a=0[cat];
    [cat][17:v]overlay=0:0:enable='between(t,0.5,2.9)'[v1];
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
  -c:v libx264 -crf 18 -pix_fmt yuv420p -r 16 -c:a aac -b:a 192k \
  -movflags +faststart "$D/kiran_ad_draft.mp4"

ffprobe -v error -show_entries format=duration:stream=codec_type,codec_name \
  -of csv=p=0 "$D/kiran_ad_draft.mp4"
