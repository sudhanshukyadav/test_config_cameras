#!/usr/bin/env python3
"""Writes the Sensor and Encoder values of a camera JSON into the camera (Save to camera and edge).

    sensor_hardware_properties: brightness, contrast, saturation, sharpness, shutter_speed_sec, gain,
        wdr_enabled, wdr_level, noise_reduction_3d, day_night_mode, white_balance_mode
    stream_encoder_properties:  codec, smart_codec, main_stream_resolution, sub_stream_resolution, fps,
        gop_keyframe_interval, bitrate_type, bitrate_kbps

Each value is written on its own, so one value the camera refuses does not stop the others.
A value the camera has no setting for is skipped ("not adjustable on this camera").
Afterwards the values are read back; a value the camera changed (e.g. a bitrate rounded) is reported.

Two ways to reach a camera:
  Dahua CGI  configManager.cgi getConfig / setConfig (VideoColor, VideoInSharpness, VideoInExposure,
             VideoInBacklight / VideoInWideDynamicRange, VideoInDenoise, VideoInDayNight,
             VideoInWhiteBalance, older firmware VideoInOptions; Encode, SmartEncode). Only the keys
             the camera lists are written (all of its day / night / normal profiles).
  ONVIF      Imaging service (Brightness, ColorSaturation, Contrast, Sharpness, WideDynamicRange,
             WhiteBalance, IrCutFilter, Exposure) scaled to the ranges the camera reports; Media
             service SetVideoEncoderConfiguration (main = first profile, sub = second).
Shutter and gain are written as the manual-exposure values; the exposure mode is left as it is, so a
camera in automatic exposure keeps adjusting itself (reported).
"""
from __future__ import annotations

import re
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth

import test_protocol as tp

SENSOR, ENCODER = "sensor_hardware_properties", "stream_encoder_properties"
RESTART_KEYS = {"codec", "main_stream_resolution", "sub_stream_resolution", "fps", "gop_keyframe_interval",
                "bitrate_type", "bitrate_kbps", "smart_codec"}


def _shutter_ms(v):
    """'1/100' -> 10.0 ms; 0.01 -> 10.0 ms."""
    s = str(v).strip()
    try:
        if "/" in s:
            a, b = s.split("/", 1)
            return round(float(a) / float(b) * 1000, 3)
        return round(float(s) * 1000, 3)
    except (ValueError, ZeroDivisionError):
        return None


def _res(v):
    if isinstance(v, str):
        v = [x for x in re.split(r"[ ,x]+", v) if x]
    try:
        w, h = (int(float(x)) for x in v)
        return w, h
    except (TypeError, ValueError):
        return None


def _same(a, b, tol=1e-6) -> bool:
    a, b = str(a).strip(), str(b).strip()
    if a.lower() == b.lower():
        return True
    try:
        return abs(float(a) - float(b)) <= tol
    except ValueError:
        return False


def _num(v):
    return int(v) if float(v).is_integer() else v


# ---------------------------------------------------------------------------
# Dahua CGI
# ---------------------------------------------------------------------------
DN = {"auto": "Brightness", "day": "Color", "night": "BlackWhite", "color": "Color", "bw": "BlackWhite"}
DN_OLD = {"day": 0, "color": 0, "auto": 1, "night": 2, "bw": 2}
WB = {"auto": "Auto", "manual": "Manual", "indoor": "Indoor", "outdoor": "Outdoor"}


