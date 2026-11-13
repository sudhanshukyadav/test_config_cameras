# Camera JSON: configuration manual

One file per camera, `<site>_<camera>.json`, made on central with the Cameras page (camera tool,
`web_ui.py`; local site `/cameras`, admin). **Save to camera and edge** writes it into central's base
(`central_storage.mount_path`) and the edge's base (`base_path` in pull.json) - the only two copies - then
asks the edge to read it (`cameras.req` / `cameras.ack`) and writes the OSD and the sensor / encoder values
into the camera (where the camera allows it; some cameras accept no settings).

On Save the tool also:
- fills keys an older JSON does not have with the defaults below (nothing already set is changed);
- **auto-corrects** values over the plant's stream limit (pull.json `settings.stream_limit`, default
  1920x1080, 15 fps, 2048 kbps) before they reach the edge and the camera, when "Auto-correct wrong values on
  Save" is on (O&M page, Website tab; on when never set). Off: saved as entered and reported. Each
  correction is listed on the O&M page ("Last corrections", `stream_autofix.json`);
- replaces another JSON with the same camera IP (deleted in both places, named in `replaces`);
- writes `storage` (`central_base`, `edge_base`) and `stream_limit`.

The camera's resolution is what the camera sends: the tool measures it (Run test), it cannot change it on
cameras that take no settings. Recording is **H.264 stream copy only**; nothing is converted.

Values marked *(not read by the edge)* are read by central's agents (face agent, plate agent, pass 2), not by the edge.
Defaults = what the tool writes into a new JSON.


## camera: the device

| Key | Default | Meaning |
|---|---|---|
| `name` | `""` | camera name in logs and folders |
| `site` | `""` | site (location) of the camera, e.g. HUB: camera ID = site_camera |
| `vendor` | `"generic"` | dahua = Dahua RTSP/CGI paths; generic = stream paths below |
| `ptz_protocol` | `"none"` | dahua_cgi, onvif_soap or none |
| `host` | `""` | camera IP address |
| `port` | `554` | RTSP port (usually 554) |
| `http_port` | `80` | web / CGI / ONVIF port (Dahua 80, cheap cameras often 8899) |
| `username` | `"admin"` | camera login |
| `password` | `""` | camera login |
| `flip_stream` | `false` | flip (done by the edge): only if the camera cannot flip its picture itself. Use sparingly; never together with the camera's own Flip |
| `ptz_markers` | `{}` | points of interest: number, name and PTZ position (set them on the view page) |
| `ptz_home` | POI 01 if there is one, else 0 | home POI: after a power / cable failure the edge sends the camera back there (0 = none; house button on the view page) |
| `main_stream_path` | - | RTSP path of the recorded stream, e.g. /live/ch0 |
| `sub_stream_path` | - | RTSP path used for motion detection |
| `main_rtsp_url` | - | full RTSP URL; overrides the path |
| `sub_rtsp_url` | - | full RTSP URL; overrides the path |
| `channel` | - | Dahua channel (1 for a single camera) |
| `main_stream_subtype` | - | Dahua main stream (0) |
| `sub_stream_subtype` | - | Dahua sub stream (1) |

## connection

| Key | Default | Meaning |
|---|---|---|
| `rtsp_transport` | `"tcp"` | tcp is reliable; udp can lose packets |
| `buffer_size` | `1` | *(not read by the edge)* |
| `reconnect_max_sec` | `60` | longest wait between two attempts to open the stream after it was lost |
| `probe_sec` | `10` | while the camera is down, its RTSP port is checked this often; the moment it answers again (power / cable back) the stream is opened at once |

## sensor_hardware_properties: picture settings written into the camera

Written into the camera by Save, as far as the camera allows. *(not read by the edge)*

| Key | Default | Meaning |
|---|---|---|
| `brightness` | `50` | brightness |
| `contrast` | `50` | contrast |
| `saturation` | `50` | saturation |
| `sharpness` | `40` | sharpness |
| `shutter_speed_sec` | `"1/100"` | shutter speed sec |
| `gain` | `12` | gain |
| `wdr_enabled` | `true` | wdr enabled |
| `wdr_level` | `3` | wdr level |
| `noise_reduction_3d` | `50` | noise reduction 3d |
| `day_night_mode` | `"auto"` | day night mode |
| `white_balance_mode` | `"auto"` | white balance mode |

## stream_encoder_properties

| Key | Default | Meaning |
|---|---|---|
| `codec` | `"H.264"` | must be H.264; the edge only copies the stream |
| `smart_codec` | `false` | keep off (H.264+ / smart codec breaks copy) *(not read by the edge)* |
| `sub_stream_resolution` | `[640, 480]` | width, height of the sub stream (measured) |
| `main_stream_resolution` | `[1920, 1080]` | width, height of the main stream (measured) |
| `fps` | `20` | frames per second the camera sends (Run test measures it) |
| `gop_keyframe_interval` | `20` | frames between keyframes; best = fps (1 s) *(not read by the edge)* |
| `bitrate_type` | `"CBR"` | *(not read by the edge)* |
| `bitrate_kbps` | `2048` | *(not read by the edge)* |

