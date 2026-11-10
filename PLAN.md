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

## Unknown cameras (`discover.py`)

```
python discover.py                                        # list cameras on this PC's network
python discover.py --identify 192.168.8.40 --user admin --password xxx --write
```

1. Find: ONVIF WS-Discovery (multicast, on the right network card) plus a TCP scan of the /24 for
   camera ports (554, 80, 8899, 8000, 8080, 2020, 34567, 37777). Hosts without ONVIF need RTSP open.
2. Identify: ONVIF maker / model / PTZ service / profiles (codec, size, fps) and each stream's RTSP
   address from GetStreamUri; Dahua CGI check (Dahua OEM cameras call themselves "General");
   without ONVIF, a short list of maker RTSP paths (Dahua, Hikvision, generic).
3. Logins: the one you give, plus no login. Given none, the usual factory logins are tried first
   (`admin` / blank, `admin` / `admin`, `admin` / `12345`, plus your own in `default_logins.json`,
   git-ignored), at most 3 per camera, one ONVIF (else Dahua CGI) call each, stopping at the first
   that works or when the camera reports a lock; only then the page asks for the login.
4. Writes a starting JSON; then run the camera test, which measures what the camera really sends.
   The camera test itself now also asks ONVIF for stream addresses.

Found this way (2026-11-08): livingroom and apcam (NVT JA-A1A) have a real sub stream,
`/live/ch00_1` 640x360 H.264; the edge_worker JSONs use `/live/ch0` for both main and sub.

## Add camera by IP (starting point)

The IP is the starting point; the camera JSON is the output. On the page: **Add camera by IP**,
login blank (default logins, see above) or given, **Identify** -> a new JSON with the measured
streams opens; then Run test, motion zones / ROI, PTZ presets, Save to out/.

## In the AiSys local site

`http://localhost:8080/cameras` on the central PC (admin only, not from other PCs): the local site
(aivision `site/local_web.py`) checks the sign-in and passes `/cameras/api/...` on to this server,
which keeps listening on 127.0.0.1:8091 only (service `AISYS-CamSetup`).

Backlog: find a camera whose IP is lost or outside this network (other subnet, factory default IP)
and move it into the control network's range.

## Page layout (one screen)

Parameters (tabs) fill the page; the test result is folded at the bottom. **View · PTZ · POI** opens
the floating window: picture, zones / ROI, OSD, the PTZ pad and the **points of interest** (numbered
01, 02 … automatically, you only type the name; Add stores the camera's current position as that
preset). The JSON's file name is decided by `camera.site` and `camera.name`: `<site>_<camera>.json`
(e.g. `hub_gate.json`), the same name on the edge and on central.

## On-screen display (OSD): title = camera ID, date format

`osd.py`, **Apply OSD** in the View window:
- **Title** = the camera ID `<site>_<camera>` from `camera.site` + `camera.name` (e.g. `hub_gate`, no
  spaces), the same name as the JSON file `hub_gate.json`. Replaces the camera's default ("IPC Camera").
- **Date format** = `osd.date_format` (the only OSD field; default `dd-MM-yyyy HH:mm:ss`). ONVIF has no
  `dd-MM-yyyy`: a camera that refuses it gets `dd/MM/yyyy` (logged).
- **Positions stay as the camera has them.** Read OSD (also when the View opens) shows them on the
  picture with the recommendation (date top left, title top middle). Only if one covers something:
  **Move date / Move title** (dashed), saved as `osd.date_position` / `osd.title_position`, sent with
  the next Apply OSD.
Dahua: ChannelTitle[ch].Name, Locales.TimeFormat, VideoWidget rectangles (only when moved). ONVIF:
GetOSDs / SetOSD / CreateOSD, keeping each OSD's own position element. Tested on simulated cameras.

## Config page (`web_ui.py`)

```
python C:\Users\sudha\camtest\web_ui.py            # then open http://127.0.0.1:8091
```

- Lists camera JSONs from the config dir (default `C:\Users\sudha\edge_worker`, read only, `--config-dir` to change) and from `out/`.
- Every JSON key becomes an editable field (lists like resolution as `1920, 1080`), on one page, with a short
  note on what it does; keys edge_worker's `multy.py` does not read are marked. Keys missing from a file
  (e.g. `motion.exclude_zones`) are added from the template.
- Motion exclude zones and the ROI can be drawn on the camera picture (red = exclude, blue = ROI). **Save to out/** writes the file;
  **Run test** saves, runs `--test` on those values and shows the log, streams and issues; the JSON is reloaded with the measured values.
- PTZ pad: hold a button or arrow key to move, release to stop (server stops any move after 4 s without a refresh).
  Zoom, focus, speed, preset Go / Save here, autofocus, lens init, read position. Hidden when `ptz_protocol` is `none`.
- Picture: one frame from the main or sub stream, refreshed after moves.
- Listens on 127.0.0.1 only. Not yet: changing encoder settings on the camera itself.

## Usage (command line)

Run from the folder holding the camera JSONs (e.g. `C:\Users\sudha\edge_worker`):

```
python C:\Users\sudha\camtest\test_protocol.py gate --test
```
