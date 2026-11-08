# test_config_cameras — plan

Starting point: `test_protocol.py` (connectivity probe plus PTZ control for Dahua CGI and ONVIF).
Goal: after testing a camera, write **one complete camera JSON** that edge_worker's `multy.py`
loads as is (same format as `edge_worker/gate.json`, `livingroom.json`, `apcam.json`).

`multy.py` only stream-copies, so the JSON must describe what the camera **really sends**,
measured, not typed by hand. Example: `livingroom.json` says 1920x1080, 20 fps, GOP 20, but the camera
actually sends **2304x1296, 15 fps, GOP 90** (measured 2026-11-08).

## Test stages (each adds to one result dict)

| # | Stage | Status | Fills JSON |
|---|---|---|---|
| 1 | Reach: ping, port sweep, RTSP OPTIONS | exists (`CameraDiagnostics`) | `camera.port`, `http_port` |
| 2 | Control: Dahua CGI / ONVIF login, profile, PTZ status | done: ONVIF login errors now reported; `none` skips PTZ | `ptz_protocol`, `onvif_profile` |
| 3 | Streams: try RTSP paths, `ffprobe` codec / resolution / fps / GOP | done (`--test`) | `main_stream_path` / `sub_stream_path` or `*_subtype`, `stream_encoder_properties` |
| 4 | Encoder: read config (Dahua `configManager.cgi?action=getConfig&name=Encode`, ONVIF `GetVideoEncoderConfigurations`); optionally set H.264 + target fps/GOP, then re-run stage 3 | Dahua read done; ONVIF read and setting not yet | `stream_encoder_properties` (codec, bitrate) |
| 5 | Copy test: `ffmpeg -rtsp_transport tcp -c copy` N seconds, check duration and keyframes | done | `connection.rtsp_transport` |
| 6 | Write JSON | done: `out/<config name>.json` + `.report.json` | everything |

## Output JSON

- Sections in this order: `camera`, `connection`, `sensor_hardware_properties`,
  `stream_encoder_properties`, `motion`, `face_quality`.
- Measured values come from stages 1–5. `motion`, `face_quality` and `sensor_hardware_properties`
  are tuning values that can't be measured: keep them from the existing camera JSON if one is given,
  otherwise use defaults from a template.
- The keys multy.py reads must be present: `camera.host/port/username/password/vendor`,
  stream paths (or Dahua `channel` + `main_stream_subtype`/`sub_stream_subtype`), `invert_stream`,
  `http_port`, `ptz_protocol`, `connection.rtsp_transport`, `stream_encoder_properties.fps /
  main_stream_resolution / sub_stream_resolution`, and the `motion` keys.
- Written to `C:\Users\sudha\camtest\out\<name>.json` (git-ignored, because it holds the password),
  plus a `<name>.report.json` with the raw test results. Copying into edge_worker is manual.
- Reject or flag: codec not H.264, GOP > 2×fps, sub stream identical to main.

## Conventions

- Log lines: `<camera> HH:MM:SS <message>`, short, no banners.
- Never commit passwords; the camera JSON is read from edge_worker or given on the command line.
- Without `--test` the script works as before (PTZ keypad, preset moves); `--test` is non-interactive.

## Usage

Run from the folder holding the camera JSONs (e.g. `C:\Users\sudha\edge_worker`):

```
python C:\Users\sudha\camtest\test_protocol.py gate --test
```
