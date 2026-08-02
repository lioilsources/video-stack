#!/usr/bin/env bash
# Kiran ad — 10 keyframů 832×480 z geometry_wars assetů.
# Pozadí: layer_* (512×1024) škálované na šířku 832 a ořezané s různým
# y-offsetem, layer_1 přimíchaný přes screen pro hloubku.
set -euo pipefail
S=/Volumes/YOTTA/Dev/Kiran/tyrian_mobile/assets/skins/geometry_wars
O="$(dirname "$0")/keyframes"
rm -rf "$O"; mkdir -p "$O"

# bg <out> <y-offset-base> <y-offset-mix>
bg() {
  magick "$S/backgrounds/layer_0.png" -resize 832x -crop "832x480+0+$2" +repage \
    \( "$S/backgrounds/layer_1.png" -resize 832x -crop "832x480+0+$3" +repage \) \
    -compose screen -composite "$1"
}
# put <canvas> <sprite> <geometry_WxH> <position+X+Y> [extra magick opts pro sprite]
put() {
  local c="$1" s="$2" g="$3" grav="$4" off="$5"; shift 5
  magick "$c" \( "$S/sprites/$s.png" -resize "$g" "$@" \) \
    -compose over -gravity "$grav" -geometry "$off" -composite "$c"
}

# putcell — jako put, ale ze sprite sheetu vyřízne prostřední políčko 240x240
putcell() {
  local c="$1" s="$2" g="$3" grav="$4" off="$5"; shift 5
  magick "$c" \( "$S/sprites/$s.png" -crop 240x240+240+240 +repage -resize "$g" "$@" \)     -compose over -gravity "$grav" -geometry "$off" -composite "$c"
}

# ---- 1: hero vessel, kamera bude kroužit (camera_control CW) ----
bg "$O/shot01.png" 200 700
put "$O/shot01.png" vessel_0 300x center +0+10
put "$O/shot01.png" starg 40x northwest +260+120
put "$O/shot01.png" starg 24x northwest +640+330

# ---- 2: drift vesmírem — vessel malý vlevo, prostor vpravo ----
bg "$O/shot02.png" 500 100
put "$O/shot02.png" vessel_0 160x northwest +150+200 -background none -rotate 12

# ---- 3: průlet meteority ----
bg "$O/shot03.png" 340 520
put "$O/shot03.png" asteroid2 260x northwest +560+40
put "$O/shot03.png" asteroid 180x northwest +60+300 -background none -rotate 25
put "$O/shot03.png" asteroid3 120x northwest +680+350
put "$O/shot03.png" asteroid1 90x northwest +330+60 -background none -rotate -40
put "$O/shot03.png" vessel_0 150x northwest +280+250 -background none -rotate -8

# ---- 4: formace falconů ----
bg "$O/shot04.png" 60 800
put "$O/shot04.png" falcon 130x northwest +351+60
put "$O/shot04.png" falcon2 120x northwest +220+150 -background none -rotate 3
put "$O/shot04.png" falcon3 120x northwest +490+150 -background none -rotate -3
put "$O/shot04.png" falcon4 110x northwest +110+250
put "$O/shot04.png" falcon5 110x northwest +600+250
put "$O/shot04.png" falconx 150x northwest +341+300

# ---- 5: souboj — vessel dole, falconi útočí, lasery ----
bg "$O/shot05.png" 420 300
put "$O/shot05.png" falcon 120x northwest +180+50 -background none -rotate 170
put "$O/shot05.png" falcon2 110x northwest +520+80 -background none -rotate -160
put "$O/shot05.png" laser '26x300!' northwest +415+120 -background none -rotate 3
put "$O/shot05.png" laser '26x260!' northwest +250+140 -background none -rotate -12
put "$O/shot05.png" vessel_2 190x northwest +330+310
putcell "$O/shot05.png" explosion2 90x northwest +610+150

# ---- 6: boss se objevuje — bouncer shora, vessel malý dole ----
bg "$O/shot06.png" 0 900
put "$O/shot06.png" bouncer 420x north +0-60
put "$O/shot06.png" vessel_0 110x northwest +360+370

# ---- 7: boss dominuje — vessel v úzkých, exploze u něj ----
bg "$O/shot07.png" 80 640
put "$O/shot07.png" bouncer 520x northeast -40-80
put "$O/shot07.png" blaster 260x northwest +290+240 -background none -rotate 35
put "$O/shot07.png" vessel_2 150x northwest +120+330 -background none -rotate -20
putcell "$O/shot07.png" explosion4 190x northwest +90+280

# ---- 8: boss se zahřívá pod palbou — vulcan stream + oranžový glow ----
bg "$O/shot08.png" 150 450
put "$O/shot08.png" bouncer 430x north +30-40 -modulate 100,130 -fill "#ff6a00" -tint 35
put "$O/shot08.png" vulcan '20x180!' northwest +390+250
put "$O/shot08.png" vulcan '20x160!' northwest +430+260 -background none -rotate 4
putcell "$O/shot08.png" explosion2 120x northwest +380+90
put "$O/shot08.png" vessel_0 170x northwest +350+360

# ---- 9: rozpad bosse ----
bg "$O/shot09.png" 260 60
put "$O/shot09.png" bouncer 380x center +0-40 -modulate 110,60
putcell "$O/shot09.png" explosion1 150x northwest +170+90
putcell "$O/shot09.png" explosion3 170x northwest +520+130
putcell "$O/shot09.png" explosion4 130x northwest +330+250
put "$O/shot09.png" asteroid1 70x northwest +150+300 -background none -rotate 80
put "$O/shot09.png" asteroid3 50x northwest +620+90 -background none -rotate -30
put "$O/shot09.png" vessel_0 130x northwest +90+370

# ---- 10: odlet do dalšího perimetru — jemná záře nahoře, vessel míří pryč ----
bg "$O/shot10.png" 544 300
put "$O/shot10.png" starg 30x northwest +300+60
put "$O/shot10.png" starg 20x northwest +520+100
put "$O/shot10.png" starg 14x northwest +410+40
put "$O/shot10.png" vessel_0 120x north +40+140

echo "hotovo:"; ls "$O"
magick montage "$O"/shot*.png -tile 5x2 -geometry +4+4 -background '#111' "$O/contact_sheet.png"