def dahua_spec(sensor: dict, enc: dict, ch: int):
    """[(section, key, label, value_text, [(config name, key regex, camera value), ...])]"""
    c = rf"\[{ch}\]"
    p = rf"{c}\[\d+\]"          # [ch][profile]: all of the camera's profiles (day / night / normal)
    out = []

    def add(sec, key, label, shown, cands):
        out.append((sec, key, label, shown, cands))

    s = sensor
    for key, dkey in (("brightness", "Brightness"), ("contrast", "Contrast"), ("saturation", "Saturation")):
        if key in s:
            add(SENSOR, key, key, s[key], [("VideoColor", rf"VideoColor{p}\.{dkey}$", _num(s[key])),
                                            ("VideoInOptions", rf"VideoInOptions{c}\.{dkey}$", _num(s[key]))])
    if "sharpness" in s:
        add(SENSOR, "sharpness", "sharpness", s["sharpness"],
            [("VideoInSharpness", rf"VideoInSharpness{p}\.Sharpness$", _num(s["sharpness"])),
             ("VideoInOptions", rf"VideoInOptions{c}\.Sharpness$", _num(s["sharpness"]))])
    ms = _shutter_ms(s.get("shutter_speed_sec")) if "shutter_speed_sec" in s else None
    if ms is not None:
        add(SENSOR, "shutter_speed_sec", "shutter", s["shutter_speed_sec"],
            [("VideoInExposure", rf"VideoInExposure{p}\.Value[12]$", _num(ms)),
             ("VideoInOptions", rf"VideoInOptions{c}\.ExposureValue[12]$", _num(ms))])
    if "gain" in s:
        add(SENSOR, "gain", "gain", s["gain"],
            [("VideoInExposure", rf"VideoInExposure{p}\.Gain$", _num(s["gain"])),
             ("VideoInOptions", rf"VideoInOptions{c}\.Gain$", _num(s["gain"]))])
    if "wdr_enabled" in s:
        on = s["wdr_enabled"] is True
        add(SENSOR, "wdr_enabled", "WDR on/off", "on" if on else "off",
            [("VideoInBacklight", rf"VideoInBacklight{p}\.Mode$", "WideDynamic" if on else "Off"),
             ("VideoInWideDynamicRange", rf"VideoInWideDynamicRange{p}\.Mode$", "On" if on else "Off"),
             ("VideoInOptions", rf"VideoInOptions{c}\.WideDynamicRangeMode$", 1 if on else 0)])
    if "wdr_level" in s:
        add(SENSOR, "wdr_level", "WDR level", s["wdr_level"],
            [("VideoInBacklight", rf"VideoInBacklight{p}\.WideDynamicRange$", _num(s["wdr_level"])),
             ("VideoInWideDynamicRange", rf"VideoInWideDynamicRange{p}\.WideDynamicRange$", _num(s["wdr_level"])),
             ("VideoInOptions", rf"VideoInOptions{c}\.WideDynamicRange$", _num(s["wdr_level"]))])
    if "noise_reduction_3d" in s:
        add(SENSOR, "noise_reduction_3d", "3D noise reduction", s["noise_reduction_3d"],
            [("VideoInDenoise", rf"VideoInDenoise{p}\.3D\w*\.\w*Level$", _num(s["noise_reduction_3d"]))])
    if "day_night_mode" in s:
        m = str(s["day_night_mode"]).lower()
        add(SENSOR, "day_night_mode", "day/night", m,
            [("VideoInDayNight", rf"VideoInDayNight{p}\.Mode$", DN.get(m, m)),
             ("VideoInOptions", rf"VideoInOptions{c}\.DayNightColor$", DN_OLD.get(m, 1))])
    if "white_balance_mode" in s:
        m = str(s["white_balance_mode"]).lower()
        add(SENSOR, "white_balance_mode", "white balance", m,
            [("VideoInWhiteBalance", rf"VideoInWhiteBalance{p}\.Mode$", WB.get(m, m)),
             ("VideoInOptions", rf"VideoInOptions{c}\.WhiteBalance$", WB.get(m, m))])

    e = enc
    v = rf"Encode{c}\.MainFormat\[0\]\.Video"
    if "codec" in e:
        add(ENCODER, "codec", "codec", e["codec"], [("Encode", rf"{v}\.Compression$", str(e["codec"]).upper())])
    r = _res(e.get("main_stream_resolution")) if "main_stream_resolution" in e else None
    if r:
        add(ENCODER, "main_stream_resolution", "main resolution", f"{r[0]}x{r[1]}",
            [("Encode", rf"{v}\.(Width|Height)$", {"Width": r[0], "Height": r[1]})])
    r = _res(e.get("sub_stream_resolution")) if "sub_stream_resolution" in e else None
    if r:
        add(ENCODER, "sub_stream_resolution", "sub resolution", f"{r[0]}x{r[1]}",
            [("Encode", rf"Encode{c}\.ExtraFormat\[0\]\.Video\.(Width|Height)$", {"Width": r[0], "Height": r[1]})])
    if "fps" in e:
        add(ENCODER, "fps", "fps", e["fps"], [("Encode", rf"{v}\.FPS$", _num(e["fps"]))])
    if "gop_keyframe_interval" in e:
        add(ENCODER, "gop_keyframe_interval", "GOP", e["gop_keyframe_interval"],
            [("Encode", rf"{v}\.GOP$", _num(e["gop_keyframe_interval"]))])
    if "bitrate_type" in e:
        add(ENCODER, "bitrate_type", "bitrate type", e["bitrate_type"],
            [("Encode", rf"{v}\.BitRateControl$", str(e["bitrate_type"]).upper())])
    if "bitrate_kbps" in e:
        add(ENCODER, "bitrate_kbps", "bitrate", f"{e['bitrate_kbps']} kbps", [("Encode", rf"{v}\.BitRate$", _num(e["bitrate_kbps"]))])
    if "smart_codec" in e:
        on = e["smart_codec"] is True
        add(ENCODER, "smart_codec", "smart codec", "on" if on else "off",
            [("SmartEncode", rf"SmartEncode{c}(\[\d+\])?\.Enable$", "true" if on else "false")])
    return out


