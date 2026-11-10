#!/usr/bin/env python3
"""On-screen display (OSD) of a camera: the title and the date / time burned into the video.

The camera JSON's "osd" section (OSD panel of the page; sent by Apply OSD / Save to camera and edge):
    "osd": {"show_date": true, "date_format": "dd-MM-yyyy HH:mm:ss", "date_position": [0.02, 0.03],
            "show_name": true, "name_position": [0.5, 0.03], "use_camera_positions": true}
  title (name)  = always the camera ID <site>_<camera> from camera.site and camera.name, e.g. HUB_Gate (spelling kept as typed)
                  (no spaces; the same name as the JSON file). Replaces the camera's default ("IPC Camera").
  date_format   = the date / time format (default dd-MM-yyyy HH:mm:ss)
  use_camera_positions  true (default): the positions are left as the camera has them (the page shows
                  them after reading the camera); false: date_position / name_position are sent
                  (share of the picture, 0..1 from the top left; the name's x is its centre).
  show_date / show_name  date / title shown or not.
  other_texts_off  true: the camera's other default texts are switched off too (Dahua: the other
                  VideoWidget titles; ONVIF: every other OSD is deleted), where the camera allows it.
Read OSD returns the camera's current title, date format and positions.

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
RECOMMENDED = {"date_position": [0.02, 0.03], "name_position": [0.5, 0.03]}
DAHUA_MAX = 8191
DATE_W, TEXT_H, CHAR_W = 0.30, 0.05, 0.022       # size estimates (share of the picture) for Dahua rectangles


def slug(v) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(v or "").strip()).strip("_")     # spelling kept as typed


def camera_id(cam_json: dict) -> str:
    """<site>_<camera> as typed (e.g. HUB_Gate): the OSD title and the JSON file name."""
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
    own = o.get("use_camera_positions", True) is not False
    return {"title": camera_id(cam_json),
            "date_format": str(o.get("date_format") or DEFAULT_DATE_FORMAT).strip(),
            "show_date": o.get("show_date", True) is not False,
            "show_name": o.get("show_name", True) is not False,
            "other_off": o.get("other_texts_off") is True,
            "date_position": None if own else _pos(o.get("date_position")),     # None = leave as the camera has it
            "title_position": None if own else _pos(o.get("name_position"))}


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
        self.log.append(f"Dahua {what}: HTTP {r.status_code} {r.text.strip()[:160] or '(empty answer)'}")
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
            out["name_position"] = [round((cr[0] + cr[2]) / 2, 3), round(cr[1], 3)]
        return out

    def apply(self, s: dict) -> bool:
        w = f"VideoWidget[{self.ch}]"
        to = lambda v: int(round(min(1.0, max(0.0, v)) * DAHUA_MAX))  # noqa: E731
        sd, sn = str(s["show_date"]).lower(), str(s["show_name"]).lower()
        vals = {f"ChannelTitle[{self.ch}].Name": s["title"]}
        # Every on/off flag the camera has for the date and the title: one per stream on most models
        # (EncodeBlend = main stream, EncodeBlendExtra1..3 = sub streams, EncodeBlendSnapshot, PreviewBlend).
        vw = self.get("VideoWidget")
        blend = re.compile(rf"^table\.(VideoWidget\[{self.ch}\]\.(TimeTitle|ChannelTitle)\.\w*Blend\w*)$")
        found = 0
        for k in vw:
            m = blend.match(k)
            if m:
                vals[m.group(1)] = sd if m.group(2) == "TimeTitle" else sn
                found += 1
        if not found:              # the camera listed none: the usual names
            vals.update({f"{w}.ChannelTitle.EncodeBlend": sn, f"{w}.ChannelTitle.PreviewBlend": sn,
                         f"{w}.TimeTitle.EncodeBlend": sd, f"{w}.TimeTitle.PreviewBlend": sd})
        self.log.append("on/off flags: " + (", ".join(sorted(k.split(".", 1)[1] for k in vals if "Blend" in k)) or "none"))
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
        if s["other_off"]:      # the camera's other titles (custom texts, logo …): off; privacy masks (Covers) stay
            pat = re.compile(rf"^table\.(VideoWidget\[{self.ch}\]\.(?!TimeTitle\.|ChannelTitle\.|Covers\[)[\w\[\].]+\.\w*Blend\w*)$")
            for k in vw:
                m = pat.match(k)
                if m:
                    vals[m.group(1)] = "false"
        ok = self.set(vals, "title" + (" + positions" if s["date_position"] or s["title_position"] else "")
                      + (" + other texts off" if s["other_off"] else ""))
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
                    out["name_position"] = self.frac_of(o)
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

    def _drop(self, existing, what: str) -> bool:
        if not existing:
            return True
        return self.soap(f"<trt:DeleteOSD><trt:OSDToken>{existing['token']}</trt:OSDToken></trt:DeleteOSD>",
                         f"DeleteOSD {what}") is not None

    def apply(self, s: dict) -> bool:
        cur = self.osds()
        date = next((o for o in cur if o["text_type"] in _DATE_TYPES), None)
        title = next((o for o in cur if o["text_type"] == "Plain"), None)
        plain = s["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if s["show_name"]:
            ok = self._put(title, self._position_xml(title, s["title_position"], RECOMMENDED["name_position"]),
                           f"<tt:Type>Plain</tt:Type><tt:PlainText>{plain}</tt:PlainText>", f"title {s['title']}")
        else:
            ok = self._drop(title, "title")
        if s["other_off"]:      # every OSD that is not our date or title: deleted
            for o in cur:
                if o is not date and o is not title:
                    ok = self._drop(o, f"other text {o['plain'] or o['text_type'] or o['token']}") and ok
        if not s["show_date"]:
            return ok and self._drop(date, "date")
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
