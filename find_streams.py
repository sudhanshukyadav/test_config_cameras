#!/usr/bin/env python3
"""Find a camera's main stream, sub stream and snapshot address - fast.

The camera is asked over ONVIF for its own stream names (GetStreamUri) and picture address
(GetSnapshotUri). Each stream is then opened for 2 s only, instead of guessing paths for 13 s each.
Only when ONVIF gives nothing are the usual RTSP paths tried (2 s each).

    python find_streams.py gallery                 # camera JSON by name / file
    python find_streams.py --ip 192.168.8.60 --user admin --password 123456
    python find_streams.py gallery --save          # write the result into the camera JSON
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

import test_protocol as tp
import discover as dc

QUICK_SECONDS = 2


def _uri_path(uri: str) -> tuple[str, int | None]:
    u = urllib.parse.urlparse(uri.replace("&amp;", "&"))
    return u.path + (f"?{u.query}" if u.query else ""), u.port


def onvif_streams(cfg: dict, say) -> dict:
    """{'port', 'login', 'clock_offset_s', 'profiles': [{token, width, height, encoding, path, rtsp_port, snapshot}]}"""
    out = {"port": 0, "login": None, "clock_offset_s": 0, "profiles": []}
    hint = [cfg.get("http_port")] if cfg.get("http_port") else []
    port = dc._find_onvif_port(cfg["host"], hint)
    if not port:
        say("ONVIF: no answer on ports " + ", ".join(str(p) for p in dict.fromkeys(hint + dc.ONVIF_PORTS)))
        return out
    out["port"] = port
    drv = tp.ONVIFSoapDriver({**cfg, "http_port": port, "ptz_protocol": "onvif_soap"})
    out["clock_offset_s"] = round(drv.clock_offset.total_seconds())
    if out["clock_offset_s"]:
        say(f"ONVIF: camera clock is {out['clock_offset_s']:+d} s off this PC (login timestamps corrected)")
    if not drv.auth_ok:
        out["login"] = "failed"
        say(f"ONVIF port {port}: login refused ({drv.last_error}) - check the user / password")
        return out
    out["login"] = "none" if drv.auth_mode == "none" else f"wsse-{drv.pw_type}"
    say(f"ONVIF port {port}: login OK ({out['login']}), media service {drv.xaddr.get('media')}")
    pr = drv._post_soap("<trt:GetProfiles/>", service="media")
    for p in dc._parse_profiles(pr.text) if pr is not None else []:
        tok = p["token"]
        body = ("<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                f"<trt:ProfileToken>{tok}</trt:ProfileToken></trt:GetStreamUri>")
        r = drv._post_soap(body, service="media")
        m = re.search(r"<[\w]*:?Uri>([^<]+)<", r.text) if r is not None else None
        if m:
            p["path"], p["rtsp_port"] = _uri_path(m.group(1))
        r = drv._post_soap(f"<trt:GetSnapshotUri><trt:ProfileToken>{tok}</trt:ProfileToken></trt:GetSnapshotUri>",
                           service="media")
        m = re.search(r"<[\w]*:?Uri>([^<]+)<", r.text) if r is not None else None
        if m:
            p["snapshot"] = m.group(1).replace("&amp;", "&").strip()
        say(f"  profile {tok}: {p.get('width')}x{p.get('height')} {p.get('encoding')} "
            f"stream {p.get('path') or '-'}  snapshot {p.get('snapshot') or '-'}")
        out["profiles"].append(p)
    return out


def quick_check(cfg: dict, path: str, port: int):
    c = {**cfg, "port": port or cfg.get("port", 554)}
    info, err = tp.ffprobe_stream(tp.rtsp_url(c, path), seconds=QUICK_SECONDS, timeout=12)
    return info, err


def snapshot_ok(url: str, user: str, password: str):
    """(seconds, bytes) when the address returns a JPEG, else None. Tries no login, digest, basic."""
    for auth in (None, HTTPDigestAuth(user, password), HTTPBasicAuth(user, password)):
        try:
            t0 = time.time()
            r = requests.get(url, auth=auth, timeout=4)
        except requests.RequestException:
            return None
        if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
            return round(time.time() - t0, 2), len(r.content), \
                {None: "none", HTTPDigestAuth: "digest", HTTPBasicAuth: "basic"}[type(auth) if auth else None]
        if r.status_code != 401:
            return None
    return None


def find(cfg: dict, say=print) -> dict:
    t0 = time.time()
    res = {"main": None, "sub": None, "snapshot": None, "onvif": None}
    ov = onvif_streams(cfg, say)
    res["onvif"] = {k: ov[k] for k in ("port", "login", "clock_offset_s")}

    found = []
    for p in ov["profiles"]:
        if not p.get("path") or any(f["path"] == p["path"] for f in found):
            continue
        if found:
            time.sleep(1)      # cheap cameras keep a closed stream counted for a moment
        info, err = quick_check(cfg, p["path"], p.get("rtsp_port"))
        if info:
            found.append({**info, "path": p["path"], "rtsp_port": p.get("rtsp_port") or cfg.get("port", 554),
                          "snapshot": p.get("snapshot")})
            say(f"  OK   {p['path']}  {info['width']}x{info['height']} {info['codec']} {info['fps']} fps")
        else:
            say(f"  FAIL {p['path']}  ({tp.mask(err or '', cfg)[-80:]})")

    if not found:
        say("No working ONVIF stream; trying the usual RTSP paths (2 s each)")
        for vendor, main_path, sub_path in dc.VENDOR_RTSP_PATHS:
            for path in (main_path, sub_path):
                if not path:
                    continue
                info, _ = quick_check(cfg, path, cfg.get("port", 554))
                if info:
                    found.append({**info, "path": path, "rtsp_port": cfg.get("port", 554), "snapshot": None})
                    say(f"  OK   {path}  {info['width']}x{info['height']} {info['codec']}")
                time.sleep(1)
            if found:
                break

    if found:
        by_size = sorted(found, key=lambda s: (s["width"] or 0) * (s["height"] or 0), reverse=True)
        res["main"] = by_size[0]
        smaller = [s for s in by_size if (s["width"], s["height"]) != (by_size[0]["width"], by_size[0]["height"])]
        res["sub"] = smaller[-1] if smaller else by_size[0]

    # Snapshot: the smallest stream's picture first (fast to load), then the others
    snaps = [s.get("snapshot") for s in ([res["sub"], res["main"]] if found else []) if s] + \
            [p.get("snapshot") for p in ov["profiles"]]
    for url in dict.fromkeys(u for u in snaps if u):
        ok = snapshot_ok(url, cfg.get("username", ""), cfg.get("password", ""))
        if ok:
            res["snapshot"] = {"url": url, "seconds": ok[0], "bytes": ok[1], "auth": ok[2]}
            say(f"  OK   snapshot {url}  {ok[1] // 1024} KB in {ok[0]} s (login {ok[2]})")
            break
        say(f"  FAIL snapshot {url}")
    res["seconds"] = round(time.time() - t0, 1)
    return res


def save(json_path: str, res: dict):
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    cam = data["camera"]
    if res["main"]:
        if not re.match(r"/cam/realmonitor", res["main"]["path"]):     # Dahua JSONs keep their subtypes
            cam["main_stream_path"] = res["main"]["path"]
            cam["sub_stream_path"] = res["sub"]["path"]
        cam["port"] = res["main"]["rtsp_port"]
        enc = data.setdefault("stream_encoder_properties", {})
        enc["main_stream_resolution"] = [res["main"]["width"], res["main"]["height"]]
        enc["sub_stream_resolution"] = [res["sub"]["width"], res["sub"]["height"]]
    if res["snapshot"]:
        u = urllib.parse.urlparse(res["snapshot"]["url"])
        cam["snapshot_path"] = u.path + (f"?{u.query}" if u.query else "")
        cam["snapshot_port"] = u.port or 80
        cam["snapshot_auth"] = res["snapshot"]["auth"]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("camera", nargs="?", help="camera JSON name or file")
    ap.add_argument("--ip"), ap.add_argument("--user", default="admin"), ap.add_argument("--password", default="")
    ap.add_argument("--http-port", type=int, default=0)
    ap.add_argument("--save", action="store_true", help="write main/sub/snapshot into the camera JSON")
    a = ap.parse_args()
    json_path = None
    if a.camera:
        json_path = tp.resolve_config_path(a.camera)
        cfg = tp.load_camera_config(a.camera)
    elif a.ip:
        cfg = tp.standardize_config({"name": a.ip, "host": a.ip, "username": a.user, "password": a.password,
                                     "http_port": a.http_port or 80})
    else:
        ap.error("give a camera JSON name or --ip")
    if a.http_port:
        cfg["http_port"] = a.http_port

    res = find(cfg)
    print()
    if res["main"]:
        print(f"MAIN     {res['main']['path']}   {res['main']['width']}x{res['main']['height']}")
        print(f"SUB      {res['sub']['path']}   {res['sub']['width']}x{res['sub']['height']}")
    else:
        print("MAIN/SUB not found")
    print(f"SNAPSHOT {res['snapshot']['url'] if res['snapshot'] else 'not found'}")
    print(f"({res['seconds']} s)")
    if a.save:
        if not json_path or not os.path.isfile(json_path):
            print("--save needs a camera JSON file", file=sys.stderr)
            return 1
        save(json_path, res)
        print(f"saved into {json_path}")
    return 0 if res["main"] else 1


if __name__ == "__main__":
    sys.exit(main())
