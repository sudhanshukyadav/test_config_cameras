"""Presets on cameras with the "/PresetList" web interface (ptz_protocol "ws_soap", e.g. FLP Gallery 192.168.8.123).

These cameras move over ONVIF but ignore ONVIF GotoPreset. Their own web page (index.js) posts a small SOAP envelope
for every command, with the login in its header (no session):
    <soap:Envelope xmlns:soap="http://www.w3.org/2001/12/soap-envelope"><soap:Header>\t<userid>U</userid>
      \t<passwd>P</passwd></soap:Header><soap:Body>BODY</soap:Body></soap:Envelope>
    U, P = hex of DES("WebLogin", text) (ECB, zero padding to 8 bytes; checked against the page's des.js)
  /PresetList     <xml><cmd>setpreset</cmd><preset>N</preset><flag>1</flag></xml>   store preset N here
                  <xml><cmd>callpreset</cmd><preset>N</preset></xml>                 go to preset N
  /getPresetList  empty body -> <PresetList><p>N</p>...</PresetList>
  /setPTZCmd      <xml><cmd>CMD</cmd><panspeed>S</panspeed><tiltspeed>S</tiltspeed></xml>, <xml><cmd>stop</cmd></xml>
Every answer is kept in .trace (and written to the camera tool's log) so a refusal can be read.
"""
from __future__ import annotations

import json
import re
import time

import requests

# ---- DES (ECB), enough for the page's des("WebLogin", text, 1, 0): no package needed on central -------------
_PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
        63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4]
_PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
        41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32]
_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
_IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6,
       64, 56, 48, 40, 32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
       61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7]
_FP = [40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30,
       37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
       34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25]
_E = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
      16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1]
_P = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25]
_S = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11]]


def _perm(v: int, table: list, nbits: int) -> int:
    out = 0
    for t in table:
        out = (out << 1) | ((v >> (nbits - t)) & 1)
    return out


def _subkeys(key: bytes) -> list:
    k = _perm(int.from_bytes(key, "big"), _PC1, 64)
    c, d, keys = k >> 28, k & 0xFFFFFFF, []
    for s in _SHIFTS:
        c = ((c << s) | (c >> (28 - s))) & 0xFFFFFFF
        d = ((d << s) | (d >> (28 - s))) & 0xFFFFFFF
        keys.append(_perm((c << 28) | d, _PC2, 56))
    return keys


def _block(b: int, keys: list) -> int:
    b = _perm(b, _IP, 64)
    left, right = b >> 32, b & 0xFFFFFFFF
    for k in keys:
        x = _perm(right, _E, 32) ^ k
        f = 0
        for i in range(8):
            six = (x >> (42 - 6 * i)) & 0x3F
            row, col = ((six & 0x20) >> 4) | (six & 1), (six >> 1) & 0xF
            f = (f << 4) | _S[i][row * 16 + col]
        left, right = right, left ^ _perm(f, _P, 32)
    return _perm((right << 32) | left, _FP, 64)


def des_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    keys = _subkeys(key[:8].ljust(8, b"\0"))
    return b"".join(_block(int.from_bytes(data[i:i + 8], "big"), keys).to_bytes(8, "big")
                    for i in range(0, len(data), 8))


def page_des_hex(text: str) -> str:
    """The page's stringToHex(des("WebLogin", text, 1, 0)): ECB, zero padding to a multiple of 8, lower-case hex."""
    raw = text.encode("latin-1", errors="replace")
    raw += b"\0" * (-len(raw) % 8)
    return des_ecb_encrypt(b"WebLogin", raw).hex()


