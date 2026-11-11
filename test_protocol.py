#!/usr/bin/env python3
"""Unified Diagnostic Probe & PTZ Control Suite for Multi-Camera Setups."""

import argparse
import base64
import datetime
import glob
import hashlib
import json
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

# Cross-platform non-blocking key reader
if sys.platform.startswith("win"):
    import msvcrt

    def get_key(timeout=0.05):
        start = time.time()
        while time.time() - start < timeout:
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b"\x00", b"\xe0"):
                    ch2 = msvcrt.getch()
                    arrow_map = {b"H": "UP", b"P": "DOWN", b"K": "LEFT", b"M": "RIGHT"}
                    return arrow_map.get(ch2, "")
                try:
                    return ch.decode("utf-8")
                except Exception:
                    return ""
            time.sleep(0.01)
        return None
else:
    import termios
    import tty

    def get_key(timeout=0.05):
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            rlist, _, _ = select.select([sys.stdin], [], [], timeout)
            if rlist:
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    seq = sys.stdin.read(2)
                    arrow_map = {"[A": "UP", "[B": "DOWN", "[D": "LEFT", "[C": "RIGHT"}
                    return arrow_map.get(seq, "")
                return ch
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# ============================================================================
# 1. CONFIG LOADER & RESOLVER
# ============================================================================
def resolve_config_path(arg_path: str) -> str:
    """Resolves config path, checking .json automatically if omitted."""
    if os.path.isfile(arg_path):
        return arg_path
    if not arg_path.lower().endswith(".json"):
        candidate = f"{arg_path}.json"
        if os.path.isfile(candidate):
            return candidate
    return arg_path


