#!/usr/bin/env python3
"""On-screen display (OSD) of a camera: the title and the date / time burned into the video.

What this tool sets (Apply OSD in the View window):
  title        = the camera ID: <site>_<camera> from camera.site and camera.name, e.g. hub_gate
                 (no spaces; the same name as the JSON file hub_gate.json). Replaces the camera's
                 own default such as "IPC Camera".
  date format  = osd.date_format in the camera JSON (default dd-MM-yyyy HH:mm:ss)
  positions    = left as the camera has them. Recommended: date top left, title top middle. Only
                 when one covers something do you move it (View window: Move date / Move title);
                 that writes osd.date_position / osd.title_position (share of the picture, 0..1 from
                 the top left; the title's x is its centre) and Apply OSD sends them too.
Read OSD shows the camera's current title, date format and positions.

    "osd": {"date_format": "dd-MM-yyyy HH:mm:ss"}            (+ date_position / title_position if moved)

Two ways to reach a camera:
  Dahua CGI   configManager.cgi: ChannelTitle[ch].Name, Locales.TimeFormat,
              VideoWidget[ch].TimeTitle / ChannelTitle (EncodeBlend, Rect in 0..8191)
  ONVIF       Media service: GetOSDs / SetOSD / CreateOSD (Pos x, y in -1..1, y up); an existing
              OSD keeps its own position element unless a new one is given
Every camera answer goes into the log, so a camera that ignores a setting shows why.
"""
from __future__ import annotations

import re
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth

import test_protocol as tp

DEFAULT_DATE_FORMAT = "dd-MM-yyyy HH:mm:ss"
RECOMMENDED = {"date_position": [0.02, 0.03], "title_position": [0.5, 0.03]}
DAHUA_MAX = 8191
DATE_W, TEXT_H, CHAR_W = 0.30, 0.05, 0.022       # size estimates (share of the picture) for Dahua rectangles


def slug(v) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", str(v or "").strip().lower()).strip("_")


def camera_id(cam_json: dict) -> str:
    """<site>_<camera> (e.g. hub_gate): the OSD title and the JSON file name."""
    cam = cam_json.get("camera") or {}
    site, name = slug(cam.get("site")), slug(cam.get("name"))
    return f"{site}_{name}" if site and name else name or site or "camera"


def _pos(v):
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            return [min(1.0, max(0.0, float(x))) for x in v]
        except (TypeError, ValueError):
            return None
    return None


def settings(cam_json: dict) -> dict:
    o = cam_json.get("osd") or {}
    return {"title": camera_id(cam_json),
            "date_format": str(o.get("date_format") or DEFAULT_DATE_FORMAT).strip(),
            "date_position": _pos(o.get("date_position")),      # None = leave as the camera has it
            "title_position": _pos(o.get("title_position"))}


def protocol_of(cam: dict) -> str:
    cfg = tp.standardize_config(cam)
    return "dahua" if cfg["vendor"] == "dahua" or cfg["ptz_protocol"] == "dahua_cgi" else "onvif"


