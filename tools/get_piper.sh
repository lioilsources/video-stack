#!/usr/bin/env bash
# get_piper.sh — vypravěč pro tools/story.py: Piper TTS + hlasy (na SPARKu, bez sudo).
#
#   tools/get_piper.sh                       # venv + výchozí hlasy (cs kasandra, en lessac)
#   tools/get_piper.sh cs_CZ-jirka-low       # další hlas podle jména z rhasspy/piper-voices
#
# Kam: $PIPER_HOME (default ~/.local/share/video-stack/piper) — venv/ a voices/.
# Vlastní venv, ne ComfyUI: Piper tahá vlastní onnxruntime a ComfyUI venv má
# pinnutý jiný (1.24) kvůli custom nodům. CPU stačí — věta je ~0,3 s.
set -euo pipefail

HOME_DIR="${PIPER_HOME:-$HOME/.local/share/video-stack/piper}"
VERSION="${PIPER_VERSION:-1.3.0}"
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOICES=("$@")
[ ${#VOICES[@]} -gt 0 ] || VOICES=(cs_CZ-kasandra-medium en_US-lessac-medium)

mkdir -p "$HOME_DIR/voices"
if [ ! -x "$HOME_DIR/venv/bin/piper" ]; then
  echo "  venv + piper-tts $VERSION → $HOME_DIR/venv"
  python3 -m venv "$HOME_DIR/venv"
  "$HOME_DIR/venv/bin/pip" install -q --upgrade pip
  "$HOME_DIR/venv/bin/pip" install -q "piper-tts==$VERSION"
fi

for v in "${VOICES[@]}"; do
  # cs_CZ-jirka-medium → cs/cs_CZ/jirka/medium/cs_CZ-jirka-medium.onnx
  IFS=- read -r loc name quality <<<"$v"
  lang="${loc%%_*}"
  path="$lang/$loc/$name/$quality/$v"
  for ext in onnx onnx.json; do
    dst="$HOME_DIR/voices/$v.$ext"
    if [ -s "$dst" ]; then continue; fi
    echo "  hlas $v.$ext"
    curl -fL --retry 3 -C - -o "$dst.part" "$BASE/$path.$ext"
    mv "$dst.part" "$dst"
  done
done

# zkouška: jedna věta na hlas, ať chyba vyleze tady a ne uprostřed mixu
for v in "${VOICES[@]}"; do
  out="$(mktemp --suffix=.wav)"
  echo "Ahoj, tady vypravěč. Hello, this is the narrator." \
    | "$HOME_DIR/venv/bin/piper" -m "$HOME_DIR/voices/$v.onnx" -f "$out" >/dev/null 2>&1 \
    && echo "  ok $v ($(stat -c %s "$out") B)" || { echo "  ! $v: syntéza selhala"; exit 1; }
  rm -f "$out"
done
echo "hotovo — story.py voice <příběh> najde Piper v $HOME_DIR"