def load_camera_config(target: str) -> dict:
    """Finds and parses the camera configuration from JSON files."""
    target_clean = target.strip()
    target_lower = target_clean.lower()

    # 1. Direct path or file match (e.g. 'gate.json' or 'gate')
    resolved = resolve_config_path(target_clean)
    if os.path.isfile(resolved):
        with open(resolved, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "camera" in data and isinstance(data["camera"], dict):
            return standardize_config(data["camera"])
        if "cameras" in data and isinstance(data["cameras"], dict):
            return standardize_config(next(iter(data["cameras"].values())))

    # 2. Match base filename in directory
    for f_path in glob.glob("*.json"):
        base_name = os.path.splitext(os.path.basename(f_path))[0].lower()
        if base_name == target_lower:
            with open(f_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "camera" in data and isinstance(data["camera"], dict):
                return standardize_config(data["camera"])

    # 3. Match internal camera name attribute
    for f_path in glob.glob("*.json"):
        try:
            with open(f_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "camera" in data and isinstance(data["camera"], dict):
                if data["camera"].get("name", "").lower() == target_lower:
                    return standardize_config(data["camera"])
        except Exception:
            continue

    print(f"[ERROR] Could not find any configuration file for camera '{target}'.", file=sys.stderr)
    sys.exit(1)


def standardize_config(cam: dict) -> dict:
    http_port = cam.get("http_port", 80)
    vendor = str(cam.get("vendor", "")).lower()
    ptz_protocol = str(cam.get("ptz_protocol") or cam.get("ptz_type", "")).lower()

    if not ptz_protocol:
        ptz_protocol = "dahua_cgi" if vendor == "dahua" else "onvif_soap"

    return {
        "name": cam.get("name", "camera"),
        "vendor": vendor,
        "ptz_protocol": ptz_protocol,
        "host": cam.get("host"),
        "port": cam.get("port", 554),
        "http_port": http_port,
        "username": cam.get("username", "admin"),
        "password": cam.get("password", ""),
        "channel": cam.get("channel", 1),
        "onvif_profile": cam.get("onvif_profile", "Profile_1"),
        "stream_path": cam.get("main_stream_path", f"/cam/realmonitor?channel={cam.get('channel', 1)}&subtype=0"),
        # ws_soap cameras (camera web page sends SOAP over a WebSocket): port and the page's fixed login tokens
        "ws_port": cam.get("ws_port", 12351),
        "ws_userid": cam.get("ws_userid", ""),
        "ws_passwd": cam.get("ws_passwd", ""),
    }


# ============================================================================
# 2. DIAGNOSTIC PROBING ENGINE
# ============================================================================
class CameraDiagnostics:
    """Runs connectivity and protocol probes before initializing the controller."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.host = cfg["host"]
        self.rtsp_port = cfg["port"]
        self.http_port = cfg["http_port"]
        self.results = {
            "ping": False,
            "open_ports": [],
            "rtsp_status": "NOT TESTED",
            "protocol_verified": False,
        }

    def run_preflight_checks(self) -> bool:
        print("\n" + "=" * 68)
        print(f"🔍 DIAGNOSTIC PROBE: {self.cfg['name'].upper()} ({self.host})")
        print("=" * 68)

        # 1. ICMP Ping Probe
        param = "-n" if sys.platform.startswith("win") else "-c"
        try:
            res = subprocess.run(["ping", param, "2", self.host], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.results["ping"] = res.returncode == 0
        except Exception:
            self.results["ping"] = False
        print(f"📡 ICMP Ping               : {'✅ SUCCESS' if self.results['ping'] else '⚠️ NO RESPONSE (May be blocked by firewall)'}")

        # 2. Port Sweep
        candidate_ports = list(dict.fromkeys([self.http_port, self.rtsp_port, 80, 8899, 8000, 34567, 8080]))
        for p in candidate_ports:
            try:
                with socket.create_connection((self.host, p), timeout=1.5):
                    self.results["open_ports"].append(p)
            except Exception:
                pass
        print(f"🔌 Open TCP Ports          : {self.results['open_ports'] if self.results['open_ports'] else '❌ NONE FOUND'}")

        if not self.results["open_ports"]:
            print(f"[ABORT] Cannot reach any TCP service on host {self.host}.", file=sys.stderr)
            return False

        # 3. RTSP Handshake Probe
        if self.rtsp_port in self.results["open_ports"] or 554 in self.results["open_ports"]:
            port = self.rtsp_port if self.rtsp_port in self.results["open_ports"] else 554
            try:
                with socket.create_connection((self.host, port), timeout=2.0) as s:
                    req = f"OPTIONS rtsp://{self.host}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n"
                    s.sendall(req.encode("utf-8"))
                    resp = s.recv(512).decode("utf-8", errors="ignore")
                    if "RTSP/1.0 200" in resp or "Public:" in resp or "RTSP/1.0 401" in resp:
                        self.results["rtsp_status"] = f"ACTIVE (Port {port})"
                    else:
                        self.results["rtsp_status"] = "PORT OPEN (No RTSP banner)"
            except Exception as e:
                self.results["rtsp_status"] = f"FAILED ({e})"
        else:
            self.results["rtsp_status"] = "PORT CLOSED"

        print(f"🎥 RTSP Stream Socket      : {self.results['rtsp_status']}")
        return True


# ============================================================================
# 3. PROTOCOL DRIVERS: DAHUA CGI & ONVIF SOAP
# ============================================================================
class DahuaCGIDriver:
    """Driver for Dahua HTTP CGI endpoint control."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.display_name = cfg.get("name", "Dahua Camera")
        self.host = cfg["host"]
        self.port = cfg.get("http_port", 80)
        self.user = cfg.get("username", "admin")
        self.password = cfg.get("password", "")
        self.channel = cfg.get("channel", 1)
        self.speed = 0.5
        self.ptz_url = f"http://{self.host}:{self.port}/cgi-bin/ptz.cgi"
        self.cfg_url = f"http://{self.host}:{self.port}/cgi-bin/configManager.cgi"
        self.session = requests.Session()
        self.auth = HTTPDigestAuth(self.user, self.password)
        self.last_active_code = None

    def _get(self, url: str, params: dict, timeout=2.5):
        try:
            res = self.session.get(url, params=params, auth=self.auth, timeout=timeout)
            if res.status_code == 401:
                res = self.session.get(url, params=params, auth=HTTPBasicAuth(self.user, self.password), timeout=timeout)
            return res
        except Exception:
            return None

    def read_ptz(self) -> tuple[dict, bool]:
        coords = {"x": 0.0, "y": 0.0, "z": 0.0, "focus": 0.0, "zoom_val": 0.0, "preset": 0}
        is_moving = False
        res = self._get(self.ptz_url, {"action": "getStatus", "channel": self.channel})
        if res and res.status_code == 200:
            for line in res.text.splitlines():
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip().lower(), v.strip()
                if any(m in k for m in ["movestatus", "pantiltstatus", "zoomstatus", "focus.status"]):
                    if v.lower() not in ["idle", "0", "false", "unknown"]:
                        is_moving = True
                try:
                    if "postion[0]" in k or "position[0]" in k:
                        coords["x"] = float(v)
                    elif "postion[1]" in k or "position[1]" in k:
                        coords["y"] = float(v)
                    elif "postion[2]" in k or "position[2]" in k:
                        coords["z"] = float(v)
                    elif "zoomvalue" in k:
                        coords["zoom_val"] = float(v)
                    elif "presetid" in k:
                        coords["preset"] = int(v)
                    elif "focusposition" in k or "focusmapvalue" in k:
                        coords["focus"] = float(v)
                except ValueError:
                    continue
        return coords, is_moving

    def set_ptz(self, direction: str, speed: float = None) -> bool:
        if speed is None:
            speed = self.speed
        direction = direction.lower().strip()
        if direction == "stop":
            return self.stop()

        dahua_codes = {
            "left": "Left", "right": "Right", "up": "Up", "down": "Down",
            "zoom_in": "ZoomTele", "zoom_out": "ZoomWide",
            "focus_near": "FocusNear", "focus_far": "FocusFar",
        }
        code = dahua_codes.get(direction)
        if not code:
            return False

        arg2 = 1 if "focus" in direction else max(1, min(8, int(speed * 8)))
        params = {"action": "start", "channel": self.channel, "code": code, "arg1": 0, "arg2": arg2, "arg3": 0}
        res = self._get(self.ptz_url, params)
        if res and res.status_code == 200:
            self.last_active_code = code
            return True
        return False

    def stop(self) -> bool:
        codes = [self.last_active_code] if self.last_active_code else [
            "Left", "Right", "Up", "Down", "ZoomTele", "ZoomWide", "FocusNear", "FocusFar"
        ]
        stopped = False
        for c in codes:
            if not c:
                continue
            res = self._get(self.ptz_url, {"action": "stop", "channel": self.channel, "code": c, "arg1": 0, "arg2": 0, "arg3": 0})
            if res and res.status_code == 200:
                stopped = True
        self.last_active_code = None
        return stopped

    def goto_preset(self, preset_id: int) -> bool:
        p = {"action": "start", "channel": self.channel, "code": "GotoPreset", "arg1": 0, "arg2": int(preset_id), "arg3": 0}
        res = self._get(self.ptz_url, p)
        return res is not None and res.status_code == 200 and "ok" in res.text.lower()

    def set_preset(self, preset_id: int) -> bool:
        p = {"action": "start", "channel": self.channel, "code": "SetPreset", "arg1": 0, "arg2": int(preset_id), "arg3": 0}
        res = self._get(self.ptz_url, p)
        return res is not None and res.status_code == 200 and "ok" in res.text.lower()

    def trigger_autofocus(self) -> bool:
        self._get(self.cfg_url, {"action": "setConfig", "VideoInFocus[0][0].Mode": 2})
        res = self._get(self.cfg_url, {"action": "setConfig", "VideoInFocus[0][0].Mode": 3})
        return res is not None and res.status_code == 200

    def init_lens(self) -> bool:
        res = self._get(self.ptz_url, {"action": "start", "channel": self.channel, "code": "LensInit", "arg1": 0, "arg2": 0, "arg3": 0})
        return res is not None and res.status_code == 200

    def wait_until_idle(self, timeout: float = 8.0) -> tuple[dict, bool]:
        start = time.time()
        time.sleep(0.35)
        coords, moving = self.read_ptz()
        while moving and (time.time() - start) < timeout:
            time.sleep(0.25)
            coords, moving = self.read_ptz()
        return coords, moving


def soap_body(text: str, limit: int = 700) -> str:
    """The Body of a SOAP answer on one line (the envelope's namespace list left out), for the log."""
    m = re.search(r"<[\w-]*:?Body[^>]*>(.*)</[\w-]*:?Body>", text or "", re.S)
    return re.sub(r"\s+", " ", m.group(1) if m else (text or ""))[:limit].strip()


class ONVIFSoapDriver:
    """Driver for ONVIF SOAP control (APCam / Xiongmai / Goke / NVT).

    Service addresses come from GetCapabilities (e.g. /onvif/Media, /onvif/Ptz) instead of
    being guessed. Login is WS-Security digest; cameras that reject it but allow anonymous
    ONVIF (seen on the NVT JA-A1A) are retried without the header, and that is remembered."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.display_name = cfg.get("name", "ONVIF Camera")
        self.host = cfg["host"]
        self.port = cfg.get("http_port", 8899)
        self.user = cfg.get("username", "admin")
        self.password = cfg.get("password", "")
        self.speed = 0.5
        base = f"http://{self.host}:{self.port}/onvif"
        self.endpoint = f"{base}/device_service"
        self.xaddr = {"device": self.endpoint, "media": self.endpoint, "ptz": self.endpoint}
        self.profile_token = cfg.get("onvif_profile", "Profile_1")
        self.last_error = None   # why the last SOAP call failed, e.g. "400 NotAuthorized"
        self.auth_ok = False     # True once GetProfiles succeeded
        self.auth_mode = "wsse"  # "wsse" or "none" (camera accepts calls without login)
        self.has_ptz = False
        self.pw_type = "digest"  # "digest", or "text" for cameras that only accept PasswordText
        self.clock_offset = self._camera_clock_offset()
        self._discover_services()
        self._discover_profile()

    def _camera_clock_offset(self) -> datetime.timedelta:
        """Camera clock minus PC clock. WS-Security logins carry a timestamp; a camera whose clock is off
        by more than a few seconds rejects a correct password as NotAuthorized. Asked without a login."""
        body = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
                '<s:Body><GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body></s:Envelope>')
        try:
            r = requests.post(self.endpoint, data=body, timeout=2.5,
                              headers={"Content-Type": "application/soap+xml; charset=utf-8"})
            utc = re.search(r"UTCDateTime>(.*?)</[\w]*:?UTCDateTime", r.text, re.S)
            if not utc:
                return datetime.timedelta(0)
            get = lambda tag: int(re.search(rf"<[\w]*:?{tag}>(\d+)<", utc.group(1)).group(1))  # noqa: E731
            cam = datetime.datetime(get("Year"), get("Month"), get("Day"), get("Hour"), get("Minute"),
                                    get("Second"), tzinfo=datetime.timezone.utc)
            off = cam - datetime.datetime.now(datetime.timezone.utc)
            return off if abs(off.total_seconds()) > 5 else datetime.timedelta(0)
        except Exception:
            return datetime.timedelta(0)

    def _build_wsse_header(self) -> str:
        if getattr(self, "pw_type", "digest") == "text":
            return f"""<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
            <wsse:UsernameToken><wsse:Username>{self.user}</wsse:Username>
                <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-1.0.xsd#PasswordText">{self.password}</wsse:Password>
            </wsse:UsernameToken></wsse:Security>"""
        now = datetime.datetime.now(datetime.timezone.utc) + getattr(self, "clock_offset", datetime.timedelta(0))
        created = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        nonce_raw = os.urandom(16)
        nonce_b64 = base64.b64encode(nonce_raw).decode("ascii")
        sha = hashlib.sha1()
        sha.update(nonce_raw + created.encode("utf-8") + self.password.encode("utf-8"))
        digest_b64 = base64.b64encode(sha.digest()).decode("ascii")

        return f"""<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">
            <wsse:UsernameToken>
                <wsse:Username>{self.user}</wsse:Username>
                <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-1.0.xsd#PasswordDigest">{digest_b64}</wsse:Password>
                <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>
                <wsu:Created>{created}</wsu:Created>
            </wsse:UsernameToken>
        </wsse:Security>"""

    def _envelope(self, body_xml: str, with_auth: bool) -> bytes:
        header = f"<s:Header>{self._build_wsse_header()}</s:Header>" if with_auth else ""
        return f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl">
    {header}
    <s:Body>{body_xml}</s:Body>
</s:Envelope>""".encode("utf-8")

    SOAP_NS = {"tptz": "http://www.onvif.org/ver20/ptz/wsdl", "trt": "http://www.onvif.org/ver10/media/wsdl",
               "tds": "http://www.onvif.org/ver10/device/wsdl", "timg": "http://www.onvif.org/ver20/imaging/wsdl"}

    def _post_soap(self, body_xml: str, timeout: float = 2.5, service: str = "ptz"):
        url = self.xaddr.get(service, self.endpoint)
        headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
        op = re.match(r"\s*<(tptz|trt|tds|timg):(\w+)", body_xml)
        if op:          # the operation's name (SOAP 1.2 action); some cameras need it to find the operation
            headers["Content-Type"] += f'; action="{self.SOAP_NS[op.group(1)]}/{op.group(2)}"'
        self.last_answer = None
        # login digest -> login text (only until one works) -> no login
        modes = [(True, self.pw_type)] if self.auth_mode == "wsse" else []
        if self.auth_mode == "wsse" and not self.auth_ok and self.pw_type == "digest" and self.password:
            modes.append((True, "text"))
        modes.append((False, None))
        kept_type = self.pw_type
        for with_auth, pw_type in modes:
            self.pw_type = pw_type or kept_type
            try:
                res = requests.post(url, data=self._envelope(body_xml, with_auth), headers=headers, timeout=timeout)
            except Exception as e:
                self.last_error = type(e).__name__
                return None
            if res.status_code == 200:
                self.last_error = None
                if not with_auth:
                    self.pw_type = kept_type
                if not with_auth and self.auth_mode != "none":
                    self.auth_mode = "none"   # login rejected but anonymous works: stop sending it
                return res
            fault = re.findall(r"<[\w-]*:?Value>\s*[\w-]+:(\w+)\s*</", res.text)
            self.last_error = f"{res.status_code} {fault[-1] if fault else ''}".strip()
            self.last_answer = soap_body(res.text)
            if "NotAuthorized" not in res.text:
                break                         # a real error, not a login problem
        self.pw_type = kept_type
        return None

    def _discover_services(self):
        """Reads Media and PTZ service addresses (GetCapabilities is usually allowed without login)."""
        body = "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>"
        res = self._post_soap(body, service="device")
        if not res:
            return
        for svc, tag in (("media", "Media"), ("ptz", "PTZ"), ("imaging", "Imaging")):
            m = re.search(rf"<[\w]*:?{tag}>\s*<[\w]*:?XAddr>([^<]+)<", res.text)
            if m:
                self.xaddr[svc] = m.group(1).strip()
        self.has_ptz = bool(re.search(r"<[\w]*:?PTZ>\s*<[\w]*:?XAddr>", res.text))

    def _discover_profile(self):
        res = self._post_soap("<trt:GetProfiles/>", service="media")
        if res and res.status_code == 200:
            self.auth_ok = True
            # Profile tokens only (not the nested configuration tokens); prefer one with PTZ.
            profiles = re.findall(r'<[\w]*:?Profiles[^>]*token="([^"]+)"(.*?)</[\w]*:?Profiles>', res.text, re.S)
            with_ptz = [t for t, inner in profiles if "PTZConfiguration" in inner]
            tokens = with_ptz or [t for t, _ in profiles]
            if tokens:
                self.profile_token = tokens[0]
                return
        self.profile_token = self.cfg.get("onvif_profile", "Profile_1")

    def read_ptz(self) -> tuple[dict, bool]:
        coords = {"x": 0.0, "y": 0.0, "z": 0.0, "focus": 0.0, "zoom_val": 0.0, "preset": 0}
        is_moving = False
        body = f"<tptz:GetStatus><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken></tptz:GetStatus>"
        res = self._post_soap(body, timeout=1.5)
        self.status_raw = None
        coords["reported"] = False                 # until the camera's answer gives a position
        if res and res.status_code == 200:
            # Read with patterns, not an XML parser: cheap cameras send prefixes they never declare.
            text = res.text
            pos = re.search(r"<[\w]*:?Position\b.*?</[\w]*:?Position>", text, re.S)
            part = pos.group(0) if pos else text
            m = re.search(r'<[\w]*:?PanTilt\b[^>]*?\bx="([-\d.eE+]+)"[^>]*?\by="([-\d.eE+]+)"', part)
            if m:
                coords["x"], coords["y"] = float(m.group(1)), float(m.group(2))
            mz = re.search(r'<[\w]*:?Zoom\b[^>]*?\bx="([-\d.eE+]+)"', part)
            if mz:
                coords["z"] = float(mz.group(1))
            coords["reported"] = bool(m or mz)
            if not coords["reported"] or not any((coords["x"], coords["y"], coords["z"])):
                self.status_raw = soap_body(text)                       # for the log: what the camera sent
            mv = re.search(r"<[\w]*:?MoveStatus>(.*?)</[\w]*:?MoveStatus>", text, re.S)
            if mv and "moving" in mv.group(1).lower():
                is_moving = True
        return coords, is_moving

    def set_ptz(self, direction: str, speed: float = None) -> bool:
        if speed is None:
            speed = self.speed
        direction = direction.lower().strip()
        if direction == "stop":
            return self.stop()

        moves = {"left": ("pt", -speed, 0.0), "right": ("pt", speed, 0.0),
                 "up": ("pt", 0.0, speed), "down": ("pt", 0.0, -speed),
                 "zoom_in": ("z", speed, 0.0), "zoom_out": ("z", -speed, 0.0)}
        if direction not in moves:
            self.last_error = f"{direction} not supported over ONVIF"
            return False
        kind, a, b = moves[direction]
        vel = f'<tt:PanTilt x="{a}" y="{b}"/>' if kind == "pt" else f'<tt:Zoom x="{a}"/>'
        body = f"""<tptz:ContinuousMove>
            <tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>
            <tptz:Velocity>{vel}</tptz:Velocity>
        </tptz:ContinuousMove>"""
        res = self._post_soap(body)
        return res is not None and res.status_code == 200

    def stop(self) -> bool:
        body = f"""<tptz:Stop>
            <tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>
            <tptz:PanTilt>true</tptz:PanTilt>
            <tptz:Zoom>true</tptz:Zoom>
        </tptz:Stop>"""
        res = self._post_soap(body, timeout=1.5)
        return res is not None and res.status_code == 200

    def goto_preset(self, preset_id: int) -> bool:
        body = f"""<tptz:GotoPreset>
            <tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>
            <tptz:PresetToken>{preset_id}</tptz:PresetToken>
        </tptz:GotoPreset>"""
        res = self._post_soap(body)
        return res is not None and res.status_code == 200

    def set_preset(self, preset_id, new_name: str | None = None) -> bool:
        """Stores the current position under preset token `preset_id`. Cameras that refuse a token they
        did not make get a new preset instead (no token sent); the camera's token is then in
        self.last_preset_token."""
        self.last_preset_token = None
        body = f"""<tptz:SetPreset>
            <tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>
            <tptz:PresetName>{new_name or f"Preset_{preset_id}"}</tptz:PresetName>
            <tptz:PresetToken>{preset_id}</tptz:PresetToken>
        </tptz:SetPreset>"""
        res = self._post_soap(body)
        if res is not None and res.status_code == 200:
            m = re.search(r"<[\w]*:?PresetToken>([^<]+)<", res.text)
            self.last_preset_token = m.group(1).strip() if m else str(preset_id)
            return True
        first_error = self.last_error
        body = f"""<tptz:SetPreset>
            <tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken>
            <tptz:PresetName>{new_name or f"Preset_{preset_id}"}</tptz:PresetName>
        </tptz:SetPreset>"""
        res = self._post_soap(body)
        if res is not None and res.status_code == 200:
            m = re.search(r"<[\w]*:?PresetToken>([^<]+)<", res.text)
            if m:
                self.last_preset_token = m.group(1).strip()
                return True
        self.last_error = first_error or self.last_error
        self.preset_refusal = self.last_answer
        lst = self._post_soap(f"<tptz:GetPresets><tptz:ProfileToken>{self.profile_token}</tptz:ProfileToken></tptz:GetPresets>")
        self.presets_answer = soap_body(lst.text) if lst is not None else f"GetPresets: {self.last_error}"
        return False

    def trigger_autofocus(self) -> bool:
        return False

    def init_lens(self) -> bool:
        return False

    def wait_until_idle(self, timeout: float = 8.0) -> tuple[dict, bool]:
        start = time.time()
        time.sleep(0.35)
        coords, moving = self.read_ptz()
        while moving and (time.time() - start) < timeout:
            time.sleep(0.25)
            coords, moving = self.read_ptz()
        return coords, moving


class WebSocketSoapDriver:
    """PTZ for cameras whose own web page sends SOAP over a WebSocket (ws://<ip>:12351/), e.g. the
    FLP Gallery camera. Commands were read from the camera page's traffic; ws_userid / ws_passwd are
    the fixed tokens that page sends with every command.

    Captured: zoomwide, zoomtele, stop, FocusNear/FarAutoOff, IrisOpen/CloseAutoOff,
    callpreset, setpreset (flag 1), clearpreset. Pan / tilt not captured yet."""

    COMMANDS = {"zoom_in": "zoomtele", "zoom_out": "zoomwide",
                "focus_near": "FocusNearAutoOff", "focus_far": "FocusFarAutoOff",
                "iris_open": "IrisOpenAutoOff", "iris_close": "IrisCloseAutoOff"}

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.display_name = cfg.get("name", "WS SOAP Camera")
        self.host = cfg["host"]
        self.port = int(cfg.get("ws_port") or 12351)
        self.userid = cfg.get("ws_userid", "")
        self.passwd = cfg.get("ws_passwd", "")
        self.speed = 0.5
        self.last_error = None
        self.last_answer = None
        self._sock = None
        self._lock = threading.Lock()

    # --- minimal WebSocket client (RFC 6455: text frames, client side masked) ---
    def _connect(self):
        import socket
        s = socket.create_connection((self.host, self.port), timeout=3)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                   f"Origin: http://{self.host}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = s.recv(1024)
            if not chunk:
                break
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode(errors="ignore")
        if " 101" not in status:
            s.close()
            raise ConnectionError(f"WebSocket refused: {status or 'no answer'}")
        self._sock = s

    def _send_frame(self, text: str):
        data = text.encode("utf-8")
        n = len(data)
        head = bytearray([0x81])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        self._sock.sendall(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read_frame(self, timeout: float):
        """One reply frame as text, or None if the camera sends nothing within timeout."""
        import socket
        self._sock.settimeout(timeout)
        try:
            h = self._sock.recv(2)
            if len(h) < 2:
                return None
            n = h[1] & 0x7F
            if n == 126:
                n = int.from_bytes(self._sock.recv(2), "big")
            elif n == 127:
                n = int.from_bytes(self._sock.recv(8), "big")
            body = b""
            while len(body) < n:
                chunk = self._sock.recv(n - len(body))
                if not chunk:
                    break
                body += chunk
            return body.decode("utf-8", errors="ignore")
        except socket.timeout:
            return None
        finally:
            self._sock.settimeout(3)

    def _link_alive(self) -> bool:
        """False when the camera has closed the connection (an idle link is often dropped). Without this
        check the next command would go into the dead socket and be lost while looking sent."""
        try:
            readable, _, _ = select.select([self._sock], [], [], 0)
            if not readable:
                return True
            if self._sock.recv(1, socket.MSG_PEEK) == b"":
                return False
            self._read_frame(0.1)          # an unread message from the camera: take it off the line
            return True
        except OSError:
            return False

    def _command(self, inner: str) -> bool:
        """Sends <xml>inner</xml> exactly like the camera page does. Reconnects once if the link dropped."""
        msg = ('<?xml version="1.0"?><soap:Envelope xmlns:soap="http://www.w3.org/2001/12/soap-envelope">'
               f"<soap:Header>\t<userid>{self.userid}</userid>\t<passwd>{self.passwd}</passwd></soap:Header>"
               f"<soap:Body><xml>{inner}</xml></soap:Body></soap:Envelope>")
        with self._lock:
            for attempt in range(2):
                try:
                    if self._sock is not None and not self._link_alive():
                        self._sock.close()
                        self._sock = None
                    if self._sock is None:
                        self._connect()
                    self._send_frame(msg)
                    self.last_answer = self._read_frame(0.3)   # the camera may or may not answer
                    self.last_error = None
                    return True
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock = None
            return False

    # --- same interface as the other drivers ---
    def read_ptz(self) -> tuple[dict, bool]:
        # position reading is not known for this camera
        return {"x": 0.0, "y": 0.0, "z": 0.0, "focus": 0.0, "zoom_val": 0.0, "preset": 0}, False

    def set_ptz(self, direction: str, speed: float = None) -> bool:
        direction = direction.lower().strip()
        if direction == "stop":
            return self.stop()
        cmd = self.COMMANDS.get(direction)
        if not cmd:
            self.last_answer = None
            self.last_error = f"'{direction}' not known for this camera yet (pan / tilt command not captured)"
            return False
        return self._command(f"<cmd>{cmd}</cmd>")

    def stop(self) -> bool:
        return self._command("<cmd>stop</cmd>")

    def goto_preset(self, preset_id: int) -> bool:
        return self._command(f"<cmd>callpreset</cmd><preset>{int(preset_id)}</preset>")

    def set_preset(self, preset_id: int) -> bool:
        # flag 1 exactly as the camera page sends it (meaning not known)
        return self._command(f"<cmd>setpreset</cmd><preset>{int(preset_id)}</preset><flag>1</flag>")

    def clear_preset(self, preset_id: int) -> bool:
        return self._command(f"<cmd>clearpreset</cmd><preset>{int(preset_id)}</preset>")

    def trigger_autofocus(self) -> bool:
        self.last_error = "autofocus command not captured yet"
        return False

    def init_lens(self) -> bool:
        self.last_error = "lens init command not captured yet"
        return False

    def wait_until_idle(self, timeout: float = 8.0) -> tuple[dict, bool]:
        time.sleep(min(timeout, 3.0))       # no position reading: give the move time to finish
        return self.read_ptz()


def build_driver(cam_cfg: dict):
    """Instantiates driver based on the verified camera configuration.
    Returns None when the camera has no PTZ (ptz_protocol 'none')."""
    proto = cam_cfg["ptz_protocol"]
    if proto in ("none", "off", "no"):
        return None
    if "dahua" in proto or proto == "dahua_cgi":
        return DahuaCGIDriver(cam_cfg)
    if proto == "ws_soap":
        return WebSocketSoapDriver(cam_cfg)
    return ONVIFSoapDriver(cam_cfg)


# ============================================================================
# 3b. CAMERA TEST -> EDGE_WORKER CAMERA JSON  (--test)
# ============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SCRIPT_DIR, "out")

# RTSP paths tried on non-Dahua cameras (after the ones already in the camera JSON).
GENERIC_RTSP_PATHS = ["/live/ch0", "/live/ch1", "/onvif1", "/onvif2", "/11", "/12",
                      "/stream0", "/stream1", "/h264/ch1/main/av_stream", "/h264/ch1/sub/av_stream"]

# Used when no existing camera JSON is given (same layout as edge_worker/livingroom.json).
JSON_TEMPLATE = {
    "camera": {"name": "", "site": "", "vendor": "generic", "ptz_protocol": "none", "host": "", "port": 554,
               "http_port": 80, "username": "admin", "password": "",
               "flip_stream": False, "ptz_markers": {}},
    "connection": {"rtsp_transport": "tcp", "buffer_size": 1},
    "sensor_hardware_properties": {
        "brightness": 50, "contrast": 50, "saturation": 50, "sharpness": 40,
        "shutter_speed_sec": "1/100", "gain": 12, "wdr_enabled": True, "wdr_level": 3,
        "noise_reduction_3d": 50, "day_night_mode": "auto", "white_balance_mode": "auto"},
    "stream_encoder_properties": {
        "codec": "H.264", "smart_codec": False, "sub_stream_resolution": [640, 480],
        "main_stream_resolution": [1920, 1080], "fps": 20, "gop_keyframe_interval": 20,
        "bitrate_type": "CBR", "bitrate_kbps": 2048},
    "motion": {
        "enabled": True, "heartbeat_interval_sec": 0.0, "grid_rows": 6, "grid_cols": 8,
        "motion_threshold_min": 1800, "max_lighting_ratio": 0.6, "initial_snap_delay_sec": 1.0,
        "diff_multiplier_threshold": 10.0, "post_roll_buffer_sec": 3.0, "cooldown_period_sec": 10.0,
        "bg_scan_frame_step": 2, "draw_motion_box": True, "exclude_zones": [], "use_roi": False,
        "roi_crop": [0.2, 0.2, 0.8, 0.8], "min_sharpness": 100, "min_brightness": 50,
        "max_brightness": 220},
    "face_quality": {"enabled": True, "min_face_width": 120, "min_face_height": 120},
    # On-screen display burned into the video by the camera (osd.py sets it; multy.py does not read it).
    # title = camera ID (site_camera, osd.py); positions stay the camera's while use_camera_positions
    "osd": {"show_date": True, "date_format": "dd-MM-yyyy HH:mm:ss", "date_position": [0.02, 0.03],
            "show_name": True, "name_position": [0.5, 0.03], "use_camera_positions": True,
            "other_texts_off": False,
            # our own text (not the camera's): optional, off by default; white, own positions and size
            "own_overlay": False, "own_date": True, "own_title": True,
            "overlay_date_position": [0.02, 0.92], "overlay_name_position": [0.5, 0.92],
            "overlay_text_size": 0.04},
}
SECTION_ORDER = ["camera", "connection", "sensor_hardware_properties",
                 "stream_encoder_properties", "motion", "face_quality", "osd"]


def log(name: str, msg: str):
    print(f"{name} {time.strftime('%H:%M:%S')} {msg}", flush=True)


def load_full_json(target: str):
    """Returns (path, full camera JSON) for the same lookups load_camera_config does, or (None, None)."""
    candidates = [resolve_config_path(target.strip())]
    candidates += [p for p in glob.glob("*.json")
                   if os.path.splitext(os.path.basename(p))[0].lower() == target.strip().lower()]
    candidates += glob.glob("*.json")
    for p in candidates:
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        cam = data.get("camera") if isinstance(data, dict) else None
        if not isinstance(cam, dict):
            continue
        if p == candidates[0] or os.path.splitext(os.path.basename(p))[0].lower() == target.lower() \
                or str(cam.get("name", "")).lower() == target.lower():
            return p, data
    return None, None


def rtsp_url(cfg: dict, path: str) -> str:
    user = urllib.parse.quote(cfg.get("username") or "", safe="")
    pw = urllib.parse.quote(cfg.get("password") or "", safe="")
    auth = f"{user}:{pw}@" if user else ""
    return f"rtsp://{auth}{cfg['host']}:{cfg['port']}{path}"


def mask(text: str, cfg: dict) -> str:
    """Hides the password (plain and URL-encoded) in tool messages."""
    pw = cfg.get("password") or ""
    for v in {pw, urllib.parse.quote(pw, safe="")}:
        if v:
            text = text.replace(v, "***")
    return text


def ffprobe_stream(url: str, seconds: int = 13, timeout: int = 30):
    """Reads ~seconds of video; returns codec, width, height, fps, gop (frames between keyframes)."""
    cmd = ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "5000000", "-select_streams", "v:0",
           "-read_intervals", f"%+{seconds}",
           "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate:frame=key_frame",
           "-of", "json", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return None, type(e).__name__
    if r.returncode != 0 or not r.stdout.strip():
        return None, (r.stderr.strip().splitlines() or ["no output"])[-1][:120]
    d = json.loads(r.stdout)
    if not d.get("streams"):
        return None, "no video stream"
    s = d["streams"][0]
    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = (s.get(key) or "0/0").partition("/")
        if float(den or 0):
            fps = round(float(num) / float(den), 2)
            break
    keys = [i for i, fr in enumerate(d.get("frames", [])) if fr.get("key_frame") == 1]
    gops = [b - a for a, b in zip(keys, keys[1:])]
    return {"codec": s.get("codec_name"), "width": s.get("width"), "height": s.get("height"),
            "fps": fps, "gop": max(gops) if gops else None,
            "frames_read": len(d.get("frames", []))}, None


def onvif_stream_paths(cfg: dict) -> list:
    """RTSP paths the camera itself reports over ONVIF (GetProfiles + GetStreamUri). [] if no ONVIF."""
    try:
        drv = ONVIFSoapDriver({**cfg, "ptz_protocol": "onvif_soap"})
    except Exception:
        return []
    if not drv.auth_ok:
        return []
    res = drv._post_soap("<trt:GetProfiles/>", service="media")
    tokens = re.findall(r'<[\w]*:?Profiles[^>]*token="([^"]+)"', res.text) if res is not None else []
    paths = []
    for tok in tokens:
        body = ("<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
                "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
                f"<trt:ProfileToken>{tok}</trt:ProfileToken></trt:GetStreamUri>")
        r = drv._post_soap(body, service="media")
        m = re.search(r"<[\w]*:?Uri>([^<]+)<", r.text) if r is not None else None
        if m:
            u = urllib.parse.urlparse(m.group(1).replace("&amp;", "&"))
            p = u.path + (f"?{u.query}" if u.query else "")
            if p not in paths:
                paths.append(p)
    return paths


def stream_candidates(cfg: dict, raw_cam: dict, extended: bool = False):
    """(label, path, dahua_subtype) tuples to try. Cheap cameras stop answering for a while
    after several bad paths, so the long generic list is only used when nothing else worked."""
    ch = cfg.get("channel", 1)
    if cfg["vendor"] == "dahua" or cfg["ptz_protocol"] == "dahua_cgi":
        return [(f"subtype{st}", f"/cam/realmonitor?channel={ch}&subtype={st}", st) for st in (0, 1, 2)]
    # Configured paths first, then what the camera reports over ONVIF, then common guesses.
    onvif = [] if extended else onvif_stream_paths(cfg)
    paths = [raw_cam.get("main_stream_path"), raw_cam.get("sub_stream_path")] + onvif + \
        (GENERIC_RTSP_PATHS if extended else ["/live/ch0", "/live/ch1"])
    seen, out = set(), []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append((p, p, None))
    return out


def test_streams(cfg: dict, raw_cam: dict, report: dict):
    name = cfg["name"]
    found = []
    candidates = stream_candidates(cfg, raw_cam)
    for i, (label, path, subtype) in enumerate(candidates):
        if i:
            time.sleep(1)   # cheap cameras keep a closed stream counted for a moment
        info, err = ffprobe_stream(rtsp_url(cfg, path))
        if not info:
            log(name, f"stream {label} none ({mask(err, cfg).split(': ')[-1]})")
            continue
        info.update({"path": path, "subtype": subtype})
        found.append(info)
        log(name, f"stream {label} {info['width']}x{info['height']} {info['codec']} "
                  f"{info['fps']}fps gop{info['gop']}")
    if not found and subtype is None:
        tried = {c[1] for c in candidates}
        for label, path, subtype in stream_candidates(cfg, raw_cam, extended=True):
            if path in tried:
                continue
            info, err = ffprobe_stream(rtsp_url(cfg, path))
            if not info:
                log(name, f"stream {label} none ({mask(err, cfg).split(': ')[-1]})")
                continue
            info.update({"path": path, "subtype": None})
            found.append(info)
            log(name, f"stream {label} {info['width']}x{info['height']} {info['codec']} "
                      f"{info['fps']}fps gop{info['gop']}")
    report["streams"] = found
    if not found:
        return None, None
    # Main = largest picture. Sub = smallest picture that differs from main, else main again.
    by_size = sorted(found, key=lambda s: (s["width"] or 0) * (s["height"] or 0), reverse=True)
    main = by_size[0]
    smaller = [s for s in by_size if (s["width"], s["height"]) != (main["width"], main["height"])]
    sub = smaller[-1] if smaller else main
    log(name, f"pick main {main['path']} sub {sub['path']}")
    return main, sub


def read_dahua_encoder(cfg: dict, report: dict):
    """Reads Encode config via CGI. Returns {'main': {...}, 'sub': {...}} or {}."""
    drv = DahuaCGIDriver(cfg)
    res = drv._get(drv.cfg_url, {"action": "getConfig", "name": "Encode"}, timeout=4)
    if not res or res.status_code != 200:
        report["encoder_error"] = f"getConfig Encode -> {res.status_code if res else 'no response'}"
        return {}
    idx = cfg.get("channel", 1) - 1
    out = {"main": {}, "sub": {}}
    pat = re.compile(rf"table\.Encode\[{idx}\]\.(MainFormat|ExtraFormat)\[0\]\.Video\.(\w+)=(.*)")
    for line in res.text.splitlines():
        m = pat.match(line.strip())
        if m:
            out["main" if m.group(1) == "MainFormat" else "sub"][m.group(2)] = m.group(3).strip()
    report["encoder"] = out
    return out


def copy_test(cfg: dict, path: str, seconds: int, report: dict) -> bool:
    """Records with -c copy like multy.py, then checks the file's length and keyframes."""
    name = cfg["name"]
    os.makedirs(OUT_DIR, exist_ok=True)
    out_file = os.path.join(OUT_DIR, f"{name}_copytest.mp4")
    cmd = ["ffmpeg", "-v", "error", "-y", "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", rtsp_url(cfg, path),
           "-t", str(seconds), "-map", "0:v:0", "-c", "copy", out_file]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 30)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        report["copy_test"] = {"ok": False, "error": type(e).__name__}
        log(name, f"copytest FAIL {type(e).__name__}")
        return False
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "packet=flags", "-of", "csv=p=0", out_file],
                           capture_output=True, text=True) if os.path.isfile(out_file) else None
    flags = probe.stdout.split() if probe and probe.returncode == 0 else []
    dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "csv=p=0", out_file], capture_output=True, text=True).stdout.strip() \
        if os.path.isfile(out_file) else ""
    duration = float(dur) if dur.replace(".", "", 1).isdigit() else 0.0
    keyframes = sum(1 for f in flags if f.startswith("K"))
    ok = r.returncode == 0 and duration >= seconds * 0.8 and keyframes >= 1
    report["copy_test"] = {"ok": ok, "seconds": seconds, "duration": round(duration, 2),
                           "packets": len(flags), "keyframes": keyframes,
                           "ffmpeg_error": mask((r.stderr.strip().splitlines() or [""])[-1], cfg)[:160]}
    log(name, f"copytest {'OK' if ok else 'FAIL'} {duration:.1f}s {len(flags)} frames {keyframes} keyframes")
    return ok


