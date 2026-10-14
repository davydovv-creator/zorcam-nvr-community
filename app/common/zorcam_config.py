#!/usr/bin/env python3
"""Zorcam settings: /var/lib/zorcam-nvr/config/zorcam.conf (INI format) and the camera list .../config/cameras.json.

A shared module of the detector (zorcam-nvr-capture), the gallery (zorcam-nvr-gallery) and go2rtc; install.sh puts a copy
next to each of them. Camera passwords live in secrets.env; gallery users in users.json (zorcam_users.py).
Cameras are added in the setup wizard and on the Settings page of the gallery.

    python3 zorcam_config.py                # current values, cameras and file errors
    python3 zorcam_config.py --example      # example file with every parameter
    python3 zorcam_config.py --rtsp-source ID   # RTSP URL of camera ID with the password, for go2rtc (echo:)
    python3 zorcam_config.py --go2rtc-streams FILE  # go2rtc streams, one per camera, see zorcam-nvr-go2rtc.service
    python3 zorcam_config.py --camera-ids [--enabled]   # camera IDs, one per line (install.sh); --enabled: switched on
"""
import base64
import configparser
import hashlib
import json
import os
import re
import socket
import stat
import sys
import threading
from datetime import datetime
from urllib.parse import quote, unquote, urlparse

sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))  # zorcam_i18n.py lies next to this file
from zorcam_i18n import Msg  # noqa: E402

# всё, что пишут сервисы (пользователь zorcam-nvr): настройки и записи; программа — в /opt/zorcam-nvr, только для чтения
STATE_DIR = os.environ.get("ZORCAM_STATE_DIR", "/var/lib/zorcam-nvr")
CONFIG_DIR = os.path.join(STATE_DIR, "config")
CONFIG_FILE = os.environ.get("ZORCAM_CONFIG", os.path.join(CONFIG_DIR, "zorcam.conf"))
SECRETS_FILE = os.environ.get("ZORCAM_SECRETS", os.path.join(CONFIG_DIR, "secrets.env"))
CAMERAS_FILE = os.environ.get("ZORCAM_CAMERAS", os.path.join(CONFIG_DIR, "cameras.json"))
# motion zones: a separate file, not cameras.json (every change of that one restarts the detectors and go2rtc)
MOTION_ZONES_FILE = os.environ.get("ZORCAM_MOTION_ZONES", os.path.join(CONFIG_DIR, "motion_zones.json"))

# (section, key, default, type, description, used by); Russian descriptions are in zorcam_i18n.PARAM_RU
PARAMS = [
    ("language", "language", "auto", str,
     "Default language of the interface and push notifications on devices where no language is picked "
     "(a language is picked on each device, on the Settings page): auto (device/browser language), en or ru",
     "gallery"),

    ("capture", "motion_threshold", 0.02, float,
     "Motion analysis sensitivity threshold (0.02 = 2%). Lower is more sensitive", "capture"),
    ("capture", "stall_timeout", 15, int,
     "Reconnect when the camera sends no frames for longer than this, s (at least 3 keyframe intervals)", "capture"),
    ("capture", "rtsp_timeout", 10, int,
     "Timeout of RTSP network operations in ffmpeg, s", "capture"),

    ("live", "webrtc_fallback_seconds", 8, int,
     "If WebRTC does not connect within this many seconds, the player switches to HLS", "gallery"),
    ("live", "hls_idle_timeout", 30, int,
     "The HLS stream stops when the player has not requested it for this many seconds", "gallery"),

    ("notifications", "motion_cooldown", 60, int,
     "At most one motion notification per this many seconds", "gallery"),
    ("notifications", "health", True, bool,
     "Push notifications about failures and recovery", "gallery"),
    ("notifications", "health_repeat_minutes", 60, int,
     "Remind about an unresolved failure every this many minutes (0: no reminders)", "gallery"),

    ("health", "check_interval", 60, int,
     "Check the system state every this many seconds", "gallery"),
    ("health", "no_frames_seconds", 120, int,
     "Failure when the camera sends no frames for longer than this, s", "gallery"),
    ("health", "min_free_disk_mb", 1024, int,
     "Below this much free disk space, MB, the oldest recordings are removed (a push to admins)", "gallery"),
    ("health", "max_cpu_temp", 80, int,
     "Failure when the CPU temperature is above this, °C", "gallery"),

    ("gallery", "videos_per_page", 30, int,
     "Clips per page on the Videos tab: 10, 30 or 60", "gallery"),
    ("gallery", "session_days", 365, int,
     "Stay signed in for this many days without entering the password (extended on every visit)", "gallery"),
    ("gallery", "environment", "prod", str,
     "Which stand this device is: prod, stage or test. Non-prod stands show a TEST / STAGE badge in the app and "
     "default to the cloud of the same stand (cloud.stage.zorcam.ru, cloud.test.zorcam.ru)", "gallery"),
]