# ---------------------------------------------------------------------------
# Dahua CGI
# ---------------------------------------------------------------------------
class _Dahua:
    def __init__(self, cam: dict, log: list):
        cfg = tp.standardize_config(cam)
        self.url = f"http://{cfg['host']}:{cfg['http_port']}/cgi-bin/configManager.cgi"
        self.auth = HTTPDigestAuth(cfg["username"], cfg["password"])
        self.ch = max(0, int(cfg.get("channel", 1)) - 1)
        self.log = log

    def get(self, name: str) -> dict:
        r = requests.get(self.url, params={"action": "getConfig", "name": name}, auth=self.auth, timeout=5)
        if r.status_code != 200:
            raise RuntimeError(f"getConfig {name}: HTTP {r.status_code}")
        return dict(line.split("=", 1) for line in r.text.splitlines() if "=" in line)

    def set(self, values: dict, what: str) -> bool:
        q = "&".join(f"{quote(k, safe='[].')}={quote(str(v), safe='')}" for k, v in values.items())
        r = requests.get(f"{self.url}?action=setConfig&{q}", auth=self.auth, timeout=5)
        ok = r.status_code == 200 and "OK" in r.text
        self.log.append(f"Dahua {what}: HTTP {r.status_code} {r.text.strip()[:80]}")
        return ok

    def read(self) -> dict:
        w = f"table.VideoWidget[{self.ch}]"
        vw = self.get("VideoWidget")
        names = self.get("ChannelTitle")
        try:
            loc = self.get("Locales")
        except RuntimeError:
            loc = {}

        def rect(kind):
            r = [vw.get(f"{w}.{kind}.Rect[{i}]") for i in range(4)]
            return [int(x) / DAHUA_MAX for x in r] if all(x not in (None, "") for x in r) else None
        tr, cr = rect("TimeTitle"), rect("ChannelTitle")
        out = {"title": names.get(f"table.ChannelTitle[{self.ch}].Name", ""),
               "date_shown": vw.get(f"{w}.TimeTitle.EncodeBlend") == "true",
               "title_shown": vw.get(f"{w}.ChannelTitle.EncodeBlend") == "true",
               "date_format": loc.get("table.Locales.TimeFormat", "")}
        if tr:
            out["date_position"] = [round(tr[0], 3), round(tr[1], 3)]
        if cr:
            out["title_position"] = [round((cr[0] + cr[2]) / 2, 3), round(cr[1], 3)]
        return out

    def apply(self, s: dict) -> bool:
        w = f"VideoWidget[{self.ch}]"
        to = lambda v: int(round(min(1.0, max(0.0, v)) * DAHUA_MAX))  # noqa: E731
        vals = {f"ChannelTitle[{self.ch}].Name": s["title"],
                f"{w}.ChannelTitle.EncodeBlend": "true", f"{w}.ChannelTitle.PreviewBlend": "true",
                f"{w}.TimeTitle.EncodeBlend": "true", f"{w}.TimeTitle.PreviewBlend": "true"}
        if s["date_position"]:
            dx, dy = s["date_position"]
            vals.update({f"{w}.TimeTitle.Rect[0]": to(dx), f"{w}.TimeTitle.Rect[1]": to(dy),
                         f"{w}.TimeTitle.Rect[2]": to(dx + DATE_W), f"{w}.TimeTitle.Rect[3]": to(dy + TEXT_H)})
        if s["title_position"]:
            nx, ny = s["title_position"]
            nw = min(0.9, CHAR_W * max(4, len(s["title"])))
            n1 = max(0.0, min(1.0 - nw, nx - nw / 2))
            vals.update({f"{w}.ChannelTitle.Rect[0]": to(n1), f"{w}.ChannelTitle.Rect[1]": to(ny),
                         f"{w}.ChannelTitle.Rect[2]": to(n1 + nw), f"{w}.ChannelTitle.Rect[3]": to(ny + TEXT_H)})
        ok = self.set(vals, "title" + (" + positions" if s["date_position"] or s["title_position"] else ""))
        if not self.set({"Locales.TimeFormat": s["date_format"]}, f"date format {s['date_format']}"):
            self.log.append("the camera did not take the date format (title is set)")
        return ok


# ---------------------------------------------------------------------------
# ONVIF (Media service)
# ---------------------------------------------------------------------------
ONVIF_DATE = {"dd-MM-yyyy": ["dd-MM-yyyy", "dd/MM/yyyy"], "yyyy-MM-dd": ["yyyy-MM-dd", "yyyy/MM/dd"]}
_DATE_TYPES = ("DateAndTime", "Date", "Time")