def test_control(cfg: dict, report: dict) -> str:
    """Checks the PTZ/config login. Returns the ptz_protocol to write."""
    name = cfg["name"]
    proto = cfg["ptz_protocol"]
    if proto in ("none", "off", "no"):
        # Still look: cheap cameras often have ONVIF PTZ even when the JSON says none.
        found = None
        if cfg["vendor"] != "dahua":
            try:
                probe = ONVIFSoapDriver({**cfg, "ptz_protocol": "onvif_soap"})
                if probe.auth_ok and probe.has_ptz:
                    found = probe
            except Exception:
                pass
        if found:
            log(name, f"ptz none in JSON, but camera has ONVIF PTZ (profile {found.profile_token}, "
                      f"login {found.auth_mode}); set ptz_protocol to onvif_soap to use it")
            report["ptz"] = {"protocol": "none", "onvif_ptz_available": True,
                             "profile": found.profile_token, "auth_mode": found.auth_mode}
        else:
            log(name, "ptz none (from camera JSON)")
            report["ptz"] = {"protocol": "none"}
        return "none"
    drv = build_driver(cfg)
    if isinstance(drv, DahuaCGIDriver):
        res = drv._get(drv.ptz_url, {"action": "getStatus", "channel": drv.channel})
        ok = bool(res and res.status_code == 200)
        coords, _ = drv.read_ptz() if ok else ({}, False)
        report["ptz"] = {"protocol": "dahua_cgi", "ok": ok, "status": coords}
        log(name, f"ptz dahua_cgi {'OK' if ok else 'FAIL'}" + (f" pan {coords['x']} tilt {coords['y']}" if ok else ""))
        return "dahua_cgi" if ok else proto
    ok = drv.auth_ok and drv.has_ptz
    report["ptz"] = {"protocol": "onvif_soap", "ok": ok, "profile": drv.profile_token,
                     "auth_mode": drv.auth_mode, "services": drv.xaddr, "error": drv.last_error}
    if ok:
        note = " (camera rejects the login but allows ONVIF without one)" if drv.auth_mode == "none" else ""
        log(name, f"ptz onvif OK profile {drv.profile_token}{note}")
    else:
        why = "no PTZ service" if drv.auth_ok else str(drv.last_error)
        log(name, f"ptz onvif FAIL {why}")
    return proto


