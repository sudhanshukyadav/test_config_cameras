"""Camera date and time (PTZ page, "Sync time"): read the camera's clock and set it from central's clock.

Central's clock = the clock of the PC this camera tool runs on (Aarav). The button first reads the camera, then:
  - time server set (camera_time.ntp_server): ticks the camera's automatic synchronisation (NTP) with it
    (Dahua: also sets the clock to central's time now),
  - no time server set and the camera already syncs automatically (NTP on) within max_offset_s of central:
    leaves the camera's time settings alone and only reports (its working sync is not switched off),
  - otherwise: sets the camera's clock to central's time now (Dahua CGI: local time, NTP left as it was;
    ONVIF: UTC, type Manual - the log says when this switches a drifting NTP off);
  then reads the clock back and reports the offset from central in seconds.
The camera's own time zone and DST are kept (ONVIF: re-sent as read) unless camera_time.posix_tz is set.

Settings: pull.json "settings" -> "camera_time" (all optional; CAMERA_TIME_DEFAULT is used for what is missing):
  ntp_server      ""   time server for the camera's automatic sync ("" = keep the camera's own sync if it works)
  ntp_port        123
  update_minutes  60   how often the camera asks the time server
  posix_tz        ""   time zone sent to ONVIF cameras, POSIX form, e.g. UTC-04:00 ("" = keep the camera's zone)
  max_offset_s    5    above this the reply says the clock is off
  timeout_s       4    camera HTTP / SOAP time-out
Dahua: the camera's own time zone and DST stay as set in the camera (Settings -> System -> General -> Date & Time).
"""
import datetime
import re

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

import test_protocol as tp

CAMERA_TIME_DEFAULT = {"ntp_server": "", "ntp_port": 123, "update_minutes": 60, "posix_tz": "",
                       "max_offset_s": 5, "timeout_s": 4}


def settings(pull: dict) -> dict:
    s = dict(CAMERA_TIME_DEFAULT)
    s.update(((pull or {}).get("settings") or {}).get("camera_time") or {})
    return s


def central_now() -> tuple[datetime.datetime, datetime.datetime]:
    """(local, utc) of central's clock, both naive."""
    utc = datetime.datetime.now(datetime.timezone.utc)
    return utc.astimezone().replace(tzinfo=None), utc.replace(tzinfo=None)


