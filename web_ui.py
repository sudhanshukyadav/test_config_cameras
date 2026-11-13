#!/usr/bin/env python3
"""Local web page for test_protocol.py: edit camera JSON, run the camera test, drive PTZ.

    python web_ui.py                      # configs from C:\\Users\\sudha\\edge_worker, page on :8091
    python web_ui.py --config-dir D:\\cams --port 8091

Only listens on 127.0.0.1. Never writes into the config dir; saves go to out/.
Also reached through the AiSys local site at http://localhost:8080/cameras (admin, central PC only):
the page then calls /cameras/api/... and the local site passes them on to this server.
"""
import argparse
import contextlib
import ipaddress
import io
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import camset
import discover as dc
import camflip
import osd
import test_protocol as tp

WEB_DIR = os.path.join(tp.SCRIPT_DIR, "web")
CONFIG_DIR = r"C:\Users\sudha\edge_worker"

_test_lock = threading.Lock()      # one camera test at a time (stdout is captured globally)
_drivers = {}                      # (host, http_port, user, password, protocol) -> driver
_drivers_lock = threading.Lock()
_watchdogs = {}                    # driver id -> Timer that stops a move nobody stopped
_snap_locks = {}                   # camera host -> Lock (one picture stream per camera at a time)


# ---- where the camera JSONs live: central's base (made here) and the site's edge base ----------
STREAM_LIMIT_DEFAULT = {"max_width": 1920, "max_height": 1080, "max_fps": 15, "max_kbps": 2048}
STD_RES = [(3840, 2160), (2560, 1440), (2304, 1296), (1920, 1080), (1600, 900), (1280, 720), (1024, 576), (960, 540),
           (2592, 1944), (2048, 1536), (1600, 1200), (1280, 960), (1024, 768), (800, 600), (704, 576), (640, 480)]


def stream_limit(pc: dict | None = None) -> dict | None:
    """The plant's limit for every camera's MAIN stream (pull.json settings.stream_limit; defaults
    1920x1080, 15 fps, 2048 kbps). "off" / false = no limit (high-bandwidth network released)."""
    raw = ((pc if pc is not None else pull_config()).get("settings") or {}).get("stream_limit", {})
    if raw in (False, "off", "none", None):
        return None
    lim = dict(STREAM_LIMIT_DEFAULT)
    if isinstance(raw, dict):
        for k in lim:
            try:
                if raw.get(k):
                    lim[k] = int(raw[k])
            except (TypeError, ValueError):
                pass
    return lim


def _fit_resolution(w: int, h: int, lim: dict) -> tuple[int, int]:
    """The biggest usual resolution of the same shape within the limit (cameras only take their own list)."""
    ok = [(a, b) for a, b in STD_RES if a <= lim["max_width"] and b <= lim["max_height"] and abs(a / b - w / h) < 0.06]
    if ok:
        return max(ok, key=lambda r: r[0] * r[1])
    f = min(lim["max_width"] / w, lim["max_height"] / h)
    return int(w * f) // 16 * 16, int(h * f) // 16 * 16


def effective_limit(data: dict, plant: dict | None) -> tuple[dict | None, bool]:
    """(limit for this camera, own?): the camera file's stream_limit_override (set on the O&M page,
    Website tab, for a camera whose pictures need the detail), else the plant's limit."""
    o = data.get("stream_limit_override")
    if isinstance(o, dict):
        lim = dict(plant or STREAM_LIMIT_DEFAULT)
        for k in STREAM_LIMIT_DEFAULT:
            try:
                if o.get(k):
                    lim[k] = int(o[k])
            except (TypeError, ValueError):
                pass
        return lim, True
    return plant, False


def apply_stream_limit(data: dict, lim: dict | None, raise_to_limit: bool = False) -> list[str]:
    """Caps the main stream in the camera JSON to the limit (written into the camera by Save); returns what
    was changed. The limit itself goes into the JSON (stream_limit) so the edge can check the camera.
    raise_to_limit (a camera with its own, higher limit): the resolution goes UP to the biggest usual one of
    its shape within the limit, and the bitrate to the limit."""
    if not lim:
        data.pop("stream_limit", None)
        return []
    enc = data.setdefault("stream_encoder_properties", {})
    notes = []
    if raise_to_limit:
        try:
            w, h = (int(v) for v in enc.get("main_stream_resolution") or (16, 9))
        except (TypeError, ValueError):
            w, h = 16, 9
        nw, nh = _fit_resolution(w, h, lim)
        if [nw, nh] != list(enc.get("main_stream_resolution") or []):
            notes.append(f"main resolution {enc.get('main_stream_resolution')} -> {nw}x{nh} (own limit)")
            enc["main_stream_resolution"] = [nw, nh]
        if int(enc.get("bitrate_kbps") or 0) != lim["max_kbps"]:
            notes.append(f"bitrate {enc.get('bitrate_kbps') or '?'} -> {lim['max_kbps']} kbps (own limit)")
            enc["bitrate_kbps"] = lim["max_kbps"]
    res = enc.get("main_stream_resolution")
    try:
        w, h = int(res[0]), int(res[1])
    except (TypeError, ValueError, IndexError):
        w = h = 0
    if w and h and (w > lim["max_width"] or h > lim["max_height"]):
        nw, nh = _fit_resolution(w, h, lim)
        enc["main_stream_resolution"] = [nw, nh]
        notes.append(f"main resolution {w}x{h} -> {nw}x{nh}")
    try:
        fps = float(enc.get("fps") or 0)
    except (TypeError, ValueError):
        fps = 0
    if fps > lim["max_fps"]:
        enc["fps"] = lim["max_fps"]
        notes.append(f"fps {fps:g} -> {lim['max_fps']}")
        try:
            if int(enc.get("gop_keyframe_interval") or 0) > lim["max_fps"]:
                enc["gop_keyframe_interval"] = lim["max_fps"]          # a keyframe every second
                notes.append(f"GOP -> {lim['max_fps']} (1 s)")
        except (TypeError, ValueError):
            pass
    try:
        kb = int(enc.get("bitrate_kbps") or 0)
    except (TypeError, ValueError):
        kb = 0
    if not kb or kb > lim["max_kbps"]:
        enc["bitrate_kbps"] = lim["max_kbps"]
        notes.append(f"bitrate {kb or '?'} -> {lim['max_kbps']} kbps")
    data["stream_limit"] = dict(lim)
    return notes