# parameters with a fixed set of values (a drop-down list on /config)
CHOICES = {
    ("language", "language"): ("auto", "en", "ru"),
    ("gallery", "videos_per_page"): (10, 30, 60),
    ("gallery", "environment"): ("prod", "stage", "test"),
}
# parameters with a format check: (regex, message key)
PATTERNS = {
}
# obsolete keys: silently skipped when reading the file
DEPRECATED = {("capture", "clip_seconds"), ("capture", "frame_interval"), ("storage", "frames_minutes"),
              ("storage", "videos_days"),  # now per camera
              ("gallery", "max_grid"), ("gallery", "horizons"),  # the Timelapse tab is only the timelapse now
              ("live", "public_ip")}  # the address comes by itself: STUN, the address the app is opened at, TURN of the cloud
SECTIONS = ("language", "capture", "live", "notifications", "health", "gallery")

_BOOL = {"yes": True, "true": True, "on": True, "1": True, "no": False, "false": False, "off": False, "0": False}


def _parse(value, typ, choices=None, pattern=None):
    value = value.strip()
    if pattern and not re.match(pattern[0], value):
        raise ValueError(Msg(pattern[1]))
    if choices:
        try:
            number = typ(value)
        except ValueError:
            number = None
        if number not in choices:
            raise ValueError(Msg("cfg.allowed", values=", ".join(str(c) for c in choices)))
        return number
    if typ is bool:
        if value.lower() not in _BOOL:
            raise ValueError(Msg("cfg.yes_no"))
        return _BOOL[value.lower()]
    if typ is list:
        try:
            items = [int(v) for v in value.replace(" ", "").split(",") if v]
        except ValueError:
            raise ValueError(Msg("cfg.positive_list")) from None
        if not items or min(items) <= 0:
            raise ValueError(Msg("cfg.positive_list"))
        return items
    if typ is str:
        return value
    try:
        number = typ(value)
    except ValueError:
        raise ValueError(Msg("cfg.not_a_number")) from None
    if number < 0:
        raise ValueError(Msg("cfg.negative"))
    return number