## motion: the trigger (edge, multy.py)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | false = the edge does not start this camera |
| `heartbeat_interval_sec` | `0.0` | seconds without an event snap before the edge checks the camera still sends a live picture (dead / frozen / blind opens a maintenance ticket, O&M page, OOS tab). 0 = no check; gates 60, rooms 900 |
| `grid_rows` | `6` | *(not read by the edge)* |
| `grid_cols` | `8` | *(not read by the edge)* |
| `motion_threshold_min` | `1800` | size of the largest moving blob, in main-frame pixels, to count as motion (bigger = less sensitive) |
| `max_lighting_ratio` | `0.6` | ignore when more than this share of the frame changes (lights, IR switch), 0–1 |
| `initial_snap_delay_sec` | `1.0` | wait this long after motion starts before the first snapshot |
| `diff_multiplier_threshold` | `10.0` | *(not read by the edge)* |
| `post_roll_buffer_sec` | `5.0` | keep recording this long after motion stops |
| `pre_roll_sec` | `0.0` | seconds recorded BEFORE motion starts (held in the edge's RAM). Entry / exit cameras: 3. 0 = off: the main stream is opened only on motion, so the clip starts 1–4 s late |
| `cooldown_period_sec` | `10.0` | after an event, ignore new motion for this long |
| `min_motion_sec` | `0.6` | an object must be followed this long before it triggers (seconds) |
| `min_travel` | `0.03` | and its centre must move this share of the picture diagonal (0.03 = 3 %); things that stay put are ignored |
| `min_clip_sec` | `10.0` | every clip lasts at least this long after the trigger |
| `min_texture` | `20.0` | light is not an object: the object must bring its own pattern (people 40–130, light up to ~11). 0 = off |
| `gray` | `true` | find motion on the black & white picture (less colour noise); off = colour |
| `keep_alive_ratio` | `0.25` | a running event stays alive while the object is at least this share of the threshold |
| `bg_scan_frame_step` | `2` | *(not read by the edge)* |
| `draw_motion_box` | `true` | draw a box around the motion on snapshots |
| `exclude_zones` | `[]` | areas to ignore: [[x1, y1, x2, y2], …] as 0–1 fractions. Draw them on the picture → |
| `use_roi` | `false` | only watch the roi_crop area |
| `roi_crop` | `[0.2, 0.2, 0.8, 0.8]` | area to watch when use_roi: x1, y1, x2, y2 as 0–1 fractions. Draw it on the picture → |
| `min_sharpness` | `100` | *(not read by the edge)* |
| `min_brightness` | `50` | *(not read by the edge)* |
| `max_brightness` | `220` | *(not read by the edge)* |

## face_quality (face agent)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | faces of this camera are used (face agent) |
| `min_face_width` | `120` | smallest face kept, width in clip pixels (face agent) |
| `min_face_height` | `120` | smallest face kept, height in clip pixels (face agent) |

## osd: on-screen text

| Key | Default | Meaning |
|---|---|---|
| `show_date` | `true` | date and time on the picture |
| `date_format` | `"dd-MM-yyyy HH:mm:ss"` | date / time format on the picture; default dd-MM-yyyy HH:mm:ss (some cameras only take their own formats) |
| `date_position` | `[0.02, 0.03]` | top-left corner of the date: x, y as share of the picture (0, 0 = top left) |
| `show_name` | `true` | title (camera ID) on the picture |
| `name_position` | `[0.5, 0.03]` | top-middle point of the title: x, y (0.5 = centred) |
| `use_camera_positions` | `true` | ticked: keep the positions the camera has (shown after reading it); unticked: the two position boxes are sent |
| `other_texts_off` | `false` | switch off the camera's other default texts (extra titles, logo) where the camera allows it |
| `own_overlay` | `false` | our own date and title in white (not the camera's); off by default. For cameras without their own OSD |
| `own_date` | `true` | our date shown (while own_overlay is on) |
| `own_title` | `true` | our title (camera ID) shown (while own_overlay is on) |
| `overlay_date_position` | `[0.02, 0.92]` | our date: top-left corner, x, y as share of the picture (Place date while own OSD text is ticked) |
| `overlay_name_position` | `[0.5, 0.92]` | our title: top-middle point, x, y (Place title while own OSD text is ticked) |
| `overlay_text_size` | `0.04` | height of our text as share of the picture height (0.04 = 4 %) |

## Top level (written by Save)

| Key | Meaning |
|---|---|
| `storage` | `central_base`, `edge_base`: where the two copies live |
| `stream_limit` | the plant's stream limit (pull.json `settings.stream_limit`); the edge checks the camera against it and reports a camera over it (O&M page, Edges) |
| `stream_limit_override` | this camera's own limit (O&M page, Website tab), e.g. a camera whose pictures need the detail; Save raises the camera up to it |
| `replaces` | camera names this file replaced (same camera IP); central drops them |

### How the trigger works (motion, decided 2026-11-12)

1. MOG2 background subtraction on the sub stream, grey (`gray`), with `exclude_zones` masked (the camera's
   clock, a TV) and only `roi_crop` watched when `use_roi`.
2. The largest moving blob must exceed `motion_threshold_min` (main-frame pixels); a change of more than
   `max_lighting_ratio` of the frame is light (or the camera moving), not motion.
3. It must be **one object** followed for `min_motion_sec`, whose centre travels `min_travel` of the diagonal,
   and which brings its own **pattern** (`min_texture`): light only scales the brightness (up to ~11 measured),
   people bring 40-130.
4. Then the event runs: first snapshot after `initial_snap_delay_sec`, clip of at least `min_clip_sec`
   (plus `pre_roll_sec` before), ending `post_roll_buffer_sec` after the last motion, kept alive while the
   object is above `keep_alive_ratio` of the threshold; then `cooldown_period_sec`.
5. `heartbeat_interval_sec` (0 = off; gates 60, rooms 900): no event snap for that long -> the edge checks
   the camera still sends a live, changing picture (alive / dead / frozen / blind); a bad result opens a
   maintenance ticket (O&M page, OOS tab).

To see why an event triggered: O&M page, Edges, **Test mode** (grid, object, path and the numbers on the
snapshot); switch it off afterwards.

### Camera out of service

`motion.enabled: false` = the edge does not start the camera, central does not watch it, its open tickets
close ("camera switched off"). Taking a camera out of service on purpose, with a remark: O&M page, OOS tab.