class _Onvif:
    def __init__(self, cam: dict, log: list):
        self.drv = tp.ONVIFSoapDriver(tp.standardize_config(cam))
        self.log = log
        if not self.drv.auth_ok:
            raise RuntimeError(f"ONVIF login failed ({self.drv.last_error}); check username / password / http_port")
        prof = self.drv._post_soap("<trt:GetProfiles/>", service="media")
        m = re.search(r'VideoSourceConfiguration[^>]*token="([^"]+)"', prof.text if prof is not None else "")
        if not m:
            raise RuntimeError("ONVIF: no video source configuration in the profiles")
        self.vsc = m.group(1)

    def soap(self, body: str, what: str):
        r = self.drv._post_soap(body, service="media", timeout=5)
        self.log.append(f"ONVIF {what}: {'OK' if r is not None else 'refused (' + str(self.drv.last_error) + ')'}")
        return r

    def osds(self) -> list[dict]:
        r = self.soap(f"<trt:GetOSDs><trt:ConfigurationToken>{self.vsc}</trt:ConfigurationToken></trt:GetOSDs>", "GetOSDs")
        out = []
        for token, inner in re.findall(r'<[\w]*:?OSDs[^>]*token="([^"]*)"[^>]*>(.*?)</[\w]*:?OSDs>', r.text if r is not None else "", re.S):
            t = re.search(r"<[\w]*:?TextString>.*?<[\w]*:?Type>(\w+)<", inner, re.S)
            pos = re.search(r'<[\w]*:?Pos[^>]*x="([-\d.]+)"[^>]*y="([-\d.]+)"', inner)
            ptype = re.search(r"<[\w]*:?Position>\s*<[\w]*:?Type>(\w+)<", inner)
            txt = re.search(r"<[\w]*:?PlainText>([^<]*)<", inner)
            df = re.search(r"<[\w]*:?DateFormat>([^<]*)<", inner)
            tf = re.search(r"<[\w]*:?TimeFormat>([^<]*)<", inner)
            out.append({"token": token, "text_type": t.group(1) if t else "",
                        "pos": (float(pos.group(1)), float(pos.group(2))) if pos else None,
                        "pos_type": ptype.group(1) if ptype else "",
                        "plain": txt.group(1) if txt else "", "date_format": df.group(1) if df else "",
                        "time_format": tf.group(1) if tf else ""})
        return out

    @staticmethod
    def to_frac(pos):
        return [round((pos[0] + 1) / 2, 3), round((1 - pos[1]) / 2, 3)]

    @staticmethod
    def to_onvif(p):
        return round(p[0] * 2 - 1, 3), round(1 - p[1] * 2, 3)

    NAMED = {"UpperLeft": [0.02, 0.03], "UpperRight": [0.75, 0.03], "LowerLeft": [0.02, 0.92], "LowerRight": [0.75, 0.92]}

    def frac_of(self, o):
        return self.to_frac(o["pos"]) if o["pos"] else self.NAMED.get(o["pos_type"])

    def read(self) -> dict:
        out = {"title": "", "date_shown": False, "title_shown": False, "date_format": ""}
        for o in self.osds():
            if o["text_type"] in _DATE_TYPES:
                out["date_shown"] = True
                out["date_format"] = f"{o['date_format']} {o['time_format']}".strip()
                if self.frac_of(o):
                    out["date_position"] = self.frac_of(o)
            elif o["text_type"] == "Plain":
                out["title_shown"], out["title"] = True, o["plain"]
                if self.frac_of(o):
                    out["title_position"] = self.frac_of(o)
        return out

    def _position_xml(self, existing, new_frac, recommended):
        if new_frac:                                    # moved by the user
            x, y = self.to_onvif(new_frac)
            return f'<tt:Position><tt:Type>Custom</tt:Type><tt:Pos x="{x}" y="{y}"/></tt:Position>'
        if existing and existing["pos"]:                # keep the camera's own position
            x, y = existing["pos"]
            return f'<tt:Position><tt:Type>Custom</tt:Type><tt:Pos x="{x}" y="{y}"/></tt:Position>'
        if existing and existing["pos_type"]:
            return f'<tt:Position><tt:Type>{existing["pos_type"]}</tt:Type></tt:Position>'
        x, y = self.to_onvif(recommended)               # new OSD: the recommended place
        return f'<tt:Position><tt:Type>Custom</tt:Type><tt:Pos x="{x}" y="{y}"/></tt:Position>'

    def _put(self, existing, position_xml: str, text: str, what: str) -> bool:
        token = existing["token"] if existing else ""
        osd = (f'<trt:OSD token="{token}"><tt:VideoSourceConfigurationToken>{self.vsc}</tt:VideoSourceConfigurationToken>'
               f'<tt:Type>Text</tt:Type>{position_xml}<tt:TextString>{text}</tt:TextString></trt:OSD>')
        op = "SetOSD" if existing else "CreateOSD"
        return self.soap(f"<trt:{op}>{osd}</trt:{op}>", f"{op} {what}") is not None

    def apply(self, s: dict) -> bool:
        cur = self.osds()
        date = next((o for o in cur if o["text_type"] in _DATE_TYPES), None)
        title = next((o for o in cur if o["text_type"] == "Plain"), None)
        plain = s["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ok = self._put(title, self._position_xml(title, s["title_position"], RECOMMENDED["title_position"]),
                       f"<tt:Type>Plain</tt:Type><tt:PlainText>{plain}</tt:PlainText>", f"title {s['title']}")
        dfmt, _, tfmt = s["date_format"].partition(" ")
        pos_xml = self._position_xml(date, s["date_position"], RECOMMENDED["date_position"])
        for d in ONVIF_DATE.get(dfmt, [dfmt]):
            text = (f"<tt:Type>DateAndTime</tt:Type><tt:DateFormat>{d}</tt:DateFormat>"
                    f"<tt:TimeFormat>{tfmt or 'HH:mm:ss'}</tt:TimeFormat>")
            if self._put(date, pos_xml, text, f"date ({d})"):
                if d != dfmt:
                    self.log.append(f"the camera does not take {dfmt}; it shows {d} instead")
                break
        else:
            ok = False
        return ok


# ---------------------------------------------------------------------------
def handle(cam_json: dict, action: str) -> dict:
    """read: the camera's current title, date format and positions;
    apply: title = camera ID, the date format, positions only if moved; then read back."""
    log: list[str] = []
    cam = cam_json.get("camera") or {}
    proto = protocol_of(cam)
    try:
        dev = _Dahua(cam, log) if proto == "dahua" else _Onvif(cam, log)
        ok = True
        if action == "apply":
            s = settings(cam_json)
            ok = dev.apply(s)
            tp.log(camera_id(cam_json), f"osd apply via {proto}: {'OK' if ok else 'FAIL'}")
        now = dev.read()
        return {"ok": ok, "protocol": proto, "osd": now, "camera_id": camera_id(cam_json),
                "recommended": RECOMMENDED, "log": log}
    except (requests.RequestException, RuntimeError, ValueError) as e:
        log.append(f"{type(e).__name__}: {e}")
        return {"ok": False, "protocol": proto, "osd": None, "camera_id": camera_id(cam_json),
                "recommended": RECOMMENDED, "log": log}