def format_value(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return f"{value:g}" if isinstance(value, float) else str(value)


def _error(e):
    """The Msg carried by a ValueError from _parse."""
    return e.args[0] if e.args and isinstance(e.args[0], Msg) else Msg("raw", text=str(e))


class Config:
    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self.exists = os.path.isfile(path)
        self.errors = []
        self.values = {(s, k): d for s, k, d, *_ in PARAMS}
        self.overridden = set()
        if not self.exists:
            return
        parser = configparser.ConfigParser(interpolation=None, strict=False)  # a repeated [section] extends the earlier one
        try:
            with open(path, encoding="utf-8") as f:
                parser.read_file(f)
        except (OSError, configparser.Error) as e:
            self.errors.append(Msg("cfg.unreadable", error=str(e)))
            return
        types = {(s, k): t for s, k, _, t, *_ in PARAMS}
        for section in parser.sections():
            for key, raw in parser.items(section):
                if (section, key) in DEPRECATED:
                    continue
                if (section, key) not in types:
                    self.errors.append(Msg("cfg.unknown", section=section, key=key))
                    continue
                try:
                    self.values[(section, key)] = _parse(raw, types[(section, key)], CHOICES.get((section, key)),
                                                           PATTERNS.get((section, key)))
                    self.overridden.add((section, key))
                except ValueError as e:
                    self.errors.append(Msg("cfg.bad_value", section=section, key=key, raw=raw, error=_error(e)))

    def get(self, section, key):
        return self.values[(section, key)]


def load(path=CONFIG_FILE):
    return Config(path)


def example(values=None):
    lines = [
        "# Zorcam settings. Path on the device: /var/lib/zorcam-nvr/config/zorcam.conf",
        "# Lines starting with # are comments. A commented-out parameter takes its default value.",
        "# Cameras are in cameras.json and their passwords in secrets.env: add and edit them on the Settings page.",
        "# Edit here or on the /config page (a user with the admin role).",
        "# The detector and the gallery notice changes to this file and restart within a minute.",
        "# Check the file: sudo -u zorcam-nvr python3 /opt/zorcam-nvr/gallery/zorcam_config.py",
    ]
    section = None
    for s, k, default, _typ, desc, who in PARAMS:
        if s != section:
            section = s
            lines += ["", f"[{s}]"]
        value = (values or {}).get((s, k), default)
        line = f"{k} = {format_value(value)}" if value != default else f"# {k} = {format_value(default)}"
        lines += [f"# {part}" for part in f"{desc} ({who})".split("\n")] + [line]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- secrets and RTSP

def read_secrets(path=SECRETS_FILE):
    """KEY=VALUE from secrets.env (read on every call, so changes apply without a restart)."""
    values = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    except OSError:
        pass
    return values


def write_secret(key, value, path=SECRETS_FILE):
    """Replace or add KEY=VALUE in secrets.env; other lines are kept, mode 600."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    out, done = [], False
    for line in lines:
        if line.strip().startswith(key + "="):
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{key}={value}")
    _atomic_write(path, "\n".join(out) + "\n", 0o600)


def remove_secret(key, path=SECRETS_FILE):
    """Remove KEY=... from secrets.env; other lines are kept, mode 600."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return
    _atomic_write(path, "\n".join(line for line in lines if not line.strip().startswith(key + "=")) + "\n", 0o600)


def _rtsp_request(sock, uri, cseq, auth=None):
    """One RTSP DESCRIBE on an open connection: ("401 Unauthorized", {header: [values]})."""
    req = f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\nUser-Agent: Zorcam\r\n"
    if auth:
        req += f"Authorization: {auth}\r\n"
    sock.sendall((req + "\r\n").encode())
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > 65536:  # not an RTSP answer (another service on this port): do not read it forever
            raise ConnectionError("not an RTSP answer")
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("the camera closed the connection")
        data += chunk
    head, body = data.split(b"\r\n\r\n", 1)
    lines = head.decode(errors="replace").split("\r\n")
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers.setdefault(key.strip().lower(), []).append(value.strip())
    length = (headers.get("content-length") or ["0"])[0]
    length = min(int(length), 65536) if length.isdigit() else 0
    while len(body) < length:  # дочитать SDP, чтобы следующий запрос на том же соединении начался с начала
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return lines[0].split(" ", 1)[-1].strip(), headers


def _rtsp_auth(challenges, user, password, uri):
    """Authorization header for a 401 answer: Digest when the camera offers it, otherwise Basic."""
    for ch in challenges:
        if ch.lower().startswith("digest"):
            f = dict(re.findall(r'(\w+)="?([^",]*)"?', ch[6:]))
            md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
            ha1, ha2 = md5(f"{user}:{f.get('realm', '')}:{password}"), md5(f"DESCRIBE:{uri}")
            nonce = f.get("nonce", "")
            if "auth" in f.get("qop", "").split(","):
                cnonce = os.urandom(8).hex()
                resp = md5(f"{ha1}:{nonce}:00000001:{cnonce}:auth:{ha2}")
                extra = f', qop=auth, nc=00000001, cnonce="{cnonce}"'
            else:
                resp, extra = md5(f"{ha1}:{nonce}:{ha2}"), ""
            return (f'Digest username="{user}", realm="{f.get("realm", "")}", nonce="{nonce}", uri="{uri}", '
                    f'response="{resp}"{extra}')
    for ch in challenges:
        if ch.lower().startswith("basic"):
            return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    return None


def probe_camera(url, timeout=5):
    """Why the camera stream is unavailable, in the camera's own words ("401 Unauthorized", "Connection refused",
    "timed out"), or None when it answers DESCRIBE with 200 OK. The password stays inside this process: no ffmpeg
    command line (seen by everyone in ps) is involved."""
    u = urlparse(url)
    if not u.hostname:
        return "no camera address"
    user, password = unquote(u.username or ""), unquote(u.password or "")
    uri = u._replace(netloc=u.netloc.rsplit("@", 1)[-1]).geturl()
    try:
        with socket.create_connection((u.hostname, u.port or 554), timeout=timeout) as sock:
            sock.settimeout(timeout)
            status, headers = _rtsp_request(sock, uri, 1)
            if status.startswith("401") and user:
                auth = _rtsp_auth(headers.get("www-authenticate", []), user, password, uri)
                if auth:
                    status, headers = _rtsp_request(sock, uri, 2, auth)
    except OSError as e:
        return e.strerror or str(e) or type(e).__name__
    return None if status.startswith("200") else status


# ---------------------------------------------------------------- cameras

# Cameras: /var/lib/zorcam-nvr/config/cameras.json, {"cameras": [{field: value}]} in the order they are shown.
# (key, default, type, description); the form shows cam.label.<key> (and cam.hint.<key>) of zorcam_i18n.STRINGS
CAMERA_FIELDS = [
    ("id", "", str,
     "Camera ID: the name of its recordings folders /var/lib/zorcam-nvr/recordings/<id> and <id>_videos and of its go2rtc stream "
     "(Latin letters, digits, - and _). It cannot be changed later"),
    ("name", "", str,
     "Camera name: the title above its Live player, its tab on Videos and Timelapse, the notification text"),
    ("enabled", True, bool,
     "Camera on. Off: no connection, motion detection, recording or Live; its recordings and settings stay"),
    ("audio", False, bool,
     "Sound in Live, as the camera sends it, never re-encoded on the device: WebRTC plays G.711 (PCMA, PCMU) and Opus, "
     "HLS plays AAC; another codec stays silent. Clips get the sound too, but only AAC (G.711 does not fit "
     "MPEG-TS and iPhone does not play it in mp4); the motion detector takes only the video"),
    ("rtsp_url", "", str,
     "Camera RTSP address without login and password, e.g. rtsp://192.168.1.50:554/live/0/MAIN"),
    ("rtsp_sub_url", "", str,
     "Optional: the camera's second, low-resolution stream (e.g. 640x360) for the motion detector, RTSP address "
     "without login and password; the same login and password. Motion detection, snapshots and clip posters use "
     "it, Live and motion clips keep the main stream. Empty: the main stream for everything"),
    ("rtsp_user", "admin", str,
     "Camera login for RTSP; without a password the camera is used without authentication"),
    ("onvif_url", "", str,
     "Optional: the camera's ONVIF address for the pan/tilt/zoom buttons in Live, "
     "e.g. http://192.168.1.50:8000/onvif/device_service (a home network address); the same login and password as RTSP. "
     "Empty: the buttons are inactive"),
    ("notify_motion", True, bool,
     "Push notifications on motion from this camera"),
    ("save_videos", True, bool,
     "Save motion clips: the camera's main stream as it is, full frame rate, from 3 s before the motion, never "
     "re-encoded on the device (off: motion is still detected and notified)"),
    ("clip_seconds", 10, int,
     "Motion clip length, s (2 to 120)"),
    ("videos_days", 5, int,
     "How long to keep motion clips, days (1 to 365)"),
    ("save_frames", True, bool,
     "Save snapshots for the Timelapse tab"),
    ("frames_interval", 5, int,
     "Save a snapshot at most once per this many seconds (1 to 3600). Motion detection still uses every keyframe: "
     "fewer snapshots spare the SD card"),
    ("frames_days", 10, int,
     "How long to keep snapshots, days (1 to 365); a snapshot every 5 s takes about 1 to 2 GB a day"),
]
# the camera fields of older versions (record_mode, video_height: clips from keyframes, re-encoded on the Pi) are
# ignored in cameras.json and dropped when it is saved
# numeric camera fields: allowed range and the error message
CAMERA_RANGES = {"clip_seconds": (2, 120, "cfg.clip_seconds"),
                 "videos_days": (1, 365, "cfg.days"), "frames_interval": (1, 3600, "cfg.frames_interval"),
                 "frames_days": (1, 365, "cfg.days")}
CAMERA_PATTERNS = {
    "id": (r"^(?!.*_videos$)[A-Za-z0-9][A-Za-z0-9_-]{0,31}$", "cfg.camera_id"),
    "name": (r"^.{1,40}$", "cfg.camera_name"),
    "rtsp_url": (r"^rtsps?://[^\s/@]+(/\S*)?$", "cfg.rtsp_url"),
    "onvif_url": (r"^(https?://[^\s/@]+(/\S*)?)?$", "cfg.onvif_url"),
    "rtsp_sub_url": (r"^(rtsps?://[^\s/@]+(/\S*)?)?$", "cfg.rtsp_url"),
}
SUB = ".sub"  # go2rtc stream of a camera's second stream: <id>.sub (a dot cannot be in a camera ID)
DATA_DIR = os.path.join(STATE_DIR, "recordings")
# the cameras through go2rtc on this Pi (go2rtc.yaml: rtsp.listen): no password here, ffmpeg command lines are public (ps)
LOCAL_RTSP = "rtsp://127.0.0.1:8554/"


def password_key(cam_id):
    """secrets.env key of the camera password: ZORCAM_CAMERA_PASSWORD_FRONT_DOOR for front-door."""
    return "ZORCAM_CAMERA_PASSWORD_" + re.sub(r"[^A-Z0-9]", "_", cam_id.upper())


def validate_camera(form, taken=()):
    """Check a camera from the form or from cameras.json; return (camera, {field: Msg}).
    taken: IDs of the other cameras (the ID and its password key must be unique)."""
    cam, errors = {}, {}
    for key, default, typ, _desc in CAMERA_FIELDS:
        raw = form.get(key, default)
        raw = format_value(raw) if not isinstance(raw, str) else raw
        try:
            cam[key] = _parse(raw, typ, None, CAMERA_PATTERNS.get(key))
        except ValueError as e:
            errors[key] = _error(e)
    for key, (low, high, msg) in CAMERA_RANGES.items():
        if key not in cam:
            continue
        if not low <= cam[key] <= high:
            errors[key] = Msg(msg)
            del cam[key]
    if "id" in cam and any(password_key(other) == password_key(cam["id"]) for other in taken):
        errors["id"] = Msg("cfg.camera_id_taken")
    return cam, errors


def load_cameras(path=None):
    """Cameras from cameras.json in the order they are shown; broken entries are skipped."""
    try:
        with open(path or CAMERAS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    cams = []
    for raw in data.get("cameras", []) if isinstance(data, dict) else []:
        if not isinstance(raw, dict):
            continue
        cam, errors = validate_camera(raw, [c["id"] for c in cams])
        if not errors:
            cams.append(cam)
    return cams


def save_cameras(cams, path=None):
    text = json.dumps({"cameras": [{k: c.get(k, d) for k, d, *_ in CAMERA_FIELDS} for c in cams]}, ensure_ascii=False, indent=1)
    _atomic_write(path or CAMERAS_FILE, text + "\n", 0o644)


def get_camera(cam_id, cams=None):
    return next((c for c in (load_cameras() if cams is None else cams) if c["id"] == cam_id), None)


def camera_password(cam_id):
    return read_secrets().get(password_key(cam_id), "")


def set_camera_password(cam_id, password):
    """Save the password (an empty one removes it: the camera without authentication or with none set)."""
    if password:
        write_secret(password_key(cam_id), password)
    else:
        remove_secret(password_key(cam_id))


ZONE_COLS, ZONE_ROWS = 16, 9  # the grid over the frame; a cell is 10×10 pixels of the detector's 160×90 frame
ZONE_MODES = ("exclude", "include")  # what the painted cells mean: not counted / the only ones counted
_ZONES_LOCK = threading.Lock()


def valid_zone(mode, cells):
    """mode is one of ZONE_MODES, cells is ZONE_COLS × ZONE_ROWS characters 0/1 row by row (the cell (col, row) is
    at row × ZONE_COLS + col)."""
    return (mode in ZONE_MODES and isinstance(cells, str) and len(cells) == ZONE_COLS * ZONE_ROWS
            and set(cells) <= {"0", "1"})


def load_zones(path=None):
    """{camera ID: {"mode", "cells"}} from motion_zones.json; a broken file or entry means no zones (the whole frame
    is watched)."""
    try:
        with open(path or MOTION_ZONES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {cam_id: {"mode": z["mode"], "cells": z["cells"]} for cam_id, z in data.items()
            if isinstance(z, dict) and valid_zone(z.get("mode"), z.get("cells"))}


def save_zone(cam_id, mode, cells, path=None):
    """Zones of a camera; nothing painted (or None) removes its entry. The file is 600; unchanged is not rewritten."""
    path = path or MOTION_ZONES_FILE
    with _ZONES_LOCK:
        zones = load_zones(path)
        if cells and "1" in cells:
            if not valid_zone(mode, cells):
                raise ValueError("motion zones: bad mode or cells")
            zones[cam_id] = {"mode": mode, "cells": cells}
        elif zones.pop(cam_id, None) is None:
            return
        _atomic_write(path, json.dumps(zones, indent=1) + "\n", 0o600)


def data_dirs(cam_id):
    """Folders of frames and clips of a camera: recordings/<id>, <id>_videos."""
    return os.path.join(DATA_DIR, cam_id), os.path.join(DATA_DIR, cam_id + "_videos")


MOTION_MARK = "_m"  # суффикс снимка, в интервале которого было движение: <ГГГГММДД_ЧЧММСС_мс>_m.jpg
FRAME_NAME_RE = re.compile(r"(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)_(\d{3})(_m)?\.jpg")


def frame_name(now, motion=False):
    """Имя снимка (без .jpg) по времени съёмки; motion — детектор видел движение с прошлого сохранённого снимка."""
    return now.strftime("%Y%m%d_%H%M%S_") + f"{now.microsecond // 1000:03d}" + (MOTION_MARK if motion else "")


def parse_frame_name(name):
    """Имя файла снимка -> (datetime без часового пояса, было ли движение), или None, если это не снимок.
    Единственное место, которое знает формат имени; остальной код берёт время из начала имени (первые 15 символов)
    и от суффикса _m не зависит."""
    m = FRAME_NAME_RE.fullmatch(name)
    if not m:
        return None
    try:
        when = datetime(*(int(g) for g in m.groups()[:6]), int(m.group(7)) * 1000)
    except ValueError:
        return None
    return when, m.group(8) is not None


def capture_status_file(cam_id):
    """State of the camera's detector for the gallery (monitoring, Settings), in memory."""
    return f"/dev/shm/zorcam_capture_{cam_id}.json"


def local_rtsp_url(cam_id, sub=False):
    return LOCAL_RTSP + cam_id + (SUB if sub else "")


def rtsp_url(cam, password=None, sub=False):
    """The full RTSP URL of a camera: address and login from cameras.json, password from secrets.env.
    No password (or no login): the camera is used without authentication. sub — its second stream (rtsp_sub_url)."""
    base, user = cam.get("rtsp_sub_url" if sub else "rtsp_url", ""), cam.get("rtsp_user", "")
    if password is None:
        password = camera_password(cam["id"]) if base and user else ""
    if not base or not user or not password:
        return base
    u = urlparse(base)
    netloc = quote(user, safe="") + ":" + quote(password, safe="") + "@" + u.netloc.rsplit("@", 1)[-1]
    return u._replace(netloc=netloc).geturl()


def go2rtc_streams(path):
    """go2rtc streams, one per camera (the second -c file of zorcam-nvr-go2rtc.service). The URL with the password is
    built by this script when go2rtc connects (echo:), so the password is never written to a go2rtc file."""
    me = os.path.abspath(__file__)
    lines = ["# generated from /var/lib/zorcam-nvr/config/cameras.json by zorcam_config.py --go2rtc-streams"]
    cams = [c for c in load_cameras() if c.get("enabled", True)]  # выключенная камера: go2rtc к ней не подключается
    streams = [f'  "{c["id"]}": echo:python3 {me} --rtsp-source {c["id"]}' for c in cams]
    streams += [f'  "{c["id"]}{SUB}": echo:python3 {me} --rtsp-source {c["id"]} sub' for c in cams if c["rtsp_sub_url"]]
    lines += ["streams:"] + streams if cams else ["streams: {}"]
    _atomic_write(path, "\n".join(lines) + "\n", 0o644)


def _atomic_write(path, text, mode):
    """Write through a temporary file and a rename. A file that would not change is left alone: every write of
    zorcam.conf, cameras.json or secrets.env restarts go2rtc and the detector (Live drops for a few seconds)."""
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                if stat.S_IMODE(os.fstat(f.fileno()).st_mode) != mode:
                    os.chmod(path, mode)
                return
    except (OSError, ValueError):  # no file yet, or not text
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def save_config(values, path=CONFIG_FILE):
    """zorcam.conf: a description for every parameter; values other than the default as active lines."""
    _atomic_write(path, example(values), 0o644)


def validate(section, key, raw):
    """Check a value from the form; return (value, None) or (None, Msg)."""
    typ = next(t for s, k, _d, t, *_ in PARAMS if s == section and k == key)
    try:
        return _parse(raw, typ, CHOICES.get((section, key)), PATTERNS.get((section, key))), None
    except ValueError as e:
        return None, _error(e)


def signature(paths=None):
    """Changes with the given files (by default zorcam.conf, cameras.json and secrets.env): the detector restarts
    on any of them, the gallery on zorcam.conf only (it reads the cameras on every request)."""
    sig = []
    for path in paths or (CONFIG_FILE, CAMERAS_FILE, SECRETS_FILE):
        try:
            st = os.stat(path)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


EDITIONS = ("community", "box")
# редакция: box — наше устройство Zorcam Box (образ ставит ZORCAM_EDITION=box ./install.sh), community — бесплатная для
# своих Pi. Файл лежит в /opt/zorcam-nvr рядом с папками программы: сервисам он только для чтения, в /config его нет
EDITION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "EDITION")


def edition(path=None):
    try:
        with open(path or EDITION_FILE) as f:
            value = f.read().strip()
    except OSError:
        return "community"
    return value if value in EDITIONS else "community"


def sd_notify(message):
    """A message to systemd (READY=1, WATCHDOG=1) via $NOTIFY_SOCKET; does nothing without systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
    except OSError:
        pass


if __name__ == "__main__":
    if "--example" in sys.argv:
        sys.stdout.write(example())
        sys.exit(0)
    if "--rtsp-source" in sys.argv:  # for go2rtc: streams.<id> = echo:python3 zorcam_config.py --rtsp-source <id>
        args = sys.argv[sys.argv.index("--rtsp-source") + 1:]
        cam = get_camera(args[0]) if args else None
        if not cam:
            sys.exit("no such camera in cameras.json")
        sub = args[1:2] == ["sub"]
        # звук — только в основном потоке и только если он включён у камеры: go2rtc отдаёт его как есть
        print(rtsp_url(cam, sub=sub) + ("" if cam.get("audio") and not sub else "#media=video"))
        sys.exit(0)
    if "--go2rtc-streams" in sys.argv:
        go2rtc_streams(sys.argv[sys.argv.index("--go2rtc-streams") + 1])
        sys.exit(0)
    if "--camera-ids" in sys.argv:
        print("\n".join(c["id"] for c in load_cameras() if "--enabled" not in sys.argv or c.get("enabled", True)))
        sys.exit(0)
    cfg = load()
    print(f"File: {cfg.path}" + ("" if cfg.exists else " (missing: all values are defaults)"))
    section = None
    for s, k, *_ in PARAMS:
        if s != section:
            section = s
            print(f"\n[{s}]")
        mark = "" if (s, k) in cfg.overridden else "   # default"
        print(f"{k} = {format_value(cfg.get(s, k))}{mark}")
    cams = load_cameras()
    print(f"\nCameras: {CAMERAS_FILE}" + ("" if cams else " (none yet: add them on the Settings page)"))
    for c in cams:  # without passwords: this output goes into debug.sh save archives
        auth = f"user {c['rtsp_user']} with a password" if c["rtsp_user"] and camera_password(c["id"]) \
            else "no authentication"
        print(f"  {c['id']}: {c['name']} | {c['rtsp_url']} | {auth}")
    if cfg.errors:
        print("\nErrors:")
        for e in cfg.errors:
            print(f"  - {e}")
        sys.exit(1)
