#!/usr/bin/env python3
"""flip@camera: the camera's OWN flip (180°), set directly on the camera from the PTZ page (no Save).

Unrelated to camera.flip_stream (page 1), which makes the EDGE (multy.py) flip the picture and is only for a
camera that cannot flip itself. Never both: the picture would be flipped twice.

  Dahua CGI   configManager.cgi VideoInOptions[ch].Flip (vertical) and .Mirror (horizontal), separately
  ONVIF       Media service: GetVideoSourceConfiguration(s) / GetVideoSourceConfigurationOptions /
              SetVideoSourceConfiguration with Extension / Rotate (Mode ON, Degree 180). Many cheap cameras
              do not offer it (the options say so) or flip only one of their streams: check the picture.
handle(cam_json, "read" | "on" | "off") -> {ok, protocol, flip (True / False / None = unknown), supported, log}
"""
from __future__ import annotations

import re

import requests

import osd
import test_protocol as tp


class _Dahua(osd._Dahua):
    """Dahua: Flip = vertical (top <-> bottom), Mirror = horizontal (left <-> right); separate settings."""
    KEY = {"v": "Flip", "h": "Mirror"}

    def read_flip(self, axis: str):
        t = self.get("VideoInOptions")
        k = f"table.VideoInOptions[{self.ch}].{self.KEY[axis]}"
        if k not in t:
            self.log.append(f"Dahua: this camera has no VideoInOptions {self.KEY[axis]} setting")
            return None, False
        return t[k].strip().lower() == "true", True

    def set_flip(self, axis: str, on: bool) -> bool:
        return self.set({f"VideoInOptions[{self.ch}].{self.KEY[axis]}": "true" if on else "false"},
                        f"{self.KEY[axis].lower()} {'on' if on else 'off'}")


class _Onvif(osd._Onvif):
    def config(self):
        r = self.soap(f"<trt:GetVideoSourceConfiguration><trt:ConfigurationToken>{self.vsc}</trt:ConfigurationToken>"
                      "</trt:GetVideoSourceConfiguration>", "GetVideoSourceConfiguration")
        return r.text if r is not None else ""

    def supported(self) -> bool:
        r = self.soap(f"<trt:GetVideoSourceConfigurationOptions><trt:ConfigurationToken>{self.vsc}</trt:ConfigurationToken>"
                      "</trt:GetVideoSourceConfigurationOptions>", "GetVideoSourceConfigurationOptions")
        ok = r is not None and bool(re.search(r"<[\w]*:?Rotate>.*?<[\w]*:?Mode>ON<", r.text, re.S))
        if not ok:
            self.log.append("ONVIF: the camera does not offer a rotate / flip setting (VideoSourceConfigurationOptions)")
        return ok

    def read_flip(self, axis: str):
        if axis == "h":       # ONVIF has ONE command (rotate); no separate horizontal
            self.log.append("ONVIF has only one flip command (used by Vertical); set the horizontal flip in the "
                            "camera's own page")
            return None, False
        x = self.config()
        m = re.search(r"<[\w]*:?Rotate>.*?<[\w]*:?Mode>(\w+)<", x, re.S)
        if not m:
            return None, self.supported()
        return m.group(1).upper() == "ON", True

    def set_flip(self, axis: str, on: bool) -> bool:
        if axis == "h":
            self.log.append("ONVIF has only one flip command (used by Vertical); set the horizontal flip in the "
                            "camera's own page")
            return False
        x = self.config()
        g = lambda tag: (re.search(rf"<[\w]*:?{tag}>([^<]*)<", x) or [None, ""])[1]     # noqa: E731
        b = re.search(r'<[\w]*:?Bounds([^/>]*)/?>', x)
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', b.group(1))) if b else {}
        if not g("SourceToken") or not attrs:
            self.log.append("ONVIF: could not read the video source configuration")
            return False
        bounds = " ".join(f'{k}="{attrs.get(k, "0")}"' for k in ("x", "y", "width", "height"))
        body = (f'<trt:SetVideoSourceConfiguration><trt:Configuration token="{self.vsc}">'
                f"<tt:Name>{g('Name') or self.vsc}</tt:Name><tt:UseCount>{g('UseCount') or 1}</tt:UseCount>"
                f"<tt:SourceToken>{g('SourceToken')}</tt:SourceToken><tt:Bounds {bounds}/>"
                f"<tt:Extension><tt:Rotate><tt:Mode>{'ON' if on else 'OFF'}</tt:Mode>"
                f"{'<tt:Degree>180</tt:Degree>' if on else ''}</tt:Rotate></tt:Extension>"
                "</trt:Configuration><trt:ForcePersistence>true</trt:ForcePersistence></trt:SetVideoSourceConfiguration>")
        return self.soap(body, f"flip {'on' if on else 'off'} (SetVideoSourceConfiguration)") is not None


def handle(cam_json: dict, action: str, axis: str = "v") -> dict:
    """axis "v" = vertical flip (top <-> bottom), "h" = horizontal flip (left <-> right, mirror)."""
    axis = "h" if axis == "h" else "v"
    name = "vertical" if axis == "v" else "horizontal"
    log: list[str] = []
    cam = cam_json.get("camera") or {}
    proto = osd.protocol_of(cam)
    try:
        dev = _Dahua(cam, log) if proto == "dahua" else _Onvif(cam, log)
        ok = True
        if action in ("on", "off"):
            ok = dev.set_flip(axis, action == "on")
            tp.log(osd.camera_id(cam_json), f"camera flip {name} {action} via {proto}: {'OK' if ok else 'FAIL'}")
        flip, sup = dev.read_flip(axis)
        if action in ("on", "off") and ok and flip is not None and flip != (action == "on"):
            ok = False
            log.append("the camera accepted the setting but reads back unchanged")
        return {"ok": ok, "protocol": proto, "axis": axis, "flip": flip, "supported": sup, "log": log}
    except (requests.RequestException, RuntimeError, ValueError) as e:
        log.append(f"{type(e).__name__}: {e}")
        return {"ok": False, "protocol": proto, "axis": axis, "flip": None, "supported": False, "log": log}