class _Dahua:
    def __init__(self, cam: dict):
        cfg = tp.standardize_config(cam)
        self.url = f"http://{cfg['host']}:{cfg['http_port']}/cgi-bin/configManager.cgi"
        self.auth = HTTPDigestAuth(cfg["username"], cfg["password"])
        self.ch = max(0, int(cfg.get("channel", 1)) - 1)
        self.cache = {}

    def get(self, name: str, fresh=False):
        if fresh or name not in self.cache:
            try:
                r = requests.get(self.url, params={"action": "getConfig", "name": name}, auth=self.auth, timeout=5)
                self.cache[name] = ({k[6:] if k.startswith("table.") else k: v.strip() for k, v in
                                     (ln.split("=", 1) for ln in r.text.splitlines() if "=" in ln)}
                                    if r.status_code == 200 else None)
            except requests.RequestException:
                self.cache[name] = None
        return self.cache[name]

    def set(self, values: dict):
        q = "&".join(f"{quote(k, safe='[].')}={quote(str(v), safe='')}" for k, v in values.items())
        try:
            r = requests.get(f"{self.url}?action=setConfig&{q}", auth=self.auth, timeout=6)
        except requests.RequestException as e:
            return False, type(e).__name__
        return r.status_code == 200 and "OK" in r.text, f"HTTP {r.status_code} {r.text.strip()[:80]}"

    def write(self, cam_json: dict) -> list:
        res = []
        for sec, key, label, shown, cands in dahua_spec(cam_json.get(SENSOR) or {}, cam_json.get(ENCODER) or {}, self.ch):
            done = False
            for name, rx, val in cands:
                table = self.get(name)
                if not table:
                    continue
                keys = [k for k in table if re.match(rx, k)]
                if not keys:
                    continue
                vals = {k: (val[re.match(rx, k).group(1)] if isinstance(val, dict) else val) for k in keys}
                done = True
                if all(_same(table[k], v) for k, v in vals.items()):     # the camera has it already
                    res.append({"section": sec, "key": key, "label": label, "value": shown, "state": "same"})
                    break
                ok, why = self.set(vals)
                res.append({"section": sec, "key": key, "label": label, "value": shown, "state": "set" if ok else "refused",
                            "why": "" if ok else why, "_check": (name, vals)})
                break
            if not done:
                res.append({"section": sec, "key": key, "label": label, "value": shown, "state": "not adjustable"})
        # read back
        fresh = {}
        for r in res:
            if r["state"] != "set" or "_check" not in r:
                continue
            name, vals = r.pop("_check")
            if name not in fresh:
                fresh[name] = self.get(name, fresh=True) or {}
            now = [fresh[name].get(k, "?") for k in vals]
            if any(k in fresh[name] and not _same(fresh[name][k], v) for k, v in vals.items()):
                r["state"], r["camera_has"] = "changed", ("x".join(now) if r["key"].endswith("resolution")
                                                          else ", ".join(sorted(set(now))))
        for r in res:
            r.pop("_check", None)
        exp = self.get("VideoInExposure") or {}
        mode = exp.get(f"VideoInExposure[{self.ch}][0].Mode")
        words = {"0": "automatic", "1": "low noise", "2": "anti-smear", "3": "manual range", "4": "manual"}
        note = (f"exposure is {words.get(mode, mode)}: shutter and gain act only in manual exposure"
                if mode not in (None, "4") else "")
        return res, note


