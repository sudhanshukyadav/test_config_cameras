#!/usr/bin/env python3
"""Find and identify unknown IP cameras, and build a starting camera JSON for edge_worker.

    python discover.py                                   # find cameras, print what they are
    python discover.py --user admin --password xxx       # also identify with your login
    python discover.py --identify 192.168.8.40 --user admin --password xxx --write

How a camera is found and identified:
1. ONVIF WS-Discovery (multicast probe, ~3 s) and a TCP scan of the subnet for camera ports.
2. ONVIF on the camera: maker, model, PTZ service, profiles with codec / resolution / fps,
   and the RTSP address of each stream (GetStreamUri) - no guessing of paths needed.
3. Dahua CGI check (magicBox.cgi), because Dahua OEM cameras often call themselves "General".
4. No ONVIF: try a short list of RTSP paths used by common makers.
Logins: the one you give (plus no login). Given none, the usual factory logins are tried first,
at most 3 per camera, one cheap check each (ONVIF, else Dahua CGI), stopping at the first that works
or when the camera reports a lock; then you are asked (find_login, default_logins.json).
camera_catalog.json lists known makers / models: their factory logins (tried first when the camera
tells its maker / model without a login), how PTZ is driven and whether its position can be read.
After this, run the camera test (test_protocol.py --test, or Run test on the page) to measure
the streams and fill in the encoder values.
"""
import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from requests.auth import HTTPDigestAuth

import test_protocol as tp

CAMERA_PORTS = [554, 80, 8899, 8000, 8080, 2020, 34567, 37777]
ONVIF_PORTS = [80, 8899, 8000, 8080, 2020, 8081]

# RTSP paths used by common makers; only tried when the camera has no ONVIF. {ch} = channel.
VENDOR_RTSP_PATHS = [
    ("dahua", "/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=1"),
    ("hikvision", "/Streaming/Channels/101", "/Streaming/Channels/102"),
    ("generic", "/live/ch0", "/live/ch1"),
    ("generic", "/11", "/12"),
    ("generic", "/onvif1", "/onvif2"),
    ("generic", "/stream1", "/stream2"),
    ("generic", "/h264/ch1/main/av_stream", "/h264/ch1/sub/av_stream"),
    ("generic", "/live/main", "/live/sub"),
    ("generic", "/", None),
]


def local_ip_for(subnet: str) -> str:
    """This PC's address on the given subnet (picks the right network card)."""
    net = ipaddress.ip_network(subnet, strict=False)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((str(net.network_address + 1), 9))
        return s.getsockname()[0]
    finally:
        s.close()