def build_camera_json(cfg, base, ptz_protocol, main, sub, encoder, copy_ok, report):
    data = json.loads(json.dumps(base or JSON_TEMPLATE))  # deep copy
    for sec in SECTION_ORDER:
        data.setdefault(sec, json.loads(json.dumps(JSON_TEMPLATE[sec])))
    cam = data["camera"]
    cam.update({"name": cfg["name"], "vendor": cfg["vendor"] or cam.get("vendor", "generic"),
                "ptz_protocol": ptz_protocol, "host": cfg["host"], "port": cfg["port"],
                "http_port": cfg["http_port"], "username": cfg["username"],
                "password": cfg["password"]})
    if "invert_stream" in cam:                      # old name of flip_stream
        cam.setdefault("flip_stream", bool(cam.pop("invert_stream")))
    cam.setdefault("flip_stream", False)
    if main and main.get("subtype") is not None:
        cam["channel"] = cfg.get("channel", 1)
        cam["main_stream_subtype"] = main["subtype"]
        cam["sub_stream_subtype"] = sub["subtype"]
        cam.pop("main_stream_path", None)
        cam.pop("sub_stream_path", None)
    elif main:
        cam["main_stream_path"] = main["path"]
        cam["sub_stream_path"] = sub["path"]
    if copy_ok:
        data["connection"]["rtsp_transport"] = "tcp"

    enc = data["stream_encoder_properties"]
    if main:
        codec = {"h264": "H.264", "hevc": "H.265"}.get(main["codec"], main["codec"])
        enc.update({"codec": codec, "main_stream_resolution": [main["width"], main["height"]],
                    "sub_stream_resolution": [sub["width"], sub["height"]],
                    "fps": int(round(main["fps"])) if main["fps"] else enc.get("fps")})
        if main["gop"]:
            enc["gop_keyframe_interval"] = main["gop"]
    m = (encoder or {}).get("main", {})
    if m.get("BitRate", "").isdigit():
        enc["bitrate_kbps"] = int(m["BitRate"])
    if m.get("BitRateControl"):
        enc["bitrate_type"] = m["BitRateControl"]

    # Problems multy.py would hit.
    issues = []
    if not main:
        issues.append("no working RTSP stream")
    else:
        if main["codec"] != "h264":
            issues.append(f"main stream is {main['codec']}, the edge needs H.264")
        if sub["codec"] != "h264":
            issues.append(f"sub stream is {sub['codec']}, the edge needs H.264")
        if main["gop"] and main["fps"] and main["gop"] > 2 * main["fps"]:
            issues.append(f"keyframe every {main['gop']} frames ({main['gop'] / main['fps']:.1f}s); "
                          f"set GOP to {int(round(main['fps']))} on the camera")
        if sub is main:
            issues.append("only one stream size found; sub stream = main stream")
    if copy_ok is False:
        issues.append("recording check failed (10 s recorded the way the edge does)")
    if ptz_protocol not in ("none",) and not report.get("ptz", {}).get("ok"):
        issues.append(f"{ptz_protocol} login failed (check the ONVIF/HTTP user and password)")
    report["issues"] = issues
    return {sec: data[sec] for sec in SECTION_ORDER} | {k: v for k, v in data.items() if k not in SECTION_ORDER}