# ---------------------------------------------------------------------------
# ONVIF
# ---------------------------------------------------------------------------
TIMG = 'xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl"'


class _Onvif:
    def __init__(self, cam: dict):
        cfg = tp.standardize_config({**cam, "ptz_protocol": "onvif_soap"})
        self.d = tp.ONVIFSoapDriver(cfg)
        res = self.d._post_soap("<trt:GetProfiles/>", service="media")
        self.profiles = []          # [(profile token, video source token, encoder config token)]
        if res is not None:
            for tok, inner in re.findall(r'<[\w]*:?Profiles[^>]*token="([^"]+)"(.*?)</[\w]*:?Profiles>', res.text, re.S):
                src = re.search(r"<[\w]*:?SourceToken>([^<]+)<", inner)
                vec = re.search(r'<[\w]*:?VideoEncoderConfiguration[^>]*token="([^"]+)"', inner)
                self.profiles.append((tok, src.group(1) if src else None, vec.group(1) if vec else None))
        self.source = next((p[1] for p in self.profiles if p[1]), None)
        self.range = {}
        if self.source and self.d.xaddr.get("imaging"):
            r = self.img(f"<timg:GetOptions {TIMG}><timg:VideoSourceToken>{self.source}</timg:VideoSourceToken></timg:GetOptions>")
            if r is not None:
                for tag in ("Brightness", "ColorSaturation", "Contrast", "Sharpness"):
                    m = re.search(rf"<[\w]*:?{tag}>\s*<[\w]*:?Min>([\d.\-]+)<[^>]*>\s*<[\w]*:?Max>([\d.\-]+)<", r.text)
                    if m:
                        self.range[tag] = (float(m.group(1)), float(m.group(2)))
                m = re.search(r"<[\w]*:?WideDynamicRange>.*?<[\w]*:?Level>\s*<[\w]*:?Min>([\d.\-]+)<[^>]*>\s*<[\w]*:?Max>([\d.\-]+)<",
                              r.text, re.S)
                if m:
                    self.range["WDRLevel"] = (float(m.group(1)), float(m.group(2)))

    def img(self, body):
        return self.d._post_soap(body, timeout=5, service="imaging")

    def imaging_now(self) -> str:
        r = self.img(f"<timg:GetImagingSettings {TIMG}><timg:VideoSourceToken>{self.source}</timg:VideoSourceToken>"
                     "</timg:GetImagingSettings>")
        return r.text if r is not None else ""

    def scale(self, tag, v):
        lo, hi = self.range.get(tag, (0.0, 100.0))
        return round(lo + (float(v) / 100.0) * (hi - lo), 2)

    def set_img(self, inner: str):
        body = (f"<timg:SetImagingSettings {TIMG}><timg:VideoSourceToken>{self.source}</timg:VideoSourceToken>"
                f"<timg:ImagingSettings>{inner}</timg:ImagingSettings>"
                "<timg:ForcePersistence>true</timg:ForcePersistence></timg:SetImagingSettings>")
        r = self.img(body)
        return r is not None, self.d.last_error or ""

    def get_enc(self, token):
        r = self.d._post_soap(f"<trt:GetVideoEncoderConfiguration><trt:ConfigurationToken>{token}</trt:ConfigurationToken>"
                              "</trt:GetVideoEncoderConfiguration>", timeout=5, service="media")
        if r is None:
            return None
        m = re.search(r"<[\w]*:?Configuration\b([^>]*)>(.*)</[\w]*:?Configuration>", r.text, re.S)
        return m.group(2) if m else None

    def set_enc(self, token, inner):
        inner = re.sub(r"\s+xmlns:\w+=\"[^\"]*\"", "", inner)
        inner = re.sub(r"<(/?)\w+:", r"<\1tt:", inner)
        body = (f'<trt:SetVideoEncoderConfiguration><trt:Configuration token="{token}">{inner}</trt:Configuration>'
                "<trt:ForcePersistence>true</trt:ForcePersistence></trt:SetVideoEncoderConfiguration>")
        r = self.d._post_soap(body, timeout=6, service="media")
        return r is not None, self.d.last_error or ""

    def write(self, cam_json: dict):
        s, e = cam_json.get(SENSOR) or {}, cam_json.get(ENCODER) or {}
        res = []

        def row(sec, key, label, shown, state, why="", check=None):
            res.append({"section": sec, "key": key, "label": label, "value": shown, "state": state, "why": why,
                        "_check": check})

        # ---- imaging
        img_ok = bool(self.source and self.d.xaddr.get("imaging"))
        items = []   # (key, label, shown, inner xml, check (tag, value))
        for key, tag in (("brightness", "Brightness"), ("saturation", "ColorSaturation"), ("contrast", "Contrast"),
                         ("sharpness", "Sharpness")):
            if key in s:
                v = self.scale(tag, s[key])
                items.append((key, key, s[key], f"<tt:{tag}>{v}</tt:{tag}>", (tag, v)))
        if "wdr_enabled" in s:
            on = s["wdr_enabled"] is True
            lvl = f"<tt:Level>{self.scale('WDRLevel', s.get('wdr_level', 50))}</tt:Level>" if on and "wdr_level" in s else ""
            items.append(("wdr_enabled", "WDR on/off", "on" if on else "off",
                          f"<tt:WideDynamicRange><tt:Mode>{'ON' if on else 'OFF'}</tt:Mode>{lvl}</tt:WideDynamicRange>",
                          ("WideDynamicRange", "ON" if on else "OFF")))
        if "white_balance_mode" in s:
            m = str(s["white_balance_mode"]).lower()
            if m in ("auto", "manual"):
                items.append(("white_balance_mode", "white balance", m,
                              f"<tt:WhiteBalance><tt:Mode>{m.upper()}</tt:Mode></tt:WhiteBalance>", ("WhiteBalance", m.upper())))
            else:
                row(SENSOR, "white_balance_mode", "white balance", m, "not adjustable", "ONVIF has auto / manual only")
        if "day_night_mode" in s:
            m = str(s["day_night_mode"]).lower()
            ir = {"auto": "AUTO", "day": "ON", "color": "ON", "night": "OFF", "bw": "OFF"}.get(m, "AUTO")
            items.append(("day_night_mode", "day/night", m, f"<tt:IrCutFilter>{ir}</tt:IrCutFilter>", ("IrCutFilter", ir)))
        cur = self.imaging_now() if img_ok else ""
        exp_mode = (re.search(r"<[\w]*:?Exposure>\s*<[\w]*:?Mode>(\w+)<", cur) or [None, None])[1]
        ms = _shutter_ms(s.get("shutter_speed_sec")) if "shutter_speed_sec" in s else None
        if ms is not None:
            if exp_mode:
                us = int(ms * 1000)
                items.append(("shutter_speed_sec", "shutter", s["shutter_speed_sec"],
                              f"<tt:Exposure><tt:Mode>{exp_mode}</tt:Mode><tt:ExposureTime>{us}</tt:ExposureTime></tt:Exposure>",
                              ("ExposureTime", us)))
            else:
                row(SENSOR, "shutter_speed_sec", "shutter", s["shutter_speed_sec"], "not adjustable")
        if "gain" in s:
            if exp_mode:
                items.append(("gain", "gain", s["gain"],
                              f"<tt:Exposure><tt:Mode>{exp_mode}</tt:Mode><tt:Gain>{s['gain']}</tt:Gain></tt:Exposure>",
                              ("Gain", s["gain"])))
            else:
                row(SENSOR, "gain", "gain", s["gain"], "not adjustable")
        for key, label, shown, inner, check in items:
            tag = check[0]
            has = bool(re.search(rf"<[\w]*:?{tag}\b", cur)) or tag in ("ExposureTime", "Gain")
            if not img_ok or not has:
                row(SENSOR, key, label, shown, "not adjustable")
                continue
            m = re.search(rf"<[\w]*:?{tag}>\s*(?:<[\w]*:?Mode>)?([^<\s]+)", cur)
            if m and _same(m.group(1), check[1], tol=1.0):
                row(SENSOR, key, label, shown, "same")
                continue
            ok, why = self.set_img(inner)
            row(SENSOR, key, label, shown, "set" if ok else "refused", "" if ok else why, check if ok else None)
        if "wdr_level" in s and not any(r["key"] == "wdr_enabled" and r["state"] in ("set", "same") for r in res):
            row(SENSOR, "wdr_level", "WDR level", s["wdr_level"], "not adjustable")
        elif "wdr_level" in s:
            row(SENSOR, "wdr_level", "WDR level", s["wdr_level"], "set" if s.get("wdr_enabled") else "not adjustable",
                "" if s.get("wdr_enabled") else "WDR is off")
        if "noise_reduction_3d" in s:
            row(SENSOR, "noise_reduction_3d", "3D noise reduction", s["noise_reduction_3d"], "not adjustable",
                "not in ONVIF")
        if img_ok and any(r["state"] == "set" and r["_check"] for r in res):
            now = self.imaging_now()
            for r in res:
                if r["state"] == "set" and r["_check"]:
                    tag, val = r["_check"]
                    m = re.search(rf"<[\w]*:?{tag}>\s*(?:<[\w]*:?Mode>)?([^<\s]+)", now)
                    if m and not _same(m.group(1), val, tol=1.0):      # scaled values: the camera rounds
                        if tag in self.range:                              # shown in our 0-100 scale
                            lo, hi = self.range[tag]
                            shown = round((float(m.group(1)) - lo) / (hi - lo) * 100) if hi > lo else m.group(1)
                        else:
                            shown = m.group(1)
                        r["state"], r["camera_has"] = "changed", shown

        # ---- encoder
        main = self.profiles[0][2] if self.profiles else None
        sub = self.profiles[1][2] if len(self.profiles) > 1 else None

        def enc(token, which, changes):
            """changes: [(key, label, shown, [(tag regex, new inner), ...])]"""
            if not changes:
                return
            inner = self.get_enc(token) if token else None
            if not inner:
                for key, label, shown, _ in changes:
                    row(ENCODER, key, label, shown, "not adjustable", f"no {which} stream encoder")
                return
            usable = []
            for key, label, shown, subs in changes:
                found = [re.search(rx.replace(r")[^<]*(", r")([^<]*)("), inner, re.S) for rx, _ in subs]
                if not all(found):
                    row(ENCODER, key, label, shown, "not adjustable")
                elif all(_same(m.group(2).strip(), v) for m, (_, v) in zip(found, subs)):
                    row(ENCODER, key, label, shown, "same")
                else:
                    usable.append((key, label, shown, subs))
            if not usable:
                return

            def apply(base, items):
                for _, _, _, subs in items:
                    for rx, rep in subs:
                        base = re.sub(rx, lambda m: m.group(1) + str(rep) + m.group(2), base, count=1, flags=re.S)
                return base
            ok, why = self.set_enc(token, apply(inner, usable))
            if ok:
                for key, label, shown, subs in usable:
                    row(ENCODER, key, label, shown, "set", check=("enc", token, subs))
            else:                   # one at a time: find the ones the camera refuses
                for item in usable:
                    base = self.get_enc(token) or inner
                    ok1, why1 = self.set_enc(token, apply(base, [item]))
                    row(ENCODER, item[0], item[1], item[2], "set" if ok1 else "refused", "" if ok1 else why1,
                        ("enc", token, item[3]) if ok1 else None)

        def T(tag):
            return rf"(<[\w]*:?{tag}>\s*)[^<]*(\s*<)"
        mc = []
        if "codec" in e:
            mc.append(("codec", "codec", e["codec"], [(T("Encoding"), str(e["codec"]).upper().replace(".", ""))]))
        r = _res(e.get("main_stream_resolution")) if "main_stream_resolution" in e else None
        if r:
            mc.append(("main_stream_resolution", "main resolution", f"{r[0]}x{r[1]}", [(T("Width"), r[0]), (T("Height"), r[1])]))
        if "fps" in e:
            mc.append(("fps", "fps", e["fps"], [(T("FrameRateLimit"), _num(e["fps"]))]))
        if "gop_keyframe_interval" in e:
            mc.append(("gop_keyframe_interval", "GOP", e["gop_keyframe_interval"], [(T("GovLength"), _num(e["gop_keyframe_interval"]))]))
        if "bitrate_kbps" in e:
            mc.append(("bitrate_kbps", "bitrate", f"{e['bitrate_kbps']} kbps", [(T("BitrateLimit"), _num(e["bitrate_kbps"]))]))
        enc(main, "main", mc)
        r = _res(e.get("sub_stream_resolution")) if "sub_stream_resolution" in e else None
        if r:
            enc(sub, "sub", [("sub_stream_resolution", "sub resolution", f"{r[0]}x{r[1]}",
                              [(T("Width"), r[0]), (T("Height"), r[1])])])
        for key, label in (("bitrate_type", "bitrate type"), ("smart_codec", "smart codec")):
            if key in e:
                row(ENCODER, key, label, e[key], "not adjustable", "not in ONVIF")
        # read back the encoders
        got = {}
        for r in res:
            ch = r.get("_check")
            if r["state"] == "set" and ch and ch[0] == "enc":
                inner = got.setdefault(ch[1], self.get_enc(ch[1]) or "")
                now, differs = [], False
                for rx, val in ch[2]:
                    m = re.search(rx.replace(r")[^<]*(", r")([^<]*)("), inner, re.S)
                    if m:
                        now.append(m.group(2).strip())
                        differs = differs or not _same(m.group(2).strip(), val)
                if differs:
                    r["state"], r["camera_has"] = "changed", "x".join(now)
        for r in res:
            r.pop("_check", None)
        note = (f"exposure is {exp_mode.lower()}: shutter and gain act only in manual exposure"
                if exp_mode and exp_mode.upper() != "MANUAL" else "")
        return res, note