def central_posix_tz() -> str:
    """Central's offset as a POSIX zone (sign reversed: UTC+4 -> 'UTC-04:00')."""
    off = datetime.datetime.now().astimezone().utcoffset() or datetime.timedelta(0)
    m = int(off.total_seconds() // 60)
    sign = "-" if m >= 0 else "+"
    m = abs(m)
    return f"UTC{sign}{m // 60:02d}:{m % 60:02d}"


def kind_of(cam: dict) -> str:
    cfg = tp.standardize_config(cam)
    return "dahua" if cfg["vendor"] == "dahua" or "dahua" in cfg["ptz_protocol"] else "onvif"


# ---- Dahua CGI ---------------------------------------------------------------------------------------------
class Dahua:
    def __init__(self, cam: dict, s: dict):
        cfg = tp.standardize_config(cam)
        self.base = f"http://{cfg['host']}:{cfg['http_port']}/cgi-bin"
        self.user, self.pw, self.t = cfg["username"], cfg["password"], float(s["timeout_s"])

    def get(self, path: str, params: dict) -> str:
        url = f"{self.base}/{path}"
        r = requests.get(url, params=params, auth=HTTPDigestAuth(self.user, self.pw), timeout=self.t)
        if r.status_code == 401:
            r = requests.get(url, params=params, auth=HTTPBasicAuth(self.user, self.pw), timeout=self.t)
        if r.status_code != 200:
            raise RuntimeError(f"{path} {params.get('action')}: HTTP {r.status_code} {r.text.strip()[:80]}")
        return r.text

    def read(self) -> dict:
        t = self.get("global.cgi", {"action": "getCurrentTime"})
        m = re.search(r"result=(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", t)
        out = {"camera_time": m.group(1) if m else None, "zone": "camera local"}
        try:
            n = self.get("configManager.cgi", {"action": "getConfig", "name": "NTP"})
            kv = dict(re.findall(r"table\.NTP\.(\w+)=(.*)", n))
            out["ntp"] = {"enabled": kv.get("Enable", "").strip().lower() == "true",
                          "server": kv.get("Address", "").strip(), "minutes": kv.get("UpdatePeriod", "").strip()}
        except Exception as e:
            out["ntp"] = {"error": str(e)}
        return out

    def offset(self, cam_time: str | None) -> float | None:
        if not cam_time:
            return None
        local, _ = central_now()
        return round((datetime.datetime.strptime(cam_time, "%Y-%m-%d %H:%M:%S") - local).total_seconds(), 1)

    def sync(self, s: dict, log: list, before: dict) -> None:
        ntp = before.get("ntp") or {}
        off = self.offset(before.get("camera_time"))
        if not s["ntp_server"] and ntp.get("enabled") and off is not None and abs(off) <= float(s["max_offset_s"]):
            log.append(f"already syncs automatically (NTP {ntp.get('server') or '?'}), {off:+.1f} s from central: "
                       "left as it is")
            return
        local, _ = central_now()
        self.get("global.cgi", {"action": "setCurrentTime", "time": local.strftime("%Y-%m-%d %H:%M:%S")})
        log.append(f"clock set to central's time {local:%Y-%m-%d %H:%M:%S}")
        if s["ntp_server"]:
            self.get("configManager.cgi", {"action": "setConfig", "NTP.Enable": "true",
                                           "NTP.Address": s["ntp_server"], "NTP.Port": str(s["ntp_port"]),
                                           "NTP.UpdatePeriod": str(s["update_minutes"])})
            log.append(f"automatic sync ticked: NTP {s['ntp_server']}:{s['ntp_port']} every {s['update_minutes']} min")
        else:
            log.append("no time server set (camera_time.ntp_server): the camera's own NTP setting kept "
                       f"({'on, ' + (ntp.get('server') or '?') if ntp.get('enabled') else 'off'})")


# ---- ONVIF -------------------------------------------------------------------------------------------------
class Onvif:
    def __init__(self, cam: dict, s: dict):
        self.drv = tp.ONVIFSoapDriver(tp.standardize_config(cam))
        self.t = float(s["timeout_s"])

    def soap(self, body: str) -> str:
        r = self.drv._post_soap(body, timeout=self.t, service="device")
        if r is None:
            raise RuntimeError(f"ONVIF: {self.drv.last_error or 'no answer'}")
        return r.text

    def read(self) -> dict:
        t = self.soap("<tds:GetSystemDateAndTime/>")
        utc = re.search(r"UTCDateTime>(.*?)</[\w]*:?UTCDateTime", t, re.S)
        val = None
        if utc:
            f = {k: int(re.search(rf"<[\w]*:?{k}>(\d+)<", utc.group(1)).group(1))
                 for k in ("Year", "Month", "Day", "Hour", "Minute", "Second")}
            val = f"{f['Year']:04d}-{f['Month']:02d}-{f['Day']:02d} {f['Hour']:02d}:{f['Minute']:02d}:{f['Second']:02d}"
        typ = re.search(r"DateTimeType>(\w+)<", t)
        tz = re.search(r"<[\w]*:?TZ>([^<]*)<", t)
        dst = re.search(r"DaylightSavings>(\w+)<", t)
        out = {"camera_time": val, "zone": "UTC", "type": typ.group(1) if typ else None,
               "tz": tz.group(1) if tz else None, "dst": (dst.group(1).lower() == "true") if dst else False}
        try:   # where the camera takes its time from (shown, not needed for the sync)
            n = self.soap("<tds:GetNTP/>")
            dhcp = re.search(r"FromDHCP>(\w+)<", n)
            servers = re.findall(r"<[\w]*:?(?:IPv4Address|IPv6Address|DNSname)>([^<]+)<", n)
            out["ntp"] = {"enabled": out["type"] == "NTP", "from_dhcp": bool(dhcp and dhcp.group(1).lower() == "true"),
                          "server": ", ".join(dict.fromkeys(servers))}
        except Exception as e:
            out["ntp"] = {"enabled": out["type"] == "NTP", "error": str(e)}
        return out

    def offset(self, cam_time: str | None) -> float | None:
        if not cam_time:
            return None
        _, utc = central_now()
        return round((datetime.datetime.strptime(cam_time, "%Y-%m-%d %H:%M:%S") - utc).total_seconds(), 1)

    def zone_xml(self, s: dict, before: dict) -> tuple[str, str]:
        """(DaylightSavings, TimeZone element): the camera's own zone and DST unless posix_tz is set."""
        tz = s["posix_tz"] or before.get("tz") or ""
        dst = "true" if before.get("dst") else "false"
        return dst, (f"<tds:TimeZone><tt:TZ>{tz}</tt:TZ></tds:TimeZone>" if tz else "")

    def sync(self, s: dict, log: list, before: dict) -> None:
        off = self.offset(before.get("camera_time"))
        on_ntp = before.get("type") == "NTP"
        if not s["ntp_server"] and on_ntp and off is not None and abs(off) <= float(s["max_offset_s"]):
            src = (before.get("ntp") or {}).get("server") or "DHCP / camera default"
            log.append(f"already syncs automatically (NTP {src}), {off:+.1f} s from central: left as it is")
            return
        dst, tzx = self.zone_xml(s, before)
        tz = s["posix_tz"] or before.get("tz") or "camera's own"
        if s["ntp_server"]:
            self.soap(f"""<tds:SetNTP><tds:FromDHCP>false</tds:FromDHCP><tds:NTPManual>
                <tt:Type>{'IPv4' if re.fullmatch(r'[\d.]+', s['ntp_server']) else 'DNS'}</tt:Type>
                <tt:{'IPv4Address' if re.fullmatch(r'[\d.]+', s['ntp_server']) else 'DNSname'}>{s['ntp_server']}</tt:{'IPv4Address' if re.fullmatch(r'[\d.]+', s['ntp_server']) else 'DNSname'}>
                </tds:NTPManual></tds:SetNTP>""")
            self.soap(f"""<tds:SetSystemDateAndTime><tds:DateTimeType>NTP</tds:DateTimeType>
                <tds:DaylightSavings>{dst}</tds:DaylightSavings>{tzx}
                </tds:SetSystemDateAndTime>""")
            log.append(f"automatic sync ticked: NTP {s['ntp_server']}, zone {tz}")
            return
        _, u = central_now()
        self.soap(f"""<tds:SetSystemDateAndTime><tds:DateTimeType>Manual</tds:DateTimeType>
            <tds:DaylightSavings>{dst}</tds:DaylightSavings>{tzx}
            <tds:UTCDateTime><tt:Time><tt:Hour>{u.hour}</tt:Hour><tt:Minute>{u.minute}</tt:Minute>
            <tt:Second>{u.second}</tt:Second></tt:Time><tt:Date><tt:Year>{u.year}</tt:Year>
            <tt:Month>{u.month}</tt:Month><tt:Day>{u.day}</tt:Day></tt:Date></tds:UTCDateTime>
            </tds:SetSystemDateAndTime>""")
        log.append(f"clock set to central's time {u:%Y-%m-%d %H:%M:%S} UTC, zone {tz}")
        if on_ntp:
            log.append(f"its automatic sync (NTP) was {off:+.1f} s off from central: switched to Manual - "
                       "set camera_time.ntp_server to sync it from a reliable server"
                       if off is not None else "its automatic sync (NTP) switched to Manual")
        else:
            log.append("no time server set (camera_time.ntp_server): automatic sync not ticked")


def handle(data: dict, action: str, pull: dict) -> dict:
    """action "read": the camera's clock and its offset from central · "sync": set it from central (+ NTP)."""
    cam = (data or {}).get("camera") or {}
    if not cam.get("host"):
        return {"ok": False, "log": ["no camera host"]}
    s = settings(pull)
    kind = kind_of(cam)
    log: list = []
    try:
        c = Dahua(cam, s) if kind == "dahua" else Onvif(cam, s)
        if action == "sync":
            c.sync(s, log, c.read())
        elif action != "read":
            return {"ok": False, "log": [f"unknown action {action}"]}
        r = c.read()
    except Exception as e:
        log.append(f"{kind}: {type(e).__name__}: {e}")
        return {"ok": False, "kind": kind, "log": log}
    if action == "sync" and s["ntp_server"]:   # did the camera really take the time server?
        got = ((r.get("ntp") or {}).get("server") or "").strip()
        if not got:
            log.append(f"the camera does not report its time server: check on its web page that it is {s['ntp_server']}")
        elif s["ntp_server"] not in got:
            r["ntp_mismatch"] = True
            log.append(f"WARNING: the camera answered OK but still uses {got}, not {s['ntp_server']}: "
                       "set the time server on the camera's own web page (Date & Time / NTP)")
    off = c.offset(r.get("camera_time"))
    local, _ = central_now()
    r.update({"ok": True, "kind": kind, "central_time": f"{local:%Y-%m-%d %H:%M:%S}", "offset_s": off,
              "off": off is None or abs(off) > float(s["max_offset_s"]) or bool(r.get("ntp_mismatch")), "max_offset_s": s["max_offset_s"]})
    log.append("camera clock: " + (f"{r['camera_time']} ({r['zone']}), {off:+.1f} s from central"
                                   if off is not None else "could not be read"))
    r["log"] = log
    tp.log(cam.get("name") or cam.get("host"), f"time {action}: " + "; ".join(log))
    return r