def default_subnet() -> str:
    """The /24 of this PC's main network card (the one with the default route)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 9))
        ip = s.getsockname()[0]
    except OSError:
        ip = "192.168.1.1"
    finally:
        s.close()
    return str(ipaddress.ip_network(f"{ip}/24", strict=False))


# ---------------------------------------------------------------------------
# 1. Finding cameras
# ---------------------------------------------------------------------------
def ws_discover(subnet: str, timeout: float = 3.0) -> dict:
    """ONVIF WS-Discovery. Returns {ip: {"xaddrs": [...], "name": .., "hardware": ..}}."""
    probe = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>
  <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>""".encode()
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        local = local_ip_for(subnet)
        sock.bind((local, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        for _ in range(2):  # UDP can drop; send twice
            sock.sendto(probe, ("239.255.255.250", 3702))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, (ip, _) = sock.recvfrom(65535)
            except socket.timeout:
                continue
            text = data.decode("utf-8", errors="ignore")
            xaddrs = re.findall(r"<[\w]*:?XAddrs>([^<]+)<", text)
            scopes = " ".join(re.findall(r"<[\w]*:?Scopes[^>]*>([^<]+)<", text))
            info = found.setdefault(ip, {"xaddrs": [], "name": None, "hardware": None})
            for x in " ".join(xaddrs).split():
                if x not in info["xaddrs"]:
                    info["xaddrs"].append(x)
            for key in ("name", "hardware"):
                m = re.search(rf"onvif://www\.onvif\.org/{key}/(\S+)", scopes)
                if m:
                    info[key] = requests.utils.unquote(m.group(1))
    except OSError as e:
        tp.log("discover", f"ws-discovery failed: {e}")
    finally:
        sock.close()
    return found


def port_scan(subnet: str, ports=CAMERA_PORTS, timeout: float = 0.4) -> dict:
    """TCP connect scan. Returns {ip: [open ports]} for hosts with a camera-like port open."""
    net = ipaddress.ip_network(subnet, strict=False)
    if net.num_addresses > 1024:
        raise ValueError("subnet too large; use a /24 or smaller")

    def check(args):
        ip, port = args
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return ip, port
        except OSError:
            return None

    jobs = [(str(h), p) for h in net.hosts() for p in ports]
    out = {}
    with ThreadPoolExecutor(max_workers=200) as ex:
        for r in ex.map(check, jobs):
            if r:
                out.setdefault(r[0], []).append(r[1])
    return {ip: sorted(p) for ip, p in out.items()}


def discover(subnet: str = None, scan: bool = True) -> list:
    """Both methods merged: [{"ip", "ports", "onvif_xaddrs", "name", "hardware"}], sorted by IP."""
    subnet = subnet or default_subnet()
    tp.log("discover", f"searching {subnet} (ONVIF probe{' + port scan' if scan else ''})")
    onvif = ws_discover(subnet)
    ports = port_scan(subnet) if scan else {}
    net = ipaddress.ip_network(subnet, strict=False)
    ips = {ip for ip in set(onvif) | set(ports) if ipaddress.ip_address(ip) in net}
    devices = []
    for ip in ips:
        o = onvif.get(ip, {})
        p = ports.get(ip, [])
        # Without ONVIF, only hosts with RTSP open are likely cameras (skips PCs/routers on :80).
        if not o and 554 not in p:
            continue
        devices.append({"ip": ip, "ports": p, "onvif_xaddrs": o.get("xaddrs", []),
                        "name": o.get("name"), "hardware": o.get("hardware")})
    devices.sort(key=lambda d: ipaddress.ip_address(d["ip"]))
    tp.log("discover", f"found {len(devices)} camera(s): " + ", ".join(d["ip"] for d in devices))
    return devices


# ---------------------------------------------------------------------------
# 2. Identifying one camera
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _find_onvif_port(ip: str, hint_ports) -> int:
    body = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            '<s:Body><GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>')
    for port in dict.fromkeys(list(hint_ports) + ONVIF_PORTS):
        try:
            r = requests.post(f"http://{ip}:{port}/onvif/device_service", data=body,
                              headers={"Content-Type": "application/soap+xml; charset=utf-8"}, timeout=2)
            if "GetSystemDateAndTimeResponse" in r.text or "NotAuthorized" in r.text:
                return port
        except requests.RequestException:
            continue
    return 0


def _parse_profiles(xml_text: str) -> list:
    """Profiles with their video encoder settings."""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for prof in root.iter():
        if _local(prof.tag) != "Profiles":
            continue
        p = {"token": prof.attrib.get("token"), "encoding": None, "width": None, "height": None,
             "fps": None, "bitrate_kbps": None, "gop": None, "ptz": False}
        for el in prof.iter():
            t = _local(el.tag)
            if t == "PTZConfiguration":
                p["ptz"] = True
            elif t == "VideoEncoderConfiguration":
                for v in el.iter():
                    vt, txt = _local(v.tag), (v.text or "").strip()
                    if vt == "Encoding" and txt:
                        p["encoding"] = txt
                    elif vt == "Width" and txt.isdigit():
                        p["width"] = int(txt)
                    elif vt == "Height" and txt.isdigit():
                        p["height"] = int(txt)
                    elif vt == "FrameRateLimit" and txt.isdigit():
                        p["fps"] = int(txt)
                    elif vt == "BitrateLimit" and txt.isdigit():
                        p["bitrate_kbps"] = int(txt)
                    elif vt == "GovLength" and txt.isdigit():
                        p["gop"] = int(txt)
        out.append(p)
    return out


def _dahua_cgi(ip: str, port: int, user: str, password: str):
    """Returns device type if the camera answers Dahua's magicBox CGI, else None."""
    url = f"http://{ip}:{port}/cgi-bin/magicBox.cgi"
    for auth in ([HTTPDigestAuth(user, password)] if user else []) + [None]:
        try:
            r = requests.get(url, params={"action": "getDeviceType"}, auth=auth, timeout=2.5)
        except requests.RequestException:
            return None
        if r.status_code == 200 and "type=" in r.text:
            return r.text.strip().split("=", 1)[1]
    return None


# Factory logins tried when none is given (most known cameras keep them). Your own common logins go
# into default_logins.json next to this file (git-ignored), tried before these:
#   {"logins": [["admin", "plant-pass"], ["admin", "admin"]], "max_tries": 3}
DEFAULT_LOGINS = [("admin", "admin123"), ("admin", ""), ("admin", "admin"), ("admin", "12345")]
MAX_LOGIN_TRIES = 3     # many cameras lock the login after ~5 wrong passwords (Dahua: 30 min)


def load_catalog() -> dict:
    """camera_catalog.json next to this file: {max_login_tries, entries: [...]} (empty if missing)."""
    path = os.path.join(tp.SCRIPT_DIR, "camera_catalog.json")
    try:
        with open(path, encoding="utf-8") as f:
            cat = json.load(f)
        return cat if isinstance(cat, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        tp.log("catalog", f"camera_catalog.json unreadable ({e}); not used")
        return {}


def _matches(words, text: str) -> bool:
    text = (text or "").lower()
    return any(w and re.search(rf"(?<![a-z0-9]){re.escape(str(w).lower())}(?![a-z0-9])", text) for w in (words or []))


def catalog_entry(maker: str | None, model: str | None) -> dict | None:
    """The catalog entry for this maker / model text (model first: it is the more precise), or None."""
    entries = load_catalog().get("entries") or []
    for e in entries:
        if model and _matches(e.get("model_match"), model):
            return e
    for e in entries:
        if maker and _matches(e.get("maker_match"), maker):
            return e
    return None


def default_logins(maker: str | None = None, model: str | None = None) -> tuple[list, int]:
    """Logins to try, in order: default_logins.json (the plant's own), the matching catalog entry, the built-in
    list, the other catalog entries. Tries: default_logins.json max_tries, else the catalog's, else 3."""
    logins, tries = [], MAX_LOGIN_TRIES
    cat = load_catalog()
    if cat.get("max_login_tries"):
        tries = int(cat["max_login_tries"])
    entry = catalog_entry(maker, model)
    first = [tuple(map(str, x)) for x in (entry or {}).get("logins", []) if x]
    others = [tuple(map(str, x)) for e in (cat.get("entries") or []) if e is not entry for x in e.get("logins", []) if x]
    path = os.path.join(tp.SCRIPT_DIR, "default_logins.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            logins = [(str(x[0]), str(x[1]) if len(x) > 1 else "") for x in cfg.get("logins", []) if x]
            tries = int(cfg.get("max_tries", tries))
        except (OSError, ValueError, TypeError, IndexError) as e:
            tp.log("login", f"default_logins.json unreadable ({e}); using the built-in list")
    out = list(dict.fromkeys(logins + first + DEFAULT_LOGINS + others))
    out = [(u, p if p is not None else "") for u, p in out]
    return out, max(1, min(tries, len(out)))


def _onvif_device_info(ip: str, port: int) -> tuple[str | None, str | None]:
    """(maker, model) when the camera tells them without a login (many do), else (None, None)."""
    body = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><s:Body><tds:GetDeviceInformation/></s:Body></s:Envelope>')
    try:
        r = requests.post(f"http://{ip}:{port}/onvif/device_service", data=body.encode(),
                          headers={"Content-Type": "application/soap+xml; charset=utf-8"}, timeout=4)
    except requests.RequestException:
        return None, None
    if r.status_code != 200:
        return None, None
    get = lambda tag: (re.search(rf"<[\w]*:?{tag}>([^<]*)<", r.text) or [None, None])[1]  # noqa: E731
    return get("Manufacturer"), get("Model")


def _onvif_login(ip: str, port: int, user: str, password: str) -> str:
    """One ONVIF call with this login: 'ok', 'bad', 'locked' or 'unknown'."""
    signer = object.__new__(tp.ONVIFSoapDriver)          # only to build the WS-Security header
    signer.user, signer.password = user, password
    signer.endpoint = f"http://{ip}:{port}/onvif/device_service"
    signer.clock_offset = signer._camera_clock_offset()   # a camera clock that is off rejects a right password
    body = (f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            f'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><s:Header>{signer._build_wsse_header()}</s:Header>'
            f'<s:Body><tds:GetDeviceInformation/></s:Body></s:Envelope>')
    try:
        r = requests.post(f"http://{ip}:{port}/onvif/device_service", data=body.encode(),
                          headers={"Content-Type": "application/soap+xml; charset=utf-8"}, timeout=4)
    except requests.RequestException:
        return "unknown"
    text = r.text.lower()
    if "lock" in text:
        return "locked"
    if r.status_code == 200 and "getdeviceinformationresponse" in text:
        return "ok"
    if "notauthorized" in text or r.status_code in (400, 401, 403):
        return "bad"
    return "unknown"


def _onvif_anonymous(ip: str, port: int) -> bool:
    """The camera answers ONVIF without a login (cheap NVT / APCam cameras reject every login but do this)."""
    body = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><s:Body><tds:GetDeviceInformation/></s:Body></s:Envelope>')
    try:
        r = requests.post(f"http://{ip}:{port}/onvif/device_service", data=body.encode(),
                          headers={"Content-Type": "application/soap+xml; charset=utf-8"}, timeout=4)
    except requests.RequestException:
        return False
    return r.status_code == 200 and "getdeviceinformationresponse" in r.text.lower()


def _onvif_stream_path(ip: str, port: int) -> tuple[str, int] | None:
    """(path, rtsp port) of the camera's first stream, asked over ONVIF without a login."""
    drv = tp.ONVIFSoapDriver({"name": ip, "host": ip, "http_port": port, "username": "", "password": ""})
    if not drv.auth_ok:
        return None
    pr = drv._post_soap("<trt:GetProfiles/>", service="media")
    for p in _parse_profiles(pr.text) if pr is not None else []:
        body = ("<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                f"<trt:ProfileToken>{p['token']}</trt:ProfileToken></trt:GetStreamUri>")
        u = drv._post_soap(body, service="media")
        m = re.search(r"<[\w]*:?Uri>([^<]+)<", u.text) if u is not None else None
        if m:
            uri = urlparse(m.group(1).replace("&amp;", "&"))
            return uri.path + (f"?{uri.query}" if uri.query else ""), uri.port or 554
    return None


def _rtsp_login(ip: str, port: int, path: str, user: str, password: str) -> str:
    """One look at the video stream with this login: 'ok', 'bad' or 'unknown'."""
    info, err = tp.ffprobe_stream(tp.rtsp_url({"host": ip, "port": port, "username": user, "password": password}, path),
                                  seconds=2, timeout=12)
    if info:
        return "ok"
    e = str(err or "").lower()
    return "bad" if ("401" in e or "unauthorized" in e or "403" in e) else "unknown"


def _dahua_login(ip: str, port: int, user: str, password: str) -> str:
    """One Dahua CGI call with this login: 'ok', 'bad', 'locked' or 'unknown' (not a Dahua)."""
    try:
        r = requests.get(f"http://{ip}:{port}/cgi-bin/magicBox.cgi", params={"action": "getDeviceType"},
                         auth=HTTPDigestAuth(user, password), timeout=4)
    except requests.RequestException:
        return "unknown"
    if "lock" in r.text.lower():
        return "locked"
    if r.status_code == 200 and "type=" in r.text:
        return "ok"
    return "bad" if r.status_code == 401 else "unknown"


def _answers_dahua_cgi(ip: str, port: int) -> bool:
    """Dahua's magicBox CGI is there (asked without a login, so no failed login is counted)."""
    try:
        r = requests.get(f"http://{ip}:{port}/cgi-bin/magicBox.cgi", params={"action": "getDeviceType"}, timeout=3)
    except requests.RequestException:
        return False
    return r.status_code == 401 or (r.status_code == 200 and "type=" in r.text)


def find_login(ip: str, ports=None, onvif_xaddrs=None, first=None) -> dict:
    """Tries `first` (the login typed on the page), then the default logins, one cheap check each,
    at most MAX_LOGIN_TRIES in all, stopping at the first that works.
    Returns {login: (user, password) or None, tried, locked, how, note}."""
    hint = [urlparse(x).port or 80 for x in (onvif_xaddrs or [])] + [p for p in (ports or []) if p in ONVIF_PORTS]
    oport = _find_onvif_port(ip, hint)
    check, how = None, None
    if oport:
        check, how = (lambda u, pw: _onvif_login(ip, oport, u, pw)), "onvif"
    elif _answers_dahua_cgi(ip, 80):
        check, how = (lambda u, pw: _dahua_login(ip, 80, u, pw)), "dahua"
    res = {"login": None, "tried": 0, "locked": False, "how": how, "note": ""}
    if check is None:
        res["note"] = "the camera answers neither ONVIF nor Dahua CGI: give its login"
        return res
    maker, model = _onvif_device_info(ip, oport) if oport else (None, None)
    entry = catalog_entry(maker, model)
    if entry:
        tp.log(ip, f"catalog: {entry.get('name')} ({maker or '?'} {model or ''}); its logins first")
    logins, tries = default_logins(maker, model)
    if first:
        logins = list(dict.fromkeys([tuple(first)] + logins))
    i = 0
    while i < min(tries, len(logins)):
        user, password = logins[i]
        i += 1
        res["tried"] += 1
        state = check(user, password)
        tp.log(ip, f"default login {res['tried']}/{tries} via {how}: {state}")
        if state == "bad" and how == "onvif" and _onvif_anonymous(ip, oport):
            # ONVIF rejects every login but answers without one: check the logins on the video instead
            sp = _onvif_stream_path(ip, oport)
            if not sp:
                res["note"] = "ONVIF answers without a login but gives no stream address: give the camera's login"
                return res
            path, rport = sp
            tp.log(ip, f"ONVIF rejects logins but answers without one; checking the logins on the video {path}")
            check, how = (lambda u, pw: _rtsp_login(ip, rport, path, u, pw)), "rtsp"
            res["how"], res["tried"], i = how, 0, 0      # ONVIF's "wrong" counted nothing: start again
            continue
        if state == "ok":
            res["login"] = (user, password)
            return res
        if state == "locked":
            res["locked"] = True
            res["note"] = "the camera has locked its login (too many wrong passwords): wait about 30 minutes"
            return res
        if state == "unknown":
            res["note"] = f"the camera stopped answering {how}; give its login"
            return res
        time.sleep(1)
    res["note"] = f"none of the {res['tried']} logins tried worked: give the camera's login"
    return res


def _rtsp_ok(ip: str, port: int, path: str, user: str, password: str):
    """ffprobe one path with the login, then without. Returns (info, used_login) or (None, None)."""
    logins = ([(user, password)] if user else []) + [("", "")]
    for u, pw in logins:
        cfg = {"host": ip, "port": port, "username": u, "password": pw}
        info, _ = tp.ffprobe_stream(tp.rtsp_url(cfg, path), seconds=2, timeout=12)
        if info:
            return info, bool(u)
    return None, None


def identify(ip: str, user: str = "", password: str = "", ports=None, onvif_xaddrs=None,
             name: str = None) -> dict:
    """Everything we can learn about a camera without changing it."""
    ports = ports or []
    res = {"ip": ip, "name": name, "maker": None, "model": None, "firmware": None, "vendor": "generic",
           "onvif_port": 0, "onvif_login": None, "ptz_protocol": "none", "rtsp_port": 554,
           "streams": [], "rtsp_login": None, "notes": []}
    hint = [urlparse(x).port or 80 for x in (onvif_xaddrs or [])] + [p for p in ports if p in ONVIF_PORTS]

    # --- ONVIF
    oport = _find_onvif_port(ip, hint)
    if oport:
        res["onvif_port"] = oport
        drv = tp.ONVIFSoapDriver({"name": name or ip, "host": ip, "http_port": oport,
                                  "username": user, "password": password})
        res["onvif_login"] = drv.auth_mode if drv.auth_ok else "failed"
        r = drv._post_soap("<tds:GetDeviceInformation/>", service="device")
        if r is not None:
            for k, tag in (("maker", "Manufacturer"), ("model", "Model"), ("firmware", "FirmwareVersion")):
                m = re.search(rf"<[\w]*:?{tag}>([^<]*)<", r.text)
                res[k] = m.group(1).strip() if m else None
        if drv.auth_ok:
            pr = drv._post_soap("<trt:GetProfiles/>", service="media")
            profiles = _parse_profiles(pr.text) if pr is not None else []
            for p in profiles:
                body = ("<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                        "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                        f"<trt:ProfileToken>{p['token']}</trt:ProfileToken></trt:GetStreamUri>")
                u = drv._post_soap(body, service="media")
                m = re.search(r"<[\w]*:?Uri>([^<]+)<", u.text) if u is not None else None
                if not m:
                    continue
                uri = urlparse(m.group(1).replace("&amp;", "&"))
                p["path"] = uri.path + (f"?{uri.query}" if uri.query else "")
                p["rtsp_port"] = uri.port or 554
                res["streams"].append(p)
            if drv.has_ptz and any(p["ptz"] for p in profiles):
                res["ptz_protocol"] = "onvif_soap"
        else:
            res["notes"].append(f"ONVIF login failed ({drv.last_error}); give the camera's login")

    # --- camera catalog (camera_catalog.json): what is known about this maker / model
    entry = catalog_entry(res["maker"], res["model"])
    if entry:
        res["catalog"] = entry.get("name")
        res["notes"].append(f"catalog: {entry.get('name')}" + (f" - {entry['note']}" if entry.get("note") else ""))
        if entry.get("ptz_position") is False:
            res["ptz_position"] = False

    # --- Dahua CGI (Dahua OEM cameras often report maker "General")
    http_port = 80 if 80 in ports or not ports else (oport or 80)
    dtype = _dahua_cgi(ip, http_port, user, password)
    if dtype:
        res["vendor"] = "dahua"
        res["ptz_protocol"] = "dahua_cgi" if ("PTZ" in dtype.upper() or res["ptz_protocol"] != "none") \
            else res["ptz_protocol"]
        res["notes"].append(f"Dahua CGI answers (type {dtype})")
    elif res["maker"] and "hikvision" in res["maker"].lower():
        res["vendor"] = "hikvision"

    # --- No ONVIF streams: try known RTSP paths (stops at the first maker pattern that works)
    if not res["streams"] and (554 in ports or not ports):
        paths = list(VENDOR_RTSP_PATHS)
        if entry and entry.get("rtsp_main"):                       # the catalog's paths for this model first
            paths.insert(0, ("catalog", entry["rtsp_main"], entry.get("rtsp_sub")))
        for vendor, main_path, sub_path in paths:
            info, used = _rtsp_ok(ip, 554, main_path, user, password)
            if not info:
                continue
            res["rtsp_login"] = "yours" if used else "none"
            res["streams"].append({"token": "main", "path": main_path, "rtsp_port": 554,
                                   "encoding": info["codec"], "width": info["width"],
                                   "height": info["height"], "fps": info["fps"], "gop": info["gop"]})
            if sub_path:
                sinfo, _ = _rtsp_ok(ip, 554, sub_path, user, password)
                if sinfo:
                    res["streams"].append({"token": "sub", "path": sub_path, "rtsp_port": 554,
                                           "encoding": sinfo["codec"], "width": sinfo["width"],
                                           "height": sinfo["height"], "fps": sinfo["fps"], "gop": sinfo["gop"]})
            if vendor != "generic" and res["vendor"] == "generic":
                res["vendor"] = vendor
            break
        if not res["streams"]:
            res["notes"].append("no RTSP stream found (wrong login, or an unusual path)")

    if res["streams"]:
        res["rtsp_port"] = res["streams"][0].get("rtsp_port", 554)
    res["http_port"] = oport if res["ptz_protocol"] == "onvif_soap" else http_port
    if not res["name"]:
        base = res["model"] or res["maker"] or "camera"
        res["name"] = re.sub(r"[^a-z0-9]+", "_", f"{base}_{ip.split('.')[-1]}".lower()).strip("_")
    return res


# ---------------------------------------------------------------------------
# 3. Starting JSON
# ---------------------------------------------------------------------------
def to_camera_json(ident: dict, user: str = "", password: str = "") -> dict:
    """Camera JSON in the edge_worker format from identify(); run the camera test next."""
    data = json.loads(json.dumps(tp.JSON_TEMPLATE))
    # Profiles without a known size (e.g. Dahua's unused third stream) are only used if nothing else is.
    sized = [s for s in ident["streams"] if s.get("width")] or ident["streams"]
    streams = sorted(sized, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0), reverse=True)
    main = streams[0] if streams else None
    smaller = [s for s in streams if main and (s.get("width"), s.get("height")) != (main.get("width"), main.get("height"))]
    sub = smaller[-1] if smaller else main

    cam = data["camera"]
    cam.update({"name": ident["name"], "vendor": "dahua" if ident["vendor"] == "dahua" else "generic",
                "ptz_protocol": ident["ptz_protocol"], "host": ident["ip"], "port": ident["rtsp_port"],
                "http_port": ident["http_port"], "username": user, "password": password})
    if ident.get("ptz_position") is False:        # camera_catalog.json: this model does not report its position
        cam["ptz_position"] = False
    dahua = re.compile(r"/cam/realmonitor\?channel=(\d+)&subtype=(\d+)")
    if main and dahua.search(main["path"]) and dahua.search(sub["path"]):
        mm, sm = dahua.search(main["path"]), dahua.search(sub["path"])
        cam.update({"vendor": "dahua", "channel": int(mm.group(1)),
                    "main_stream_subtype": int(mm.group(2)), "sub_stream_subtype": int(sm.group(2))})
    elif main:
        cam["main_stream_path"] = main["path"]
        cam["sub_stream_path"] = sub["path"]

    enc = data["stream_encoder_properties"]
    if main:
        codec = (main.get("encoding") or "").upper()
        enc["codec"] = {"H264": "H.264", "H265": "H.265", "HEVC": "H.265"}.get(codec, main.get("encoding") or enc["codec"])
        if main.get("width"):
            enc["main_stream_resolution"] = [main["width"], main["height"]]
        if sub and sub.get("width"):
            enc["sub_stream_resolution"] = [sub["width"], sub["height"]]
        if main.get("fps"):
            enc["fps"] = int(round(main["fps"]))
        if main.get("gop"):
            enc["gop_keyframe_interval"] = main["gop"]
        if main.get("bitrate_kbps"):
            enc["bitrate_kbps"] = main["bitrate_kbps"]
    return data


def main():
    ap = argparse.ArgumentParser(description="Find and identify IP cameras")
    ap.add_argument("--subnet", help="e.g. 192.168.8.0/24 (default: this PC's network)")
    ap.add_argument("--no-scan", action="store_true", help="ONVIF discovery only, no port scan")
    ap.add_argument("--identify", metavar="IP", help="identify one camera instead of searching")
    ap.add_argument("--user", default=os.environ.get("CAM_USER", ""))
    ap.add_argument("--password", default=os.environ.get("CAM_PASSWORD", ""))
    ap.add_argument("--write", action="store_true", help="write out/<name>.json for each identified camera")
    a = ap.parse_args()

    targets = [{"ip": a.identify, "ports": [], "onvif_xaddrs": [], "name": None}] if a.identify \
        else discover(a.subnet, scan=not a.no_scan)
    for d in targets:
        if not a.identify:
            tp.log(d["ip"], f"ports {d['ports']} onvif {'yes' if d['onvif_xaddrs'] else 'no'}"
                            + (f" name {d['name']}" if d.get("name") else ""))
        if not (a.identify or a.user or a.write):
            continue
        ident = identify(d["ip"], a.user, a.password, d["ports"], d["onvif_xaddrs"], None)
        streams = "; ".join(f"{s['path']} {s.get('width')}x{s.get('height')} {s.get('encoding')}"
                            for s in ident["streams"]) or "none"
        tp.log(ident["name"], f"{ident['maker']} {ident['model']} vendor {ident['vendor']} ptz {ident['ptz_protocol']} "
                              f"onvif {ident['onvif_port'] or 'no'} ({ident['onvif_login']}) streams {streams}")
        for n in ident["notes"]:
            tp.log(ident["name"], f"note {n}")
        if a.write:
            os.makedirs(tp.OUT_DIR, exist_ok=True)
            path = os.path.join(tp.OUT_DIR, f"{ident['name']}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(to_camera_json(ident, a.user, a.password), f, indent=4)
            tp.log(ident["name"], f"wrote {path}; run the camera test next")


if __name__ == "__main__":
    main()
