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
  stream paths (or Dahua `channel` + `main_stream_subtype`/`sub_stream_subtype`), `flip_stream`,
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

## The page (Camera config and testing)

- **Header:** Back · title · camera picker (saved cameras by camera ID) · IP box (starts `192.168.`) +
  **Identify** (default logins first; login boxes appear only when none works) · **Run test** ·
  **Save to camera and edge**.
- **One page of panels:** Camera, Connection, Sensor, Encoder, Motion, Face, OSD and **PTZ · POI**
  (status: PTZ protocol, last position, the points of interest; **Open view · PTZ · POI**) fit one
  screen. Agent fields are blue (not explained on the page). **Back** on the view returns here.
- **Panels** per category fill the page. **Camera ID** = `<site>_<camera>` as typed (e.g. `HUB_Gate`; capitals kept, spaces -> `_`): the
  file name (`hub_gate.json`, overwritten on save) and the OSD title.
- **Run test** includes the recording check (10 s recorded the way the edge does, no box). Progress
  scrolls by in the **status bar** at the bottom; failures and issues stay there in red. The result
  opens in its own **floating window** and is kept as `out\hub_gate.txt` (overwritten by each test).
- **Save to camera and edge:** `<id>.json` into **central's base** (`central_storage.mount_path` in
  central's `pull.json`) and **the site's edge base** (`edge_nodes[].base_path`), overwritten; the only two
  copies (`out\` keeps the test results). Another camera JSON with the **same camera IP** in either place
  is deleted and its camera name written to `"replaces"` (central then drops the old camera; its events
  keep the name); the bottom bar says so when the camera is opened. Then the OSD and the Sensor / Encoder
  values go to the camera. Every saved JSON says where both copies are:
  `"storage": {"central_base": <central's folder>, "edge_base": <the edge's folder>}` (as in pull.json).
  After saving, **central is told** to load that file (NOTIFY `aisys_cameras`)
  and **the edge is asked** to read its camera files (`cameras.req` in its base; multy.py answers in
  `cameras.ack`, shown in the bottom bar; Save waits up to 10 s). The camera picker lists central's base.
- **Identify and its login:** when no login is typed, the login of an existing camera JSON with the same
  IP is tried first (central's base, the edge bases, `out\`, `..\edge_worker`, e.g. livingroom.json), then
  the default logins (3 tries in all). Cameras whose ONVIF rejects every login but answers without one
  (cheap NVT / APCam cameras on port 8899) have their logins checked on the video stream instead
  (the RTSP address from ONVIF), so the right login is accepted.
- **Points of interest** are stored with their PTZ position, read from the camera at Add / Set:
  `"01": {"name": "Door", "pan": .., "tilt": .., "zoom": ..}` in `camera.ptz_markers`; `"presets"` keeps
  the names. Central fills its `camera_poi` table from them.
- **OSD panel:** show_date, date_format, date_position, show_name, name_text (= camera ID, not
  editable), name_position, **use_camera_positions** (ticked: the camera's own positions, read from
  it and shown greyed; unticked: the two position boxes are sent).
- **View page:** live picture (fresh frame every 5 s), zones / ROI under it; side panel: **OSD**
  (use the camera's default positions, show OSD positions on the picture, **Place date / Place
  title** by clicking the picture, Read OSD, Apply OSD, OSD off), **PTZ** (pad, Zoom + / −, Focus
  near / far, Autofocus, Lens init, speed, and the position as numbers: Pan, Tilt, Zoom, Focus,
  read after every stop, POI Go and on opening; ↻ reads it now) and **Points of interest**
  (01, 02 … automatic; Go / Set / Add act at once, no confirmation). **POI numbers are never
  reused:** the highest number given is kept as `poi_last` in the camera JSON, so after deleting
  02 the next one is still 05, not 02. The picture is at most 640 px wide and shrinks so the page
  fits the screen height.
- **Panels page:** four columns (Camera · Sensor + Encoder · Motion + Connection · Face + OSD + PTZ · POI)
  that all end on the same bottom line; ptz_markers in a bigger box; Face: enabled alone, min width and
  height side by side. **OSD panel:** the ticks in the left column, each with its fields (show_date ->
  date_format, date_position; show_name -> name_text, name_position; use_camera_positions;
  other_texts_off; own_overlay -> overlay_date_position, overlay_name_position, overlay_text_size).
- **Own OSD text** (`osd.own_overlay`, off by default): our own date and title, plain white text,
  no boxes, with its own ticks for the date (`own_date`) and the title (`own_title`), own positions
  (default bottom left / bottom middle) and size (`overlay_text_size`, share of
  the picture height). Independent of the camera's OSD (both may show; a camera that enforces its OSD
  ignores the camera ticks). On the view: tick "own OSD text"; Place date / Place title move ours while
  it is on, else the camera's (sent by Apply OSD). Shown on the page now; the edge burning it into
  snapshots and clips comes with the edge change-over.
- **View row:** sub stream, Refresh, **live** (ticked: a frame every 5 s and one after each move;
  unticked: after moves and Refresh only), Exclude zone, ROI, Clear zones, Clear ROI.
- **Camera settings (`camset.py`):** Save writes the Sensor and Encoder values into the camera, each on
  its own (Dahua CGI: only the keys the camera lists, all its profiles; ONVIF: Imaging scaled to the
  camera's ranges, Media encoder: main = first profile, sub = second). Values the camera already has are
  not rewritten; values it has no setting for are listed as "not adjustable"; refused values and values
  the camera changed (read back) are alarms. Shutter and gain are the manual-exposure values (exposure
  mode left as it is, reported). After a resolution / fps / bitrate change: "the camera restarts its
  stream, the edge reconnects".
- **Messages:** none in the panels; every message goes to the **bottom bar** with its time.
  ◀ ▶ step through the history; the alarm count shows only the alarms (click), **clear alarms**
  removes them.
- Removed from the page: the left column (Cameras card, Find cameras, New camera), tabs, file box,
  copy-test box. `discover.py` and `/api/discover` remain for the command line.

## On-screen display (OSD)

`osd.py`: title = camera ID; date format; positions kept as the camera has them while
use_camera_positions is ticked. Dahua: ChannelTitle[ch].Name, Locales.TimeFormat, VideoWidget: every
on/off flag the camera lists for TimeTitle / ChannelTitle (EncodeBlend, EncodeBlendExtra1…, EncodeBlendSnapshot,
PreviewBlend: one per stream), Rect only for own positions; the flags found are logged. ONVIF: GetOSDs / SetOSD /
CreateOSD / DeleteOSD (hidden = deleted), each OSD keeps its own position element; a camera that
refuses `dd-MM-yyyy` gets `dd/MM/yyyy` (shown in red in the status bar). Tested on simulated cameras.

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

## Camera catalog and logins (2026-11-11)

- `camera_catalog.json` (in git): known makers / models with their factory logins, PTZ protocol, whether the
  PTZ position can be read (`ptz_position`), RTSP paths and a note. Identify asks the camera its maker /
  model without a login (ONVIF GetDeviceInformation, many allow it) and tries that entry's logins first.
  Add each new model here when it arrives.
- `default_logins.json` (not in git): the plant's own logins, tried before everything else.
- Order: default_logins.json, the matching catalog entry, the built-in list, the other catalog entries; at
  most `max_login_tries` (3) in all (cameras lock after a few wrong passwords).
- **The login box is shown whenever identify cannot work**: no default login worked, the camera answers neither
  ONVIF nor Dahua CGI and no login was typed, the login is accepted but no video stream opens or ONVIF refuses
  it, or identify fails for any other reason.
- A model with `ptz_position: false` (e.g. YM800S5: shows P/T/Z on its OSD, reports 0,0,0 over ONVIF) gets
  `camera.ptz_position: false` in its JSON and a note; its events are named P00T00Z00.

## Orientation, motion while configuring, events (decisions 2026-11-11)

- **Flip:** set on the camera itself when it can: **flip@camera** button on the PTZ page (camflip.py; sets the
  camera's own flip at once, Dahua VideoInOptions Flip / ONVIF VideoSourceConfiguration Rotate; no Save). `camera.flip_stream` (page 1 field only, unrelated to flip@camera;
  old name `invert_stream`, still read) only when the camera cannot; used sparingly, never together with the
  camera's own Flip. Then multy.py flips snapshots, motion and live, clips get a 180° tag, the camera's OSD is
  switched off and the edge writes our date / title (unticking switches the camera's OSD back on). The view
  in the tool always shows the picture as the camera sends it (red notice "flip_stream is ON"). How a camera
  is mounted is irrelevant to the software; the installer decides. The edge never mirrors.
- **Events:** an event starts above `motion_threshold_min`; once running it stays alive while the biggest
  object is above `keep_alive_ratio` (default 0.25) of it, so a person walking away and coming back is one
  event. `post_roll_buffer_sec` ends it, then `cooldown_period_sec` blocks new events: keep the cooldown
  short (a few seconds), set per camera.
- **Backlog:** lock events (no motion detection on the edge) for a camera without a PTZ position reading
  while its configuration file is open in the tool, until the editing is finished (moving it, or switching
  its OSD, made false events on FLP Gallery).
- **Done 2026-11-11:** one term, "flip", on the page, in the manual and in the JSON (`flip_stream`; the old
  `invert_stream` is still read and rewritten as `flip_stream` on Save). The edge only flips, never mirrors.