LAST_REPORT = None   # the last test's report (for the web page)


def report_text(report: dict) -> str:
    """The test result as plain text (<camera>.txt next to the camera JSON; only the latest is kept)."""
    out = [f"Camera test {report.get('name', '')} {report.get('host', '')}  {report.get('time', '')}", ""]
    for s_ in report.get("streams", []) or []:
        out.append(f"stream {s_.get('path')}: {s_.get('width')}x{s_.get('height')} {s_.get('codec')} "
                   f"{s_.get('fps')} fps, keyframe every {s_.get('gop', '?')}")
    ct = report.get("copy_test")
    if ct:
        out.append(f"recording check: {'OK' if ct.get('ok') else 'FAILED'} {ct.get('duration')} s, {ct.get('keyframes')} keyframes")
    issues = report.get("issues") or []
    out += ["", "Issues:" if issues else "No issues: ready for the edge."] + [f"  - {i}" for i in issues]
    return "\n".join(out) + "\n"


def run_camera_test(target: str, copy_seconds: int) -> int:
    path, base = load_full_json(target)
    cfg = load_camera_config(target)
    name = cfg["name"]
    log(name, f"test start {cfg['host']} (config {path or 'none, using template'})")
    report = {"name": name, "host": cfg["host"], "time": time.strftime("%Y-%m-%d %H:%M:%S"),
              "source_config": path}

    prober = CameraDiagnostics(cfg)
    if not prober.run_preflight_checks():
        log(name, "test stop: camera not reachable")
        return 1
    report["reach"] = prober.results

    ptz_protocol = test_control(cfg, report)
    main, sub = test_streams(cfg, (base or {}).get("camera", {}), report)
    encoder = read_dahua_encoder(cfg, report) if ptz_protocol == "dahua_cgi" else {}
    if encoder.get("main"):
        e = encoder["main"]
        log(name, f"encoder main {e.get('Compression')} {e.get('Width')}x{e.get('Height')} "
                  f"{e.get('FPS')}fps gop{e.get('GOP')} {e.get('BitRateControl')} {e.get('BitRate')}kbps")
    copy_ok = None if copy_seconds <= 0 else False   # None = skipped, not failed
    if main and copy_seconds > 0:
        copy_ok = copy_test(cfg, main["path"], copy_seconds, report)
        if not copy_ok:
            log(name, "copytest retry in 5s")
            time.sleep(5)
            copy_ok = copy_test(cfg, main["path"], copy_seconds, report)

    data = build_camera_json(cfg, base, ptz_protocol, main, sub, encoder, copy_ok, report)
    os.makedirs(OUT_DIR, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0] if path else name
    json_path = os.path.join(OUT_DIR, f"{stem}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    with open(os.path.join(OUT_DIR, f"{stem}.txt"), "w", encoding="utf-8") as f:   # overwritten by each test
        f.write(report_text(report))
    global LAST_REPORT
    LAST_REPORT = report
    for issue in report["issues"]:
        log(name, f"ISSUE {issue}")
    log(name, f"wrote {json_path} ({'ready' if not report['issues'] else str(len(report['issues'])) + ' issues'})")
    return 0 if not report["issues"] else 2


# ============================================================================
# 4. INTERACTIVE KEYPAD CONTROLLER LOOP
# ============================================================================
def run_interactive_controller(driver):
    proto = "DAHUA CGI" if isinstance(driver, DahuaCGIDriver) else "ONVIF SOAP"
    print("\n" + "=" * 68)
    print(f"🎮 PTZ CONTROLLER ACTIVE: {driver.display_name.upper()} [{proto}]")
    print(f"📡 Target: {driver.host}:{driver.port}")
    print("=" * 68)
    print("  PAN / TILT:")
    print("    [8] or [W] or [↑]   : Tilt UP")
    print("    [2] or [S] or [↓]   : Tilt DOWN")
    print("    [4] or [A] or [←]   : Pan LEFT")
    print("    [6] or [D] or [→]   : Pan RIGHT")
    print("  ZOOM:")
    print("    [7] or [U] or [+]   : Zoom IN (Tele)")
    print("    [9] or [O] or [-]   : Zoom OUT (Wide)")
    print("  PRESETS:")
    print("    [G] or [P]          : GOTO Preset Number")
    print("    [M]                 : MEMORIZE/SAVE Current Position as Preset")
    print("  FOCUS & LENS:")
    print("    [J] / [K]           : Focus NEAR / Focus FAR")
    print("    [T]                 : Reset AUTO-FOCUS")
    print("    [L]                 : Calibrate LENS (LensInit)")
    print("  GENERAL:")
    print("    [5] or [Space]      : STOP Motor")
    print("    [R]                 : Read Live Coordinates")
    print("    [1] / [2] / [3]     : Speed (Slow 0.2 / Med 0.5 / Fast 0.8)")
    print("    [Q]                 : Quit Controller")
    print("=" * 68)

    coords, moving = driver.read_ptz()
    print(f"📍 Initial: Pan={coords['x']:.2f}°, Tilt={coords['y']:.2f}°, Zoom={coords['z']:.2f} | State: {'MOVING' if moving else 'IDLE'}\n")

    cmd_label = "IDLE"
    try:
        while True:
            key = get_key(timeout=0.08)
            if not key:
                continue

            k = key.lower() if isinstance(key, str) else ""

            if k == "q":
                print("\n[KEYPAD] Exiting...")
                driver.stop()
                break
            elif k in ("w", "8", "up"):
                ok = driver.set_ptz("up")
                cmd_label = "TILT UP [OK]" if ok else "TILT UP [FAIL]"
            elif k in ("s", "2", "down"):
                ok = driver.set_ptz("down")
                cmd_label = "TILT DOWN [OK]" if ok else "TILT DOWN [FAIL]"
            elif k in ("a", "4", "left"):
                ok = driver.set_ptz("left")
                cmd_label = "PAN LEFT [OK]" if ok else "PAN LEFT [FAIL]"
            elif k in ("d", "6", "right"):
                ok = driver.set_ptz("right")
                cmd_label = "PAN RIGHT [OK]" if ok else "PAN RIGHT [FAIL]"
            elif k in ("u", "7", "+"):
                ok = driver.set_ptz("zoom_in")
                cmd_label = "ZOOM IN [OK]" if ok else "ZOOM IN [FAIL]"
            elif k in ("o", "9", "-"):
                ok = driver.set_ptz("zoom_out")
                cmd_label = "ZOOM OUT [OK]" if ok else "ZOOM OUT [FAIL]"
            elif k in ("j", "n"):
                ok = driver.set_ptz("focus_near")
                cmd_label = "FOCUS NEAR"
            elif k in ("k", "f"):
                ok = driver.set_ptz("focus_far")
                cmd_label = "FOCUS FAR"
            elif k == "t":
                driver.trigger_autofocus()
                cmd_label = "AUTOFOCUS"
            elif k == "l":
                driver.init_lens()
                cmd_label = "LENS INIT"
            elif k in ("g", "p"):
                driver.stop()
                val = input("\n🎯 Enter Preset Number: ").strip()
                if val.isdigit():
                    num = int(val)
                    print(f"🚀 Moving to Preset {num}...")
                    if driver.goto_preset(num):
                        c, _ = driver.wait_until_idle()
                        print(f"✅ Reached Preset {num} -> Pan: {c['x']:.2f}°, Tilt: {c['y']:.2f}°")
                        cmd_label = f"PRESET {num}"
                    else:
                        print(f"❌ Failed to reach Preset {num}.")
                continue
            elif k == "m":
                driver.stop()
                val = input("\n💾 Enter Preset Number to save current position: ").strip()
                if val.isdigit():
                    num = int(val)
                    if driver.set_preset(num):
                        print(f"✅ Position saved as Preset {num}!")
                        cmd_label = f"SAVED P{num}"
                    else:
                        print(f"❌ Failed to save Preset {num}.")
                continue
            elif k in (" ", "5"):
                driver.stop()
                cmd_label = "STOPPED"
            elif k == "1":
                driver.speed = 0.2
                cmd_label = "SPEED 0.2"
            elif k == "2":
                driver.speed = 0.5
                cmd_label = "SPEED 0.5"
            elif k == "3":
                driver.speed = 0.8
                cmd_label = "SPEED 0.8"
            elif k == "r":
                coords, moving = driver.read_ptz()
                print(f"\n📍 Live: Pan: {coords['x']:.2f}° | Tilt: {coords['y']:.2f}° | Zoom: {coords['z']:.2f} | Focus: {coords['focus']:.0f} | State: {'MOVING' if moving else 'IDLE'}")
                continue

            print(f"\rCommand: {cmd_label:<20} | Speed: {driver.speed:.1f} ", end="", flush=True)

    except KeyboardInterrupt:
        print("\n[KEYPAD] Interrupted.")
    finally:
        driver.stop()


# ============================================================================
# 5. MAIN ENTRY POINT
# ============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Unified Diagnostic Prober & PTZ Controller for IP Cameras",
        usage="%(prog)s <camera_name_or_json> [preset_number]",
    )
    parser.add_argument("camera", nargs="?", default=None, help="Camera name or JSON file (e.g. 'gate', 'gate.json', 'apcam').")
    parser.add_argument("preset", nargs="?", default=None, help="Preset number to move camera directly to.")
    parser.add_argument("--set-preset", type=int, default=None, help="Save current position as preset number.")
    parser.add_argument("--test", action="store_true",
                        help="Test the camera (reach, PTZ login, streams, encoder, copy record) and write "
                             "out/<name>.json for edge_worker plus out/<name>.report.json.")
    parser.add_argument("--copy-seconds", type=int, default=10,
                        help="Length of the stream-copy record test in --test (0 skips it).")
    args = parser.parse_args()

    if not args.camera:
        parser.print_help()
        sys.exit(1)

    if args.test:
        sys.exit(run_camera_test(args.camera, args.copy_seconds))

    cam_cfg = load_camera_config(args.camera)

    # Stage 1: Diagnostic Pre-Flight Probing
    prober = CameraDiagnostics(cam_cfg)
    if not prober.run_preflight_checks():
        sys.exit(1)

    # Stage 2: Initialize Appropriate Helper Driver
    driver = build_driver(cam_cfg)
    if driver is None:
        print(f"[INFO] {cam_cfg['name']} has ptz_protocol 'none'; no PTZ control. Use --test to test streams.")
        sys.exit(0)

    # Stage 3: Initial PTZ Status Verification
    coords, moving = driver.read_ptz()
    proto = "DAHUA CGI" if isinstance(driver, DahuaCGIDriver) else "ONVIF SOAP"

    print("\n" + "=" * 68)
    print("📋 INITIAL TELEMETRY DASHBOARD")
    print("=" * 68)
    print(f"  Camera Name        : {driver.display_name}")
    print(f"  Resolved Protocol  : {proto}")
    print(f"  Pan (X) / Tilt (Y) : {coords['x']:.2f}° / {coords['y']:.2f}°")
    print(f"  Zoom / Focus       : {coords['z']:.2f} / {coords['focus']:.0f}")
    print(f"  Motor Status       : {'🏃 MOVING' if moving else '🟢 IDLE'}")
    print("=" * 68)

    # CLI Direct Action: Save current position as preset
    if args.set_preset is not None:
        print(f"\n💾 Saving Preset {args.set_preset} on {driver.display_name}...")
        if driver.set_preset(args.set_preset):
            print("✅ Preset saved successfully.")
        else:
            print("❌ Failed to save preset.")
        sys.exit(0)

    # CLI Direct Action: Move directly to a preset number
    if args.preset is not None and args.preset.isdigit():
        preset_num = int(args.preset)
        print(f"\n🎯 Moving {driver.display_name} to Preset {preset_num}...")
        if driver.goto_preset(preset_num):
            final_coords, _ = driver.wait_until_idle()
            print(f"✅ Arrived at Preset {preset_num}! Pan: {final_coords['x']:.2f}°, Tilt: {final_coords['y']:.2f}°, Zoom: {final_coords['z']:.2f}")
        else:
            print(f"❌ Could not move to Preset {preset_num}.")
        sys.exit(0)

    # Default Action: Launch Keypad Controller Loop
    run_interactive_controller(driver)


if __name__ == "__main__":
    main()