def pull_path() -> str | None:
    """Where the AiSys central pull.json is (AISYS_CONFIG_DIR, or next to this repo)."""
    for p in (os.path.join(os.environ.get("AISYS_CONFIG_DIR", ""), "pull.json"),
              os.path.join(tp.SCRIPT_DIR, "..", "aivision", "site", "pull.json")):
        if p and os.path.isfile(p):
            return p
    return None


def pull_config() -> dict:
    """The AiSys central pull.json (next to this repo, or in AISYS_CONFIG_DIR)."""
    p = pull_path()
    if p:
        try:
            with open(p, encoding="utf-8-sig") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def autocorrect_on() -> bool:
    """O&M page, Website tab, "Auto-correct wrong values on Save" (site_settings.json next to pull.json;
    on when never set)."""
    p = pull_path()
    try:
        with open(os.path.join(os.path.dirname(p), "site_settings.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("stream_autofix", True))
    except (TypeError, OSError, ValueError):
        return True


def note_correction(camera: str, found: str, result: str, ok: bool) -> None:
    """The O&M page's "Last corrections" (stream_autofix.json next to pull.json, the last 50)."""
    p = pull_path()
    if not p:
        return
    path = os.path.join(os.path.dirname(p), "stream_autofix.json")
    try:
        with open(path, encoding="utf-8") as f:
            log = json.load(f)
    except (OSError, ValueError):
        log = []
    log.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "camera": camera, "found": found, "result": result, "ok": ok})
    with contextlib.suppress(OSError):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log[-50:], f, indent=1)


def local_path(raw: str) -> str:
    """A configured share path as this machine opens it (AISYS_PATH_MAP "raw=local;..." for tests)."""
    for pair in filter(None, os.environ.get("AISYS_PATH_MAP", "").split(";")):
        k, _, v = pair.partition("=")
        if k and raw.lower().startswith(k.lower()):
            return v + raw[len(k):].replace("\\", "/")
    return raw


def central_base():
    raw = ((pull_config().get("central_storage") or {}).get("mount_path") or "").strip()
    return local_path(raw) if raw else None


def edge_shares() -> dict:
    """site (upper case) -> the edge's base folder, from pull.json."""
    return {str(n.get("site_name", "")).upper(): local_path(n.get("base_path"))
            for n in pull_config().get("edge_nodes", []) if n.get("site_name") and n.get("base_path")}


