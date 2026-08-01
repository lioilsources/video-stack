# Fáze 4 — Audio (2026-08-01)

## MMAudio post-hoc foley — funguje

- Modely: Kijai/MMAudio_safetensors fp16 (large_44k_v2 + synchformer + VAE
  + Apple DFN CLIP), 4.8 GB do models/mmaudio/.
- Workflow audio_mmaudio.json: VHS_LoadVideo → MMAudioSampler (25 kroků,
  cfg 4.5) → VHS_VideoCombine (h264-mp4 + AAC mux). ffmpeg na stroji je
  (VHS nabízí i nvenc formáty).
- Test na v2v_control výstupu, prompt "jet engine roar, wind rushing past":
  345 s vč. načtení modelů, výstup h264+AAC, mean -31.7 dB / max -6.1 dB.
- Nalezený bug nodu: MMAudioFeatureUtilsLoader deklaruje mode/precision jako
  optional, ale funkce je vyžaduje → nutno v API workflow zadat explicitně
  ("mode": "44k", "precision": "fp16").

## Neřešeno (vědomě)

- LTX-2 nativní audio — Tier C stále odložen; MMAudio pokrývá potřebu.
- InfiniteTalk lip-sync — až fáze 5+ (dialogy z české TTS pipeline).
