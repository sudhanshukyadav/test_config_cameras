#!/usr/bin/env python3
"""Local web page for test_protocol.py: edit camera JSON, run the camera test, drive PTZ.

    python web_ui.py                      # configs from C:\\Users\\sudha\\edge_worker, page on :8091
    python web_ui.py --config-dir D:\\cams --port 8091

Only listens on 127.0.0.1. Never writes into the config dir; saves go to out/.
"""
import argparse
import contextlib
import io
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import discover as dc
import test_protocol as tp

WEB_DIR = os.path.join(tp.SCRIPT_DIR, "web")
CONFIG_DIR = r"C:\Users\sudha\edge_worker"

_test_lock = threading.Lock()      # one camera test at a time (stdout is captured globally)
_drivers = {}                      # (host, http_port, user, password, protocol) -> driver
_drivers_lock = threading.Lock()
_watchdogs = {}                    # driver id -> Timer that stops a move nobody stopped
_snap_locks = {}                   # camera host -> Lock (one picture stream per camera at a time)


def list_configs():
    """Camera JSONs in the config dir (source) and in out/ (generated or saved here)."""
    items = []
    for where, folder in (("source", CONFIG_DIR), ("out", tp.OUT_DIR)):
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            if not fn.endswith(".json") or fn.endswith(".report.json"):
                continue
            try:
                with open(os.path.join(folder, fn), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if isinstance(data, dict) and isinstance(data.get("camera"), dict):
                cam = data["camera"]
                items.append({"where": where, "file": fn, "name": cam.get("name", fn[:-5]),
                              "host": cam.get("host"), "ptz": cam.get("ptz_protocol", "")})
    return items


def config_path(where, fn):
    folder = CONFIG_DIR if where == "source" else tp.OUT_DIR
    fn = os.path.basename(fn)               # no paths from the page
    if not fn.endswith(".json"):
        raise ValueError("not a .json file")
    return os.path.join(folder, fn)


def save_out(fn, data):
    if not isinstance(data, dict) or not isinstance(data.get("camera"), dict):
        raise ValueError("JSON needs a camera section")
    os.makedirs(tp.OUT_DIR, exist_ok=True)
    path = config_path("out", fn)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    return path


def run_test(fn, data, copy_seconds):
    """Saves the page's JSON to out/, tests the camera from it, returns log + new JSON + report."""
    path = save_out(fn, data)
    buf = io.StringIO()
    with _test_lock, contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = tp.run_camera_test(path, int(copy_seconds))
        except SystemExit as e:
            code = e.code
        except Exception as e:  # keep the server up, show the error on the page
            print(f"ERROR {type(e).__name__}: {e}")
            code = 1
    stem = os.path.splitext(fn)[0]
    out = {"code": code, "log": tp.mask(buf.getvalue(), tp.standardize_config(data["camera"]))}
    for key, name in (("json", f"{stem}.json"), ("report", f"{stem}.report.json")):
        p = os.path.join(tp.OUT_DIR, name)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                out[key] = json.load(f)
    return out


def get_driver(cam):
    cfg = tp.standardize_config(cam)
    if cfg["ptz_protocol"] in ("none", "off", "no"):
        raise ValueError(f"{cfg['name']} has ptz_protocol 'none'")
    key = (cfg["host"], cfg["http_port"], cfg["username"], cfg["password"], cfg["ptz_protocol"])
    with _drivers_lock:
        if key not in _drivers:
            _drivers[key] = tp.build_driver(cfg)
        return _drivers[key]


def ptz(cam, action, speed=None, preset=None):
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
        ok = drv.goto_preset(int(preset))
        if ok:
            extra["status"], _ = drv.wait_until_idle()
    elif action == "set_preset":
        ok = drv.set_preset(int(preset))
    elif action == "autofocus":
        ok = drv.trigger_autofocus()
    elif action == "lens_init":
        ok = drv.init_lens()
    elif action == "status":
        coords, moving = drv.read_ptz()
        extra = {"status": coords, "moving": moving}
        ok = True
    else:
        raise ValueError(f"unknown action {action}")
    err = getattr(drv, "last_error", None)
    tp.log(name, f"ptz {action} {'OK' if ok else 'FAIL'}" + (f" {err}" if (err and not ok) else ""))
    return {"ok": ok, "error": None if ok else err, **extra}


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
    tmp = os.path.join(tp.OUT_DIR, f"_snap_{cfg['name']}.jpg")
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
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)
    return data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the console to our own short log lines
        pass

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
                report = None
                rp = os.path.join(tp.OUT_DIR, os.path.splitext(os.path.basename(q["file"]))[0] + ".report.json")
                if os.path.isfile(rp):
                    with open(rp, encoding="utf-8") as f:
                        report = json.load(f)
                return self._send(200, {"json": data, "report": report})
            if u.path == "/api/template":
                return self._send(200, {"json": tp.JSON_TEMPLATE})
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
                return self._send(200, run_test(b["file"], b["json"], b.get("copy_seconds", 10)))
            if u.path == "/api/ptz":
                return self._send(200, ptz(b["camera"], b["action"], b.get("speed"), b.get("preset")))
            if u.path == "/api/discover":
                devices = dc.discover(b.get("subnet") or None, scan=b.get("scan", True))
                known = {i["host"]: i for i in list_configs()}
                for d in devices:
                    k = known.get(d["ip"])
                    d["known"] = f"{k['where']}/{k['file']}" if k else None
                return self._send(200, {"subnet": b.get("subnet") or dc.default_subnet(), "devices": devices})
            if u.path == "/api/identify":
                ident = dc.identify(b["ip"], b.get("username", ""), b.get("password", ""),
                                    b.get("ports") or [], b.get("onvif_xaddrs") or [], None)
                return self._send(200, {"identify": ident,
                                        "json": dc.to_camera_json(ident, b.get("username", ""), b.get("password", ""))})
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