# ---------------------------------------------------------------------------
def write(cam_json: dict) -> dict:
    """Writes the Sensor and Encoder values into the camera. Returns
    {"ok", "protocol", "results": [{section, key, label, value, state, why, camera_has}], "note", "restart"}."""
    cam = cam_json.get("camera") or {}
    cfg = tp.standardize_config(cam)
    proto = "dahua" if cfg["vendor"] == "dahua" or cfg["ptz_protocol"] == "dahua_cgi" else "onvif"
    try:
        res, note = (_Dahua(cam) if proto == "dahua" else _Onvif(cam)).write(cam_json)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "protocol": proto, "results": [], "note": f"camera settings not written: {e}", "restart": False}
    restart = any(r["state"] in ("set", "changed") and r["section"] == ENCODER and r["key"] in RESTART_KEYS for r in res)
    return {"ok": not any(r["state"] == "refused" for r in res), "protocol": proto, "results": res,
            "note": note, "restart": restart}


def lines(result: dict) -> list:
    """Bottom-bar lines: [{ok, step}]. Refused and changed values are alarms; not adjustable is information."""
    out, res = [], result.get("results") or []
    setv = [f"{r['label']} {r['value']}" for r in res if r["state"] == "set"]
    if setv:
        out.append({"ok": True, "step": f"camera settings written via {result['protocol']}: " + ", ".join(setv)})
    for r in res:
        if r["state"] == "refused":
            out.append({"ok": False, "step": f"{r['label']} {r['value']}: the camera refused it"
                                               + (f" ({r['why']})" if r.get("why") else "")})
        elif r["state"] == "changed":
            out.append({"ok": False, "step": f"{r['label']}: asked {r['value']}, the camera has {r.get('camera_has')}"})
    same = [r["label"] for r in res if r["state"] == "same"]
    if same:
        out.append({"ok": True, "step": "already so on the camera: " + ", ".join(same)})
    na = [r["label"] + (f" ({r['why']})" if r.get("why") else "") for r in res if r["state"] == "not adjustable"]
    if na:
        out.append({"ok": True, "step": "not adjustable on this camera: " + ", ".join(na)})
    if result.get("note"):
        out.append({"ok": result.get("ok", True) or bool(res), "step": result["note"]})
    if result.get("restart"):
        out.append({"ok": True, "step": "encoder changed: the camera restarts its stream, the edge reconnects in a few seconds"})
    return out