# ---- the camera -------------------------------------------------------------------------------------------
class IpcRpc:
    # exactly what the camera's page sends (checked with the browser 2026-11-13): the camera's web server matches
    # this content type literally; with "; charset=utf-8" added the request lands in its ONVIF part (SOAP fault)
    FORM = "application/x-www-form-urlencoded"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/157.0.0.0 Safari/537.36")

    def __init__(self, host: str, http_port: int, user: str, password: str, timeout: float = 4.0):
        self.base = f"http://{host}:{http_port}"
        self.timeout = timeout
        self.uid, self.pwd = page_des_hex(user), page_des_hex(password)
        self.trace: list[str] = []
        self.http = requests.Session()

    def _note(self, what: str, text) -> None:
        t = str(text).replace("\n", " ").replace("\r", " ")
        self.trace.append(f"{what}: {t[:240]}")
        del self.trace[:-30]

    def post(self, path: str, body: str = ""):
        env = ('<?xml version="1.0"?><soap:Envelope xmlns:soap="http://www.w3.org/2001/12/soap-envelope">'
               f"<soap:Header>\t<userid>{self.uid}</userid>\t<passwd>{self.pwd}</passwd></soap:Header>"
               f"<soap:Body>{body}</soap:Body></soap:Envelope>")
        try:
            r = self.http.post(self.base + path, data=env.encode("utf-8"), timeout=self.timeout,
                               headers={"Content-type": self.FORM, "X-Requested-With": "XMLHttpRequest",
                                        "Accept": "text/javascript, text/html, application/xml, text/xml, */*",
                                        "Referer": self.base + "/", "User-Agent": self.UA})
        except requests.RequestException as e:
            self._note(f"{path} {body[:60]}", f"{type(e).__name__}: {e}")
            return None
        fault = re.findall(r"<(?:\w+:)?(?:Text|faultstring)[^>]*>([^<]{1,200})<", r.text)
        self._note(f"{path} {body[:60]} HTTP {r.status_code}", ("FAULT " + " | ".join(fault)) if fault else r.text)
        return r if r.status_code == 200 and not fault else None

    def goto_preset(self, n: int) -> bool:
        return self.post("/PresetList", f"<xml><cmd>callpreset</cmd><preset>{int(n)}</preset></xml>") is not None

    def set_preset(self, n: int, name: str = "") -> bool:
        return self.post("/PresetList", f"<xml><cmd>setpreset</cmd><preset>{int(n)}</preset><flag>1</flag></xml>") \
            is not None

    def get_presets(self) -> list:
        r = self.post("/getPresetList")
        if r is None:
            return []
        return [int(x) if x.strip().isdigit() else x.strip() for x in re.findall(r"<p>([^<]*)</p>", r.text)]


def make_ws_soap_driver(tp, cfg: dict):
    """ONVIF for moving (works on these cameras), the camera's /PresetList web interface for presets."""
    class WsSoapDriver(tp.ONVIFSoapDriver):
        def __init__(self, c):
            super().__init__({**c, "ptz_protocol": "onvif_soap"})
            self.rpc = IpcRpc(c["host"], int(c.get("http_port") or 80), c.get("username", "admin"),
                              c.get("password", ""))
            self.last_preset_token = None
            self.last_trace: list = []

        def _log_trace(self, what: str):
            self.last_trace = list(self.rpc.trace[-8:])     # also returned to the page (diagnosis)
            for line in self.last_trace:
                tp.log(self.display_name, f"ipc {what}: {line}")
            self.rpc.trace.clear()

        def goto_preset(self, preset_id) -> bool:
            n = int(str(preset_id).strip() or 0)
            ok = self.rpc.goto_preset(n)
            self._log_trace(f"goto {n}")
            return ok

        def set_preset(self, preset_id, new_name: str | None = None) -> bool:
            n = int(str(preset_id).strip() or 0)
            ok = self.rpc.set_preset(n, new_name or "")
            self.last_preset_token = str(n) if ok else None
            self._log_trace(f"set {n}")
            return ok

        def get_presets(self) -> list:
            p = self.rpc.get_presets()
            self._log_trace("list")
            return p

        def wait_until_idle(self, timeout: float = 8.0):
            time.sleep(2.0)              # no position from these cameras: give the move time
            return self.read_ptz()[0], False

    return WsSoapDriver(cfg)