def _camera_jsons(folder):
    """[(file name, JSON)] of the camera JSONs (with a camera section) in a folder."""
    out = []
    for fn in sorted(os.listdir(folder)):
        if not fn.lower().endswith(".json") or fn.endswith(".report.json"):
            continue
        try:
            with open(os.path.join(folder, fn), encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and isinstance(data.get("camera"), dict):
            out.append((fn, data))
    return out


def list_configs():
    """Camera JSONs in central's base; older ones still in out/ are listed too (Save moves them)."""
    items, seen = [], set()
    for where, folder in (("central", central_base()), ("out", tp.OUT_DIR)):
        if not folder or not os.path.isdir(folder):
            continue
        for fn, data in _camera_jsons(folder):
            if fn.lower() in seen:
                continue
            seen.add(fn.lower())
            cam = data["camera"]
            items.append({"where": where, "file": fn, "id": fn[:-5], "name": cam.get("name", fn[:-5]),
                          "host": cam.get("host"), "ptz": cam.get("ptz_protocol", "")})
    return items


def config_path(where, fn):
    folder = central_base() if where == "central" else CONFIG_DIR if where == "source" else tp.OUT_DIR
    if not folder:
        raise ValueError("central base unknown (central_storage.mount_path in pull.json)")
    fn = os.path.basename(fn)               # no paths from the page
    if not fn.endswith(".json"):
        raise ValueError("not a .json file")
    return os.path.join(folder, fn)


def save_out(fn, data):
    """The test's working copy (out/); removed again after the test."""
    if not isinstance(data, dict) or not isinstance(data.get("camera"), dict):
        raise ValueError("JSON needs a camera section")
    os.makedirs(tp.OUT_DIR, exist_ok=True)
    path = config_path("out", fn)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    return path


COPY_SECONDS = 10        # recording check inside Run test: this many seconds recorded like the edge does
_progress = {"lines": [], "running": False, "cam": {}}


class _Progress(io.StringIO):
    """Captures the test's output and keeps finished lines for /api/test/progress."""
    def __init__(self):
        super().__init__()
        self._part = ""

    def write(self, text):
        self._part += text
        while "\n" in self._part:
            line, self._part = self._part.split("\n", 1)
            if line.strip():
                _progress["lines"].append(line)
        return super().write(text)


def camera_file(data) -> str:
    """<site>_<camera>.json: the file name always follows from camera.site and camera.name."""
    return osd.camera_id(data) + ".json"


def run_test(data, copy_seconds=COPY_SECONDS):
    """Saves the page's JSON to out/, tests the camera from it, returns log + new JSON + result text.
    The result is kept as out/<site>_<camera>.txt (overwritten by each test)."""
    fn = camera_file(data)
    path = save_out(fn, data)
    buf = _Progress()
    with _test_lock:
        _progress.update(lines=[], running=True, cam=tp.standardize_config(data["camera"]))
        tp.LAST_REPORT = None
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                try:
                    code = tp.run_camera_test(path, int(copy_seconds))
                except SystemExit as e:
                    code = e.code
                except Exception as e:  # keep the server up, show the error on the page
                    print(f"ERROR {type(e).__name__}: {e}")
                    code = 1
        finally:
            _progress["running"] = False
    stem = os.path.splitext(fn)[0]
    out = {"code": code, "file": fn, "log": tp.mask(buf.getvalue(), _progress["cam"]), "report": tp.LAST_REPORT}
    p = os.path.join(tp.OUT_DIR, fn)
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            out["json"] = json.load(f)
        with contextlib.suppress(OSError):
            os.remove(p)                    # the JSON lives in central's base (Save); out/ keeps the .txt
    t = os.path.join(tp.OUT_DIR, f"{stem}.txt")
    if os.path.isfile(t):
        with open(t, encoding="utf-8") as f:
            out["report_text"] = f.read()
    return out


def progress(since: int) -> dict:
    lines = _progress["lines"][since:]
    return {"lines": [tp.mask(x, _progress["cam"]) for x in lines], "next": since + len(lines),
            "running": _progress["running"]}


# ---- Save to camera and edge -----------------------------------------------
def same_ip(data) -> dict:
    """Other camera JSONs with this camera's IP in central's base and in the site's edge base:
    Save replaces them (they are deleted; central drops the old camera, its events keep the name)."""
    fn = camera_file(data)
    cam = data.get("camera") or {}
    host, site = str(cam.get("host") or "").strip(), str(cam.get("site") or "").strip()
    out = {"file": fn, "central": central_base(), "edge": edge_shares().get(site.upper()), "old": []}
    for where in ("central", "edge"):
        folder = out[where]
        if not host or not folder or not os.path.isdir(folder):
            continue
        for other, doc in _camera_jsons(folder):
            if other.lower() != fn.lower() and str(doc["camera"].get("host") or "").strip() == host:
                out["old"].append({"where": where, "file": other, "name": str(doc["camera"].get("name") or other[:-5])})
    return out


EDGE_ANSWER_SEC = 10    # how long Save waits for multy.py's answer (cameras.ack)
LOCK_SUFFIX = "_config.lock"   # <edge base>/<camera>_config.lock: this page has the camera open (multy.py: no events)


def config_lock(b: dict) -> dict:
    """While a camera is open on this page, the page asks every 20 s to keep <edge base>/<camera>_config.lock
    fresh; multy.py then makes no events for it (only cameras whose PTZ position cannot be read; it pauses on
    a position change otherwise). action "release": the lock goes (page closed, another camera opened); a lock
    not refreshed for 60 s is over anyway (decided 2026-11-11)."""
    site, name = str(b.get("site") or "").strip().upper(), str(b.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        return {"ok": False, "error": "camera name"}
    folder = edge_shares().get(site)
    if not folder or not os.path.isdir(folder):
        return {"ok": False, "error": f"no edge base for site {site or '?'}"}
    path = os.path.join(folder, name + LOCK_SUFFIX)
    if b.get("action") == "release":
        with contextlib.suppress(OSError):
            os.remove(path)
        return {"ok": True, "locked": False}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"by": "camera tool", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
    return {"ok": True, "locked": True}


def tell_central(fn: str) -> dict:
    """NOTIFY aisys_cameras <file>: central's pull service loads that one camera file now."""
    db = pull_config().get("database") or {}
    try:
        import psycopg2   # noqa: PLC0415  (on the central PC with the AiSys services)
    except ImportError:
        return {"ok": False, "step": "central not told (psycopg2 missing here); it loads the file at its next start"}
    try:
        conn = psycopg2.connect(host=db.get("host", "127.0.0.1"), port=db.get("port", 5432), dbname=db.get("dbname"),
                                user=db.get("user"), password=db.get("password"), connect_timeout=5,
                                application_name="aisys-camera-tool")
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_notify('aisys_cameras', %s)", (fn,))
        finally:
            conn.close()
        return {"ok": True, "step": f"central told to load {fn}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "step": f"central not told ({type(e).__name__}); it loads the file at its next start"}


def ask_edge(folder: str, fn: str) -> dict:
    """<edge base>/cameras.req: multy.py re-reads its camera files and answers in cameras.ack."""
    ticket = f"{int(time.time() * 1000)}-{os.getpid()}"
    req, ack = os.path.join(folder, "cameras.req"), os.path.join(folder, "cameras.ack")
    try:
        with open(req + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"ticket": ticket, "file": fn}, f)
        os.replace(req + ".tmp", req)
    except OSError as e:
        return {"ok": False, "step": f"could not ask the edge to read ({e})"}
    end = time.time() + EDGE_ANSWER_SEC
    while time.time() < end:
        time.sleep(0.5)
        try:
            with open(ack, encoding="utf-8") as f:
                a = json.load(f)
        except (OSError, ValueError):
            continue
        if a.get("ticket") != ticket:
            continue
        txt = f"edge read the camera files: running {', '.join(a.get('running') or []) or 'none'}"
        if a.get("off"):
            txt += f"; switched off {', '.join(a['off'])}"
        if a.get("notes"):
            txt += "; " + "; ".join(a["notes"])
        return {"ok": a.get("state") == "done" and not a.get("notes"), "step": txt}
    return {"ok": False, "step": f"the edge has not answered in {EDGE_ANSWER_SEC} s (multy.py not running, or an older "
                                 "multy.py); the request stays, it reads the files when it next starts"}


def save_all(data) -> dict:
    """Save to camera and edge: <site>_<camera>.json into central's base and the site's edge base
    (both overwritten; the only two copies). Another JSON with the same camera IP is replaced: deleted
    in both places, its camera name written to "replaces" so central drops the old camera. Then the OSD
    and the Sensor / Encoder values go to the camera."""
    steps = []
    fn = camera_file(data)
    chk = same_ip(data)
    host = (data.get("camera") or {}).get("host")
    me = str((data.get("camera") or {}).get("name") or "").strip()
    old_names = sorted({o["name"] for o in chk["old"]} - {me})
    if old_names:
        data = dict(data)
        data["replaces"] = sorted(set(data.get("replaces") or []) | set(old_names))
    if not chk["central"]:
        return {"file": fn, "steps": [{"ok": False, "step": "central base unknown (central_storage.mount_path in pull.json)"}]}
    # where both copies live (as pull.json names them), so the edge knows central's copy too
    pc = pull_config()
    site_u = str((data.get("camera") or {}).get("site") or "").strip().upper()
    edge_raw = next((n.get("base_path") for n in pc.get("edge_nodes", []) if str(n.get("site_name", "")).upper() == site_u), None)
    data = dict(data)
    cam_d = data["camera"] = dict(data.get("camera") or {})
    if "invert_stream" in cam_d:                     # old name: written as flip_stream from now on
        cam_d.setdefault("flip_stream", bool(cam_d.get("invert_stream")))
        cam_d.pop("invert_stream", None)
    data["storage"] = {"central_base": (pc.get("central_storage") or {}).get("mount_path"), "edge_base": edge_raw}
    lim, own = effective_limit(data, stream_limit(pc))
    who = f"{site_u}/{me}"
    if own or autocorrect_on():          # own limit: set on the O&M page on purpose, always applied
        capped = apply_stream_limit(data, lim, raise_to_limit=own)
        if capped:
            steps.append({"ok": True, "step": f"stream limit{' (own, this camera)' if own else ''} ({lim['max_width']}x{lim['max_height']}, "
                                              f"{lim['max_fps']} fps, {lim['max_kbps']} kbps): " + "; ".join(capped)})
            if not own:
                note_correction(who, "entered over the limit", "; ".join(capped), True)
    else:                                # auto-correct off: saved as entered, reported
        trial = json.loads(json.dumps(data))
        over = apply_stream_limit(trial, lim)
        if lim:
            data["stream_limit"] = dict(lim)         # the edge still checks the camera against it
        if over:
            steps.append({"ok": False, "step": f"over the stream limit, NOT corrected (auto-correct is off on the O&M page, "
                                               f"Website tab): would be " + "; ".join(over)})
            note_correction(who, "entered over the limit", "not corrected (auto-correct off)", False)
    for where, folder in (("central", chk["central"]), ("edge", chk["edge"])):
        if not folder:
            steps.append({"ok": False, "step": f"no edge base known for site {(data.get('camera') or {}).get('site') or '?'} (pull.json)"})
            continue
        try:
            for other in os.listdir(folder):     # the same file in other capitals (hub_bedroom.json): renamed
                if other.lower() == fn.lower() and other != fn:
                    os.remove(os.path.join(folder, other))
            with open(os.path.join(folder, fn), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
            steps.append({"ok": True, "step": f"saved {fn} on {where}: {folder}"})
        except OSError as e:
            steps.append({"ok": False, "step": f"save on {where} failed ({folder}): {type(e).__name__} "
                                                f"[WinError {getattr(e, 'winerror', None)}, errno {e.errno}] {e}"})
            continue
        for o in chk["old"]:
            if o["where"] == where:
                try:
                    os.remove(os.path.join(folder, o["file"]))
                    steps.append({"ok": True, "step": f"{o['file']} deleted on {where} (same camera IP {host}; replaced by {fn})"})
                except OSError as e:
                    steps.append({"ok": False, "step": f"could not delete {o['file']} on {where}: {e}"})
    with contextlib.suppress(OSError):
        os.remove(os.path.join(tp.OUT_DIR, fn))          # an older copy in out/: central's base has it now
    if any(s["ok"] and s["step"].startswith(f"saved {fn} on central") for s in steps):
        steps.append(tell_central(fn))                   # central loads this one file
    if chk["edge"] and any(s["ok"] and s["step"].startswith(f"saved {fn} on edge") for s in steps):
        steps.append(ask_edge(chk["edge"], fn))          # multy.py reads its camera files now
    if host:
        r = osd.handle(data, "apply")
        steps.append({"ok": r["ok"], "step": f"camera OSD: title {r['camera_id']} via {r['protocol']}"
                      + ("" if r["ok"] else f" failed ({'; '.join(r['log'][-2:])})"), "osd": r.get("osd")})
        steps += camset.lines(camset.write(data))
    for st in steps:                         # every Save step in the service log too (errors included)
        tp.log(fn[:-5], f"save {'OK' if st['ok'] else 'FAIL'}: {st['step']}")
    return {"file": fn, "steps": steps, "json": data}


def get_driver(cam):
    cfg = tp.standardize_config(cam)
    if cfg["ptz_protocol"] in ("none", "off", "no"):
        raise ValueError(f"{cfg['name']} has ptz_protocol 'none'")
    key = (cfg["host"], cfg["http_port"], cfg["username"], cfg["password"], cfg["ptz_protocol"])
    with _drivers_lock:
        if key not in _drivers:
            _drivers[key] = tp.build_driver(cfg)
        return _drivers[key]


_status_logged = set()     # cameras whose position reply was already written to the log


ABS_ZOOM_SPACE = "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"


def absolute_move(drv, x, y, z=None) -> bool:
    """ONVIF AbsoluteMove: pan x and tilt y from -1 to 1, zoom z from 0 to 1 (generic spaces); z None = keep."""
    x, y = max(-1.0, min(1.0, float(x))), max(-1.0, min(1.0, float(y)))
    zoom = "" if z is None else f'<tt:Zoom x="{max(0.0, min(1.0, float(z)))}" space="{ABS_ZOOM_SPACE}"/>'
    body = (f"<tptz:AbsoluteMove><tptz:ProfileToken>{drv.profile_token}</tptz:ProfileToken><tptz:Position>"
            f'<tt:PanTilt x="{x}" y="{y}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"/>'
            f"{zoom}</tptz:Position></tptz:AbsoluteMove>")
    return drv._post_soap(body, timeout=4) is not None


def ptz(cam, action, speed=None, preset=None, token=None, pos=None):
    drv = get_driver(cam)
    name = drv.display_name
    if speed is not None:
        drv.speed = max(0.1, min(1.0, float(speed)))
    ok, extra = False, {}
    if action in ("left", "right", "up", "down", "zoom_in", "zoom_out", "focus_near", "focus_far"):
        ok = drv.set_ptz(action)
        # Safety: stop by itself after 4 s if the page never sends stop (tab closed, lost mouseup).
        old = _watchdogs.pop(id(drv), None)
        if old:
            old.cancel()
        t = threading.Timer(4.0, drv.stop)
        t.daemon = True
        _watchdogs[id(drv)] = t
        t.start()
    elif action == "stop":
        old = _watchdogs.pop(id(drv), None)
        if old:
            old.cancel()
        ok = drv.stop()
    elif action == "goto":
        ok = drv.goto_preset(token if token and isinstance(drv, tp.ONVIFSoapDriver) else int(preset))
        if ok:
            extra["status"], _ = drv.wait_until_idle()
    elif action == "set_preset":
        if isinstance(drv, tp.ONVIFSoapDriver):
            ok = drv.set_preset(token or int(preset))
            extra["token"] = drv.last_preset_token          # the camera may give its own preset token
            if not ok:
                tp.log(name, f"ptz set_preset refused; the camera's answer: {getattr(drv, 'preset_refusal', None)}")
                tp.log(name, f"ptz presets on the camera: {getattr(drv, 'presets_answer', None)}")
            if ok and drv.last_preset_token != str(token or int(preset)):
                tp.log(name, f"ptz set_preset: the camera keeps this point as its preset {drv.last_preset_token}")
        else:
            ok = drv.set_preset(int(preset))
    elif action == "absolute":                       # go to typed pan / tilt / zoom (ONVIF generic spaces)
        if not isinstance(drv, tp.ONVIFSoapDriver):
            raise ValueError("go to a typed position works on ONVIF PTZ cameras only (this one uses Dahua CGI)")
        pos = pos or {}
        z = pos.get("z")
        ok = absolute_move(drv, pos.get("x", 0), pos.get("y", 0), None if z in (None, "") else z)
        extra["target"] = {"x": pos.get("x"), "y": pos.get("y"), "z": z}
    elif action == "autofocus":
        ok = drv.trigger_autofocus()
    elif action == "lens_init":
        ok = drv.init_lens()
    elif action == "status":
        coords, moving = drv.read_ptz()
        extra = {"status": coords, "moving": moving}
        ok = True
        raw = getattr(drv, "status_raw", None)
        if raw and name not in _status_logged:
            _status_logged.add(name)
            tp.log(name, f"ptz status: the camera reports {'no position' if not coords.get('reported') else 'all zeros'}; "
                         f"its answer: {raw}")
    else:
        raise ValueError(f"unknown action {action}")
    err = getattr(drv, "last_error", None)
    tp.log(name, f"ptz {action} {'OK' if ok else 'FAIL'}" + (f" {err}" if (err and not ok) else ""))
    return {"ok": ok, "error": None if ok else err, **extra}


ABS_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"


def absmove_test(cam):
    """Does the camera obey ONVIF AbsoluteMove even if it reports no position? Pictures before / after moves to
    pan -0.5, +0.5 and -0.5 again (tilt 0, zoom unchanged) tell: it moved, and it came back to the same view.
    If so, our software can keep the POI positions itself and send the camera there (no camera presets needed)."""
    import base64

    import cv2
    import numpy as np
    drv = get_driver(cam)
    name = drv.display_name
    if not isinstance(drv, tp.ONVIFSoapDriver):
        return {"ok": False, "verdict": "only for ONVIF PTZ cameras", "steps": [], "pictures": []}
    steps, pics = [], []
    nodes = drv._post_soap("<tptz:GetNodes/>")
    space = bool(nodes is not None and "AbsolutePanTiltPositionSpace" in nodes.text)
    steps.append(f"camera lists absolute pan / tilt positions: {'yes' if space else 'no'}")

    def picture(label):
        data = snapshot(cam, "sub")
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
        small = cv2.resize(img, (320, int(img.shape[0] * 320 / img.shape[1])))
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
        pics.append({"label": label, "jpg": "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode()})
        return cv2.GaussianBlur(small, (5, 5), 0).astype(np.float32)

    def move(x):
        ok = absolute_move(drv, x, 0)
        steps.append(f"absolute move to pan {x:+.1f}: " + ("accepted" if ok else f"refused ({drv.last_error})"))
        time.sleep(5)                                    # time to get there
        return ok

    diff = lambda a, b: float(np.mean(np.abs(a - b)))   # noqa: E731  mean grey-level change, 0..255
    p0 = picture("before")
    a1 = move(-0.5)
    p1 = picture("pan -0.5")
    a2 = move(0.5)
    p2 = picture("pan +0.5")
    a3 = move(-0.5)
    p3 = picture("pan -0.5 again")
    moved = max(diff(p0, p1), diff(p1, p2)) > 12
    back = diff(p1, p3) < 6
    steps.append(f"picture change: before -> -0.5: {diff(p0, p1):.0f}, -0.5 -> +0.5: {diff(p1, p2):.0f}, "
                 f"-0.5 again vs first -0.5: {diff(p1, p3):.0f} (0 = same view)")
    if not (a1 or a2 or a3):
        verdict = "NO: the camera refuses absolute moves"
    elif not moved:
        verdict = "NO: the camera accepts absolute moves but does not move"
    elif back:
        verdict = "YES: the camera goes to absolute positions and returns to the same view; POIs can be kept by our software"
    else:
        verdict = "PARTLY: the camera moves, but does not return to the same view; POIs would not be reliable"
    for st in steps:
        tp.log(name, f"absolute move test: {st}")
    tp.log(name, f"absolute move test: {verdict}")
    return {"ok": verdict.startswith("YES"), "verdict": verdict, "steps": steps, "pictures": pics}


def snapshot(cam, which, encoder=None):
    """One up-to-date JPEG frame from the main or sub stream (ffmpeg, like the copy test)."""
    fresh_cam = {"_encoder": encoder or {}}
    cfg = tp.standardize_config(cam)
    if "main_stream_subtype" in cam or cfg["vendor"] == "dahua":
        st = cam.get("sub_stream_subtype", 1) if which == "sub" else cam.get("main_stream_subtype", 0)
        path = f"/cam/realmonitor?channel={cfg['channel']}&subtype={st}"
    else:
        path = cam.get("sub_stream_path" if which == "sub" else "main_stream_path") or "/live/ch0"
    # The first frame a camera sends is its last keyframe, which can be several seconds old
    # (livingroom: keyframe every 6 s). So read one keyframe interval and keep the last frame.
    enc = fresh_cam.get("_encoder", {})
    fps = float(enc.get("fps") or 0) or 15.0
    gop = float(enc.get("gop_keyframe_interval") or 0) or fps
    read_s = min(8.0, max(0.5, gop / fps + 0.5))
    # own file per picture: two pictures of one camera (live refresh + a test) must not share a file
    tmp = os.path.join(tp.OUT_DIR, f"_snap_{cfg['name']}_{threading.get_ident()}_{time.time_ns()}.jpg")
    os.makedirs(tp.OUT_DIR, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-rtsp_transport", "tcp", "-timeout", "5000000",
           "-i", tp.rtsp_url(cfg, path), "-t", f"{read_s:.1f}", "-an", "-vf", "scale='min(1280,iw)':-2",
           "-update", "1", "-q:v", "4", tmp]
    # Cheap cameras allow ~3 RTSP streams and keep a closed one counted for a few seconds.
    # multy.py already holds one, so: one picture per camera at a time, and retry when refused.
    with _snap_locks.setdefault(cfg["host"], threading.Lock()):
        err = ""
        for attempt in range(3):
            if os.path.isfile(tmp):
                os.remove(tmp)
            r = subprocess.run(cmd, capture_output=True, timeout=30)
            if os.path.isfile(tmp):
                break
            err = tp.mask(r.stderr.decode(errors="ignore").strip(), cfg)
            tp.log(cfg["name"], f"picture refused (try {attempt + 1}/3): {err.splitlines()[-1][-80:] if err else '?'}")
            time.sleep(3)
        else:
            if "Invalid data" in err or "4XX" in err or "503" in err:
                raise RuntimeError("camera refused the stream 3 times. It allows only a few streams at once "
                                   "(multy.py uses one, a running test uses more). Wait a few seconds and refresh.")
            raise RuntimeError(err.splitlines()[-1][-200:] if err else "no frame")
        try:                       # read and remove while still holding the camera's lock
            with open(tmp, "rb") as f:
                data = f.read()
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp)
    return data


def _small_gray(jpeg: bytes):
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    return None if img is None else cv2.resize(img, (64, 36), interpolation=cv2.INTER_AREA).astype("float32")


def what_changed(before: bytes, after: bytes) -> str:
    """How the camera's picture changed: "turned" (180°: flip + mirror), "flip" (top-bottom only), "mirror"
    (left-right only), "same", or "" (cannot tell: scene changed / too symmetric / no OpenCV)."""
    try:
        import numpy as np
        a, b = _small_gray(before), _small_gray(after)
        if a is None or b is None:
            return ""
        d = {"same": a, "turned": a[::-1, ::-1], "flip": a[::-1, :], "mirror": a[:, ::-1]}
        err = sorted((float(np.mean(np.abs(v - b))), k) for k, v in d.items())
        return err[0][1] if err[0][0] < 0.7 * err[1][0] else ""
    except Exception:
        return ""


def camera_flip(data: dict, action: str, axis: str = "v") -> dict:
    """Flip vertical / Flip horizontal on the PTZ page: written into the camera (camflip.py). After on / off the
    PICTURE is compared before and after, and the answer says what the camera really did."""
    if action not in ("on", "off"):
        return camflip.handle(data, action, axis)
    cam = data.get("camera") or {}
    try:
        before = snapshot(cam, "sub", data.get("stream_encoder_properties"))
    except Exception:
        before = None
    r = camflip.handle(data, action, axis)
    if not r.get("ok") or before is None:
        return r
    time.sleep(2.5)
    try:
        after = snapshot(cam, "sub", data.get("stream_encoder_properties"))
    except Exception:
        return r
    r["picture"] = got = what_changed(before, after)
    want = "flip" if axis != "h" else "mirror"
    note = {"turned": "the camera turned the picture 180° (vertical AND horizontal together)",
            "mirror": "the camera flipped the picture horizontally (left-right)",
            "flip": "the camera flipped the picture vertically (top-bottom)",
            "same": "the picture did not change",
            "": "could not tell what changed (the scene moved); look at the picture"}[got]
    r["log"].append("picture check: " + note)
    if got == "same":
        r["ok"] = False
    r["as_asked"] = got == want
    tp.log(osd.camera_id(data), f"flip {'horizontal' if axis == 'h' else 'vertical'} {action}: {note}")
    return r


def known_login(ip: str):
    """{login, file} from a camera JSON with this IP: central's base, the edge bases, out/, the old
    edge_worker files (next to this repo, or --config-dir)."""
    folders = [central_base(), *edge_shares().values(), tp.OUT_DIR, CONFIG_DIR,
               os.path.join(tp.SCRIPT_DIR, "..", "edge_worker")]
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        try:
            items = _camera_jsons(folder)
        except OSError:
            continue
        for fn, data in items:
            cam = data["camera"]
            if str(cam.get("host") or "").strip() == ip and (cam.get("username") or cam.get("password")):
                return {"login": (str(cam.get("username") or ""), str(cam.get("password") or "")), "file": fn}
    return None


def identify_ip(b):
    """Identify a camera by its IP. The login typed on the page is tried first, then the default logins
    (discover.find_login, at most 3 tries in all); none works -> {need_login: True, note}.
    The login that worked goes into the new JSON."""
    ip = str(b.get("ip") or "").strip()
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError(f"not an IP address: {ip!r}")
    user, password = str(b.get("username") or ""), str(b.get("password") or "")
    ports, xaddrs = b.get("ports") or [], b.get("onvif_xaddrs") or []
    known = None
    if not (user or password):                  # a camera JSON with this IP already has a working login
        known = known_login(ip)
        if known:
            user, password = known["login"]
    found = dc.find_login(ip, ports, xaddrs, first=(user, password) if (user or password) else None)
    if found["how"] is None:
        if not (user or password):      # neither ONVIF nor Dahua CGI and no login typed: ask for it
            return {"need_login": True, "locked": False, "tried": 0,
                    "note": found["note"] or "the camera's login could not be checked: give its login"}
        login_from = "yours"            # neither ONVIF nor Dahua CGI: identify tries the given login on RTSP
    elif found["login"] is None:
        return {"need_login": True, "locked": found["locked"], "note": found["note"], "tried": found["tried"]}
    else:
        login_from = (f"from {known['file']}" if known else "yours") if found["login"] == (user, password) else "default"
        user, password = found["login"]
    ident = dc.identify(ip, user, password, ports, xaddrs, None)
    # the login was accepted but nothing works with it (no stream, ONVIF refuses it): ask for the login again
    # instead of making a camera JSON that cannot show a picture
    if not ident.get("streams") or ident.get("onvif_login") == "failed":
        why = "ONVIF refused the login" if ident.get("onvif_login") == "failed" else "no video stream opened with that login"
        return {"need_login": True, "locked": False, "tried": found.get("tried", 0),
                "note": f"{why}: give the camera's login" + (f" ({'; '.join(ident.get('notes') or [])})" if ident.get("notes") else "")}
    if found.get("how") == "rtsp":
        ident.setdefault("notes", []).append("ONVIF on this camera takes no login; the login was checked on the video")
    return {"identify": ident, "login_from": login_from, "login_user": user,
            "json": dc.to_camera_json(ident, user, password)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the console to our own short log lines
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass                           # the page went away (reload) while an answer was on its way

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/configs":
                return self._send(200, {"config_dir": CONFIG_DIR, "out_dir": tp.OUT_DIR,
                                        "items": list_configs()})
            if u.path == "/api/config":
                with open(config_path(q["where"], q["file"]), encoding="utf-8") as f:
                    data = json.load(f)
                text = None
                rp = os.path.join(tp.OUT_DIR, os.path.splitext(os.path.basename(q["file"]))[0] + ".txt")
                if os.path.isfile(rp):
                    with open(rp, encoding="utf-8") as f:
                        text = f.read()
                return self._send(200, {"json": data, "report_text": text})
            if u.path == "/api/test/progress":
                return self._send(200, progress(int(q.get("since", 0))))
            if u.path == "/api/template":       # with the sites of central's pull.json (the site list)
                sites = [str(n.get("site_name")) for n in pull_config().get("edge_nodes", []) if n.get("site_name")]
                return self._send(200, {"json": tp.JSON_TEMPLATE, "sites": list(dict.fromkeys(sites)),
                                        "stream_limit": stream_limit()})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/save":
                return self._send(200, {"saved": save_out(b["file"], b["json"])})
            if u.path == "/api/test":
                return self._send(200, run_test(b["json"]))
            if u.path == "/api/config_lock":
                return self._send(200, config_lock(b))
            if u.path == "/api/same_ip":
                return self._send(200, same_ip(b["json"]))
            if u.path == "/api/save_all":
                return self._send(200, save_all(b["json"]))
            if u.path == "/api/ptz/absmove_test":
                return self._send(200, absmove_test(b["camera"]))
            if u.path == "/api/ptz":
                return self._send(200, ptz(b["camera"], b["action"], b.get("speed"), b.get("preset"), b.get("token"),
                                           b.get("pos")))
            if u.path == "/api/discover":
                devices = dc.discover(b.get("subnet") or None, scan=b.get("scan", True))
                known = {i["host"]: i for i in list_configs()}
                for d in devices:
                    k = known.get(d["ip"])
                    d["known"] = f"{k['where']}/{k['file']}" if k else None
                return self._send(200, {"subnet": b.get("subnet") or dc.default_subnet(), "devices": devices})
            if u.path == "/api/identify":
                return self._send(200, identify_ip(b))
            if u.path == "/api/camflip":               # flip@camera: the camera's own flip, set at once (no Save)
                return self._send(200, camera_flip(b["json"], b.get("action", "read"), b.get("axis", "v")))
            if u.path == "/api/osd":
                return self._send(200, osd.handle(b["json"], b.get("action", "read")))
            if u.path == "/api/snapshot":
                return self._send(200, snapshot(b["camera"], b.get("which", "sub"), b.get("encoder")), "image/jpeg")
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})


def main():
    global CONFIG_DIR
    p = argparse.ArgumentParser(description="Camera config page (local only)")
    p.add_argument("--config-dir", default=CONFIG_DIR, help="folder with the camera JSONs (read only)")
    p.add_argument("--port", type=int, default=8091)
    a = p.parse_args()
    CONFIG_DIR = os.path.abspath(a.config_dir)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    tp.log("web", f"page on http://127.0.0.1:{a.port}  configs {CONFIG_DIR}  saves to {tp.OUT_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
