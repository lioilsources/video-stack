# Camera preset workflows (WAN 2.2 Fun Camera, Lightning)

Fast image-to-video workflows demonstrating each `WanCameraEmbedding` camera move,
derived from the Kiran `shot01` Fun Camera base.

## What's baked in
- **Model:** WAN 2.2 Fun Camera 14B (high + low noise experts)
- **Speed:** Lightning 4-step LoRAs (Seko V1, high/low) → 8 steps split 4+4, cfg 1.0,
  shift 5.0 — roughly 5× faster than the 20-step / cfg-5 base
- **Motion:** `camera_pose` per file, `speed = 0.5` (tune 0.2 gentle … 1.0 full sweep)
- **Output:** 1280×704, length 49, SaveWEBM → `output/camera_presets/<preset>`

| File | camera_pose | Move |
|------|-------------|------|
| `camera_preset_pan_up.json`    | Pan Up    | tilt up (feet → head) |
| `camera_preset_pan_down.json`  | Pan Down  | tilt down (head → feet) |
| `camera_preset_pan_left.json`  | Pan Left  | pan left |
| `camera_preset_pan_right.json` | Pan Right | pan right |
| `camera_preset_zoom_in.json`   | Zoom In   | push in |
| `camera_preset_zoom_out.json`  | Zoom Out  | pull back |
| `camera_preset_acw.json`       | Anti Clockwise (ACW) | orbit left |
| `camera_preset_cw.json`        | ClockWise (CW)       | orbit right (= fast shot01) |

## Usage
Swap the keyframe in the `LoadImage` node (default `kiran_ad_shot01.png`) and the
positive prompt (scene/style only — the move is driven by `camera_pose`, not text).
Lower `speed` if the camera sweeps the subject out of frame.

The Lightning LoRAs live in `models/loras/` on the SPARK box; download via
`download_models.sh` if setting up fresh.
