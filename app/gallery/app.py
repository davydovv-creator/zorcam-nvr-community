#!/usr/bin/env python3
import os
import posixpath
import collections
import base64
import calendar
import gzip
import hashlib
import heapq
import ipaddress
import queue
import socket
import sys
import json
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template
from urllib.parse import urlparse, unquote, parse_qs, quote, urlencode

from PIL import Image

sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))  # запуск из репозитория
sys.path.insert(2, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "storage"))
import zorcam_config  # noqa: E402
import zorcam_users  # noqa: E402
import onvif  # noqa: E402
import zorcam_cloud  # noqa: E402
import brand  # noqa: E402
import zorcam_storage  # noqa: E402
import yandex  # noqa: E402
import mqtt  # noqa: E402
import stun  # noqa: E402
import zorcam_i18n as i18n  # noqa: E402
from zorcam_i18n import Msg, Raw, t  # noqa: E402
from zorcam_html import cat, esc, h, js_json, trusted  # noqa: E402

CFG = zorcam_config.load()
try:  # секрет детектора для /internal/motion; нет прав на папку настроек — движение не принимается, остальное работает
    INTERNAL_TOKEN = zorcam_config.internal_token(create=True)
except OSError as e:
    INTERNAL_TOKEN = None
    print(f"internal token unavailable, motion signals are refused: {e}", flush=True)

try:
    import push as webpush  # push.py рядом с app.py; нужен python3-cryptography
except ImportError as e:
    webpush = None
    print(f"push notifications disabled: {e}", flush=True)

DEBUG = os.environ.get("ZORCAM_DEBUG") == "1"  # app/debug.sh on
CHILD_ENV = {k: v for k, v in os.environ.items() if k != "NOTIFY_SOCKET"}  # иначе systemctl шлёт свой статус в наш сокет
VERSION = "0.5.2"  # номер релиза; меняется вместе с CHANGELOG.md и тегом X.Y.Z


def installed_build():
    """Установленный коммит (файл BUILD пишет install.sh), или VERSION — запуск из клона."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "BUILD")) as f:
            return f.read().split()[0]
    except (OSError, IndexError):
        return VERSION


BASE_DIR = zorcam_config.DATA_DIR  # /var/lib/zorcam-nvr/recordings; кадры и ролики камеры — в <id> и <id>_videos
PORT = 80
TUNNEL_PORT = zorcam_cloud.TUNNEL_PORT  # второй порт, только 127.0.0.1: туда приходит туннель облака (frpc), см. Handler.on_tunnel
MAX_CONNECTIONS = 96  # одновременно обрабатываемых соединений на порт; сверх — сразу закрываем (каждое — поток)
HOST_IPS_MAX = 64  # записей в кеше DNS адресов, по которым открыта галерея (Host приходит от клиента)
ENVIRONMENT = CFG.get("gallery", "environment")  # prod, stage или test: стенд этого устройства
VIDEOS_PER_PAGE = CFG.get("gallery", "videos_per_page")
TABS = ["live", "records"]  # вкладки нижней панели; «Записи» — переключатель двух панелей RECORD_TABS
RECORD_TABS = ("all", "videos")  # панели «Записи» (порядок — как в переключателе): Timelapse и Видео (старые ссылки ?tab=videos и ?tab=all работают)
RECORDS_DEFAULT = "all"  # «Записи» открываются на Timelapse (решение владельца 13.10.2026)
DEFAULT_TAB = "live"

LIVE_DIR = "/dev/shm/zorcam_live"  # HLS камеры — в <id>/, в оперативной памяти, чтобы не изнашивать SD-карту
LIVE_IDLE_TIMEOUT = CFG.get("live", "hls_idle_timeout")  # сек без запросов плеера — ffmpeg останавливается
WEBRTC_FALLBACK = CFG.get("live", "webrtc_fallback_seconds")
stun.configure(ENVIRONMENT, CFG.get("live", "stun_servers"))
GO2RTC_WEBRTC_URL = "http://127.0.0.1:1984/api/webrtc?src="  # + ID камеры; API go2rtc слушает только localhost
MAX_SDP_SIZE = 65536
HLS_JS_FILE = "hls.min.js"  # hls.js 1.7.3 (Apache-2.0, текст — static/hls.LICENSE.txt) лежит в static/ как есть, без CDN
WEBRTC_PORT = 8555  # webrtc.listen в go2rtc.yaml; этот же порт проброшен на роутере

ROLE_LEVEL = {"liveviewer": 0, "viewer": 1, "admin": 2}  # liveviewer — только Live, без записей и push
SESSION_COOKIE = "zorcam_session"
SESSION_MAX_AGE = 86400 * max(1, CFG.get("gallery", "session_days"))  # продлевается при заходе, не чаще раза в сутки
SESSION_REFRESH = 60 * 60 * 24
# сессии на диске, чтобы перезапуск галереи не разлогинивал; хранятся только хэши токенов
SESSIONS_FILE = os.path.join(zorcam_config.CONFIG_DIR, "sessions.json")
THUMB_DIR = "/dev/shm/zorcam_thumbs"  # уменьшенные превью для сеток, в оперативной памяти
THUMB_SIZE = (320, 180)
THUMB_PRUNE_INTERVAL = 600  # сек: удаление превью, чьи исходные кадры уже стёрты
# сек: удаление записей старше сроков камер; сроки — в днях, а проверка возраста — это stat каждого файла
# (десятки тысяч снимков), так что не при каждой проверке состояния
CLEANUP_INTERVAL = 600
# папка записей -> (файлов, байт, когда посчитано); пишет Health.cleanup() раз в CLEANUP_INTERVAL, читает «Настройки»
RECORDINGS_STATS = {}
# места меньше min_free_disk_mb: удалить самые старые записи, чтобы стало min_free_disk_mb + столько — с запасом,
# а не понемногу на каждой проверке
FREE_MARGIN_MB = 512
LANG_SETTING = CFG.get("language", "language")  # auto — язык устройства (Accept-Language), иначе en или ru
PUSH_COOLDOWN = CFG.get("notifications", "motion_cooldown")  # сек: не чаще одного уведомления о движении
HEALTH_URL = "http://127.0.0.1:%d/healthz"
# счётчик high в memory.events: сколько раз ядро притормаживало процессы детектора у его MemoryHigh;
# растёт — детекторам не хватает памяти, они теряют кадры и переподключаются (камер больше, чем тянет Pi)
CAPTURE_MEMORY_EVENTS = "/sys/fs/cgroup/system.slice/zorcam-nvr-capture.service/memory.events"
MEMORY_HIGH_LIMIT = 100  # столько торможений за одну проверку — уже проблема
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
# адрес значка -> файл в static/; значок из папки brand/ надстройки (brand.py) заменяет одноимённый: ICON_FILES, см. apply_brand
STATIC_FILES = {
    "/apple-touch-icon.png": "apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png": "apple-touch-icon.png",  # iOS ищет и эти адреса, если ссылка не сработала
    "/apple-touch-icon-180x180.png": "apple-touch-icon.png",
    "/icon-192.png": "icon-192.png",
    "/icon-512.png": "icon-512.png",
    "/favicon.ico": "icon-192.png",
}


def icon_url(path):
    """Адрес значка с хешем содержимого: Safari хранит значок домашнего экрана по адресу и, несмотря на no-cache,
    при новом добавлении берёт старый — новый рисунок должен прийти по новому адресу."""
    try:
        with open(ICON_FILES[path], "rb") as f:
            return f"{path}?v={hashlib.sha256(f.read()).hexdigest()[:12]}"
    except OSError:
        return path


def icon_data_url(path):
    """Значок «На экран Домой» прямо в странице: через прокси роутера (KeenDNS и т.п.) iPhone не мог скачать его
    отдельным запросом (тот идёт без входа на прокси) и рисовал букву «Z»; data: качать не нужно."""
    try:
        with open(ICON_FILES[path], "rb") as f:
            return "data:image/png;base64," + base64.b64encode(f.read()).decode()
    except OSError:
        return ICON_URLS[path]


SETUP_COOKIE = "zorcam_setup"  # CSRF мастера первичной настройки, пока нет ни одного пользователя

_sessions_lock = threading.Lock()
SESSIONS = {}
_failed_lock = threading.Lock()
FAILED_ATTEMPTS = {}
MAX_ATTEMPTS = 30  # неудачных входов с одного адреса за ATTEMPT_WINDOW: защита от перебора по многим именам
ATTEMPT_WINDOW = 300
# вход под одним именем закрывается растущей паузой: после USER_LOCK_AFTER неудач подряд — на минуту, каждая следующая
# неудача удваивает паузу до 15 минут; верный вход сбрасывает счёт. Имя считается как набрано (без учёта регистра),
# существует ли оно — не важно: ответ не выдаёт, есть ли такой пользователь
USER_LOCK_AFTER = 5
USER_LOCK_FIRST = 60
USER_LOCK_MAX = 900
USER_FAILS_IDLE = 3600  # сек без неудач после конца паузы — счёт забывается
USER_FAILS_MAX = 2000  # записей в памяти; имена придумывает тот, кто перебирает
USER_FAILS = {}  # имя (casefold) -> [неудач подряд, закрыто до, время последней неудачи]
# Адреса навыка Яндекса открыты из интернета: поверх лимита по IP — общий лимит неудач на всё устройство
# (адреса, с которых перебирают, можно менять)
YANDEX_MAX_FAILURES = 20
YANDEX_FAILURE_WINDOW = 3600
YANDEX_FAILURES = []


def yandex_locked_out():
    now = time.time()
    with _failed_lock:
        YANDEX_FAILURES[:] = [t for t in YANDEX_FAILURES if now - t < YANDEX_FAILURE_WINDOW]
        return len(YANDEX_FAILURES) >= YANDEX_MAX_FAILURES


def yandex_register_failed():
    """Неудача на /yandex/auth или /yandex/token; True — это последняя разрешённая, дальше до конца окна отказ."""
    now = time.time()
    with _failed_lock:
        YANDEX_FAILURES[:] = [t for t in YANDEX_FAILURES if now - t < YANDEX_FAILURE_WINDOW]
        YANDEX_FAILURES.append(now)
        return len(YANDEX_FAILURES) == YANDEX_MAX_FAILURES


def is_locked_out(ip):
    now = time.time()
    with _failed_lock:
        attempts = [t for t in FAILED_ATTEMPTS.pop(ip, []) if now - t < ATTEMPT_WINDOW]
        if attempts:  # без пустых записей: иначе в словаре оставался бы каждый адрес, с которого пытались войти
            FAILED_ATTEMPTS[ip] = attempts
        return len(attempts) >= MAX_ATTEMPTS


def register_failed(ip):
    now = time.time()
    with _failed_lock:
        if len(FAILED_ATTEMPTS) > 1000:  # перебор с многих адресов: забыть тех, чьё окно уже прошло
            for old in [a for a, times in FAILED_ATTEMPTS.items() if now - times[-1] >= ATTEMPT_WINDOW]:
                del FAILED_ATTEMPTS[old]
        FAILED_ATTEMPTS.setdefault(ip, []).append(now)


def login_key(name):
    return (name or "").strip().casefold()[:64]


def user_locked_out(name):
    """Сколько секунд вход под этим именем ещё закрыт (0 — открыт)."""
    now = time.time()
    with _failed_lock:
        entry = USER_FAILS.get(login_key(name))
        return max(0, int(entry[1] - now) + 1) if entry and entry[1] > now else 0


def register_user_failed(name):
    """Неудачный вход под именем. Возвращает паузу (сек), на которую вход закрыт, или 0."""
    now = time.time()
    key = login_key(name)
    with _failed_lock:
        entry = USER_FAILS.get(key)
        if entry is None or now - max(entry[1], entry[2]) > USER_FAILS_IDLE:
            entry = [0, 0.0, now]
        entry[0] += 1
        entry[2] = now
        pause = 0
        if entry[0] >= USER_LOCK_AFTER:
            pause = min(USER_LOCK_MAX, USER_LOCK_FIRST * 2 ** min(entry[0] - USER_LOCK_AFTER, 10))
            entry[1] = now + pause
        USER_FAILS.pop(key, None)
        USER_FAILS[key] = entry  # порядок словаря — по последней неудаче
        if len(USER_FAILS) > USER_FAILS_MAX:  # забыть сначала тех, чья пауза и запас времени прошли, потом самых давних
            for old in [k for k, v in USER_FAILS.items() if v[1] <= now and now - v[2] > USER_FAILS_IDLE]:
                del USER_FAILS[old]
            while len(USER_FAILS) > USER_FAILS_MAX:
                del USER_FAILS[next(iter(USER_FAILS))]
        return pause


def clear_user_failed(name):
    with _failed_lock:
        USER_FAILS.pop(login_key(name), None)


def parse_proxies(raw):
    """Адреса из trusted_proxies: IP через запятую; непонятное пропускается."""
    out = set()
    for part in (raw or "").split(","):
        try:
            out.add(str(ipaddress.ip_address(part.strip())))
        except ValueError:
            pass
    return out


TRUSTED_PROXIES = parse_proxies(CFG.get("gallery", "trusted_proxies"))


def _token_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _load_sessions():
    try:
        with open(SESSIONS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    now = time.time()
    sessions = {}
    for k, v in data.items():
        if isinstance(v, dict) and v.get("exp", 0) > now and v.get("user"):
            sessions[k] = v
    return sessions


def _save_sessions():
    # вызывается под _sessions_lock
    tmp = SESSIONS_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(SESSIONS_FILE), exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(SESSIONS, f)
        os.replace(tmp, SESSIONS_FILE)
    except OSError as e:
        print(f"cannot save sessions: {e}", flush=True)


SESSIONS.update(_load_sessions())


def check_login(username, password):
    """(имя, как оно сохранено; роль) при верных логине и пароле, иначе (None, None). Регистр логина неважен."""
    name = zorcam_users.find(username.strip())
    role = zorcam_users.verify(name, password) if name else zorcam_users.verify(username, password)
    return (name, role) if role else (None, None)


def create_session(user):
    token = secrets.token_hex(32)
    now = time.time()
    with _sessions_lock:
        for k in [k for k, v in SESSIONS.items() if v["exp"] < now]:
            del SESSIONS[k]
        SESSIONS[_token_key(token)] = {"exp": now + SESSION_MAX_AGE, "user": user}
        _save_sessions()
    return token


def check_session(token):
    """(пользователь, роль, продлена ли сессия) или (None, None, False)."""
    if not token:
        return None, None, False
    key = _token_key(token)
    now = time.time()
    with _sessions_lock:
        sess = SESSIONS.get(key)
        if sess is None:
            return None, None, False
        role = zorcam_users.role_of(sess["user"])
        if sess["exp"] < now or role is None:  # истекла или пользователь удалён
            del SESSIONS[key]
            _save_sessions()
            return None, None, False
        if sess["exp"] - now < SESSION_MAX_AGE - SESSION_REFRESH:
            sess["exp"] = now + SESSION_MAX_AGE
            _save_sessions()
            return sess["user"], role, True
        return sess["user"], role, False


def end_sessions(user, keep=None):
    """Завершить все сессии пользователя, кроме keep (токен того, кто меняет свой же пароль или выходит на других
    устройствах). Возвращает, сколько сессий завершено. Общая облачная сессия пользователя (_cloud_sessions) остаётся,
    только если это keep: в ней сидят все его клиенты облака, отличить их нельзя."""
    keep_key = _token_key(keep) if keep else None
    with _sessions_lock:
        gone = [k for k, v in SESSIONS.items() if v["user"] == user and k != keep_key]
        for k in gone:
            del SESSIONS[k]
        if gone:
            _save_sessions()
    with _cloud_sessions_lock:
        shared = _cloud_sessions.get(user)
        if shared and _token_key(shared) != keep_key:
            del _cloud_sessions[user]
    return len(gone)


def is_valid_session(token):
    return check_session(token)[0] is not None


def push_users(cam_id=None):
    """Кому слать push о движении: всем, кроме роли liveviewer; с cam_id — только тем, кому доступна эта камера."""
    return {u for u, v in zorcam_users.load().items()
            if v["role"] != "liveviewer" and (cam_id is None or (can_see(u, v["role"], cam_id)
                                                                 and zorcam_users.wants_push(u, cam_id)))}


def motion_push_url(cam_id, at=None):
    """Адрес, который открывает push о движении: Timelapse камеры на моменте движения (at — YYYYMMDD_HHMMSS);
    времени нет — просто Timelapse камеры."""
    return f"/?tab=all&cam={quote(cam_id, safe='')}" + (f"&at={at}" if at else "")


def motion_push(cam_id, body, at=None):
    """Push о движении тем, кому доступна камера; итог — в журнал."""
    users = push_users(cam_id)
    delivered, total = PUSH.send_all(lambda s: {
        "title": t(push_lang(s), "push.motion"), "body": body, "tag": "motion-" + cam_id,
        "url": motion_push_url(cam_id, at),
    }, log, users=users)
    log(f"motion push {cam_id}: delivered to {delivered} of {total} subscriptions"
        + ("" if total else f" (none of users who can see the camera: {', '.join(sorted(users)) or '-'})"))


_cloud_sessions = {}  # пользователь галереи -> сессия входа через облако
_cloud_sessions_lock = threading.Lock()
_cloud_events = queue.Queue(maxsize=50)  # события движения для облака: один поток, очередь ограничена


CLOUD_EVENT_RETRIES = (5, 30, 120)  # сек: паузы перед повторами — облако или связь пропали ненадолго


def deliver_cloud_event(cam_id, cam_name, name, viewers, sleep=time.sleep):
    ts = time.time()  # время движения, а не удачной попытки
    for pause in (*CLOUD_EVENT_RETRIES, None):
        status = zorcam_cloud.send_event(cam_id, cam_name, name, viewers, ts=ts)
        # не привязана к облаку или 4xx — облако отказало (устройство отвязано): повтор не поможет
        if status in (200, None) or 400 <= status < 500 or pause is None:
            break
        sleep(pause)
    if status != 200:
        log(f"cloud: motion event {cam_id} not delivered ({status or 'no connection'})")
    return status


def cloud_event_loop():
    while True:
        deliver_cloud_event(*_cloud_events.get())


def setup_needed():
    """Мастер первичной настройки: пока нет ни одного пользователя."""
    return not zorcam_users.load()


def is_local_client(ip, forwarded=None):
    """Адрес из домашней сети (или сам Pi), в том числе за обратным прокси: мастер настройки доступен только так —
    иначе первым администратором мог бы стать любой, кто откроет проброшенный порт раньше владельца."""
    for addr in [ip] + [a.strip() for a in (forwarded or "").split(",") if a.strip()]:
        try:
            a = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if not (a.is_private or a.is_loopback or a.is_link_local):
            return False
    return True


# ---------------------------------------------------------------- камеры

_cameras_cache = [None, []]


def cameras():
    """Камеры из cameras.json по порядку; файл перечитывается при изменении — галерея не перезапускается."""
    try:
        st = os.stat(zorcam_config.CAMERAS_FILE)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    if sig != _cameras_cache[0]:
        _cameras_cache[:] = [sig, zorcam_config.load_cameras()]
    return _cameras_cache[1]


def camera(cam_id):
    return next((c for c in cameras() if c["id"] == cam_id), None)


def cam_dirs(cam_id):
    """Имена каталогов кадров и роликов камеры в BASE_DIR: <id>, <id>_videos."""
    return cam_id, cam_id + "_videos"


def known_dirs(cams=None):
    return {d for c in (cameras() if cams is None else cams) for d in cam_dirs(c["id"])}


def user_cameras(user, role):
    """Камеры, доступные пользователю: admin — все, остальным — включённые на вкладке «Польз.»
    (новая камера закрыта всем, пока admin не включит её пользователю)."""
    if ROLE_LEVEL.get(role, -1) >= ROLE_LEVEL["admin"]:
        return cameras()
    allowed = zorcam_users.cameras_of(user)
    return [c for c in cameras() if c["id"] in allowed]


def can_see(user, role, cam_id):
    return any(c["id"] == cam_id for c in user_cameras(user, role))


def drop_session(token):
    if not token:
        return
    with _sessions_lock:
        if SESSIONS.pop(_token_key(token), None) is not None:
            _save_sessions()


def session_cookie(token, secure=False):
    """secure — страница открыта по HTTPS (через облако): cookie не уйдёт по HTTP."""
    return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_MAX_AGE}"
            + ("; Secure" if secure else ""))


def parse_ts(filename):
    try:
        return datetime.strptime(filename[:15], "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def format_caption(filename):
    ts = parse_ts(filename)
    return ts.strftime("%H:%M:%S %d.%m.%Y") if ts else filename


def card_caption(filename):
    """Подпись карточки в «Видео»: время и дата всегда в две строки."""
    ts = parse_ts(filename)
    if not ts:
        return h("div", filename, class_="cap")
    return h("div", h("span", ts.strftime("%H:%M:%S")), h("span", ts.strftime("%d.%m.%Y")), class_="cap")


def list_files(dirname, ext, sort=True):
    try:
        files = [f for f in os.listdir(os.path.join(BASE_DIR, dirname)) if f.lower().endswith(ext)]
    except FileNotFoundError:
        return []
    if sort:
        files.sort(reverse=True)  # имя = YYYYMMDD_HHMMSS_mmm, лексикографически = хронологически
    return files


TIMEZONE_LINK, TIMEZONE_FILE = "/etc/localtime", "/etc/timezone"
TZ_CACHE_SECONDS = 60
_tz_cache = [0.0, None]


def zone_name():
    """Имя часового пояса в настройках системы (Europe/Moscow) или None. Сначала ссылка /etc/localtime: её меняет
    timedatectl, а /etc/timezone в новых Debian может не быть или он остаётся старым."""
    try:
        link = os.readlink(TIMEZONE_LINK)
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        with open(TIMEZONE_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


# пояс процесса задаётся при запуске: glibc не перечитывает /etc/localtime (даже по tzset), поэтому новый пояс
# у детектора (имена записей) и галереи — только после их перезапуска
TZ_AT_START = zone_name()


def device_info(model_paths=("/proc/device-tree/model", "/sys/class/dmi/id/product_name"), meminfo="/proc/meminfo"):
    """Модель устройства (из device tree, на x86 — из DMI) и ОЗУ в МБ (MemTotal) для «Статуса»; чего нет — None.
    MemTotal меньше номинала (часть памяти забирают видеоядро и ядро): номинал — ближайшая степень двойки сверху,
    если до неё не больше четверти, иначе — как есть."""
    model = None
    for path in model_paths:
        try:
            with open(path, "rb") as f:
                model = f.read(200).decode(errors="replace").strip("\0 \n") or None
        except OSError:
            continue
        if model:
            break
    ram = None
    try:
        with open(meminfo) as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    ram = int(line.split()[1]) // 1024
                    break
    except (OSError, ValueError, IndexError):
        pass
    if ram:
        nominal = 1 << (ram - 1).bit_length()
        ram = nominal if ram >= nominal * 3 // 4 else ram
    return model, ram


def fmt_ram(lang, mb):
    return t(lang, "st.ram_gb", n=f"{round(mb / 1024, 1):g}") if mb >= 1024 else t(lang, "st.ram_mb", n=mb)


def pi_timezone():
    """Часовой пояс, по которому идут время записей и снимков: {"name": "Europe/Moscow", "abbr": "MSK",
    "offset": "UTC+03:00", "label": короткое обозначение для показа ("MSK"; если его нет или это цифры вида
    "+03" — смещение), "pending": пояс, выставленный в системе после запуска (нужен перезапуск), или None}.
    Кэш на TZ_CACHE_SECONDS: перезапуска галереи для проверки не нужно."""
    now = time.monotonic()
    if _tz_cache[1] is not None and now - _tz_cache[0] < TZ_CACHE_SECONDS:
        return _tz_cache[1]
    lt = time.localtime()
    off = lt.tm_gmtoff or 0
    offset = f"UTC{'+' if off >= 0 else '-'}{abs(off) // 3600:02d}:{abs(off) % 3600 // 60:02d}"
    abbr = lt.tm_zone or time.strftime("%Z")
    current = zone_name()
    info = {"name": TZ_AT_START or time.tzname[lt.tm_isdst > 0] or offset, "abbr": abbr, "offset": offset,
            "label": abbr if abbr.isalpha() else offset,
            "pending": current if TZ_AT_START and current and current != TZ_AT_START else None}
    _tz_cache[:] = [now, info]
    return info


TIMELAPSE_PERIODS = ("hour", "today")
TIMELAPSE_SECONDS = {"hour": 3600}  # "today" — с полуночи
TIMELAPSE_CACHE_SECONDS = 20  # повторные запросы плеера читают папку не каждый раз
TIMELAPSE_CACHE_ENTRIES = 6  # сутки снимков — ~17 тысяч имён (календарный день — столько же), сотни КБ на запись: на Pi 3 держим немного
TIMELAPSE_DAYS_MAX = 400  # дней в календаре под плеером
_timelapse_cache = {}  # (камера, период | "d:ГГГГ-ММ-ДД" | "days") -> [когда, JSON, он же в gzip или None]
_timelapse_lock = threading.Lock()
_DAY_RE = re.compile(r"\d{4}-\d\d-\d\d")


def parse_timelapse_day(text):
    """'ГГГГ-ММ-ДД' -> date; не дата (в том числе 2026-02-30) — None."""
    if not _DAY_RE.fullmatch(text or ""):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def timelapse_frames(cam_id, period, now=None, day=None):
    """Имена всех кадров камеры за период для таймлапса по возрастанию времени (кадры не прореживаются). Папка
    читается один раз, файлы не открываются — только имена. period: hour, today (с полуночи);
    day (date) — вместо периода: календарный день по часам Pi, с 00:00:00 до 23:59:59."""
    now = now or datetime.now()
    until = None
    if day is not None:
        cutoff = day.strftime("%Y%m%d")  # имя начинается с даты: отсечка по первым 8 знакам
        until = cutoff
    else:
        since = (now.replace(hour=0, minute=0, second=0, microsecond=0) if period == "today"
                 else now - timedelta(seconds=TIMELAPSE_SECONDS[period]))
        cutoff = since.strftime("%Y%m%d_%H%M%S")
    frames = []
    try:
        with os.scandir(os.path.join(BASE_DIR, cam_dirs(cam_id)[0])) as it:
            for e in it:
                if until is not None:
                    if e.name[:8] != until:
                        continue
                elif e.name[:15] < cutoff:  # дешёвая отсечка по строке, без разбора даты
                    continue
                if zorcam_config.parse_frame_name(e.name):
                    frames.append(e.name)
    except FileNotFoundError:
        pass
    frames.sort()  # начало имени — время, поэтому порядок имён и есть порядок времени
    return frames


def timelapse_videos(cam_id, period, now=None, day=None):
    """Имена готовых роликов (есть .mp4, без .failed) за то же окно, что и кадры таймлапса, по возрастанию времени:
    плеер по ним ведёт от метки движения к /watch/<камера>/<имя>. Имя ролика начинается так же, как имя кадра."""
    now = now or datetime.now()
    if day is not None:
        keep = lambda n: n[:8] == day.strftime("%Y%m%d")  # noqa: E731
    else:
        since = (now.replace(hour=0, minute=0, second=0, microsecond=0) if period == "today"
                 else now - timedelta(seconds=TIMELAPSE_SECONDS[period]))
        cutoff = since.strftime("%Y%m%d_%H%M%S")
        keep = lambda n: n[:15] >= cutoff  # noqa: E731
    return sorted(n for n in list_videos(cam_id) if n[8:9] == "_" and n[:8].isdigit() and keep(n))


def timelapse_days(cam_id, now=None):
    """Календарь под плеером: {"days": [{"d": "ГГГГ-ММ-ДД", "frames": N, "clips": M}, …], "today": "ГГГГ-ММ-ДД"} —
    каждый день от самого раннего со снимками или роликами до сегодня (пустые тоже, чтобы в календаре не было дыр),
    не больше TIMELAPSE_DAYS_MAX последних. Только имена: по одному чтению каждой папки, счёт по первым 8 знакам
    имени. Ролик — готовый .mp4, как в списке «Видео»; постер без него (пишется) и .failed не считаются: смотреть в них нечего."""
    today = (now or datetime.now()).date()
    frames, clips = {}, {}
    raw_dir, video_dir = cam_dirs(cam_id)
    try:
        with os.scandir(os.path.join(BASE_DIR, raw_dir)) as it:
            for e in it:
                n = e.name
                if n[8:9] == "_" and n.endswith(".jpg") and n[:8].isdigit():
                    frames[n[:8]] = frames.get(n[:8], 0) + 1
    except FileNotFoundError:
        pass
    try:
        names = os.listdir(os.path.join(BASE_DIR, video_dir))
    except FileNotFoundError:
        names = []
    for n in names:  # как список «Видео»: только готовый .mp4 (постер без него — ролик ещё пишется)
        if n.endswith(".mp4") and n[8:9] == "_" and n[:8].isdigit():
            clips[n[:8]] = clips.get(n[:8], 0) + 1
    found = []
    for key in set(frames) | set(clips):  # мусор вроде 20269999_… отбрасывается один раз на день, а не на файл
        try:
            found.append(datetime.strptime(key, "%Y%m%d").date())
        except ValueError:
            pass
    last = max(found + [today])
    first = max(min(found + [today]), last - timedelta(days=TIMELAPSE_DAYS_MAX - 1))
    days = []
    for i in range((last - first).days + 1):
        d = first + timedelta(days=i)
        key = d.strftime("%Y%m%d")
        days.append({"d": d.isoformat(), "frames": frames.get(key, 0), "clips": clips.get(key, 0)})
    return {"days": days, "today": today.isoformat()}


def timelapse_reply(cam_id, period, gzipped=False, day=None):
    """Тело ответа /timelapse/<камера> (JSON в байтах, при gzipped — сжатое gzip): имена кадров списком строк (время и
    движение плеер берёт из самих имён), адреса превью/полных кадров и часовой пояс Pi, по которому это время (tz).
    day (date) — кадры одного календарного дня вместо периода (period тогда None); period == "days" — вместо кадров
    список дней для календаря (timelapse_days). Кэшируются уже готовые байты на TIMELAPSE_CACHE_SECONDS, не больше
    TIMELAPSE_CACHE_ENTRIES записей."""
    key = (cam_id, "d:" + day.isoformat() if day else period)
    now = time.monotonic()
    with _timelapse_lock:
        entry = _timelapse_cache.get(key)
        if entry and now - entry[0] >= TIMELAPSE_CACHE_SECONDS:
            entry = None
    if entry is None:
        raw_dir = cam_dirs(cam_id)[0]
        if period == "days" and not day:
            reply = timelapse_days(cam_id)
        else:
            tz = pi_timezone()
            reply = {"camera": cam_id, "period": None if day else period, "base": f"/thumb/{raw_dir}/",
                     "full": f"/img/{raw_dir}/", "tz": {"abbr": tz["abbr"], "offset": tz["offset"], "label": tz["label"]},
                     "frames": timelapse_frames(cam_id, period, day=day),
                     # время Pi «как UTC» (как в именах кадров): до него плеер доводит конец шкалы сегодняшнего дня
                     "now": int(calendar.timegm(time.localtime())),
                     "videos": timelapse_videos(cam_id, period, day=day), "clip": (camera(cam_id) or {}).get("clip_seconds") or 10}
            if day:
                reply["day"] = day.isoformat()
        entry = [now, json.dumps(reply, separators=(",", ":")).encode(), None]
        with _timelapse_lock:
            for old in [k for k, v in _timelapse_cache.items() if now - v[0] >= TIMELAPSE_CACHE_SECONDS]:
                del _timelapse_cache[old]
            while len(_timelapse_cache) >= TIMELAPSE_CACHE_ENTRIES:
                del _timelapse_cache[min(_timelapse_cache, key=lambda k: _timelapse_cache[k][0])]
            _timelapse_cache[key] = entry
    if not gzipped:
        return entry[1]
    if entry[2] is None:  # имена сжимаются раз в ~10: через туннель облака это главная экономия
        entry[2] = gzip.compress(entry[1], 5)
    return entry[2]


def accepts_gzip(header):
    """Accept-Encoding клиента допускает gzip (и не запрещает его как gzip;q=0)."""
    for part in (header or "").split(","):
        name, _, params = part.strip().partition(";")
        if name.strip().lower() in ("gzip", "x-gzip"):
            q = params.replace(" ", "").lower()
            return not (q.startswith("q=") and q[2:].strip("0.") == "")
    return False


def list_videos(cam_id, pending=False):
    """Ролики камеры от новых к старым: и постер, и .mp4. pending — все ролики: постер камера пишет при движении,
    .mp4 — когда ролик записан, метку .failed — если он не сохранился; тогда список — [(имя, "ready" | "pending" |
    "failed")]. Одно чтение папки вместо проверки .mp4 каждого ролика по отдельности."""
    try:
        names = os.listdir(os.path.join(BASE_DIR, cam_dirs(cam_id)[1]))
    except FileNotFoundError:
        return []
    have = set(names)
    posters = sorted((n[:-4] for n in names if n.lower().endswith(".jpg")), reverse=True)
    if pending:
        return [(n, "ready" if n + ".mp4" in have else "failed" if n + ".failed" in have else "pending")
                for n in posters]
    return [n for n in posters if n + ".mp4" in have]


def poster_version(video_dir, name):
    """?v=<время изменения> к адресу превью ролика: постер меняется, когда вместо ролика делается заглушка
    с надписью, а превью браузер кэширует на сутки."""
    try:
        return f"?v={int(os.path.getmtime(os.path.join(BASE_DIR, video_dir, name + '.jpg')))}"
    except OSError:
        return ""


def video_meta(video_dir, name):
    """<имя>.json ролика от детектора: {"box": рамка области движения в долях кадра | None, "motion_at": секунды от
    начала ролика | None}. Нет файла (старые ролики), он испорчен или в нём ничего нет — None; значения проверяются:
    они попадают в страницу."""
    try:
        with open(os.path.join(BASE_DIR, video_dir, name + ".json"), "rb") as f:
            data = json.loads(f.read(4096))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    box, at = data.get("box"), data.get("motion_at")
    if not (isinstance(box, list) and len(box) == 4 and all(num(v) and 0 <= v <= 1 for v in box)
            and box[0] < box[2] and box[1] < box[3]):
        box = None
    else:
        box = [round(float(v), 3) for v in box]
    at = round(float(at), 1) if num(at) and 0 <= at < 86400 else None
    return {"box": box, "motion_at": at} if box or at is not None else None


def motion_box_html(box):
    """Оранжевая рамка области движения поверх постера: проценты контейнера, в котором кадр во всю площадь; если
    картинка в нём обрезана или с полями, JS (placeBox) пересчитывает по её размеру."""
    x0, y0, x1, y1 = box
    return h("span", class_="mbox", data_box=json.dumps(box),
             style=f"left:{x0 * 100:g}%;top:{y0 * 100:g}%;width:{(x1 - x0) * 100:g}%;height:{(y1 - y0) * 100:g}%")


def watch_media(cam_id, video_dir, name, meta, lang):
    """Плеер страницы просмотра ролика. Есть метаданные детектора — рамка поверх кадра, значок «Движение» и шкала
    с засечкой момента движения (показывает JS страницы по currentTime); нет — просто видео."""
    video = h("video", src=f"/vid/{cam_id}/{name}.mp4", poster=f"/img/{video_dir}/{name}.jpg",
              controls=True, autoplay=True, muted=True, playsinline=True)
    if not meta:
        return video
    at = meta["motion_at"]
    overlay = cat(motion_box_html(meta["box"]) if meta["box"] else "",
                  h("span", t(lang, "watch.motion"), class_="mbadge") if at is not None else "")
    scale = (h("div", h("div", class_="mprog"),
               h("button", type="button", class_="mtick", aria_label=t(lang, "watch.jump"), title=t(lang, "watch.jump")),
               class_="mscale") if at is not None else "")
    return h("div", h("div", video, overlay, class_="vframe", data_motion=at), scale, class_="vwrap")


def login_page(lang, error=""):
    """Страница входа; error — ключ сообщения над формой (неверный пароль, слишком много попыток)."""
    return render_template("login", lang, error=h("div", t(lang, error), class_="error") if error else "")


def watch_neighbours(cam_id, name):
    """Адреса соседних роликов камеры для свайпа на странице просмотра: (более новый, более старый), "" — нет."""
    videos = list_videos(cam_id)  # от новых к старым, как в сетке
    try:
        i = videos.index(name)
    except ValueError:
        return "", ""
    url = lambda n: f"/watch/{cam_id}/{n}"  # noqa: E731
    return (url(videos[i - 1]) if i > 0 else ""), (url(videos[i + 1]) if i + 1 < len(videos) else "")


# иконки нижней панели: тонкие линейные, как SF Symbols; активная вкладка красится цветом текста (currentColor)
ICONS = {
    "live": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" fill="currentColor"/><path d="M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4M5 5a10 10 0 0 0 0 14M19 5a10 10 0 0 1 0 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    "records": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><rect x="2.5" y="6" width="13" height="12" rx="2.5"/><path d="M15.5 10.5 21 7.5v9l-5.5-3"/></svg>',
    "settings": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
}
# кнопки «в ряд» в Live: n прямоугольников (кадров) в ряд — 1, 2, 3, а на широком экране ещё 4
COLS_ICON = {n: '<svg viewBox="0 0 24 24" width="18" height="18">' + "".join(
    f'<rect x="{2 + i * 20 / n + (1 if n > 1 else 0):.3g}" y="4" width="{20 / n - (2 if n > 1 else 0):.3g}" height="16" rx="1.6" '
    f'fill="currentColor"/>' for i in range(n)) + '</svg>' for n in (1, 2, 3, 4)}
RELOAD_ICON = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" '
               'stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v5h-5"/></svg>')
BACK_ICON = '<svg viewBox="0 0 12 20" width="12" height="20" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2 2 10l8 8"/></svg>'
# динамик для кнопки звука в Live: со звуковыми волнами (включён) и перечёркнутый (выключен)
SOUND_ICON = ('<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" '
              'stroke-linecap="round" stroke-linejoin="round"><path d="M11 5L6 9H2v6h4l5 4z" fill="currentColor"/>'
              '<path class="waves" d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/>'
              '<path class="mute" d="M16 9l6 6M22 9l-6 6"/></svg>')
PLAY_ICON = '<svg viewBox="0 0 24 24" width="30" height="30"><rect x="1" y="1" width="22" height="22" fill="rgba(0,0,0,0.6)"/><path d="M10 8l6 4-6 4z" fill="#fff"/></svg>'

# значок стенда рядом с названием: у prod его нет; на обоих языках один и тот же
ENV_BADGE = "" if ENVIRONMENT == "prod" else h("span", ENVIRONMENT.upper(), class_="envbadge")

_MARK_RE = re.compile(r"\[\[([\w.]+)\]\]")
_localized = {}


def localize(template, lang):
    """Шаблон страницы на языке lang: [[ключ]] — текст из zorcam_i18n, [[lang]] — код языка. Результат кэшируется;
    дальше шаблон заполняется через string.Template, поэтому «$» в тексте удваивается."""
    key = (id(template), lang)
    if key not in _localized:
        _localized[key] = _MARK_RE.sub(
            lambda m: lang if m.group(1) == "lang" else t(lang, m.group(1)).replace("$", "$$"), template)
    return _localized[key]


# Service worker: показывает push-уведомления, по нажатию открывает Live.
SW_JS = """self.addEventListener('install', function() { self.skipWaiting(); });
self.addEventListener('activate', function(e) { e.waitUntil(self.clients.claim()); });

self.addEventListener('push', function(e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) {}
  e.waitUntil(self.registration.showNotification(d.title || 'Zorcam', {
    body: d.body || '',
    tag: d.tag || 'zorcam',
    renotify: true,
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    data: { url: d.url || '/?tab=live' }
  }));
});

self.addEventListener('notificationclick', function(e) {
  e.notification.close();
  var url = new URL((e.notification.data && e.notification.data.url) || '/?tab=live', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(list) {
    for (var i = 0; i < list.length; i++) {
      var c = list[i];
      if (new URL(c.url).origin !== self.location.origin) continue;
      // the gallery is open: a push with a moment (at) goes there, the others just switch to Live
      if (new URL(c.url).pathname === '/' && !new URL(url).searchParams.has('at')) {
        c.postMessage({ type: 'open-live' });  // the gallery is already open: switch to Live
        return c.focus();
      }
      if ('navigate' in c) return c.navigate(url).then(function(w) { return (w || c).focus(); });
    }
    return self.clients.openWindow(url);
  }));
});
""".encode()

# яблоко Apple: кнопка подсказки «как установить приложение на iPhone» (видна только в Safari на iPhone)
APPLE_ICON = ('<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M12.152 6.896c-.948 0-2.415'
              '-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 '
              '3.792 3.039 1.52-.065 2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 1.156'
              '-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 2.597-4.559-1.429-2.09'
              '-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052'
              '-2.662.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701"/></svg>')

SHARE_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12M8 7l4-4 4 4"/><path d="M6 11H5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-8a1 1 0 0 0-1-1h-1"/></svg>'

# значок режима просмотра в шапке: «устройство» (открыто напрямую) и «облако» (через облако Zorcam)
_MODE_SVG = ('<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" '
             'stroke-linecap="round" stroke-linejoin="round">%s</svg>')
MODE_ICONS = {
    "nvr": _MODE_SVG % '<rect x="3" y="7" width="18" height="10" rx="1.5"/><path d="M7 12h5"/><path d="M17 12h.01"/>',
    "cloud": _MODE_SVG % '<path d="M7 18a4 4 0 0 1-.7-7.94A5.5 5.5 0 0 1 17 9.6 4.2 4.2 0 0 1 17.5 18z"/>',
}
BELL_ICON = '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/><path class="slash" d="M3 3l18 18"/></svg>'


# Вёрстка лежит в файлах, не в строках кода: templates/*.html (каркасы страниц, string.Template: $имя; тексты — [[ключ]]) и
# static/*.css, static/*.js. Всё читается один раз при старте (install.sh перезапускает галерею при любом их изменении),
# поэтому хеш содержимого в адресе (?v=) считается тоже один раз: браузер хранит файл «вечно» и берёт новый после обновления.
ASSET_TYPES = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}


def load_assets():
    """{имя файла: (содержимое, Content-Type, версия)} для CSS и JS из static/."""
    assets = {}
    for name in sorted(os.listdir(STATIC_DIR)):
        ctype = ASSET_TYPES.get(os.path.splitext(name)[1])
        if ctype:
            with open(os.path.join(STATIC_DIR, name), "rb") as f:
                body = f.read()
            assets[name] = (body, ctype, hashlib.sha256(body).hexdigest()[:12])
    return assets


def load_templates():
    """{имя без .html: текст} для templates/."""
    out = {}
    for name in sorted(os.listdir(TEMPLATE_DIR)):
        if name.endswith(".html"):
            with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as f:
                out[name[:-5]] = f.read().rstrip("\n")
    return out


ASSETS = load_assets()
TEMPLATES = load_templates()
_T_KEY_RE = re.compile(r"""\bT\[['"]([\w.]+)['"]\]""")
# чужой файл (hls.js) не разбираем: ключей T[...] в нём нет, а читать 600 КБ минифицированного кода незачем
JS_KEYS = {name: sorted(set(_T_KEY_RE.findall(body.decode()))) for name, (body, _, _) in ASSETS.items()
           if name.endswith(".js") and name != HLS_JS_FILE}
# подстановки во всех шаблонах: css_app — адрес /static/app.css?v=<хеш>, js_main — /static/main.js?v=<хеш>;
# то, что зависит от папки brand/ (app_head, app_name, touch_icon, theme_css, login_logo), ставит apply_brand
TEMPLATE_VARS = {
    "version": VERSION, "env_badge": ENV_BADGE,
    "back_icon": trusted(BACK_ICON), "apple_icon": trusted(APPLE_ICON), "share_icon": trusted(SHARE_ICON),
    "ptr": trusted(TEMPLATES["ptr"]),
    **{f"{name.rsplit('.', 1)[1]}_{name.rsplit('.', 1)[0]}": f"/static/{name}?v={version}" for name, (_, _, version) in ASSETS.items() if name != HLS_JS_FILE},
}


def apply_brand(folder):
    """Всё, что <head> страниц, манифест, значки и шапка берут из папки brand/ надстройки (brand.Brand): имя приложения,
    цвет, значки, theme.css (последним <link> каждой страницы), знак. Папки нет — то, что было всегда. Вызывается при
    запуске; тест подставляет свою папку."""
    global BRAND, APP_NAME, APP_HEAD, MANIFEST, ICON_FILES, ICON_URLS, TOUCH_ICON_DATA
    BRAND = folder if isinstance(folder, brand.Brand) else brand.Brand(folder, log=lambda msg: print(msg, flush=True))
    base = BRAND.app_name or "Zorcam"
    APP_NAME = base if ENVIRONMENT == "prod" else f"{base} {ENVIRONMENT}"  # заголовок вкладки и подпись под иконкой
    color = BRAND.theme_color or "#f3f3f1"
    # тёмный цвет сборки — тёмная строка состояния iPhone в режиме приложения (default там всегда светлая)
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    bar = "black" if 0.299 * r + 0.587 * g + 0.114 * b < 128 else "default"
    ICON_FILES = {path: BRAND.icons.get(name) or os.path.join(STATIC_DIR, name) for path, name in STATIC_FILES.items()}
    ICON_URLS = {path: icon_url(path) for path in STATIC_FILES}
    TOUCH_ICON_DATA = icon_data_url("/apple-touch-icon.png")
    # «На экран Домой» на iPhone открывает галерею как отдельное приложение, без панелей Safari.
    # В Android и обычном браузере эти теги ничего не меняют (Chrome только предложит установить).
    APP_HEAD = f"""<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="{bar}">
<meta name="apple-mobile-web-app-title" content="{esc(APP_NAME)}">
<meta name="theme-color" content="{esc(color)}">
<link rel="apple-touch-icon" sizes="180x180" href="{TOUCH_ICON_DATA}">
<link rel="icon" type="image/png" sizes="192x192" href="{ICON_URLS['/icon-192.png']}">
<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">
"""
    MANIFEST = json.dumps({
        "name": base,
        "short_name": base,
        "start_url": "/?tab=live",
        "scope": "/",
        "display": "standalone",
        "background_color": color,
        "theme_color": color,
        "icons": [
            {"src": ICON_URLS["/icon-192.png"], "sizes": "192x192", "type": "image/png"},
            {"src": ICON_URLS["/icon-512.png"], "sizes": "512x512", "type": "image/png"},
        ],
    }, ensure_ascii=False).encode()
    TEMPLATE_VARS.update(
        app_head=trusted(APP_HEAD), app_name=esc(APP_NAME), touch_icon=esc(ICON_URLS["/apple-touch-icon.png"]),
        # theme.css — последним <link> страницы, перед </head>; знак — над формой входа
        theme_css=cat(h("link", rel="stylesheet", href=BRAND.url("theme.css")), "\n") if BRAND.url("theme.css") else "",
        login_logo=logo_html("loginlogo"))


def logo_html(cls):
    """Знак надстройки (brand/logo.svg) как картинка; файла нет — ничего."""
    return h("img", class_=cls, src=BRAND.url("logo.svg"), alt="") if BRAND.url("logo.svg") else ""


apply_brand(zorcam_config.BRAND_DIR)
_templates = {}
_texts = {}


def render_template(name, lang, **values):
    """Страница из templates/<name>.html на языке lang; values — готовый HTML (данные в нём экранирует вызывающий: h(), esc())."""
    if (name, lang) not in _templates:
        _templates[name, lang] = Template(localize(TEMPLATES[name], lang))
    return _templates[name, lang].substitute(TEMPLATE_VARS, **values)


def data_script(lang, files, page=None):
    """<script> перед скриптами страницы: window.T — тексты, которые нужны их JS (T['ключ'] в файле), на языке lang,
    window.PAGE — данные страницы (адрес hls.js, ICE-серверы…). «<» в JSON экранирован: строка не закроет <script>."""
    if (lang, files) not in _texts:
        _texts[lang, files] = {k: t(lang, k) for name in files for k in JS_KEYS.get(name, ())}
    code = f"window.T = {js_json(_texts[lang, files])};"
    if page is not None:
        code += f"\nwindow.PAGE = {js_json(page)};"
    return trusted(f"<script>{code}</script>")


# Запрос этого потока пришёл через туннель облака (ставит Handler.parse_request): шапка показывает режим просмотра
REQUEST = threading.local()


def brand_html(lang, cls):
    """Название в шапке: «Zorcam.nvr» (открыто напрямую с устройства) или «Zorcam.cloud» (через облако) со значком режима
    (по наведению или нажатию static/app.js показывает подсказку; открытое напрямую устройство, подключённое к облаку, —
    и ссылку на облако) и значком стенда, если он не prod. Надстройка (brand/) заменяет «Zorcam.nvr» своим названием и
    ставит свой знак перед названием в обоих режимах."""
    mode = "cloud" if getattr(REQUEST, "cloud", False) else "nvr"
    name = (BRAND.name_for(lang) if mode == "nvr" else "") or t(lang, "brand." + mode)
    cloud = yandex_cloud_url() if mode == "nvr" else ""  # адрес этого устройства в облаке, если подключено
    tip = [h("span", t(lang, "brand.tip_" + mode))]
    if cloud:
        tip.append(h("a", t(lang, "brand.open_cloud"), href=cloud + "/"))
    return h("div" if cls == "brand" else "span", logo_html("brandlogo"), h("span", name),
             h("button", trusted(MODE_ICONS[mode]), type="button", class_="modebtn", data_view=mode,
               aria_label=t(lang, "brand.tip_" + mode)),
             h("span", *tip, class_="modetip", hidden=True, role="tooltip"),
             ENV_BADGE, class_=cls)


def clock_html():
    """Часы шапки: время и пояс Pi (pi_timezone), до первого тика JS показано время с сервера."""
    tz = pi_timezone()
    return h("span", time.strftime("%H:%M:%S"), class_="clock", id="clock", data_off=time.localtime().tm_gmtoff or 0,
             title=f'{tz["name"]} ({tz["offset"]})')


# Таймлапс (вкладка Timelapse): кадры за период, 15 к/с, ×1/×2/×4. Кадры подгружаются с опережением
# несколькими запросами и выгружаются после показа: на iPhone в памяти не больше AHEAD картинок.
PAUSE_ICON = ('<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 5.5v13M15.5 5.5v13"/></svg>')
START_ICON = ('<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round" stroke-linejoin="round"><path d="M7.5 5.2v13.6a.6.6 0 0 0 .9.5l11-6.8a.6.6 0 0 0 0-1L8.4 4.7a.6.6 0 0 0-.9.5z"/></svg>')

CHEVRON_LEFT = ('<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.2" '
                'stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg>')
CHEVRON_RIGHT = CHEVRON_LEFT.replace("M15 5l-7 7 7 7", "M9 5l7 7-7 7")
CHEVRON_UP = CHEVRON_LEFT.replace("M15 5l-7 7 7 7", "M5 15l7-7 7 7")
CHEVRON_DOWN = CHEVRON_LEFT.replace("M15 5l-7 7 7 7", "M5 9l7 7 7-7")
ZOOM_IN_ICON = CHEVRON_LEFT.replace("M15 5l-7 7 7 7", "M12 5v14M5 12h14")
ZOOM_OUT_ICON = CHEVRON_LEFT.replace("M15 5l-7 7 7 7", "M5 12h14")
PTZ_BUTTONS = (("left", CHEVRON_LEFT), ("up", CHEVRON_UP), ("down", CHEVRON_DOWN), ("right", CHEVRON_RIGHT),
               ("out", ZOOM_OUT_ICON), ("in", ZOOM_IN_ICON))

def pager(page, pages, total, per_page, lang, href):
    """Навигация по страницам «Видео»; href — адрес страницы без её номера (…&vpage=)."""
    if pages <= 1:
        return ""
    first = (page - 1) * per_page + 1
    last = min(page * per_page, total)

    def link(p, label):
        return h("a", label, class_="pill", href=f"{href}{p}")
    # записи идут от новых к старым: следующая страница — раньше по времени, поэтому она слева, как на шкале времени
    older, newer = t(lang, "pager.older"), t(lang, "pager.newer")
    earlier = link(page + 1, older) if page < pages else h("span", older, class_="pill disabled")
    later = link(page - 1, newer) if page > 1 else h("span", newer, class_="pill disabled")
    info = t(lang, "pager.info", first=first, last=last, total=total, page=page, pages=pages)
    return h("div", earlier, h("span", info, class_="pager-info"), later, class_="pager")


def grid_cells(items, name_of, cell_of):
    """Ячейки сетки «Видео»; перед первой ячейкой каждого дня — заголовок дня (на широком экране он липнет
    к верху при прокрутке, на телефоне скрыт: там дата в подписи карточки)."""
    out, day = [], None
    for item in items:
        name = name_of(item)
        ts = parse_ts(name)
        if ts and name[:8] != day:
            day = name[:8]
            out.append(h("h3", ts.strftime("%d.%m.%Y"), class_="dayhead"))
        out.append(cell_of(item))
    return cat(out)


def no_cameras(lang, role):
    """Камер ещё нет: администратору — ссылка на добавление; камеры есть, но ни одна не доступна — так и сказать."""
    if cameras() and ROLE_LEVEL[role] < ROLE_LEVEL["admin"]:
        return h("p", t(lang, "cams.no_access"), class_="empty")
    add = (cat(" ", h("a", t(lang, "cams.add"), href="/camera/new")) if ROLE_LEVEL[role] >= ROLE_LEVEL["admin"] else "")
    return h("p", t(lang, "cams.none"), add, class_="empty")


# спидометр перед кнопками скорости таймлапса: без него «×0.25 … ×4» непонятно, к чему относятся
SPEED_ICON = ('<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" '
              'stroke-linecap="round"><path d="M3.5 17.5a9 9 0 1 1 17 0"/><path d="M12 14.5l4.5-5"/>'
              '<path d="M12 5.5v1.5M5.6 8.1l1 1M18.4 8.1l-1 1"/></svg>')


CLOSE_ICON = ('<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" '
              'stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>')


def valid_at(at):
    """Момент движения из адреса push: YYYYMMDD_HHMMSS, настоящая дата и время; иное — игнорируется."""
    return bool(at) and re.fullmatch(r"\d{8}_\d{6}", at) is not None and parse_ts(at) is not None


def timelapse_html(cam, lang, at=None):
    """Вкладка Timelapse: плеер таймлапса (его оживляет static/timelapse.js); at — момент (YYYYMMDD_HHMMSS), на котором
    он встаёт (открыт из push о движении)."""
    speeds = cat(h("button", f"×{n}", type="button", class_="pill active" if n == 1 else "pill", data_speed=n)
                 for n in (0.25, 0.5, 1, 2, 4))
    stage = h("div", h("canvas", class_="tl-canvas", width=320, height=180),
              h("div", h("span", t(lang, "watch.motion"), class_="tl-motion"), class_="tl-top"),
              h("div", h("span", class_="tl-time"), class_="tl-bottom"),
              h("div", class_="tl-msg"), class_="tl-stage")
    controls = h("div",
                 h("button", h("span", trusted(START_ICON), class_="i-play"), h("span", trusted(PAUSE_ICON), class_="i-pause"),
                   type="button", class_="tl-play", aria_label=t(lang, "tl.play")),
                 h("input", type="range", class_="tl-range", min=0, max=0, value=0, step=1, disabled=True,
                   aria_label=t(lang, "tl.position")),
                 h("div", class_="tl-strip", aria_label=t(lang, "tl.marks")), class_="tl-controls")
    calendar = h("div",
                 h("button", trusted(CHEVRON_LEFT), type="button", class_="tl-calnav prev", aria_label=t(lang, "tl.days_earlier")),
                 h("div", class_="tl-days", role="group", aria_label=t(lang, "tl.days")),
                 h("button", trusted(CHEVRON_RIGHT), type="button", class_="tl-calnav next", aria_label=t(lang, "tl.days_later")),
                 class_="tl-cal", hidden=True)
    speeds = cat(h("span", trusted(SPEED_ICON), class_="tl-speed-icon", title=t(lang, "tl.speed"), aria_hidden="true"), speeds)
    # окно ролика над плеером (static/timelapse.js кладёт в .tl-modal-body плеер со страницы /watch)
    head = h("div", h("span", class_="tl-modal-time"),
             h("button", trusted(CLOSE_ICON), type="button", class_="tl-modal-x", aria_label=t(lang, "tl.clip_close")),
             class_="tl-modal-head")
    stage_clip = h("div", h("div", class_="tl-modal-body"),
                   h("button", trusted(CHEVRON_LEFT), type="button", class_="tl-clip-arrow tl-clip-prev", hidden=True,
                     aria_label=t(lang, "tl.clip_prev")),
                   h("button", trusted(CHEVRON_RIGHT), type="button", class_="tl-clip-arrow tl-clip-next", hidden=True,
                     aria_label=t(lang, "tl.clip_next")),
                   class_="tl-modal-stage")
    box = h("div", head, stage_clip,
            h("a", t(lang, "tl.clip_download"), href="#", class_="tl-modal-dl", download=True),
            class_="tl-modal-box", role="dialog", aria_modal="true")
    modal = h("div", box, class_="tl-modal", hidden=True)
    return h("div", stage, h("p", class_="tl-note", role="status"), controls,
             h("div", speeds, class_="seg tl-speed", role="group", aria_label=t(lang, "tl.speed")),
             calendar, modal,
             h("p", t(lang, "tl.keys"), class_="tl-keys"),
             class_="tl", data_cam=cam["id"], data_api=f"/timelapse/{cam['id']}", data_at=at)


def ptz_html(c, lang, csrf, opened=False):
    """Блок поворота и зума под плеером: переключатель и кнопки крестом (зум в центре), свёрнутые, кроме Live одной
    камеры (opened); весь скрыт, пока JS не узнает у /ptz/<камера>, что камера это умеет (и какие кнопки показать);
    без адреса ONVIF блока нет."""
    if not c.get("onvif_url"):
        return ""
    buttons = cat(
        h("button", trusted(icon), type="button", class_="ptz-btn", data_dir=d,
          aria_label=t(lang, "ptz." + d), title=t(lang, "ptz." + d)) for d, icon in PTZ_BUTTONS)
    pad = h("div", buttons, h("span", class_="ptz-msg", role="status"), class_="ptz-pad", hidden=not opened)
    toggle = h("button", t(lang, "ptz.toggle"), type="button", class_="ptz-toggle", aria_expanded="true" if opened else "false")
    return h("div", toggle, pad, class_="ptz", role="group",
             aria_label=t(lang, "ptz.group"), data_cam=c["id"], data_csrf=csrf, hidden=True)


def live_cam_html(c, lang, csrf="", link=False, ptz_open=False):
    """Плеер Live одной камеры: название, кадр, статус (его оживляет Player в static/main.js); csrf — для
    кнопок поворота (viewer и выше, не во встроенном плеере); link — название ведёт в Live только этой камеры;
    ptz_open — кнопки поворота сразу раскрыты (Live одной камеры)."""
    audio = bool(c.get("audio"))
    wrap = h("div", h("video", muted=True, playsinline=True, controls=True),
             h("span", t(lang, "js.connecting"), class_="live-status"),
             h("button", trusted(SOUND_ICON), type="button", class_="live-sound", aria_label=t(lang, "live.sound_on"),
               title=t(lang, "live.sound_on")) if audio else "",
             class_="live-wrap", data_cam=c["id"], data_audio=audio)
    name = (h("h2", h("a", h("span", c["name"]), h("span", "›", aria_hidden="true", class_="go"),
                      href=f"/?tab=live&cam={quote(c['id'], safe='')}", aria_label=t(lang, "live.only_cam")),
              class_="camera-name link")
            if link else h("h2", c["name"], class_="camera-name"))
    return h("div", name, wrap, h("div", class_="live-note"),
             ptz_html(c, lang, csrf, ptz_open) if csrf else "", class_="cam")


def page_data(ice=()):
    """window.PAGE главной страницы: что подставлялось в JS через .format (адрес hls.js, ICE-серверы облака, пауза до HLS)."""
    return {"hlsUrl": f"/static/{HLS_JS_FILE}?v={ASSETS[HLS_JS_FILE][2]}",
            "webrtcFallbackMs": WEBRTC_FALLBACK * 1000, "iceServers": list(ice)}


PAGE_SCRIPTS = ("app.js", "timelapse.js", "main.js")  # скрипты главной страницы по порядку; тексты для них — window.T


def render_embed(role, user, lang, cam_id, ice=()):
    """Встроенный Live (?tab=live&cam=ID&embed=1): только плеер одной камеры, без шапки, вкладок и заголовка.
    Камера не названа, закрыта пользователю, выключена или не существует — страница без плеера и с одним и тем же
    сообщением, чтобы по ответу нельзя было узнать, есть ли такая камера."""
    cam = next((c for c in user_cameras(user, role) if c["id"] == cam_id and c.get("enabled", True)), None)
    body = live_cam_html(cam, lang) if cam else h("p", t(lang, "live.embed_unavailable"), class_="empty")
    return render_template("page", lang, body_class="embed", chrome="", tabbar="", panels=body,
                           data_script=data_script(lang, PAGE_SCRIPTS, page_data(ice)))


def render(active_tab, role="viewer", user="", vpage=1, lang=i18n.DEFAULT, cam_id=None, ice=(), csrf="", at=None):
    live_only = role == "liveviewer"
    if live_only or active_tab not in ("live",) + RECORD_TABS:
        active_tab = "live"
    cams = user_cameras(user, role)
    found = next((c for c in cams if c["id"] == cam_id), None)
    cam = found or (cams[0] if cams else None)
    at = at if found and valid_at(at) else None  # момент из push — только для камеры, которая названа и видна
    raw_dir, video_dir = cam_dirs(cam["id"]) if cam else ("", "")
    videos = [] if live_only or not cam else list_videos(cam["id"], pending=True)

    def cam_tabs(tab, items=None, first=None, active=None):
        """Подвкладки камер во «Видео» и Timelapse (переключатель): названия камер; стрелки по краям (static/main.js)
        появляются, когда полоса не помещается и её можно прокрутить. В Live items — включённые камеры, first — вкладка
        «Все» перед ними, active — открытая камера (None — «Все»)."""
        active = cam if items is None else active
        tabs = h("div", first or "",
                 (h("a", c["name"], class_="pill active" if c is active else "pill", href=f"/?tab={tab}&cam={c['id']}")
                  for c in (cams if items is None else items)), class_="horizon-pills camtabs")
        return h("div", tabs,
                 h("button", trusted(CHEVRON_LEFT), type="button", class_="camarr prev", hidden=True,
                   aria_label=t(lang, "cam.tabs_prev")),
                 h("button", trusted(CHEVRON_RIGHT), type="button", class_="camarr next", hidden=True,
                   aria_label=t(lang, "cam.tabs_next")), class_="camtabs-wrap")

    if not cam:
        videos_body = no_cameras(lang, role)
    elif videos:
        pages = max(1, -(-len(videos) // VIDEOS_PER_PAGE))
        vpage = min(max(1, vpage), pages)
        shown = videos[(vpage - 1) * VIDEOS_PER_PAGE: vpage * VIDEOS_PER_PAGE]
        # ролик, который ещё пишется или кодируется, — постер с надписью; JS заменит его роликом, когда тот готов
        prep = t(lang, "videos.preparing")

        def box_html(video_dir, n):  # рамка движения из <имя>.json детектора; у старых роликов файла нет
            meta = video_meta(video_dir, n)
            return motion_box_html(meta["box"]) if meta and meta["box"] else ""

        def card(v):
            # ролик, который не сохранился, — только постер с надписью «Could not save the video», его не открыть
            name, state = v
            kind = {"ready": "card", "failed": "card failed"}.get(state, "card pending")
            return h("a", h("img", src=f"/thumb/{video_dir}/{name}.jpg{poster_version(video_dir, name)}", loading="lazy", alt=""),
                     box_html(video_dir, name) if state != "failed" else "",
                     h("span", trusted(PLAY_ICON), class_="play"), h("span", prep, class_="prep"), card_caption(name),
                     class_=kind, href=f"/watch/{cam['id']}/{name}",
                     data_vid=f"/vid/{cam['id']}/{name}.mp4" if kind == "card pending" else None)
        cells = grid_cells(shown, lambda v: v[0], card)
        nav = pager(vpage, pages, len(videos), VIDEOS_PER_PAGE, lang, f"/?tab=videos&cam={cam['id']}&vpage=")
        videos_body = cat(h("div", cam_tabs("videos"), class_="toolbar"), h("div", cells, class_="grid"), nav)
    else:
        videos_body = cat(h("div", cam_tabs("videos"), class_="toolbar"), h("p", t(lang, "videos.empty"), class_="empty"))

    if not cam:
        all_body = no_cameras(lang, role)
    else:
        all_body = cat(h("div", cam_tabs("all"), class_="toolbar"), timelapse_html(cam, lang, at))

    # Live: камеры в 1, 2 или 3 колонки (выбор хранится на устройстве, см. JS), над каждой — её название;
    # выключенных камер в Live нет, их записи остаются в «Видео» и в Timelapse
    live_cams = [c for c in cams if c.get("enabled", True)]
    ptz_csrf = csrf if csrf and not live_only else ""  # liveviewer не управляет камерами
    # ?tab=live&cam=ID — Live одной камеры (только включённая и доступная; иначе «Все»); выбор нигде не хранится
    # (cam в «Записях» — выбор их камеры, поэтому режим одной камеры только при tab=live)
    single = next((c for c in live_cams if c["id"] == cam_id), None) if len(live_cams) > 1 and active_tab == "live" else None
    live_body = cat(live_cam_html(c, lang, ptz_csrf, link=len(live_cams) > 1 and not single) for c in live_cams)
    # «Переподключить»: в приложении на iPhone жест «потянуть» над видео не срабатывает (касания забирает плеер)
    reload_btn = h("button", trusted(RELOAD_ICON), type="button", class_="pill reload", id="live-reload",
                   aria_label=t(lang, "live.reload"), title=t(lang, "live.reload"))
    # при двух и более камерах над Live всегда полоса «Все» + камеры; «Все» (без cam) — сетка всех камер
    all_tab = h("a", t(lang, "live.all_cams"), class_="pill" if single else "pill active", href="/?tab=live")
    live_tabs = h("div", cam_tabs("live", live_cams, all_tab, single), class_="toolbar") if len(live_cams) > 1 else ""
    if single:
        live_body = cat(live_tabs, h("div", reload_btn, class_="horizon-pills livecols"),
                        h("div", live_cam_html(single, lang, ptz_csrf, ptz_open=True), class_="live-single"))
    elif len(live_cams) > 1:
        # на телефоне 1/2/3, на широком экране (от 1024 px) 2/3/4: лишнее прячет CSS (.only-narrow, .only-wide)
        cols = cat(
            h("button", trusted(COLS_ICON[n]), type="button",
              class_="pill" + (" active" if n == 1 else "") + (" only-narrow" if n == 1 else " only-wide" if n == 4 else ""),
              data_cols=n, aria_label=t(lang, "live.columns", n=n), title=t(lang, "live.columns", n=n))
            for n in (1, 2, 3, 4))
        live_body = cat(live_tabs, h("div", h("div", cols, class_="seg"), reload_btn, class_="horizon-pills livecols"),
                        h("div", live_body, class_="live-grid", data_cols=1))
    elif live_cams:
        live_body = cat(h("div", reload_btn, class_="horizon-pills livecols"), live_body)
    live_body = live_body or no_cameras(lang, role)

    def panel(tab, body):
        head, switch = t(lang, "tab." + ("records" if tab in RECORD_TABS else tab)), ""
        if tab in RECORD_TABS:  # Timelapse / Видео
            switch = h("div", (h("button", t(lang, "tab." + k), type="button", class_="pill active" if k == tab else "pill", data_go=k)
                               for k in RECORD_TABS), class_="seg recseg")
        return h("div", h("h1", head, class_="large"), switch, body, class_="panel", data_panel=tab,
                 data_single=bool(single) if tab == "live" else None, hidden=active_tab != tab)
    panels = cat(panel("live", live_body), "" if live_only else cat(panel("videos", videos_body), panel("all", all_body)))
    bell = ("" if live_only else h("button", trusted(BELL_ICON), class_="bell", id="bell", title=t(lang, "bell"),
                                   aria_label=t(lang, "bell"), data_csrf=csrf))
    chrome = render_template("chrome", lang, brand=brand_html(lang, "appname"), bell=bell, clock=clock_html())
    return render_template("page", lang, body_class="tabbed", chrome=chrome,
                           tabbar=tab_bar(lang, role, active_tab, switch=True, user=user), panels=panels,
                           data_script=data_script(lang, PAGE_SCRIPTS, page_data(ice)))


GO2RTC_PROBE_URL = "http://127.0.0.1:1984/api/streams?video=all&audio=all&src="  # + ID камеры: кодеки её потока


AUDIO_CODECS = ("aac", "pcma", "pcmu", "opus", "pcm", "mp3")
CODEC_NAMES = {"h264": "H.264", "h265": "H.265", "hevc": "H.265", "pcma": "G.711 (PCMA)", "pcmu": "G.711 (PCMU)",
               "pcm_alaw": "G.711 (PCMA)", "pcm_mulaw": "G.711 (PCMU)",
               "aac": "AAC", "opus": "Opus", "mp3": "MP3", "pcm": "PCM"}


def is_audio_codec(name):
    """Кодек звука по имени: go2rtc называет G.711 то pcma/pcmu, то pcm_alaw/pcm_mulaw (любой pcm_*); видео-кодеком
    такое имя считать нельзя, даже если звук идёт в потоке первым."""
    return name in AUDIO_CODECS or name.startswith("pcm")


_codecs = {}  # ID камеры -> (когда спросили, (видео, звук)): «Статус» не спрашивает go2rtc при каждом открытии


def camera_codecs(cam_id, max_age=0):
    """Кодеки основного потока камеры по go2rtc: (видео, звук) — h264, h265, pcma…; None — нет или не ответил.
    max_age — сколько секунд можно отвечать прошлым ответом."""
    cached = _codecs.get(cam_id)
    if max_age and cached and time.time() - cached[0] < max_age:
        return cached[1]
    try:
        with urllib.request.urlopen(GO2RTC_PROBE_URL + quote(cam_id), timeout=5) as resp:
            text = resp.read(200_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None, None
    codecs = re.findall(r'"codec_name":\s*"([a-z0-9_]+)"', text)
    found = (next((c for c in codecs if not is_audio_codec(c)), None), next((c for c in codecs if is_audio_codec(c)), None))
    _codecs[cam_id] = (time.time(), found)
    return found


def camera_audio_codec(cam_id):
    """Кодек звука камеры по go2rtc (aac, pcma, pcmu, opus…), или None: звука нет или go2rtc не ответил."""
    return camera_codecs(cam_id)[1]


def hls_audio_args(cam):
    """Звук в HLS: только AAC и только без перекодирования (MPEG-TS берёт его как есть); иначе HLS без звука."""
    if cam and cam.get("audio") and camera_audio_codec(cam["id"]) == "aac":
        return ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy"]
    return ["-an", "-c:v", "copy"]


class LiveStream:
    """HLS-трансляция камеры по запросу: ffmpeg без перекодирования, работает, пока плеер запрашивает поток."""

    def __init__(self, cam_id):
        self.cam_id = cam_id
        self.dir = os.path.join(LIVE_DIR, cam_id)
        self.lock = threading.Lock()
        self.proc = None
        self.last_access = 0.0
        threading.Thread(target=self._reaper, daemon=True).start()

    def touch(self):
        with self.lock:
            self.last_access = time.time()
            if self.proc is None or self.proc.poll() is not None:
                shutil.rmtree(self.dir, ignore_errors=True)
                os.makedirs(self.dir, exist_ok=True)
                self.proc = subprocess.Popen(
                    [
                        "ffmpeg", "-loglevel", "error", "-rtsp_transport", "tcp",
                        "-i", zorcam_config.local_rtsp_url(self.cam_id),
                        *hls_audio_args(camera(self.cam_id)), "-f", "hls", "-hls_time", "1", "-hls_list_size", "6",
                        "-hls_flags", "delete_segments+independent_segments+omit_endlist",
                        "-hls_segment_filename", os.path.join(self.dir, "seg_%05d.ts"),
                        os.path.join(self.dir, "stream.m3u8"),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=CHILD_ENV,
                )

    def _reaper(self):
        while True:
            time.sleep(5)
            with self.lock:
                if self.proc is None or time.time() - self.last_access < LIVE_IDLE_TIMEOUT:
                    continue
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
                self.proc = None
                shutil.rmtree(self.dir, ignore_errors=True)

    def wait_playlist(self, timeout=12):
        path = os.path.join(self.dir, "stream.m3u8")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(path) as f:
                    text = f.read()
                if text.count("#EXTINF") >= 2:
                    return text
            except FileNotFoundError:
                pass
            time.sleep(0.3)
        return None


_lives_lock = threading.Lock()
LIVES = {}  # ID камеры -> LiveStream, создаётся при первом запросе HLS


def live_stream(cam_id):
    with _lives_lock:
        if cam_id not in LIVES:
            LIVES[cam_id] = LiveStream(cam_id)
        return LIVES[cam_id]


try:
    PUSH = webpush.PushService() if webpush else None
except Exception as e:  # например, нет прав на /var/lib/zorcam-nvr/config
    PUSH = None
    print(f"push notifications disabled: {e}", flush=True)
_push_lock = threading.Lock()
_last_push = {}  # ID камеры -> время последнего push о движении


def log(msg):
    """Строка в журнал одной записью: print пишет текст и перевод строки отдельно, и строки потоков склеивались."""
    sys.stdout.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    sys.stdout.flush()


try:
    YANDEX = yandex.Yandex(log=log, enabled=lambda: bool(yandex_cloud_url()))
except Exception as e:  # например, нет прав на /var/lib/zorcam-nvr/config
    YANDEX = None
    print(f"yandex smart home disabled: {e}", flush=True)


try:
    MQTT = mqtt.Mqtt(cameras=lambda: cameras(), version=VERSION, log=log)
except Exception as e:  # например, нет прав на /var/lib/zorcam-nvr/config
    MQTT = None
    print(f"mqtt disabled: {e}", flush=True)


_host_ips = {}  # имя -> (IP, когда узнали): DNS не на каждое подключение; не больше HOST_IPS_MAX (Host задаёт клиент)
_host_ips_lock = threading.Lock()


def host_ip(host):
    """IP адреса, по которому открыта галерея (домен DDNS или внешний IP), или None."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    cached = _host_ips.get(host)
    if cached and time.time() - cached[1] < 300:
        return cached[0]
    try:
        ip = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except (OSError, IndexError):
        ip = None
    with _host_ips_lock:
        _host_ips.pop(host, None)  # порядок словаря — порядок обновления: лишним уходит самая давняя запись
        _host_ips[host] = (ip, time.time())
        while len(_host_ips) > HOST_IPS_MAX:
            del _host_ips[next(iter(_host_ips))]
    return ip


ICE_URL_RE = re.compile(r"turns?:[a-z0-9.-]+(:\d{1,5})?(\?transport=(udp|tcp))?")


def cloud_ice(raw):
    """TURN-серверы облака из X-Zorcam-Ice (облако добавляет их к каждому запросу через туннель): список для
    RTCPeerConnection. Только понятное: адреса turn:/turns:, логин и пароль — короткие строки; иначе []."""
    try:
        servers = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(servers, list) or len(servers) > 4:
        return []
    out = []
    for srv in servers:
        if not isinstance(srv, dict):
            return []
        urls, user, cred = srv.get("urls"), srv.get("username"), srv.get("credential")
        if (not isinstance(urls, list) or not urls or len(urls) > 4
                or not all(isinstance(u, str) and ICE_URL_RE.fullmatch(u) for u in urls)
                or not all(isinstance(v, str) and 0 < len(v) <= 256 and v.isprintable() for v in (user, cred))):
            return []
        out.append({"urls": urls, "username": user, "credential": cred})
    return out


def add_candidates(sdp, ip, port=WEBRTC_PORT):
    """Дописать в SDP-ответ go2rtc кандидатов ip:port (UDP и TCP) в каждый медиараздел, если их там нет:
    браузер из интернета подключится по тому же адресу, по которому открыл галерею."""
    if f" {ip} {port} " in sdp:
        return sdp
    eol = "\r\n" if "\r\n" in sdp else "\n"
    extra = [f"a=candidate:zorcam1 1 udp 2130706431 {ip} {port} typ host",
             f"a=candidate:zorcam2 1 tcp 2130706430 {ip} {port} typ host tcptype passive"]
    lines = sdp.split(eol)
    tail = []
    while lines and lines[-1] == "":
        tail.append(lines.pop())
    out, in_media = [], False
    for line in lines:  # в конец каждого m=-раздела
        if line.startswith("m="):
            if in_media:
                out += extra
            in_media = True
        out.append(line)
    if in_media:
        out += extra
    return eol.join(out + tail)


def sdp_candidates(sdp):
    """'udp 1.2.3.4:8555 host, ...' из строк a=candidate SDP-ответа."""
    out = []
    for line in sdp.splitlines():
        if line.startswith("a=candidate:"):
            f = line.split()
            if len(f) >= 8:
                out.append(f"{f[2].lower()} {f[4]}:{f[5]} {f[7]}")
    return ", ".join(out) or "none"


def thumb_path(dirname, name):
    """Путь к превью кадра (создаётся при первом запросе); при ошибке — исходный кадр."""
    src = os.path.join(BASE_DIR, dirname, name)
    dst = os.path.join(THUMB_DIR, dirname, name)
    try:
        if os.path.getmtime(dst) >= os.path.getmtime(src):
            return dst
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with Image.open(src) as img:
            img.draft("RGB", (THUMB_SIZE[0] * 2, THUMB_SIZE[1] * 2))  # JPEG декодируется сразу в уменьшенном виде
            img = img.convert("RGB")
            img.thumbnail(THUMB_SIZE)
            tmp = f"{dst}.{threading.get_ident()}.tmp"
            img.save(tmp, "JPEG", quality=72)
        os.replace(tmp, dst)
        return dst
    except Exception as e:
        log(f"thumb error {dirname}/{name}: {e}")
        return src


def prune_thumbs():
    while True:
        time.sleep(THUMB_PRUNE_INTERVAL)
        try:
            dirnames = os.listdir(THUMB_DIR)
        except FileNotFoundError:
            continue
        for dirname in dirnames:
            try:
                names = os.listdir(os.path.join(THUMB_DIR, dirname))
            except FileNotFoundError:
                continue
            for name in names:
                if not os.path.exists(os.path.join(BASE_DIR, dirname, name)):
                    try:
                        os.remove(os.path.join(THUMB_DIR, dirname, name))
                    except OSError:
                        pass


def run_quiet(cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=CHILD_ENV).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def usb_disks():
    """USB-диски для записей (без системного): тот же отбор, что у root-сервиса zorcam-nvr-storage."""
    try:
        return zorcam_storage.usb_disks(json.loads(run_quiet(zorcam_storage.LSBLK, timeout=5) or "{}"))
    except ValueError:
        return []


def storage_disk():
    """Выбранный для записей диск (disk.json пишет zorcam-nvr-storage) или None — записи на SD-карте."""
    return zorcam_storage.read_json(zorcam_storage.DISK_FILE)


def fmt_bytes(n, lang):
    for unit in ("b", "kb", "mb", "gb"):
        if n < 1024 or unit == "gb":
            name = t(lang, "units." + unit)
            return f"{n:.0f} {name}" if unit == "b" else f"{n:.1f} {name}"
        n /= 1024


def record_files(base):
    """(время изменения, путь, байт) всех записей всех камер; недописанные .part не трогаются."""
    try:
        dirs = [e.path for e in os.scandir(base) if e.is_dir()]
    except OSError:
        return
    for d in dirs:
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.endswith(".part"):
                        continue
                    try:
                        if e.is_file():
                            st = e.stat()
                            yield st.st_mtime, e.path, st.st_size
                    except OSError:
                        pass
        except OSError:
            pass


def free_disk(base, need, batch=2000):
    """Удалить самые старые записи (у ролика — вместе с постером), пока не освободится need байт.
    Кадров может быть сотни тысяч: в памяти держатся только batch самых старых, обход повторяется.
    Возвращает (удалено файлов, освобождено байт)."""
    removed = freed = 0
    while freed < need:
        before = removed
        for _mtime, path, _size in heapq.nsmallest(batch, record_files(base)):
            stem, ext = os.path.splitext(path)
            # ролик — вместе с постером, метаданными детектора и меткой несохранённого
            pair = [path] + ([p for p in (stem + ".jpg", stem + ".mp4", stem + ".json", stem + ".failed") if p != path]
                             if os.path.basename(os.path.dirname(path)).endswith("_videos") else [])
            for p in pair:
                try:
                    size = os.stat(p).st_size
                    os.remove(p)
                except OSError:
                    continue
                removed, freed = removed + 1, freed + size
            if freed >= need:
                break
        if removed == before:  # удалять больше нечего
            break
    return removed, freed


# ---------------------------------------------------------------- мониторинг

class Health:
    """Раз в check_interval: состояние камеры, сервисов, диска, температуры (push о них не шлём — только «Состояние»).
    Там же — очистка старых кадров и роликов по срокам каждой камеры. Названия проверок — check.<ключ> в zorcam_i18n."""

    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.time()
        self.checks = {}      # ключ -> (ok, Msg)
        self.problems = {}    # ключ -> {"since"}
        self.events = collections.deque(maxlen=30)  # (время, Msg заголовка, Msg текста, исправлено ли)
        self.checked = None
        self.interval = max(10, CFG.get("health", "check_interval"))
        self.memory_high = None  # (счётчик high, время) прошлой проверки

    def capture_status(self, cam_id):
        try:
            with open(zorcam_config.capture_status_file(cam_id)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def run_checks(self):
        now = time.time()
        checks = {}
        limit = CFG.get("health", "no_frames_seconds")
        for cam in (c for c in cameras() if c.get("enabled", True)):  # кадры каждой включённой камеры: ключ frames:<id>
            st = self.capture_status(cam["id"])
            last = (st or {}).get("last_frame")
            err = capture_error(st)
            if last:
                age = now - last
                # детектор не получает кадров (Live при этом может работать): «Дом: детектор движения — нет кадров 40 мин»
                text = Msg("health.no_frames_for" if age > limit else "health.last_frame",
                           ago=lambda lang, age=age: fmt_ago(age, lang))
                if age > limit and err:
                    text = Msg("health.with_error", text=text, error=err)
                checks["frames:" + cam["id"]] = (age <= limit, text)
            else:
                waited = now - max(self.started, (st or {}).get("started") or 0)
                text = Msg("health.no_frames_yet")
                checks["frames:" + cam["id"]] = (waited <= limit,
                                                 Msg("health.with_error", text=text, error=err) if err else text)

        # оба сервиса одним вызовом systemctl, по строке на сервис; пусто — systemctl недоступен (запуск не на Pi)
        states = run_quiet(["systemctl", "is-active", "zorcam-nvr-capture", "zorcam-nvr-go2rtc"]).split()
        for key, state in zip(("capture", "go2rtc"), states, strict=False):
            checks[key] = (state == "active", Msg("health.running") if state == "active" else Raw(state))

        try:
            free_mb = shutil.disk_usage(BASE_DIR).free / 1048576
            checks["disk"] = (free_mb >= CFG.get("health", "min_free_disk_mb"),
                              Msg("health.free", free=lambda lang: fmt_bytes(free_mb * 1048576, lang)))
        except OSError:
            pass
        disk = storage_disk()
        if disk:  # диск для записей выбран: подключён ли он (нет — записи идут на SD-карту)
            on = os.path.ismount(BASE_DIR)
            checks["storage"] = (on, Msg("health.storage_disk" if on else "health.storage_missing", model=disk["model"]))
        temp = cpu_temp()
        if temp is not None:
            checks["temp"] = (temp <= CFG.get("health", "max_cpu_temp"), Raw(f"{temp:.1f} °C"))
        memory = self.memory_check(now)
        if memory:
            checks["memory"] = memory
        return checks

    def memory_check(self, now):
        """Хватает ли памяти детектору: прирост счётчика high с прошлой проверки. None — нет данных (не Pi,
        первая проверка, сервис перезапущен)."""
        try:
            with open(CAPTURE_MEMORY_EVENTS) as f:
                high = int(next(line.split()[1] for line in f if line.startswith("high ")))
        except (OSError, ValueError, StopIteration, IndexError):
            return None
        prev, self.memory_high = self.memory_high, (high, now)
        if prev is None or high < prev[0]:
            return None
        n, minutes = high - prev[0], max(1, round((now - prev[1]) / 60))
        if n <= MEMORY_HIGH_LIMIT:
            return True, Msg("health.memory_ok")
        return False, Msg("health.memory_short", n=n, minutes=minutes)

    def evaluate(self, checks):
        now = time.time()
        with self.lock:
            self.checks, self.checked = checks, now
            for key in [k for k in self.problems if k not in checks]:  # удалённая камера: её проблем больше нет
                del self.problems[key]
            for key, (ok, text) in checks.items():
                title = check_title(key)
                prob = self.problems.get(key)
                if not ok and prob is None:
                    self.problems[key] = {"since": now}
                    self.events.appendleft((now, Msg("health.event_problem", title=title), text, False))
                    log(f"health: problem {key}: {text}")
                elif ok and prob is not None:
                    del self.problems[key]
                    self.events.appendleft((now, Msg("health.event_recovered", title=title), text, True))
                    log(f"health: recovered {key}: {text}")
            self.write_summary(checks, now)

    def write_summary(self, checks, now):
        """Итог проверок для отчёта в админку облака (zorcam-nvr-cloud читает его раз в час): только ключи, итог и
        текст проблем по-английски, без названий камер."""
        data = {"checked": now, "checks": {}}
        for key, (ok, text) in checks.items():
            item = {"ok": bool(ok)}
            if not ok:
                item["text"] = text.text("en")[:200]
                item["since"] = (self.problems.get(key) or {}).get("since")
            data["checks"][key] = item
        try:
            zorcam_config._atomic_write(zorcam_cloud.HEALTH_FILE, json.dumps(data), 0o644)
        except OSError as e:
            log(f"health: cannot write {zorcam_cloud.HEALTH_FILE}: {e}")

    def cleanup(self):
        """Сроки хранения каждой камеры (frames_days, videos_days в cameras.json); папки камер, уже удалённых
        из cameras.json, — по срокам по умолчанию."""
        defaults = {k: d for k, d, *_ in zorcam_config.CAMERA_FIELDS}
        ages = {}
        for cam in cameras():
            raw_dir, video_dir = cam_dirs(cam["id"])
            ages[raw_dir], ages[video_dir] = cam["frames_days"] * 86400, cam["videos_days"] * 86400
        now = time.time()
        try:
            dirnames = [e.name for e in os.scandir(BASE_DIR) if e.is_dir()]
        except OSError:
            dirnames = []
        for dirname in dirnames:
            is_videos = dirname.endswith("_videos")
            max_age = ages.get(dirname, defaults["videos_days" if is_videos else "frames_days"] * 86400)
            count = size = 0  # что осталось после очистки: для «Настройки» → «Камеры» (RECORDINGS_STATS)
            try:
                with os.scandir(os.path.join(BASE_DIR, dirname)) as it:
                    for e in it:
                        try:
                            if not e.is_file():
                                continue
                            st = e.stat()
                            if now - st.st_mtime > max_age:
                                os.remove(e.path)
                                continue
                            # у ролика числом считается его постер .jpg (рядом .mp4, .json, .failed), байты — всех
                            count += e.name.endswith(".jpg") if is_videos else 1
                            size += st.st_size
                        except OSError:
                            pass
            except FileNotFoundError:
                pass
            RECORDINGS_STATS[dirname] = (count, size, time.time())

    def free_space(self):
        """Места меньше min_free_disk_mb (сроки хранения не успевают): удалить самые старые записи всех камер.
        Проверка «Место на диске» после этого в норме, поэтому очистка записана в события отдельно."""
        limit = CFG.get("health", "min_free_disk_mb")
        try:
            free = shutil.disk_usage(BASE_DIR).free
        except OSError:
            return
        if free >= limit * 1048576:
            return
        removed, freed = free_disk(BASE_DIR, (limit + FREE_MARGIN_MB) * 1048576 - free)
        free_now = free + freed
        log(f"low disk: {free // 1048576} MB free, below {limit} MB: removed {removed} oldest files "
            f"({freed // 1048576} MB), {free_now // 1048576} MB free now")
        text = Msg("health.disk_cleanup", limit=lambda lang: fmt_bytes(limit * 1048576, lang), removed=removed,
                   freed=lambda lang: fmt_bytes(freed, lang), free=lambda lang: fmt_bytes(free_now, lang))
        now = time.time()
        with self.lock:
            self.events.appendleft((now, Msg("health.event_disk_cleanup"), text, False))

    def loop(self):
        cleaned = 0.0
        while True:
            try:
                if time.time() - cleaned >= CLEANUP_INTERVAL:
                    self.cleanup()
                    cleaned = time.time()
                self.free_space()
                self.evaluate(self.run_checks())
            except Exception as e:
                log(f"health check error: {e}")
            time.sleep(self.interval)


def restart_self(reason, delay=1.0):
    """Выйти, чтобы systemd (Restart=always) запустил галерею с новыми настройками."""
    if not os.environ.get("INVOCATION_ID"):  # не под systemd — перезапускать некому
        log(f"{reason}; restart the gallery to apply")
        return
    log(f"{reason}, restarting")

    def _exit():
        time.sleep(delay)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()


def config_watch_loop():
    # только zorcam.conf: камеры (cameras.json) и их пароли галерея перечитывает сама, без перезапуска
    sig = zorcam_config.signature((zorcam_config.CONFIG_FILE,))
    while True:
        time.sleep(10)
        if zorcam_config.signature((zorcam_config.CONFIG_FILE,)) != sig:
            restart_self("zorcam.conf changed", delay=0)
            return


def watchdog_loop():
    """Пинг systemd watchdog, только пока HTTP-сервер галереи отвечает."""
    if not os.environ.get("NOTIFY_SOCKET"):
        return
    zorcam_config.sd_notify("READY=1")
    while True:
        try:
            with urllib.request.urlopen(HEALTH_URL % PORT, timeout=10) as r:
                if r.status == 200:
                    zorcam_config.sd_notify("WATCHDOG=1")
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(20)


def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000
    except (OSError, ValueError):
        return None


def fmt_ago(sec, lang):
    sec = int(sec)
    if sec < 90:
        return t(lang, "dur.sec", n=sec)
    if sec < 5400:
        return t(lang, "dur.min", n=sec // 60)
    return t(lang, "dur.hm", h=sec // 3600, m=sec % 3600 // 60)


def capture_error(st):
    """Последняя ошибка камеры из файла состояния zorcam-nvr-capture (Msg) или None."""
    st = st or {}
    data = st.get("last_error_msg")
    if isinstance(data, dict):
        reason = Msg.from_dict(data.get("reason"))
        detail = data.get("detail")
        return Msg("capture.with_detail", reason=reason, detail=detail) if detail else reason
    return Raw(st["last_error"]) if st.get("last_error") else None  # детектор до версии с переводами


# язык, выбранный в «Настройках», хранится на самом устройстве (в приложении или браузере): cookie, а не на Pi
LANG_COOKIE = "zorcam_lang"


def push_lang(sub):
    """Язык уведомления для подписки: выбранный на этом устройстве (pick), иначе [language] language, при auto —
    язык устройства; оба сохранены в подписке."""
    return (i18n.normalize(sub.get("pick")) or i18n.normalize(LANG_SETTING)
            or i18n.normalize(sub.get("lang")) or i18n.DEFAULT)


def check_title(key):
    """Название проверки: frames:<id> — «<название камеры>: детектор движения», остальные — check.<ключ>."""
    if key.startswith("frames:"):
        cam = camera(key[len("frames:"):])
        return Msg("check.frames_of", name=cam["name"] if cam else key[len("frames:"):])
    return Msg("check." + key)


def fmt_clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M %d.%m")


HEALTH = Health()


# ---------------------------------------------------------------- страница «Настройки»

def status_rows(lang):
    """Разделы «Состояние Zorcam» (итог, общие проверки, журнал событий) и «Состояние камер» (кадры, детектор,
    переподключения, ошибка каждой камеры) — оба видны и на iPhone. Возвращает (строки, строки камер)."""
    na = t(lang, "not_found")

    def row(label, value, cls="", sub=None, sub_cls=""):
        return (label, value, "muted" if value == na else cls, sub, sub_cls)

    with HEALTH.lock:
        checks, checked, problems = dict(HEALTH.checks), HEALTH.checked, dict(HEALTH.problems)
        events = list(HEALTH.events)
    # кодеки включённых камер — у go2rtc, разом (по 5 с на камеру по очереди «Статус» открывался бы долго)
    codecs = {}
    on = [c["id"] for c in cameras() if c.get("enabled", True)]
    probes = [threading.Thread(target=lambda i=i: codecs.__setitem__(i, camera_codecs(i, max_age=300)), daemon=True)
              for i in on]
    for p in probes:
        p.start()
    for p in probes:
        p.join(6)
    # версия надстройки (brand.json) — рядом с редакцией: «Версия» остаётся версией ядра
    edition = " ".join(filter(None, (t(lang, "edition." + zorcam_config.edition()), BRAND.version)))
    rows = [row(t(lang, "st.version"), VERSION), row(t(lang, "st.edition"), edition)]
    serial = zorcam_config.device_serial()  # device.json пишет установщик надстройки; у NVR его нет
    if serial:
        rows.append(row(t(lang, "st.serial"), serial))
    model, ram = device_info()
    if model or ram:
        rows.append(row(t(lang, "st.device"), "\n".join(  # две строки: модель, ОЗУ под ней (.v — pre-line)
            [model] * bool(model) + [t(lang, "st.ram", size=fmt_ram(lang, ram))] * bool(ram))))
    # только совет, янтарным: не проверка и не тревога (в «Итог» не входит); всё в порядке — строки нет
    net = zorcam_config.network_state()
    if net["via"] == "wifi":
        rows.append(row(t(lang, "st.network"), t(lang, "st.network_wifi"), cls="warn",
                        sub=t(lang, "st.network_why_wifi"), sub_cls="warn"))
    elif net["wifi"] or net["bluetooth"]:
        radio = "both" if net["wifi"] and net["bluetooth"] else "wifi" if net["wifi"] else "bt"
        rows.append(row(t(lang, "st.network"), t(lang, f"st.network_radio_{radio}"), cls="warn",
                        sub=t(lang, "st.network_why_radio"), sub_cls="warn"))
    if ENVIRONMENT != "prod":
        rows.append(row(t(lang, "st.environment"), ENVIRONMENT))
    ext = stun.snapshot()  # внешний адрес для WebRTC из интернета: не определён — янтарным, это не тревога
    if ext["checked"]:
        host = ext["server"].rsplit(":", 1)[0] if ext["ip"] else None  # адрес STUN без порта
        rows.append(row(t(lang, "st.external"), t(lang, "st.external_ok", ip=ext["ip"], server=host) if ext["ip"]
                        else t(lang, "st.external_none"), cls="" if ext["ip"] else "warn",
                        sub=None if ext["ip"] else t(lang, "st.external_why_cgnat" if ext["why"] else "st.external_why"),
                        sub_cls="warn"))
    if MQTT is not None and MQTT.enabled():  # брокер недоступен — янтарным: это не неисправность устройства
        st = MQTT.status()
        where = {"host": st["host"], "port": st["port"]}
        if st["state"] == "connected":
            rows.append(row(t(lang, "st.mqtt"), t(lang, "st.mqtt_ok", **where)))
        elif st["error"]:
            rows.append(row(t(lang, "st.mqtt"), t(lang, "st.mqtt_none", **where), cls="warn",
                            sub=st["error"].text(lang), sub_cls="warn"))
        else:
            rows.append(row(t(lang, "st.mqtt"), t(lang, "st.mqtt_connecting", **where)))
    tz = pi_timezone()  # время записей и снимков; пояс сменили, а сервисы не перезапущены — красным
    rows.append(row(t(lang, "st.timezone"), f'{tz["name"]} ({tz["offset"]})', cls="bad" if tz["pending"] else "",
                    sub=t(lang, "st.tz_pending", zone=tz["pending"]) if tz["pending"] else None, sub_cls="bad"))
    if checked is None:
        rows.append(row(t(lang, "st.check"), t(lang, "st.not_yet")))
    else:
        rows.append(row(t(lang, "st.summary"),
                        t(lang, "st.problems", n=len(problems)) if problems else t(lang, "st.all_ok"),
                        cls="bad" if problems else "ok"))
    def check_row(key, title=None):
        ok, text = checks[key]
        title = title or check_title(key).text(lang)
        since = t(lang, "st.since", since=fmt_clock(problems[key]["since"])) if key in problems else ""
        return row(title, t(lang, "st.ok" if ok else "st.problem") + text.text(lang) + since,
                   cls="ok" if ok else "bad")
    rows += [check_row(key) for key in checks if not key.startswith("frames:")]
    cam_rows, capture_errors = [], []
    for cam in cameras():
        st = HEALTH.capture_status(cam["id"]) or {}
        capture_errors += [Msg.from_dict(e) for e in st.get("config_errors") or []]
        has_check = "frames:" + cam["id"] in checks
        if has_check or st:  # название камеры — строка-заголовок, показатели под ней без названия
            cam_rows.append((cam["name"], "", "head"))
        if has_check:
            cam_rows.append(check_row("frames:" + cam["id"], t(lang, "st.frames")))
        if not st:
            continue
        state = st.get("state")
        state_text = t(lang, "st.state." + state) if "st.state." + str(state) in i18n.STRINGS else (state or na)
        cam_rows.append(row(t(lang, "st.detector"), state_text))
        video, audio = codecs.get(cam["id"]) or (None, None)
        if video:  # H.265 по WebRTC показывает WebKit (любой браузер на iPhone, Safari на Mac); Chrome и Firefox — не везде
            text = CODEC_NAMES.get(video, video.upper()) + (
                " · " + t(lang, "st.codec_sound", codec=CODEC_NAMES.get(audio, audio.upper())) if audio else "")
            h265 = video in ("h265", "hevc")  # совет — янтарным, как предупреждение
            cam_rows.append(row(t(lang, "st.codec"), text, cls="",
                                sub=t(lang, "st.codec_h265") if h265 else None, sub_cls="warn" if h265 else ""))
        if st.get("keyframe_fps"):  # сколько ключевых кадров в секунду на самом деле присылает камера
            cam_rows.append(row(t(lang, "st.keyframes"), f'{st["keyframe_fps"]:.1f}'))
        if cam["rtsp_sub_url"]:  # второй поток задан: смотрит ли детектор его
            on_sub = st.get("detector_stream") == "sub"
            cam_rows.append(row(t(lang, "st.detector_stream"),
                                t(lang, "st.stream.sub" if on_sub else "st.stream.main"), cls="" if on_sub else "bad"))
        cam_rows.append(row(t(lang, "st.reconnects"), str(len(st.get("reconnects") or []))))
        err = capture_error(st)
        if err:
            # длинный текст ffmpeg справа узкой колонкой не читается: подпись и время — строкой, текст — под ней во всю ширину
            when = fmt_clock(st["last_error_at"]) if st.get("last_error_at") else ""
            cam_rows.append((t(lang, "st.last_error"), err.text(lang), "wide", when))
    if checked:
        rows.append(row(t(lang, "st.last_check"), t(lang, "st.ago", ago=fmt_ago(time.time() - checked, lang))))
    for ts, title, text, ok in events[:10]:  # текст проблемы — красным под заголовком, исправленной — зелёным
        rows.append(row(title.text(lang), fmt_clock(ts), sub=text.text(lang), sub_cls="ok" if ok else "bad"))
    # ошибки zorcam.conf — в «Состоянии»: отдельного раздела о файле настроек нет (#3)
    for e in dict.fromkeys(e.text(lang) for e in CFG.errors + capture_errors):
        rows.insert(1, row(t(lang, "cf.error"), e, cls="bad"))
    return rows, cam_rows


LIVE_URL = "/?tab=live"
PASSWORD_URL = "/setup/password"  # «Смените пароль»: куда главная ведёт admin, пока у него пароль от установщика


def replace_later(url, seconds):
    """Перейти на url через seconds секунд, заменив текущую страницу в истории: иначе «Назад» после сохранения
    вернёт на страницу «Сохранено» (#10) — это делает static/settings.js; meta refresh — запасной вариант без JavaScript."""
    return cat(h("meta", http_equiv="refresh", content=f"{seconds};url={url}"),
               h("span", hidden=True, data_replace=url, data_after=seconds * 1000 - 500))


def tab_bar(lang, role, active, switch=False, user=""):
    """Нижняя панель вкладок iOS: Live, Видео, Timelapse (liveviewer — только Live) и «Настройки». switch=True (главная) —
    вкладки-кнопки переключают панели без перезагрузки; иначе (страница «Настроек») — ссылки на главную.
    На широком экране (от 1024 px) тот же <nav> — боковая панель: название сверху (.brand), кто вошёл — внизу (.who);
    на телефоне эти два блока скрыты."""
    items = [brand_html(lang, "brand")]
    for tab in (["live"] if role == "liveviewer" else TABS):
        cls = "tab active" if tab == active or (tab == "records" and active in RECORD_TABS) else "tab"
        label = cat(trusted(ICONS[tab]), h("span", t(lang, "tab." + tab)))
        items.append(h("button", label, class_=cls, data_tab=tab) if switch
                     else h("a", label, class_=cls, href=f"/?tab={RECORDS_DEFAULT if tab == 'records' else tab}"))
    cls = "tab active" if active == "settings" else "tab"
    items.append(h("a", trusted(ICONS["settings"]), h("span", t(lang, "settings")), class_=cls, href="/settings"))
    if user:
        items.append(h("div", h("span", user[:1], class_="ava"), h("span", user, h("small", t(lang, "role." + role)), class_="nm"),
                       class_="who"))
    return h("nav", items, class_="tabbar")


SETTINGS_SCRIPTS = ("app.js", "settings.js")


def settings_page(lang, heading, back="", badge="", intro="", sections="", tabbar="", extra=""):
    """Страница в оформлении «Настроек»; back — куда ведёт «Назад» (пусто — кнопки нет, как в мастере настройки);
    tabbar — нижняя панель вкладок (только у самих «Настроек»). Страница без «Назад» и панели (мастер, «Нет доступа») —
    body.plain: на широком экране колонка уже, чем у вложенных страниц. extra — файл static/<extra>.css и .js страницы."""
    backlink = h("a", trusted(BACK_ICON), t(lang, "back"), class_="back", href=back) if back else ""
    if tabbar:  # главная «Настроек»: название приложения, как в шапке других вкладок
        backlink = cat(brand_html(lang, "appname"), clock_html())
    head_extra = cat(h("link", rel="stylesheet", href=TEMPLATE_VARS[f"css_{extra}"]), "\n") if extra else ""
    script_extra = cat(h("script", src=TEMPLATE_VARS[f"js_{extra}"]), "\n") if extra else ""
    return render_template("settings", lang, heading=esc(heading), backlink=backlink, badge=badge, intro=intro,
                           sections=sections, tabbar=tabbar, head_extra=head_extra, script_extra=script_extra,
                           data_script=data_script(lang, SETTINGS_SCRIPTS + ((f"{extra}.js",) if extra else ())),
                           bodycls=' class="tabbed"' if tabbar else "" if back else ' class="plain"',
                           logo="" if tabbar or back else logo_html("loginlogo"))  # знак надстройки — над заголовком мастера


def banner(cls, text):
    return h("div", text, class_=f"banner {cls}")


def th(lang, key, **kw):
    """Перевод как разметка: сам текст свой (в нём бывают <code>, <a>), а подставляемое в него (имена, адреса,
    ответы устройств) экранируется."""
    return trusted(t(lang, key, **{k: esc(i18n.render(v, lang)) for k, v in kw.items()}))


def csrf_token(session_token):
    return _token_key((session_token or "") + ":csrf")[:32]


def same_secret(given, expected):
    """Сравнение секретов за постоянное время; compare_digest(str, str) падает на не-ASCII (ответ 500), поэтому — байты."""
    return secrets.compare_digest(str(given or "").encode("utf-8", "surrogatepass"),
                                  str(expected or "").encode("utf-8", "surrogatepass"))


def choice_label(lang, section, key, value):
    """Подпись варианта в выпадающем списке /config: для языка — его название."""
    if (section, key) == ("language", "language"):
        return t(lang, "cfgpage.lang.auto") if value == "auto" else i18n.LANG_NAMES.get(value, value)
    return str(value)


def public_host(host):
    """Имя или адрес из Host, если по нему галерея доступна из интернета (домен DDNS, внешний IP), иначе None."""
    host = (host or "").strip().lower()
    if host.startswith("["):  # IPv6 в квадратных скобках
        host = host[1:host.find("]")] if "]" in host else ""
    elif host.count(":") == 1:
        host = host.split(":")[0]
    if not host or not re.fullmatch(r"([a-z0-9-]+\.)*[a-z0-9-]+|[0-9a-f:]+:[0-9a-f:.]*", host):
        return None
    try:
        return host if ipaddress.ip_address(host).is_global else None
    except ValueError:
        pass
    return host if "." in host and not host.endswith((".local", ".lan", ".home", ".internal", ".localdomain")) else None


def config_fields(lang, keys, raw=None, errors=None):
    """Поля формы параметров zorcam.conf: {раздел: html}, в порядке PARAMS; keys — какие параметры показать."""
    errors = errors or {}
    out = {}
    for sec, key, _default, typ, desc, _who in zorcam_config.PARAMS:
        if (sec, key) not in keys:
            continue
        value = raw[(sec, key)] if raw else zorcam_config.format_value(CFG.get(sec, key))
        name = f"{sec}.{key}"
        choices = zorcam_config.CHOICES.get((sec, key))
        if choices:
            control = h("select", (h("option", choice_label(lang, sec, key, c), value=c, selected=str(c) == value)
                                   for c in choices), name=name)
        elif typ is bool:
            yes = value.lower() in ("yes", "true", "on", "1")
            control = h("select", h("option", t(lang, "yes"), value="yes", selected=yes),
                        h("option", t(lang, "no"), value="no", selected=not yes), name=name)
        else:
            control = h("input", type="text", name=name, value=value, inputmode="decimal" if typ in (int, float) else None,
                        autocomplete="off")
        err = errors.get((sec, key))
        out[sec] = cat(out.get(sec, ""), h(
            "div", h("div", i18n.param_desc(lang, sec, key, desc), class_="k"), control,
            h("div", err.text(lang), class_="msg") if err else "",
            class_="row field err" if err else "row field"))
    return out


def read_config_form(form):
    """Параметры zorcam.conf из формы: (значения, ошибки {(раздел, ключ): Msg}, введённый текст);
    параметры, которых в форме нет, остаются как есть."""
    values, errors, raw = {}, {}, {}
    for sec, key, *_ in zorcam_config.PARAMS:
        if f"{sec}.{key}" not in form:  # этого параметра нет в форме (внешний IP — в «Общих настройках»)
            values[(sec, key)] = CFG.get(sec, key)
            raw[(sec, key)] = zorcam_config.format_value(values[(sec, key)])
            continue
        text = form.get(f"{sec}.{key}", [""])[0].strip()
        raw[(sec, key)] = text
        value, err = zorcam_config.validate(sec, key, text)
        if err:
            errors[(sec, key)] = err
        else:
            values[(sec, key)] = value
    return values, errors, raw


def render_config_page(csrf, lang, raw=None, errors=None, message=None):
    """Форма редактирования zorcam.conf (только admin), кроме «Общих настроек». errors: {(раздел, ключ): Msg}."""
    errors = errors or {}
    parts = []
    if message:
        parts.append(banner(*message))
    if errors:
        parts.append(banner("bad", t(lang, "cfgpage.errors", n=len(errors))))
    fields = config_fields(lang, {(s, k) for s, k, *_ in zorcam_config.PARAMS}, raw, errors)
    parts.append(h("form", h("input", type="hidden", name="csrf", value=csrf),
                   (h("section", h("h2", t(lang, "section." + sec)), body) for sec, body in fields.items()),
                   h("div", h("a", t(lang, "cfgpage.cancel"), href="/settings"), h("button", t(lang, "cfgpage.save"), type="submit"),
                     class_="actions"), method="POST", action="/config"))
    intro = h("p", th(lang, "cfgpage.intro"), class_="intro")
    return settings_page(lang, t(lang, "cfgpage.title"), "/settings", h("span", "admin", class_="ro"), intro, cat(parts))


def lang_picker(chosen, lang):
    """Выбор языка на этом устройстве: <select>, выбор запоминает static/settings.js в cookie LANG_COOKIE
    (на год; «авто» — удаляет её) и перезагружает страницу. chosen — выбранный язык или None («авто»)."""
    current = chosen or "auto"
    names = {"auto": t(lang, "lang.auto"), **i18n.LANG_NAMES}
    return h("select", (h("option", names[c], value=c, selected=c == current) for c in ("auto", *i18n.LANGS)),
             name="language", data_lang_pick=True, data_cookie=LANG_COOKIE, aria_label=t(lang, "lang.pick"),
             title=t(lang, "lang.pick"))


def post_button(action, fields, label, question, csrf, danger=False):
    """Кнопка-форма POST с подтверждением; danger — красным текстом (удаление, отключение)."""
    return h("form", h("input", type="hidden", name="csrf", value=csrf),
             (h("input", type="hidden", name=k, value=v) for k, v in fields.items()),
             h("button", label, type="submit", class_="danger" if danger else None),
             method="POST", action=action, onsubmit=f"return confirm({json.dumps(question, ensure_ascii=False)})")


def recordings_section(lang, csrf, cams):
    """«Записи» — по камере: заголовок с именем (как в «Состоянии камер»), сколько кадров и роликов
    и кнопки удаления с подтверждением (видно и на iPhone). Числа берутся из RECORDINGS_STATS: папки здесь
    не обходятся (кадров сотни тысяч, обход на Pi 3 занимает минуты и страница не открывалась)."""
    body = []
    for cam in cams:
        raw_dir, video_dir = cam_dirs(cam["id"])
        name = cam["name"]

        def button(scope, label, question, cam=cam):
            return post_button("/storage/delete", {"cam": cam["id"], "scope": scope}, t(lang, label), question, csrf)

        def cell(label, dirname):
            stats = RECORDINGS_STATS.get(dirname)
            value = f"{stats[0]} ({fmt_bytes(stats[1], lang)})" if stats else t(lang, "rec.counting")
            return h("div", h("div", label, class_="k"), h("div", value, class_="v"), class_="rc")
        # две колонки: Timelapse слева, Video справа; значения сверху, кнопки удаления под ними (сетка в settings.css)
        body += [h("div", h("div", name, class_="k"), class_="row all"),
                 h("div", cell(t(lang, "tab.all"), raw_dir), cell(t(lang, "tab.videos"), video_dir),
                   button("frames", "rec.delete", t(lang, "rec.confirm_frames", name=name)),
                   button("videos", "rec.delete", t(lang, "rec.confirm_videos", name=name)),
                   # общая строка на обе колонки: и снимки, и ролики
                   button("all", "rec.delete_all", t(lang, "rec.confirm_all", name=name)), class_="reccols")]
    return h("section", h("h2", t(lang, "rec.section")), body) if body else ""


def delete_recordings(cam_id, scope):
    """Удалить все кадры камеры ("frames"), все её ролики ("videos") или и то, и другое ("all").
    Возвращает (кадров, роликов, байт)."""
    if scope == "all":
        return tuple(map(sum, zip(delete_recordings(cam_id, "frames"), delete_recordings(cam_id, "videos"), strict=True)))
    raw_dir, video_dir = cam_dirs(cam_id)
    dirname = raw_dir if scope == "frames" else video_dir
    RECORDINGS_STATS[dirname] = (0, 0, time.time())  # список камер сразу покажет нули, не дожидаясь очистки
    frames = videos = size = 0
    try:
        entries = list(os.scandir(os.path.join(BASE_DIR, dirname)))
    except FileNotFoundError:
        entries = []
    for e in entries:
        try:
            if not e.is_file():
                continue
            st = e.stat()
            os.remove(e.path)
        except OSError:
            continue
        size += st.st_size
        if scope == "frames":
            frames += 1
        elif e.name.endswith(".jpg"):  # ролик — его постер (у несохранённого .mp4 нет)
            videos += 1
    return frames, videos, size


def camera_host(cam):
    """Под названием камеры в списках — только хост или IP из её RTSP-адреса."""
    return urlparse(cam["rtsp_url"]).hostname or cam["rtsp_url"]


def camera_working(cam_id):
    """Работает ли включённая камера — по проверке мониторинга «кадры» (та же, что в «Состоянии камер»), без запросов
    к go2rtc. None — мониторинг ещё не решил (проверок не было или кадров ещё нет, но срок не вышел)."""
    with HEALTH.lock:
        check = HEALTH.checks.get("frames:" + cam_id)
    if check is None:
        return None
    if not check[0]:
        return False
    return True if (HEALTH.capture_status(cam_id) or {}).get("last_frame") else None


def camera_frame_html(lang, cam_id, small=False):
    """Рамка 16:9 с недавним кадром камеры (/camera-frame/<id>); кадра нет — под картинкой плашка «Нет кадра»,
    а битую картинку убирает static/settings.js. Список кадров здесь не читаем: на SD их тысячи."""
    return h("div", h("span", t(lang, "cams.no_frame"), class_="camframe-none"),
             h("img", src=f"/camera-frame/{quote(cam_id, safe='')}", alt="", loading="lazy", class_="camframe-img"),
             class_="camframe small" if small else "camframe")


def cameras_section(lang, csrf=""):
    """Список камер: тумблер «Камера включена» (сохраняется сразу, без страницы камеры), ссылка на изменение,
    кнопка «Добавить камеру» (admin, и на телефоне)."""
    rows = []
    for cam in cameras():
        on = cam.get("enabled", True)
        off = "" if on else cat(" · ", h("span", t(lang, "cams.off"), class_="off"))
        state = camera_working(cam["id"]) if on else None
        if state is not None:
            off = cat(" · ", h("span", t(lang, "cams.working" if state else "cams.broken"),
                               class_="working" if state else "broken"))
        label = f'{t(lang, "cam.label.enabled")}: {cam["name"]}'
        switch = h("form", h("input", type="hidden", name="csrf", value=csrf),
                   h("input", type="hidden", name="id", value=cam["id"]),
                   h("input", type="hidden", name="enabled", value="no"),
                   h("input", type="checkbox", class_="switch", name="enabled", value="yes", checked=on, aria_label=label,
                     title=label, onchange="this.form.submit()"), method="POST", action="/camera/enable")
        rows.append(h("div", camera_frame_html(lang, cam["id"], small=True),
                      h("a", cam["name"], h("span", camera_host(cam), off, class_="sub"),
                        class_="k" if on else "k dim", href=f"/camera/{cam['id']}", title=t(lang, "cams.edit")),
                      h("div", switch, h("a", trusted("&#8250;"), class_="chev", href=f"/camera/{cam['id']}", aria_hidden="true",
                                         tabindex="-1"), class_="v cam-actions"), class_="row"))
    if not rows:
        rows = h("div", h("div", t(lang, "cams.none"), class_="k"), class_="row")
    return h("section", h("h2", t(lang, "cams.section")), rows,
             h("a", t(lang, "cams.add"), class_="row action", href="/camera/new"))


def section_html(title, rows):
    body = []
    for label, value, vcls, *sub in rows:  # (подпись, значение, класс значения[, текст под подписью, его класс])
        if vcls == "head":  # заголовок группы (камера) — как «Все камеры» в «Уведомлениях»
            body.append(h("div", h("div", label, class_="k"), class_="row all"))
            continue
        if vcls == "wide":  # подпись (и время справа) — строкой, значение — под ней во всю ширину
            body.append(h("div", h("div", h("div", label, class_="k"), h("div", sub[0] if sub else "", class_="v"), class_="wide-head"),
                          h("div", str(value), class_="wide-text"), class_="row wide"))
            continue
        note = h("span", sub[0], class_=f"sub {sub[1]}") if sub and sub[0] else ""
        body.append(h("div", h("div", label, note, class_="k"), h("div", str(value), class_=f"v {vcls}"), class_="row"))
    return h("section", h("h2", title), body)


# поле IP или имени хоста: клавиатура с точкой (у decimal в русской раскладке iPhone — запятая), без автозамен
HOST_INPUT = {"inputmode": "url", "autocapitalize": "off", "autocorrect": "off", "spellcheck": "false", "autocomplete": "off"}

# вкладки «Настроек»: (ключ, с какой роли видна)
SETTINGS_TABS = [("general", "liveviewer"), ("notify", "viewer"), ("cameras", "admin"), ("status", "admin"),
                 ("users", "liveviewer")]


def settings_url(tab):
    return f"/settings?tab={tab}"


def settings_tabs(role):
    return [key for key, need in SETTINGS_TABS if ROLE_LEVEL[role] >= ROLE_LEVEL[need]]


def notify_section(lang, csrf, user, role):
    """Вкладка «Уведомления»: общий переключатель («Все камеры», выделен) и под ним камеры пользователя одним списком; без кнопки «Сохранить» — переключатель
    уходит на сервер сразу (fetch), при ошибке возвращается назад."""
    all_on, off = zorcam_users.notify_prefs(user)

    def row(label, cam, on, cls="row field"):
        return h("div", h("label", h("span", label), h("input", type="checkbox", class_="switch", data_notify=cam, checked=on),
                          class_="switch-row"), class_=cls)

    # камера с выключенным тумблером в её настройках показана выключенной; включение здесь включает и его (notify_set)
    cams = cat(row(c["name"], c["id"], c["id"] not in off and c["notify_motion"]) for c in user_cameras(user, role))
    if not cams:
        cams = h("div", h("div", t(lang, "notify.none"), class_="k"), class_="row")
    return h("section", h("h2", t(lang, "notify.section")), row(t(lang, "notify.all"), "all", all_on, "row field all"), cams,
             data_csrf=csrf)


def switch_row(label, name, value, on):
    return h("div", h("label", h("span", label), h("input", type="checkbox", class_="switch", name=name, value=value, checked=on),
                      class_="switch-row"), class_="row field")


USER_ROLES = ("viewer", "liveviewer")  # роли, которые admin выдаёт пользователям; admin — один, с именем admin


def role_select(lang, role):
    return h("div", h("div", t(lang, "users.rights"), class_="k"),
             h("select", (h("option", t(lang, "role." + r), value=r, selected=r == role) for r in USER_ROLES), name="role"),
             class_="row field")


def password_rows(lang, placeholder, error=None):
    return h("div", h("div", t(lang, "login.password"), class_="k"),
             h("input", type="password", name="password", placeholder=placeholder, autocomplete="new-password"),
             h("input", type="password", name="repeat", placeholder=t(lang, "setup.repeat"), autocomplete="new-password"),
             h("div", error.text(lang), class_="msg") if error else "", class_="row field err" if error else "row field")


def camera_switches(lang, allowed):
    cams = cameras()
    if not cams:
        return ""
    return cat(h("div", h("div", t(lang, "users.cameras"), class_="k"), class_="row"),
               (switch_row(c["name"], "cam", c["id"], c["id"] in allowed) for c in cams))


def user_section(lang, csrf, name, role, error=None):
    """Пользователь на вкладке «Польз.» (только admin). admin: только пароль. Остальные: права (Live и галерея
    или только Live), пароль, доступ к каждой камере, «Удалить пользователя». Имя в интерфейсе не меняется."""
    who = cat(h("input", type="hidden", name="user", value=name),
              h("div", h("div", t(lang, "login.user"), class_="k"), h("div", name, class_="v"), class_="row"))
    password = password_rows(lang, t(lang, "cfgpage.password_placeholder"), error)
    if role == "admin":
        title, rights = t(lang, "setup.user_title.admin"), h(
            "div", h("div", t(lang, "users.rights"), class_="k"), h("div", t(lang, "role.admin"), class_="v"), class_="row")
        access = delete = ""
    else:
        title, rights = name, role_select(lang, role)
        access = camera_switches(lang, zorcam_users.cameras_of(name))
        # «Завершить сеансы» — обычная действие-строка (красный — только удаление), «Удалить» — красным
        delete = cat(h("div", post_button("/users/sessions", {"user": name}, t(lang, "users.end_sessions"),
                                          t(lang, "users.end_sessions_q", name=name), csrf), class_="actions"),
                     h("div", post_button("/users/delete", {"user": name}, t(lang, "users.delete"),
                                          t(lang, "users.confirm_delete", name=name), csrf), class_="rec"))
    return h("section", h("h2", title),
             h("form", h("input", type="hidden", name="csrf", value=csrf), who, rights, password, access,
               h("div", h("button", t(lang, "cfgpage.save"), type="submit"), class_="actions"),
               method="POST", action="/users/save"), delete)


def new_user_section(lang, csrf, error=None, raw=None):
    """«Новый пользователь»: имя, права, пароль, камеры (по умолчанию — все). raw — введённое (кроме пароля)."""
    raw = raw or {}
    name_err = error if error and error.key in ("users.bad_name", "users.name_taken") else None
    pass_err = error if error and not name_err else None
    # новому пользователю по умолчанию включены все камеры; после ошибки — как было отмечено
    allowed = set(raw["cam"]) if "cam" in raw else {c["id"] for c in cameras()}
    return h("section", h("h2", t(lang, "users.new_title")), h("div", t(lang, "users.new_tip"), class_="tip"),
             h("form", h("input", type="hidden", name="csrf", value=csrf), h("input", type="hidden", name="new", value="1"),
               h("div", h("div", t(lang, "login.user"), class_="k"),
                 h("input", type="text", name="name", value=raw.get("name", ""), maxlength=zorcam_users.MAX_NAME,
                   autocomplete="off", autocapitalize="none", spellcheck="false"),
                 h("div", name_err.text(lang), class_="msg") if name_err else "",
                 class_="row field err" if name_err else "row field"),
               role_select(lang, raw.get("role", "viewer")), password_rows(lang, "", pass_err), camera_switches(lang, allowed),
               h("div", h("button", t(lang, "users.add"), type="submit"), class_="actions"),
               method="POST", action="/users/save"))


def users_sections(lang, csrf, error=None, raw=None):
    """admin, остальные пользователи по имени, затем «Новый пользователь».
    error — (раздел: имя пользователя или "new", Msg); raw — введённое в форме нового пользователя."""
    key, msg = error or (None, None)
    users = zorcam_users.load()
    names = sorted(users, key=lambda n: (n != zorcam_users.ADMIN, n.lower()))
    parts = [user_section(lang, csrf, n, users[n]["role"], msg if key == n else None) for n in names]
    parts.append(new_user_section(lang, csrf, msg if key == "new" else None, raw if key == "new" else None))
    if msg and key != "new" and key not in users:  # форма устарела (пользователя уже нет) — сообщение сверху
        parts.insert(0, banner("bad", msg.text(lang)))
    return parts


def render_settings(role, lang, user, csrf="", general="", tab="general", users_error=None, saved=None, users_raw=None,
                    chosen_lang=None, added=None, ended=None, signed_out=False):
    """«Настройки» по вкладкам: «Общие» (язык; у admin — облако и Яндекс), «Камеры» (список и записи каждой камеры),
    «Статус», «Польз.» (кто вошёл; у admin — пароль и доступ к камерам каждого пользователя; «Выйти»).
    «Камеры» и «Статус» — только admin."""
    admin = ROLE_LEVEL[role] >= ROLE_LEVEL["admin"]
    tabs = settings_tabs(role)
    tab = tab if tab in tabs else tabs[0]
    nav = h("nav", (h("a", t(lang, "settab." + key), class_="active" if key == tab else None, href=settings_url(key))
                    for key in tabs), class_="subtabs")
    parts = []
    if tab == "general":
        parts.append(h("section", h("h2", t(lang, "section.language")), h("div", lang_picker(chosen_lang, lang), class_="row field")))
        if admin:
            if general:
                parts.append(general)
            parts.append(storage_section(lang, csrf))
            parts.append(cloud_section(lang, csrf))
            parts.append(yandex_link_section(lang))
            parts.append(mqtt_link_section(lang))
    elif tab == "notify":
        parts.append(notify_section(lang, csrf, user, role))
    elif tab == "cameras":
        parts.append(TEMPLATE_VARS["ptr"])  # потянуть — обновить: статусы камер и счётчики записей
        cam = camera(added) if added else None
        if cam and any(v["role"] != "admin" for v in zorcam_users.load().values()):  # новая камера видна только admin
            parts.append(banner("ok", t(lang, "cams.added_closed", name=cam["name"])))
        parts.append(cameras_section(lang, csrf))
        parts.append(recordings_section(lang, csrf, cameras()))
    elif tab == "status":
        parts.append(TEMPLATE_VARS["ptr"])  # потянуть — обновить (static/app.js)
        rows, cam_rows = status_rows(lang)
        parts.append(section_html(t(lang, "st.section." + zorcam_config.edition()), rows))
        if cam_rows:
            parts.append(section_html(t(lang, "st.cameras"), cam_rows))
    else:
        before, after = t(lang, "account.signed_in", user="\0").split("\0")  # имя — отдельным полужирным элементом
        if signed_out:  # «Выйти на других устройствах» нажато
            parts.append(banner("ok", t(lang, "users.signout_others_done")))
        parts.append(h("section", h("div", h("div", before, h("b", user), after, class_="k"), class_="row"),
                       h("div", h("a", t(lang, "logout"), href="/logout"), class_="signout"),
                       h("div", post_button("/sessions/others", {}, t(lang, "users.signout_others"),
                                            t(lang, "users.signout_others_q"), csrf), class_="signout")))
        if admin:
            parts.append(h("div", t(lang, "users.devices"), class_="grouphead"))
            if saved:
                parts.append(banner("ok", t(lang, "users.saved", name=saved)))
            if ended:
                parts.append(banner("ok", t(lang, "users.sessions_ended", name=ended)))
            parts += users_sections(lang, csrf, users_error, users_raw)
    # на широком экране список вкладок — левая колонка, содержимое вкладки (.detail) — правая; на телефоне это не видно
    return settings_page(lang, t(lang, "settings.title"), sections=cat(nav, h("div", parts, class_="detail")),
                         tabbar=tab_bar(lang, role, "settings", user=user))


# ---------------------------------------------------------------- камеры: добавление и изменение

CAMERA_FORM_KEYS = [k for k, *_ in zorcam_config.CAMERA_FIELDS]
CAMERA_SWITCHES = [k for k, _d, typ, _desc in zorcam_config.CAMERA_FIELDS if typ is bool]
CAMERA_DEFAULTS = {k: d for k, d, *_ in zorcam_config.CAMERA_FIELDS}
# поле активно, только когда включён тумблер: {поле: тумблер}
CAMERA_NEEDS = {"clip_seconds": "save_videos", "videos_days": "save_videos",
                "frames_interval": "save_frames", "frames_days": "save_frames"}


# PTZ камер: ID -> (до какого момента верно, {"url","token","way"} или None — не умеет или не ответила)
PTZ_CACHE = {}
PTZ_CACHE_LOCK = threading.Lock()
PTZ_TTL, PTZ_TTL_OFF = 600, 120
PTZ_MOVE_SECONDS = 2  # Timeout у ContinuousMove: сама остановится, если Stop потерялся
PTZ_SPEED = 0.5
PTZ_MIN_GAP = 0.2  # с между командами движения одной камеры (кнопка повторяет раз в 0,8 с)
PTZ_LAST = {}
PTZ_DIRS = {"left": (-1, 0, 0), "right": (1, 0, 0), "up": (0, 1, 0), "down": (0, -1, 0), "in": (0, 0, 1), "out": (0, 0, -1)}


def ptz_forget(cam_id):
    with PTZ_CACHE_LOCK:
        PTZ_CACHE.pop(cam_id, None)


def ptz_of(cam):
    """Что нужно для управления камерой (см. onvif.ptz_support) или None; ответ камеры помнится минуты, ошибка — не
    ошибка страницы, а «кнопок нет»."""
    now = time.monotonic()
    with PTZ_CACHE_LOCK:
        until, cap = PTZ_CACHE.get(cam["id"], (0, None))
    if until > now:
        return cap
    url, cap = onvif.camera_url(cam), None
    if url:
        try:
            cap = onvif.ptz_support(url, cam["rtsp_user"], zorcam_config.camera_password(cam["id"]))
        except onvif.OnvifError as e:
            if e.key != "no_ptz":
                log(f"ptz: {cam['id']}: {e}")
    with PTZ_CACHE_LOCK:
        PTZ_CACHE[cam["id"]] = (now + (PTZ_TTL if cap else PTZ_TTL_OFF), cap)
    return cap


# пароль, введённый при поиске ONVIF, — до сохранения формы камеры (в форму пароли не возвращаются)
ONVIF_PASSWORDS = {}
ONVIF_PASSWORD_TTL = 1800
ONVIF_SCAN_SECONDS = 3
ONVIF_SUBNET_COOKIE = "zorcam_onvif_subnet"
ONVIF_SUBNET_DEFAULT = "192.168.1.0"  # подсеть в поле до первого выбора; очищенное поле запоминается пустым


def onvif_keep_password(password):
    now = time.time()
    for token in [k for k, (_p, until) in ONVIF_PASSWORDS.items() if until < now]:
        del ONVIF_PASSWORDS[token]
    token = secrets.token_urlsafe(18)
    ONVIF_PASSWORDS[token] = (password, now + ONVIF_PASSWORD_TTL)
    return token


def onvif_password(token):
    password, until = ONVIF_PASSWORDS.get(token, ("", 0))
    return password if until >= time.time() else ""


def camera_hosts():
    """Адреса уже добавленных камер (основной и дополнительный поток)."""
    return {urlparse(c.get(k) or "").hostname for c in cameras() for k in ("rtsp_url", "rtsp_sub_url")} - {None}


def suggest_camera_id(host):
    """ID новой камеры по её адресу: cam50 для 192.168.1.50; занятый — cam50-2 и дальше."""
    taken = {c["id"] for c in cameras()}
    base = "cam" + re.sub(r"[^0-9A-Za-z]", "", host.rsplit(".", 1)[-1] or host)[:20]
    return next(i for i in [base] + [f"{base}-{n}" for n in range(2, 100)] if i not in taken)


STORAGE_ACTIONS = ("use", "connect", "forget")


def storage_section(lang, csrf):
    """«Общие» у admin: где записи — SD-карта или USB-диск; подключить диск (отформатировать или как есть, если он
    уже в ext4), отключить. Делает root-сервис zorcam-nvr-storage по заявке: здесь только заявка и состояние."""
    disk, state = storage_disk(), zorcam_storage.read_json(zorcam_storage.STATE_FILE) or {}
    pending = os.path.exists(zorcam_storage.REQUEST_FILE)
    mounted = os.path.ismount(BASE_DIR)
    try:
        free = fmt_bytes(shutil.disk_usage(BASE_DIR).free, lang)
    except OSError:
        free = "?"
    rows = []
    if disk and mounted:
        rows.append((t(lang, "storage.usb", model=disk["model"], size=fmt_bytes(disk["size"], lang)),
                     t(lang, "storage.free", free=free), ""))
    elif disk:
        rows.append((t(lang, "storage.missing", model=disk["model"]), t(lang, "storage.free", free=free), "bad"))
    else:
        rows.append((t(lang, "storage.sd"), t(lang, "storage.free", free=free), ""))
    phase = state.get("phase")
    busy = pending or phase in ("formatting", "moving")
    if busy:
        rows.append((t(lang, "storage.busy_" + phase) if phase in ("formatting", "moving") else t(lang, "storage.busy"),
                     "", ""))
    body = [h("div", h("div", k, class_="k"), h("div", v, class_=f"v {cls}"), class_="row") for k, v, cls in rows]
    if phase == "error" and not pending:
        body.append(h("div", h("div", t(lang, "storage.error", error=state.get("message", "")), class_="k bad"), class_="row"))
    actions = []
    if not busy:
        if disk:
            actions.append(post_button("/settings/storage", {"action": "forget"}, t(lang, "storage.forget"),
                                       t(lang, "storage.forget_q", model=disk["model"]), csrf))
        own = {disk["uuid"]} if disk else set()
        others = [d for d in usb_disks() if not own & set(d["uuids"])]
        for d in others:  # у каждого диска — свои кнопки под ним
            name = f'{d["model"]}, {fmt_bytes(d["size"], lang)}'
            buttons = [post_button("/settings/storage", {"action": "connect", "disk": d["path"]},
                                   t(lang, "storage.connect", model=d["model"]),
                                   t(lang, "storage.connect_q", model=d["model"]), csrf)] if d["ext4"] else []
            buttons.append(post_button("/settings/storage", {"action": "use", "disk": d["path"]},
                                       t(lang, "storage.format", model=d["model"]),
                                       t(lang, "storage.format_q", model=name), csrf, danger=True))
            body.append(h("div", h("div", name, h("span", d["path"] + (f' · {t(lang, "storage.ext4")}' if d["ext4"] else ""),
                                                  class_="sub"), class_="k"), class_="row"))
            body.append(h("div", buttons, class_="actions disk"))
        if not others and not disk:
            body.append(h("div", t(lang, "storage.none"), class_="tip"))
    # страница обновляет себя (static/settings.js), пока root-сервис работает
    return cat(h("section", h("h2", t(lang, "storage.title")), body, h("div", actions, class_="actions") if actions else "",
                 id="storage"), h("span", hidden=True, data_reload=5000) if busy else "")


def cloud_section(lang, csrf):
    """«Общие» у admin: облако Zorcam — подключить (код для приложения), состояние, отвязать."""
    st, data = zorcam_cloud.state(), zorcam_cloud.load()
    head = h("h2", t(lang, "cloud.title"))
    if st == "paired":
        url = data.get("url") or ""
        owner = data.get("owner_name")
        return h("section", head,
                 h("div", h("div", t(lang, "cloud.paired"), h("span", h("a", url, href=f"{url}/"), class_="sub"), class_="k"),
                   class_="row"),
                 h("div", h("div", t(lang, "cloud.owner"), class_="k"), h("div", owner, class_="v"), class_="row")
                 if isinstance(owner, str) and owner else "",
                 h("div", post_button("/cloud/disconnect", {}, t(lang, "cloud.disconnect"), t(lang, "cloud.disconnect_q"), csrf,
                                      danger=True), class_="actions"))
    if st == "pending":
        link = data.get("pair_url") or ""
        return h("section", head,
                 h("div", h("div", t(lang, "cloud.enter_code"),
                            h("span", h("b", data.get("code", ""), style="font-size:1.4rem;letter-spacing:.1em"), class_="sub"),
                            h("span", h("a", link, href=link, target="_blank", rel="noopener"), class_="sub"), class_="k"),
                   class_="row"),
                 h("div", post_button("/cloud/disconnect", {}, t(lang, "cloud.cancel"), t(lang, "cloud.cancel_q"), csrf),
                   class_="actions"),
                 h("span", hidden=True, data_reload=5000))  # страница ждёт ввода кода и обновляет себя (static/settings.js)
    note = h("div", t(lang, "cloud.expired"), class_="msg") if st == "expired" else ""
    return h("section", head,
             h("form", h("input", type="hidden", name="csrf", value=csrf), h("div", t(lang, "cloud.intro"), class_="tip"),
               h("div", h("div", t(lang, "cloud.server"), class_="k"),
                 h("input", type="text", name="server", value=data.get("server") or CLOUD_DEFAULT, **HOST_INPUT,
                   placeholder="https://cloud.…", required=True), note, class_="row field"),
               h("div", h("button", t(lang, "cloud.connect"), type="submit", data_busy=t(lang, "cloud.connecting")),
                 class_="actions"), method="POST", action="/cloud/connect"))


# облако по умолчанию — того же стенда, что и это устройство; уже сохранённый адрес остаётся как есть
CLOUD_URLS = {"prod": "https://cloud.zorcam.ru", "stage": "https://cloud.stage.zorcam.ru",
              "test": "https://cloud.test.zorcam.ru"}
CLOUD_DEFAULT = os.environ.get("ZORCAM_CLOUD_URL", CLOUD_URLS[ENVIRONMENT])


YANDEX_TOKEN_URL = "https://oauth.yandex.ru/authorize?response_type=token&client_id=c473ca268cd749d3a8371351a8f2bcbd"


def yandex_cloud_url():
    """Адрес этого Pi в облаке Zorcam (https://<id>.devices.<домен облака>), пока оно подключено, иначе "".
    «Умный дом Яндекса» работает только через него: запросы Яндекса приходят через туннель облака."""
    if zorcam_cloud.state() != "paired":
        return ""
    url = (zorcam_cloud.load().get("url") or "").rstrip("/")
    return url if zorcam_cloud.URL_RE.fullmatch(url) else ""


def is_yandex_path(path):
    """Облако пропускает /yandex и /yandex/* без своего входа, поэтому всё, что хоть как-то (в том числе после
    раскодирования и сворачивания «..») начинается с /yandex, отдаётся только обработчикам Яндекса."""
    plain = unquote(path).lower()
    return any(p.lstrip("/").startswith("yandex") for p in (path.lower(), plain, posixpath.normpath(plain)))


def yandex_need_cloud(lang):
    return h("div", t(lang, "ya.need_cloud"), class_="tip")


def yandex_link_section(lang):
    """«Общие» у admin: строка «Умный дом Яндекса» со ссылкой на страницу подключения; без облака — пояснение."""
    if not yandex_cloud_url():
        return h("section", h("h2", t(lang, "ya.title")), yandex_need_cloud(lang))
    state = t(lang, "ya.linked" if YANDEX and YANDEX.status()["linked"] else "ya.not_linked")
    return h("section", h("h2", t(lang, "ya.title")),
             h("div", h("div", t(lang, "ya.short"), h("span", state, class_="sub"), class_="k"), class_="row"),
             h("div", h("a", t(lang, "ya.open"), href="/yandex"), class_="actions"))


def render_yandex_page(lang, csrf, saved=False):
    """Подключение к «Умному дому Яндекса»: что вписать в навык на dialogs.yandex.ru, секрет, ID и токен навыка,
    состояние связки. Адреса — от адреса Pi в облаке, как бы ни открыли эту страницу; без облака — только пояснение."""
    base = yandex_cloud_url()
    if not base:
        return settings_page(lang, t(lang, "ya.title"), settings_url("general"), sections=h(
            "section", h("h2", t(lang, "ya.title")), yandex_need_cloud(lang),
            h("div", h("a", t(lang, "ya.back"), href=settings_url("general")), class_="actions")))
    st = YANDEX.status()
    rows = [(t(lang, "ya.endpoint"), f"{base}/yandex"), (t(lang, "ya.client_id"), yandex.CLIENT_ID),
            (t(lang, "ya.auth_url"), f"{base}/yandex/auth"), (t(lang, "ya.token_url"), f"{base}/yandex/token"),
            (t(lang, "ya.refresh_url"), f"{base}/yandex/token")]
    values = cat(h("div", h("div", k, h("span", v, class_="sub"), class_="k"), class_="row") for k, v in rows)
    if st["linked"]:
        since = time.strftime("%d.%m.%Y %H:%M", time.localtime(st["linked_since"])) if st["linked_since"] else ""
        state = t(lang, "ya.linked_since", since=since) if since else t(lang, "ya.linked")
    else:
        state = t(lang, "ya.not_linked")
    status = h("div", h("div", state, h("span", t(lang, "ya.last", result=st["last_result"]), class_="sub")
                        if st["last_result"] else "", class_="k"), class_="row")
    unlink = (h("div", post_button("/yandex/unlink", {}, t(lang, "ya.unlink"), t(lang, "ya.unlink_q"), csrf, danger=True),
                class_="actions") if st["linked"] else "")
    keep = t(lang, "ya.keep")
    form = h("form", h("input", type="hidden", name="csrf", value=csrf),
             h("div", h("div", t(lang, "ya.secret"), h("span", t(lang, "ya.secret_hint"), class_="sub"), class_="k"),
               h("input", type="password", name="client_secret", value="", autocomplete="new-password",
                 placeholder=keep if st["has_secret"] else None), class_="row field"),
             h("div", h("div", t(lang, "ya.skill_id"), h("span", t(lang, "ya.skill_id_hint"), class_="sub"), class_="k"),
               h("input", type="text", name="skill_id", value=st["skill_id"], **HOST_INPUT), class_="row field"),
             h("div", h("div", t(lang, "ya.skill_token"),
                        h("span", th(lang, "ya.skill_token_hint", url=YANDEX_TOKEN_URL), class_="sub"), class_="k"),
               h("input", type="password", name="skill_token", value="", autocomplete="new-password",
                 placeholder=keep if st["has_token"] else None), class_="row field"),
             h("div", h("button", t(lang, "cfgpage.save"), type="submit"), class_="actions"),
             method="POST", action="/yandex/save")

    def text_row(key):
        return h("div", h("div", t(lang, key), class_="k"), class_="row")
    sections = cat(banner("ok", t(lang, "ya.saved")) if saved else "",
                   h("section", h("h2", t(lang, "ya.title")), text_row("ya.intro"), status, unlink),
                   h("section", h("h2", t(lang, "ya.skill_section")), text_row("ya.skill_intro"), values),
                   h("section", h("h2", t(lang, "ya.keys_section")), form),
                   h("section", h("h2", t(lang, "ya.next_section")), text_row("ya.next")))
    return settings_page(lang, t(lang, "ya.title"), settings_url("general"), sections=sections)


def mqtt_link_section(lang):
    """«Общие» у admin: строка «MQTT и Home Assistant» со ссылкой на страницу настройки."""
    cfg = MQTT.settings() if MQTT is not None else None
    state = t(lang, "mqtt.on", host=cfg["host"], port=cfg["port"]) if cfg and cfg["enabled"] else t(lang, "mqtt.off")
    return h("section", h("h2", t(lang, "mqtt.title")),
             h("div", h("div", t(lang, "mqtt.short"), h("span", state, class_="sub"), class_="k"), class_="row"),
             h("div", h("a", t(lang, "mqtt.open"), href="/mqtt"), class_="actions"))


def render_mqtt_page(lang, csrf, saved=False, values=None, errors=None):
    """Настройка MQTT: брокер, логин и пароль (пароль в форму не возвращается), префикс, имя устройства, discovery.
    values — введённое в форме после ошибки; errors — {поле: Msg}."""
    cfg = MQTT.form_values()
    if values:
        cfg.update(values)
    errors = errors or {}

    def field(name, label, control, hint=None):
        err = errors.get(name)
        return h("div", h("div", label, h("span", hint, class_="sub") if hint else "", class_="k"), control,
                 h("div", err.text(lang), class_="msg") if err else "",
                 class_="row field err" if err else "row field")

    def text(name, value, **kw):
        return h("input", type="text", name=name, value=value, **{**HOST_INPUT, **kw})

    form = h("form", h("input", type="hidden", name="csrf", value=csrf),
             switch_row(t(lang, "mqtt.enabled"), "enabled", "yes", cfg["enabled"]),
             field("host", t(lang, "mqtt.host"), text("host", cfg["host"], maxlength=253), t(lang, "mqtt.host_hint")),
             field("port", t(lang, "mqtt.port"), text("port", cfg["port"], inputmode="numeric", maxlength=5), t(lang, "mqtt.port_hint")),
             switch_row(t(lang, "mqtt.tls"), "tls", "yes", cfg["tls"]),
             h("div", t(lang, "mqtt.tls_hint"), class_="tip"),
             field("username", t(lang, "mqtt.username"), text("username", cfg["username"], maxlength=mqtt.MAX_LOGIN),
                   t(lang, "mqtt.username_hint")),
             field("password", t(lang, "mqtt.password"),
                   h("input", type="password", name="password", value="", autocomplete="new-password", maxlength=mqtt.MAX_LOGIN,
                     placeholder=t(lang, "mqtt.keep") if cfg["has_password"] else None)),
             field("prefix", t(lang, "mqtt.prefix"), text("prefix", cfg["prefix"], maxlength=mqtt.MAX_PREFIX),
                   t(lang, "mqtt.prefix_hint")),
             field("device", t(lang, "mqtt.device"), text("device", cfg["device"], maxlength=32), t(lang, "mqtt.device_hint")),
             switch_row(t(lang, "mqtt.discovery"), "discovery", "yes", cfg["discovery"]),
             h("div", t(lang, "mqtt.discovery_hint"), class_="tip"),
             h("div", h("button", t(lang, "cfgpage.save"), type="submit"), class_="actions"),
             method="POST", action="/mqtt/save")
    sections = cat(banner("ok", t(lang, "mqtt.saved")) if saved else "",
                   banner("bad", t(lang, "mqtt.invalid")) if errors else "",
                   h("section", h("h2", t(lang, "mqtt.title")), h("div", t(lang, "mqtt.intro"), class_="tip"), form))
    return settings_page(lang, t(lang, "mqtt.title"), settings_url("general"), sections=sections)


def render_yandex_auth(lang, params, error=""):
    """Страница, на которую Яндекс отправляет владельца при связке аккаунтов: вход admin и разрешение."""
    hidden = cat(h("input", type="hidden", name=k, value=params.get(k, "")) for k in ("client_id", "redirect_uri", "state", "response_type"))
    sections = cat(banner("bad", error) if error else "",
                   h("section", h("h2", t(lang, "ya.auth_title")),
                     h("form", hidden, h("div", h("div", t(lang, "ya.auth_text"), class_="k"), class_="row"),
                       h("div", h("div", t(lang, "login.user"), class_="k"),
                         h("input", type="text", name="username", value="admin", autocomplete="username", autocapitalize="off"),
                         class_="row field"),
                       h("div", h("div", t(lang, "login.password"), class_="k"),
                         h("input", type="password", name="password", value="", autocomplete="current-password"),
                         class_="row field"),
                       h("div", h("button", t(lang, "ya.allow"), type="submit"), class_="actions"),
                       method="POST", action="/yandex/auth")))
    return settings_page(lang, t(lang, "ya.auth_title"), sections=sections)


def render_onvif_page(lang, csrf, devices, setup=False, subnet="", error=None):
    """Камеры, ответившие на поиск ONVIF; уже добавленные помечены. У каждой — логин и пароль для чтения адресов.
    subnet — подсеть, где искать ещё (камеры в другой подсети за роутером), error — её нельзя было взять."""
    hosts = camera_hosts()
    sections = []
    for dev in devices:
        title = dev["name"] or dev["hardware"] or dev["host"]
        detail = " · ".join(x for x in (dev["host"], dev["hardware"] if dev["hardware"] != title else "") if x)
        added = dev["host"] in hosts
        tag = h("span", t(lang, "onvif.added"), class_="tag") if added else ""
        sections.append(h(
            "section", h("h2", title, tag),
            h("form", h("input", type="hidden", name="csrf", value=csrf), h("input", type="hidden", name="xaddr", value=dev["xaddr"]),
              h("input", type="hidden", name="name", value=dev["name"]),
              h("input", type="hidden", name="setup", value="1" if setup else ""),
              h("div", h("div", detail, class_="k"), class_="row field"),
              h("div", h("div", t(lang, "cam.label.rtsp_user"), class_="k"),
                h("input", type="text", name="rtsp_user", value="admin", autocomplete="off", autocapitalize="off"),
                class_="row field"),
              h("div", h("div", t(lang, "cam.password"), class_="k"),
                h("input", type="password", name="password", value="", autocomplete="new-password"), class_="row field"),
              h("div", h("button", t(lang, "onvif.add"), type="submit", data_busy=t(lang, "onvif.reading")), class_="actions"),
              method="POST", action="/onvif"), class_="added" if added else None))
    intro = cat(banner("bad", error) if error else "", banner("ok" if devices else "bad", t(lang, "onvif.intro" if devices else "onvif.none")))
    q = "?setup=1" if setup else ""
    # «Искать снова» — форма: можно добавить подсеть за роутером (поиск по ней — запросом каждому адресу)
    actions = cat(
        h("form", h("section", h("h2", t(lang, "onvif.subnet")), h("input", type="hidden", name="setup", value="1" if setup else ""),
                    h("div", h("div", t(lang, "onvif.subnet_label"), h("span", t(lang, "onvif.subnet_hint"), class_="sub"), class_="k"),
                      # the decimal keypad of an iPhone in Russian has a comma instead of a dot: the URL keypad has the dot
                      h("input", type="text", name="subnet", value=subnet, placeholder="192.168.2.0", **HOST_INPUT,
                        oninput="this.value = this.value.replace(/,/g, '.')"), class_="row field"),
                    h("div", h("button", t(lang, "onvif.again"), type="submit", data_busy=t(lang, "onvif.searching")),
                      class_="actions")), method="GET", action="/onvif"),
        h("div", h("a", t(lang, "onvif.manual"), href=f"/camera/new{q}"), class_="choice"))
    back = "/setup/cameras" if setup else "/camera/new"
    return settings_page(lang, t(lang, "onvif.title"), back, "", "", cat(intro, sections, actions))


def camera_address_hosts(cam):
    """Имена хостов (без регистра) всех заданных адресов камеры: потоки и ONVIF."""
    hosts = set()
    for key in ("rtsp_url", "rtsp_sub_url", "onvif_url"):
        if cam.get(key):
            try:
                hosts.add((urlparse(cam[key]).hostname or "").lower())
            except ValueError:
                hosts.add(cam[key])  # не разобрался — считается другим хостом
    return hosts


def render_camera_form(lang, csrf, cam=None, form=None, errors=None, problem=None, setup=False, notices=(),
                       onvif_token=""):
    """Форма камеры (только admin): новая (cam None) или изменение; form — введённое (без пароля),
    problem — что ответила камера при проверке (тогда можно сохранить, отметив «Сохранить всё равно»);
    notices — (класс, текст) над формой; onvif_token — пароль, введённый при поиске ONVIF, ждёт в ONVIF_PASSWORDS."""
    errors = errors or {}
    values = form or ({k: zorcam_config.format_value(cam[k]) for k in CAMERA_FORM_KEYS} if cam else
                      {k: zorcam_config.format_value(d) for k, d, *_ in zorcam_config.CAMERA_FIELDS})
    parts = []
    if errors:
        parts.append(banner("bad", t(lang, "cfgpage.errors", n=len(errors))))
    if problem:
        parts.append(banner("bad", t(lang, "cam.probe_failed", error=problem)))
    parts.extend(banner(cls, text) for cls, text in notices)
    if cam and not setup:
        parts.append(camera_frame_html(lang, cam["id"]))
    if not cam and not onvif_token and not form:
        parts.append(h("div", h("a", t(lang, "onvif.find"), href="/onvif?setup=1" if setup else "/onvif",
                                data_busy=t(lang, "onvif.searching")), class_="choice"))
    parts.append(h("details", h("summary", t(lang, "cam.tips_title")), h("div", th(lang, "cam.tips"), class_="tip"),
                   class_="tips"))
    fields = []
    for key, _default, typ, _desc in zorcam_config.CAMERA_FIELDS:
        value = values.get(key, "")
        hint = h("span", t(lang, "cam.hint." + key), class_="sub") if "cam.hint." + key in i18n.STRINGS else ""
        if typ is bool:  # тумблер; выключенный флажок форма не отправляет — camera_save считает его «no»
            on = value.lower() in ("yes", "true", "on", "1")
            # «enabled» без отметки = «как было»: скрытое «no» говорит, что тумблер на странице был и его сняли
            hidden = h("input", type="hidden", name=key, value="no") if key == "enabled" else ""
            fields.append(h("div", h("label", h("span", t(lang, "cam.label." + key), hint), hidden,
                                     h("input", type="checkbox", class_="switch", name=key, value="yes", checked=on),
                                     class_="switch-row"), class_="row field"))
            continue
        if key == "id" and cam:  # ID не меняется (имя папок записей): строка, не поле; сервер берёт его из «edit»
            fields.append(h("div", h("div", t(lang, "cam.label.id"), hint, class_="k"), h("div", cam["id"], class_="v"),
                            class_="row"))
            continue
        err = errors.get(key)
        needs = CAMERA_NEEDS.get(key)  # поле активно, только пока включён этот тумблер
        off = bool(needs) and values.get(needs, "").lower() not in ("yes", "true", "on", "1")
        placeholder = {"rtsp_url": "rtsp://192.168.1.50:554/live/0/MAIN",
                       "rtsp_sub_url": "rtsp://192.168.1.50:554/live/0/SUB"}.get(key)
        fields.append(h("div", h("div", t(lang, "cam.label." + key), hint, class_="k"),
                        h("input", type="text", name=key, value=value, placeholder=placeholder,
                          inputmode="decimal" if typ is float else "numeric" if typ is int else None, data_needs=needs,
                          disabled=off, autocomplete="off"),
                        h("div", err.text(lang), class_="msg") if err else "",
                        class_="row field" + (" err" if err else "") + (" off" if off else "")))
        if key == "rtsp_user":
            has = bool(cam and zorcam_config.camera_password(cam["id"]))
            placeholder = t(lang, "onvif.password_kept" if onvif_token else "cfgpage.password_placeholder" if has else "cam.password_none")
            err = errors.get("password")
            fields.append(h("div", h("div", t(lang, "cam.password"), class_="k"),
                            h("input", type="password", name="password", value="", placeholder=placeholder,
                              autocomplete="new-password"),
                            h("div", err.text(lang), class_="msg") if err else "",
                            class_="row field" + (" err" if err else "")))
    if problem:
        fields.append(h("div", h("label", h("input", type="checkbox", name="force", value="1"), t(lang, "cam.force"), class_="check"),
                        class_="row field"))
    parts.append(h("form", h("input", type="hidden", name="csrf", value=csrf),
                   h("input", type="hidden", name="edit", value=cam["id"] if cam else ""),
                   h("input", type="hidden", name="setup", value="1" if setup else ""),
                   h("input", type="hidden", name="onvif", value=onvif_token),
                   h("section", h("h2", t(lang, "cam.section")), fields),
                   h("div", h("a", t(lang, "cfgpage.cancel"), href="/setup/cameras" if setup else settings_url("cameras")),
                     h("button", t(lang, "cfgpage.save"), type="submit"), class_="actions"),
                   method="POST", action="/camera/save"))
    if cam and not setup:
        parts.append(h("section", h("a", t(lang, "zones.link"), class_="row action", href=f"/camera/{quote(cam['id'], safe='')}/zones")))
        parts.append(h("section", h("h2", t(lang, "cam.delete_section")), h("div", t(lang, "cam.delete_note"), class_="tip"),
                       h("div", post_button("/camera/delete", {"id": cam["id"], "recordings": ""}, t(lang, "cam.delete"),
                                            t(lang, "cam.confirm_delete", name=cam["name"]), csrf),
                         post_button("/camera/delete", {"id": cam["id"], "recordings": "1"}, t(lang, "cam.delete_all"),
                                     t(lang, "cam.confirm_delete_all", name=cam["name"]), csrf), class_="rec")))
    heading = t(lang, "cam.edit_title", name=cam["name"]) if cam else t(lang, "cam.new_title")
    steps = h("p", t(lang, "setup.step", n=3), class_="steps") if setup else ""
    return settings_page(lang, heading, "/setup/cameras" if setup else settings_url("cameras"), "", steps, cat(parts))


def render_zones_page(lang, csrf, cam):
    """Зоны движения камеры (admin): сетка 16×9 поверх недавнего кадра, рисует static/zones.js (касание и протяжка
    переключают клетки). Клетки лежат в скрытом поле cells (144 символа 0/1), режим — в списке; сохраняет /camera/zones."""
    zone = zorcam_config.load_zones().get(cam["id"]) or {"mode": "exclude", "cells": "0" * (zorcam_config.ZONE_COLS * zorcam_config.ZONE_ROWS)}
    modes = [h("option", t(lang, "zones.mode." + m), value=m, selected=m == zone["mode"]) for m in zorcam_config.ZONE_MODES]
    sections = h("form",
                 h("input", type="hidden", name="csrf", value=csrf),
                 h("input", type="hidden", name="id", value=cam["id"]),
                 h("input", type="hidden", name="cells", value=zone["cells"]),
                 h("div", camera_frame_html(lang, cam["id"]),
                   h("div", class_="zonegrid", data_cols=zorcam_config.ZONE_COLS, data_rows=zorcam_config.ZONE_ROWS,
                     role="group", aria_label=t(lang, "zones.grid")),
                   class_="zones"),
                 h("div", t(lang, "zones.all_painted"), class_="banner bad", id="zones-warn", hidden=True),
                 h("section", h("div", h("div", t(lang, "zones.mode"), class_="k"), h("select", modes, name="mode"),
                                class_="row field"),
                   h("div", t(lang, "zones.note"), class_="tip")),
                 h("div", h("a", t(lang, "cfgpage.cancel"), href=f"/camera/{quote(cam['id'], safe='')}"),
                   h("button", t(lang, "zones.clear"), type="button", class_="secondary", id="zones-clear"),
                   h("button", t(lang, "cfgpage.save"), type="submit"), class_="actions"),
                 method="POST", action="/camera/zones")
    return settings_page(lang, t(lang, "zones.title", name=cam["name"]), f"/camera/{quote(cam['id'], safe='')}", "",
                         h("p", t(lang, "zones.intro"), class_="intro"), sections, extra="zones")


# ---------------------------------------------------------------- мастер первичной настройки

# шаг 1: сразу виден язык, остальное — под «Другие настройки» со значениями по умолчанию
SETUP_MAIN = [("language", "language")]


def render_setup_general(lang, csrf, raw=None, errors=None):
    """Шаг 1: язык, остальное — под «Другие настройки»."""
    errors = errors or {}
    parts = [h("p", t(lang, "setup.step", n=1), class_="steps"), h("p", t(lang, "setup.intro"), class_="intro")]
    if errors:
        parts.append(banner("bad", t(lang, "cfgpage.errors", n=len(errors))))
    current = raw[("language", "language")] if raw else zorcam_config.format_value(CFG.get("language", "language"))
    main = h("div", h("div", t(lang, "setup.lang_label"), class_="k"),
             h("select", (h("option", choice_label(lang, "language", "language", c), value=c, selected=c == current)
                          for c in zorcam_config.CHOICES[("language", "language")]), name="language.language"),
             class_="row field")
    rest = config_fields(lang, {(s, k) for s, k, *_ in zorcam_config.PARAMS} - set(SETUP_MAIN),
                         raw, errors)
    other = cat(h("section", h("h2", t(lang, "section." + sec)), body) for sec, body in rest.items())
    parts.append(h("form", h("input", type="hidden", name="csrf", value=csrf),
                   h("section", h("h2", t(lang, "setup.general")), main),
                   h("details", h("summary", t(lang, "setup.other")), other, open=any(key not in SETUP_MAIN for key in errors)),
                   h("div", h("button", t(lang, "setup.next"), type="submit"), class_="actions"), method="POST", action="/setup"))
    return settings_page(lang, t(lang, "setup.title"), sections=cat(parts))


def password_error(p1, p2, required):
    """Пароль и повтор: None — годится (или не задан, когда он не обязателен), иначе Msg."""
    if not required and not p1 and not p2:
        return None
    if p1 != p2:
        return Msg("setup.mismatch")
    return Msg("setup.short") if len(p1) < 6 else None


def render_setup_users(lang, csrf, error=None):
    """Шаг 2: пароль администратора admin. Остальные пользователи — потом, на вкладке «Польз.»."""
    form = h("form", h("input", type="hidden", name="csrf", value=csrf),
             h("section", h("h2", t(lang, "setup.user_title.admin")), h("div", t(lang, "setup.user_note.admin"), class_="tip"),
               h("div", h("div", t(lang, "login.user"), class_="k"), h("div", zorcam_users.ADMIN, class_="v"), class_="row"),
               password_rows(lang, "", error)),
             h("div", h("button", t(lang, "setup.next"), type="submit"), class_="actions"), method="POST", action="/setup/users")
    return settings_page(lang, t(lang, "setup.title"), sections=cat(
        h("p", t(lang, "setup.step", n=2), class_="steps"), h("p", t(lang, "setup.users_intro"), class_="intro"), form))


def render_password_change(lang, csrf, error=None):
    """«Смените пароль»: admin с паролем от установщика (zorcam_users.py set … --initial) задаёт свой или пропускает;
    сюда ведёт главная страница, пока у admin стоит метка initial. Страница без «Назад» и панели, как мастер."""
    form = h("form", h("input", type="hidden", name="csrf", value=csrf),
             h("section", h("h2", t(lang, "setup.user_title.admin")),
               h("div", h("div", t(lang, "login.user"), class_="k"), h("div", zorcam_users.ADMIN, class_="v"), class_="row"),
               password_rows(lang, "", error)),
             h("div", h("button", t(lang, "cfgpage.save"), type="submit"), class_="actions"),
             method="POST", action=PASSWORD_URL)
    skip = h("form", h("input", type="hidden", name="csrf", value=csrf),
             h("button", t(lang, "pw.skip"), type="submit"), method="POST", action=PASSWORD_URL + "/skip", class_="skip")
    return settings_page(lang, t(lang, "pw.title"), sections=cat(h("p", t(lang, "pw.intro"), class_="intro"), form, skip))


def render_setup_cameras(lang):
    """Шаг 3: «Готовы добавить камеру?» — добавить (ещё одну) или закончить."""
    cams = cameras()
    parts = [h("p", t(lang, "setup.step", n=3), class_="steps")]
    if cams:
        rows = cat(h("div", h("div", c["name"], h("span", camera_host(c), class_="sub"), class_="k"),
                     h("div", t(lang, "setup.added"), class_="v ok"), class_="row") for c in cams)
        parts += [h("section", h("h2", t(lang, "cams.section")), rows), h("p", t(lang, "setup.more"), class_="intro"),
                  h("div", h("a", t(lang, "setup.add_more"), href="/camera/new?setup=1"),
                    h("a", t(lang, "setup.finish"), class_="primary", href=LIVE_URL), class_="choice")]
    else:
        parts += [h("p", t(lang, "setup.ready"), class_="intro"),
                  h("div", h("a", t(lang, "setup.add_yes"), class_="primary", href="/camera/new?setup=1"),
                    h("a", t(lang, "setup.add_later"), href=LIVE_URL), class_="choice")]
    return settings_page(lang, t(lang, "setup.title"), sections=cat(parts))


def safe_name(name):
    return name and "/" not in name and ".." not in name


class Handler(BaseHTTPRequestHandler):
    # сек ожидания клиента на сокете: телефон, пропавший из сети посреди запроса, не держит поток вечно (и простаивающее
    # keep-alive соединение закрывается через столько же)
    timeout = 60
    # keep-alive: таймлапс берёт сотни превью подряд, а через облако каждое новое подключение — новый рабочий канал
    # туннеля. Ответ без длины тела и ответ на POST (тело запроса не всегда прочитано) закрывают соединение —
    # см. end_headers; send_error Python закрывает сам. Без Nagle: заголовки и тело уходят двумя записями.
    protocol_version = "HTTP/1.1"
    server_version = "zorcam"  # без версии Python в заголовке Server
    sys_version = ""
    disable_nagle_algorithm = True
    status_code = 0
    sized = False  # в ответе есть Content-Length

    def version_string(self):
        return self.server_version

    def log_message(self, fmt, *args):
        pass

    def log_request(self, code="-", size="-"):
        self.status_code = code

    def handle_one_request(self):
        if not DEBUG:
            return super().handle_one_request()
        # requestline остаётся от прошлого запроса: без сброса закрытие keep-alive попадало в журнал повтором с кодом None
        start, self.status_code, self.requestline = time.monotonic(), None, ""
        super().handle_one_request()
        if getattr(self, "requestline", ""):  # app/debug.sh on: каждый запрос с кодом и временем ответа
            path = self.requestline.rsplit(" ", 1)[0]
            log(f"debug: {path} -> {self.status_code} in {time.monotonic() - start:.3f} s "
                f"({threading.active_count()} threads)")

    def parse_request(self):
        self._identity = None  # вход через облако считается заново для каждого запроса
        ok = super().parse_request()
        REQUEST.cloud = ok and self.via_cloud()
        return ok

    def get_session_token(self):
        if self._identity is None:
            self._identity = self.cloud_session() or ""
        return self._identity or self.cookie(SESSION_COOKIE)

    @property
    def on_tunnel(self):
        """Соединение принял туннельный порт (TUNNEL_PORT, только 127.0.0.1), куда frpc подключается за облако. Решает
        порт, а не адрес клиента: обратный прокси на самом Pi тоже приходит с 127.0.0.1 на порт 80."""
        return bool(getattr(self.server, "tunnel", False))

    def via_cloud(self):
        """Запрос пришёл через туннель облака: облако добавляет к каждому запросу X-Zorcam-Identity. На порту 80 заголовок
        игнорируется."""
        return self.on_tunnel and bool(self.headers.get("X-Zorcam-Identity"))

    def cloud_session(self):
        """Вход через облако: X-Zorcam-Identity от туннеля (порт TUNNEL_PORT), подписанный облаком,
        для этого Pi. Дальше — как обычный вход этого пользователя галереи: его роль и камеры из users.json."""
        if not self.via_cloud():
            return None
        user = zorcam_cloud.verify_identity(self.headers["X-Zorcam-Identity"])
        if not user or zorcam_users.role_of(user) is None:
            return None
        cookie = self.cookie(SESSION_COOKIE)
        if check_session(cookie)[0] == user:
            return cookie
        # одна сессия на пользователя облака, а не на каждый запрос без cookie (облако само спрашивает список камер
        # и постеры): иначе sessions.json переписывался бы на SD на каждый такой запрос
        with _cloud_sessions_lock:
            token = _cloud_sessions.get(user)
            if check_session(token)[0] != user:
                token = create_session(user)
                _cloud_sessions[user] = token
                log(f"login: {user} through the cloud")
        self.refresh_cookie = session_cookie(token, secure=True)  # облако — только HTTPS
        return token

    def send_response_only(self, code, message=None):
        self.sized = False
        super().send_response_only(code, message)

    def send_header(self, keyword, value):
        if keyword.lower() == "content-length":
            self.sized = True
        super().send_header(keyword, value)

    def frame_ancestors(self):
        """Кому можно встраивать страницу в рамку: ей самой; через туннель и для встроенного Live — ещё приложению облака
        (его адрес облако присылает при привязке), иначе облако не покажет Live в своей странице."""
        sources = ["'self'"]
        if self.on_tunnel or self.is_embed():
            origin = urlparse(zorcam_cloud.load().get("app_url") or "")
            if (origin.scheme in ("https", "http") and origin.netloc
                    and re.fullmatch(r"[A-Za-z0-9.-]+(:\d{1,5})?", origin.netloc)):
                sources.append(f"{origin.scheme}://{origin.netloc}")
        return " ".join(sources)

    def is_embed(self):
        parsed = urlparse(getattr(self, "path", ""))  # path нет, если разбор строки запроса не удался (ответ 400)
        return parsed.path == "/" and parse_qs(parsed.query).get("embed") == ["1"]

    def end_headers(self):
        cookie = getattr(self, "refresh_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
            self.refresh_cookie = None
        # script-src не ограничиваем: в страницах есть встроенные скрипты (window.T, window.PAGE) и обработчики oninput
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         f"frame-ancestors {self.frame_ancestors()}; object-src 'none'; base-uri 'none'")
        # HTTP/1.1: клиент отличает конец ответа по Content-Length. Перенаправление и 416 без тела получают нулевую
        # длину; ответ с телом неизвестной длины — закрытие соединения; POST — тоже (обработчик мог не прочитать тело
        # запроса, остаток стал бы «следующим запросом»)
        code = self.status_code or 0
        close = self.command == "POST"
        if not self.sized and self.command != "HEAD":
            if code in (301, 302, 303, 307, 308, 416):
                self.send_header("Content-Length", "0")
            elif code >= 200 and code not in (204, 304):
                close = True
        if close and not self.close_connection:  # send_header("Connection", "close") сам закрывает соединение после ответа
            self.send_header("Connection", "close")
        super().end_headers()

    def require_auth(self):
        token = self.get_session_token()
        user, role, refreshed = check_session(token)
        if user is not None:
            self.user, self.role = user, role
            if refreshed:
                self.refresh_cookie = session_cookie(token, secure=self.https_front())  # продлеваем cookie вместе с сессией
            return True
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()
        return False

    @property
    def lang(self):
        """Язык страницы: выбранный на этом устройстве, иначе [language] language, при auto — язык браузера."""
        return self.chosen_lang or i18n.resolve(LANG_SETTING, self.headers.get("Accept-Language"))

    @property
    def chosen_lang(self):
        """Язык, выбранный в «Настройках» на этом устройстве (cookie), или None — «авто»."""
        return i18n.normalize(self.cookie(LANG_COOKIE))

    def require_role(self, role):
        """После require_auth: хватает ли прав; иначе страница 403."""
        if ROLE_LEVEL[self.role] >= ROLE_LEVEL[role]:
            return True
        lang = self.lang
        self.send_html(settings_page(lang, t(lang, "denied.title"), LIVE_URL, sections=(
            f'<section><h2>{t(lang, "denied.title")}</h2><div class="row"><div class="k">'
            f'{t(lang, "denied.text")}</div></div></section>')), status=403)
        return False

    def redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def read_form(self, limit=65536):
        """Поля формы (application/x-www-form-urlencoded) или None, если тело пустое или слишком большое."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        return parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"), keep_blank_values=True)

    def cookie(self, name):
        """Значение cookie или None. Заголовок разбирается сам: SimpleCookie отвергает его целиком из-за одной
        cookie с «недопустимым» именем (например, a@b от другой программы на этом же адресе) — и вход пропадал."""
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, sep, value = part.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip('"')
        return None

    def https_front(self):
        """Браузер пришёл по HTTPS (обратный прокси говорит об этом в X-Forwarded-Proto; облако — только HTTPS): cookie
        сессии тогда с Secure."""
        return self.on_tunnel or (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower() == "https"

    def request_host(self):
        """Адрес, который набрал пользователь: за обратным прокси (HTTPS) — X-Forwarded-Host."""
        return (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0]

    def setup_allowed(self):
        """Шаги 1–2 мастера: пока нет пользователей и только из домашней сети; иначе — страница «Нет доступа»."""
        # туннель облака — это интернет, а не домашняя сеть: мастер через него недоступен
        if not self.on_tunnel and is_local_client(self.client_address[0], self.headers.get("X-Forwarded-For")):
            return True
        lang = self.lang
        self.send_html(settings_page(lang, t(lang, "denied.title"), sections=(
            f'<section><h2>{t(lang, "denied.title")}</h2><div class="row"><div class="k">'
            f'{t(lang, "setup.local_only")}</div></div></section>')), status=403)
        return False

    def setup_csrf(self):
        """CSRF мастера до входа: случайное значение в cookie и то же — в скрытом поле формы."""
        token = self.cookie(SETUP_COOKIE)
        if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
            token = secrets.token_hex(16)
            self.refresh_cookie = f"{SETUP_COOKIE}={token}; Path=/setup; HttpOnly; SameSite=Strict"
        return token

    def setup_form(self):
        """Форма шага 1–2 с проверкой CSRF мастера; None — ответ уже отправлен."""
        form = self.read_form()
        token = self.cookie(SETUP_COOKIE) or ""
        if form is None or not token or not same_secret(form.get("csrf", [""])[0], token):
            self.send_error(403)
            return None
        return form

    def send_html(self, body_str, status=200, cookies=()):
        body = body_str.encode("utf-8")
        self.send_response(status)
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype, cache="no-cache", filename=None):
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng and rng.startswith("bytes="):
            try:
                s, e = rng[len("bytes="):].split("-", 1)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                else:
                    start = max(0, size - int(e))
                end = min(end, size - 1)
                partial = start <= end
            except ValueError:
                start, end = 0, size - 1
        if rng and not partial:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    self.close_connection = True  # файл стал короче, чем обещала длина: оборвать, а не ждать недостающее
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in STATIC_FILES:
            try:
                self.send_file(ICON_FILES[path], "image/png")
            except FileNotFoundError:
                self.send_error(404)
            return

        if path.startswith("/static/"):  # CSS и JS страниц: без входа (нужны странице входа и мастеру), секретов в них нет
            name = path[len("/static/"):]
            asset = ASSETS.get(name)
            if asset is None and name.startswith("brand/"):  # theme.css и знак надстройки (brand/): то же, но из своей папки
                asset = BRAND.assets.get(name[len("brand/"):])
            if asset is None:
                self.send_error(404)
                return
            body, ctype, _ = asset
            # адрес с ?v=<хеш>: новое содержимое — новый адрес, поэтому старый можно хранить вечно
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/sw.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(SW_JS)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(SW_JS)
            return

        if path == "/manifest.webmanifest":
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Content-Length", str(len(MANIFEST)))
            self.end_headers()
            self.wfile.write(MANIFEST)
            return

        if path == "/healthz":  # для systemd watchdog; без авторизации, ничего не раскрывает
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)
            return

        if path != "/yandex" and is_yandex_path(path):  # адреса для «Умного дома Яндекса»: свой вход и токены
            self.yandex_get(path, parsed.query)
            return

        # пока нет ни одного пользователя, всё ведёт в мастер первичной настройки
        if setup_needed() and path not in ("/setup", "/setup/users"):
            self.redirect("/setup")
            return
        if path in ("/setup", "/setup/users"):
            if not setup_needed():
                self.redirect("/setup/cameras" if is_valid_session(self.get_session_token()) else "/login")
            elif self.setup_allowed():
                csrf = self.setup_csrf()
                self.send_html(render_setup_general(self.lang, csrf) if path == "/setup"
                               else render_setup_users(self.lang, csrf))
            return

        if path == "/login":
            if is_valid_session(self.get_session_token()):
                self.send_response(302)  # уже вошли — сразу в Live
                self.send_header("Location", "/?tab=live")
                self.end_headers()
                return
            self.send_html(login_page(self.lang))
            return

        if path == "/logout":
            token = self.get_session_token()
            if token:
                drop_session(token)
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0")
            self.end_headers()
            return

        if not self.require_auth():
            return

        if path == "/":
            qs = parse_qs(parsed.query)
            # пароль admin задал установщик: сначала «Смените пароль» (и после входа формой, и после входа через облако);
            # встроенный Live в рамке облака это не касается
            if self.role == "admin" and zorcam_users.is_initial(self.user) and qs.get("embed", [""])[0] != "1":
                self.redirect(PASSWORD_URL)
                return
            tab = qs.get("tab", [DEFAULT_TAB])[0]
            if tab not in TABS and tab not in RECORD_TABS:
                tab = DEFAULT_TAB
            if tab == "records":
                tab = RECORDS_DEFAULT
            try:
                vpage = int(qs.get("vpage", ["1"])[0])
            except ValueError:
                vpage = 1
            ice = cloud_ice(self.headers.get("X-Zorcam-Ice")) if self.via_cloud() else []
            stun.note_cloud_ice(ice)
            if tab == "live" and qs.get("embed", [""])[0] == "1":
                self.send_html(render_embed(self.role, self.user, self.lang, qs.get("cam", [""])[0], ice=ice))
                return
            # старые ссылки на сетку снимков (mode=, horizon=, fpage=) открывают ту же вкладку: параметры не читаются
            self.send_html(render(tab, self.role, self.user, vpage, self.lang, qs.get("cam", [None])[0], ice=ice,
                                  csrf=csrf_token(self.get_session_token()), at=qs.get("at", [None])[0]))
            return

        if re.fullmatch(r"/ptz/[^/]+", path):
            self.ptz_status()
            return

        live = re.fullmatch(r"/live/([^/]+)/(stream\.m3u8|seg_\d{5}\.ts)", path)
        if path.startswith("/live/") and (not live or not can_see(self.user, self.role, live.group(1))):
            self.send_error(404)
            return

        if live and live.group(2) == "stream.m3u8":
            stream = live_stream(live.group(1))
            stream.touch()
            playlist = stream.wait_playlist()
            if playlist is None:
                self.send_error(503, "Live stream is not ready")
                return
            body = playlist.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if live:
            stream = live_stream(live.group(1))
            stream.touch()
            try:
                self.send_file(os.path.join(stream.dir, live.group(2)), "video/mp2t")
            except FileNotFoundError:
                self.send_error(404)
            return

        if path == "/config":
            if self.require_role("admin"):
                self.send_html(render_config_page(csrf_token(self.get_session_token()), self.lang))
            return

        if path == "/setup/cameras":
            if self.require_role("admin"):
                self.send_html(render_setup_cameras(self.lang))
            return

        if path == PASSWORD_URL:
            if self.require_role("admin"):
                self.send_html(render_password_change(self.lang, csrf_token(self.get_session_token())))
            return

        if path == "/cloud/v1/cameras":  # облачное приложение: камеры, которые видит этот пользователь
            self.send_json({"cameras": [{"id": c["id"], "name": c["name"]} for c in user_cameras(self.user, self.role)
                                        if c.get("enabled", True)]})
            return

        if path == "/cloud/v1/health":  # админка облака по запросу: тот же отчёт о самочувствии, что раз в час
            if self.require_role("admin"):
                self.send_json(zorcam_cloud.health_report(installed_build()))
            return

        if path.startswith("/cloud/v1/poster/"):  # последний кадр камеры — плитка в облачном приложении
            if not self.require_role("viewer"):  # снимки liveviewer закрыты, как и в галерее
                return
            cam_id = unquote(path[len("/cloud/v1/poster/"):])
            frames = list_files(cam_id, ".jpg") if can_see(self.user, self.role, cam_id) else []
            if not frames:
                self.send_error(404)
                return
            try:
                self.send_file(thumb_path(cam_id, frames[0]), "image/jpeg", cache="private, max-age=30")
            except FileNotFoundError:
                self.send_error(404)
            return

        if path.startswith("/camera-frame/"):  # недавний кадр в форме камеры и в списке «Камеры»
            if not self.require_role("admin"):
                return
            cam_id = unquote(path[len("/camera-frame/"):])
            frames = list_files(cam_id, ".jpg") if camera(cam_id) else []
            if not frames:
                self.send_error(404)
                return
            try:
                self.send_file(thumb_path(cam_id, frames[0]), "image/jpeg", cache="private, max-age=30")
            except FileNotFoundError:
                self.send_error(404)
            return

        if path.startswith("/timelapse/"):  # кадры для таймлапса во вкладке Timelapse
            if not self.require_role("viewer"):  # снимки liveviewer закрыты, как и в галерее
                return
            cam_id = unquote(path[len("/timelapse/"):])
            if not can_see(self.user, self.role, cam_id):
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            gzipped = accepts_gzip(self.headers.get("Accept-Encoding"))
            if query.get("days"):  # календарь под плеером
                self.send_json_bytes(timelapse_reply(cam_id, "days", gzipped), gzipped)
                return
            if query.get("day"):  # один календарный день вместо периода
                day = parse_timelapse_day(query["day"][0])
                if day is None:
                    self.send_error(400, "day: YYYY-MM-DD")
                    return
                self.send_json_bytes(timelapse_reply(cam_id, None, gzipped, day=day), gzipped)
                return
            period = query.get("period", [""])[0]
            if period not in TIMELAPSE_PERIODS:
                self.send_error(400, "period: " + ", ".join(TIMELAPSE_PERIODS))
                return
            self.send_json_bytes(timelapse_reply(cam_id, period, gzipped), gzipped)
            return

        if path == "/yandex":
            if self.require_role("admin"):
                if YANDEX is None:
                    self.send_error(503)
                    return
                self.send_html(render_yandex_page(self.lang, csrf_token(self.get_session_token()),
                                                  parse_qs(parsed.query).get("saved") == ["1"]))
            return

        if path == "/mqtt":
            if self.require_role("admin"):
                if MQTT is None:
                    self.send_error(503)
                    return
                self.send_html(render_mqtt_page(self.lang, csrf_token(self.get_session_token()),
                                                parse_qs(parsed.query).get("saved") == ["1"]))
            return

        if path == "/onvif":  # поиск камер ONVIF в домашней сети (несколько секунд)
            if self.require_role("admin"):
                qs = parse_qs(parsed.query, keep_blank_values=True)
                setup = qs.get("setup") == ["1"]
                # подсеть из формы запоминается на этом устройстве; пустое поле — только сеть Pi, и так и запоминается
                remembered = self.cookie(ONVIF_SUBNET_COOKIE)
                subnet = (qs["subnet"][0].strip().replace(",", ".") if "subnet" in qs
                          else ONVIF_SUBNET_DEFAULT if remembered is None else remembered)
                cookies = []
                if "subnet" in qs:
                    cookies.append(f"{ONVIF_SUBNET_COOKIE}={quote(subnet)}; Path=/onvif; SameSite=Lax; Max-Age=31536000")
                error = None
                try:
                    devices = onvif.discover(ONVIF_SCAN_SECONDS, subnet)
                except ValueError:
                    error, devices = t(self.lang, "onvif.subnet_bad", subnet=subnet), onvif.discover(ONVIF_SCAN_SECONDS)
                self.send_html(render_onvif_page(self.lang, csrf_token(self.get_session_token()), devices, setup,
                                                 subnet=subnet, error=error), cookies=cookies)
            return

        if path.startswith("/camera/") and path.endswith("/zones"):
            if not self.require_role("admin"):
                return
            cam = camera(unquote(path[len("/camera/"):-len("/zones")]))
            if cam is None:
                self.send_error(404)
                return
            self.send_html(render_zones_page(self.lang, csrf_token(self.get_session_token()), cam))
            return

        if path.startswith("/camera/"):
            if not self.require_role("admin"):
                return
            cam_id = unquote(path[len("/camera/"):])
            csrf = csrf_token(self.get_session_token())
            setup = parse_qs(parsed.query).get("setup") == ["1"]
            if cam_id == "new":
                self.send_html(render_camera_form(self.lang, csrf, setup=setup))
            elif camera(cam_id):
                self.send_html(render_camera_form(self.lang, csrf, camera(cam_id)))
            else:
                self.send_error(404)
            return

        if path == "/settings":  # у каждой роли: «Общие» и «Пользователи»; у admin — и «Камеры», «Состояние»
            if self.require_role("liveviewer"):
                qs = parse_qs(parsed.query)
                saved = qs.get("saved", [""])[0]
                ended = qs.get("ended", [""])[0]  # сеансы этого пользователя завершены (admin)
                self.send_html(render_settings(self.role, self.lang, self.user, csrf_token(self.get_session_token()),
                                               tab=qs.get("tab", ["general"])[0], chosen_lang=self.chosen_lang,
                                               saved=saved if saved in zorcam_users.load() else None,
                                               added=qs.get("added", [""])[0] or None,
                                               ended=ended if ended in zorcam_users.load() else None,
                                               signed_out=qs.get("signed_out", [""])[0] == "1"))
            return

        if path == "/push/key":
            self.send_json({"key": PUSH.public_key if PUSH else None})
            return

        if self.role == "liveviewer" and path.startswith(("/watch/", "/vid/", "/thumb/", "/img/")):
            self.require_role("viewer")  # записи недоступны: страница «Нет доступа»
            return

        # записи: /watch/<камера>/<ролик>, /vid/<камера>/<ролик>.mp4
        rec = re.fullmatch(r"/(watch|vid)/([^/]+)/([^/]+)", unquote(path))
        if rec:
            kind, cam_id, name = rec.groups()
            video_dir = cam_dirs(cam_id)[1]
            fpath = os.path.join(BASE_DIR, video_dir,
                                 name + ".mp4" if kind == "watch" else name)
            if (not can_see(self.user, self.role, cam_id) or not safe_name(name) or not os.path.isfile(fpath)
                    or (kind == "vid" and not name.endswith(".mp4"))):
                self.send_error(404)
                return
            if kind == "vid":
                # ?dl=1 — «Скачать» в окне ролика над Timelapse: файлом, а не в плеере браузера
                dl = parse_qs(parsed.query).get("dl") == ["1"]
                self.send_file(fpath, "video/mp4",
                               filename=re.sub(r"[^A-Za-z0-9._-]", "_", f"{cam_id}_{name}") if dl else None)
                return
            media = watch_media(cam_id, video_dir, name, video_meta(video_dir, name), self.lang)
            if parse_qs(parsed.query).get("frag") == ["1"]:  # окно ролика над Timelapse: только плеер, без страницы
                self.send_html(str(media))
                return
            newer, older = watch_neighbours(cam_id, name)
            caption = f'{camera(cam_id)["name"]} · {format_caption(name)}'
            self.send_html(render_template("watch", self.lang, caption=esc(caption), media=media, newer=esc(newer),
                                           older=esc(older)))
            return

        if path.startswith("/thumb/"):
            parts = unquote(path[len("/thumb/"):]).split("/")
            if len(parts) != 2 or parts[0] not in known_dirs(user_cameras(self.user, self.role)) or not safe_name(parts[1]):
                self.send_error(404)
                return
            if not os.path.isfile(os.path.join(BASE_DIR, parts[0], parts[1])):
                self.send_error(404)
                return
            try:
                # имена кадров уникальны (время съёмки), содержимое не меняется — браузер может кэшировать
                self.send_file(thumb_path(parts[0], parts[1]), "image/jpeg", cache="private, max-age=86400")
            except FileNotFoundError:
                self.send_error(404)
            return

        if path.startswith("/img/"):
            parts = unquote(path[len("/img/"):]).split("/")
            if len(parts) != 2 or parts[0] not in known_dirs(user_cameras(self.user, self.role)) or not safe_name(parts[1]):
                self.send_error(404)
                return
            fpath = os.path.join(BASE_DIR, parts[0], parts[1])
            if not os.path.isfile(fpath):
                self.send_error(404)
                return
            self.send_file(fpath, "image/jpeg")
            return

        self.send_error(404)

    def tunnel_client_ip(self):
        """Адрес клиента, которого облако ставит в X-Zorcam-Client-IP запросам Яндекса через туннель (там
        client_address всегда 127.0.0.1). Верим только запросу на туннельном порту и только настоящему IP; иначе None."""
        if not self.on_tunnel:
            return None
        try:
            ip = ipaddress.ip_address((self.headers.get("X-Zorcam-Client-IP") or "").strip())
        except ValueError:
            return None
        return None if ip.is_loopback else str(ip)  # не делить счётчик с местными запросами

    def client_ip(self):
        """Адрес клиента для счётчиков неудачных входов. Через туннель — X-Zorcam-Client-IP от облака; от доверенного
        обратного прокси ([gallery] trusted_proxies) — последний элемент X-Forwarded-For (его дописал сам прокси);
        иначе адрес соединения: заголовок от кого попало ничего не значит."""
        peer = self.client_address[0]
        if self.on_tunnel:
            return self.tunnel_client_ip() or peer
        if peer in TRUSTED_PROXIES:
            last = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            try:
                return str(ipaddress.ip_address(last))
            except ValueError:
                pass
        return peer

    def client_desc(self):
        fwd = self.headers.get("X-Forwarded-For")
        tunnel = self.tunnel_client_ip()
        return (self.client_address[0] + (f" (X-Forwarded-For {fwd})" if fwd else "")
                + (f" (client {tunnel})" if tunnel else ""))

    def webrtc_log(self):
        """Причина, по которой браузер не смог подключиться по WebRTC (для journalctl)."""
        if not is_valid_session(self.get_session_token()):
            self.send_error(401)
            return
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 2000)
        except ValueError:
            length = 0
        text = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
        text = "".join(ch if ch.isprintable() else " " for ch in text)
        log(f"webrtc FAIL from {self.client_desc()}: {text}")
        self.send_response(204)
        self.end_headers()

    def proxy_webrtc(self):
        """Сигнализация WebRTC: SDP offer браузера -> go2rtc, SDP answer обратно. Медиа идёт напрямую на порт 8555."""
        user, role, _ = check_session(self.get_session_token())
        if user is None:
            self.send_error(401)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_SDP_SIZE:
            self.send_error(400)
            return
        offer = self.rfile.read(length)
        cam_id = parse_qs(urlparse(self.path).query).get("cam", [""])[0]
        if not can_see(user, role, cam_id):
            self.send_error(404)
            return
        req = urllib.request.Request(
            GO2RTC_WEBRTC_URL + cam_id, data=offer, method="POST", headers={"Content-Type": "application/sdp"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                answer = resp.read(MAX_SDP_SIZE)
        except urllib.error.HTTPError as e:
            detail = e.read(300).decode("utf-8", errors="replace").strip()
            log(f"webrtc offer from {self.client_desc()}: go2rtc HTTP {e.code}: {detail}")
            self.send_error(502, f"go2rtc error {e.code}")
            return
        except (urllib.error.URLError, OSError) as e:
            log(f"webrtc offer from {self.client_desc()}: go2rtc is not available: {e}")
            self.send_error(502, "go2rtc is not available")
            return
        text = answer.decode("utf-8", errors="replace")
        # кандидаты, по которым браузер из интернета дойдёт до проброшенного порта: внешний IP, найденный по STUN
        # (работает и через облако: устройство само за NAT с пробросом, TURN облака остаётся запасным путём), и адрес,
        # по которому открыта галерея (домен DDNS) — но не адрес облака: <id>.devices.<домен облака> ведёт на его сервер, а там
        # порта 8555 нет; домен, который у нас резолвится в частный адрес (KeenDNS из дома), тоже не годится
        if self.via_cloud():
            stun.note_cloud_ice(cloud_ice(self.headers.get("X-Zorcam-Ice")))
        host = public_host(self.request_host()) if not self.via_cloud() else None
        page_ip = host_ip(host) if host else None
        ips = []
        for ip in (stun.external()[0], page_ip if page_ip and stun.usable(page_ip) else None):
            if ip and ip not in ips:
                ips.append(ip)
                text = add_candidates(text, ip)
        if ips:
            answer = text.encode()
        log(f"webrtc offer from {self.client_desc()}: candidates: {sdp_candidates(text)}")
        self.send_response(201)
        self.send_header("Content-Type", "application/sdp")
        self.send_header("Content-Length", str(len(answer)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(answer)

    def send_json(self, obj, status=200):
        self.send_json_bytes(json.dumps(obj).encode(), status=status)

    def send_json_bytes(self, body, gzipped=False, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if gzipped:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, limit=8192):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def push_subscribe(self, subscribe):
        user, role, _ = check_session(self.get_session_token())
        if user is None:
            self.send_error(401)
            return
        if role == "liveviewer":
            self.send_error(403)
            return
        data = self.read_json()
        if PUSH is None or data is None:
            self.send_error(400)
            return
        if not same_secret(data.get("csrf", ""), csrf_token(self.get_session_token())):
            self.send_error(403)
            return
        if not subscribe:
            # чужую подписку не снять (её endpoint — секрет её владельца, но не повод доверять); admin снимает любую
            endpoint = str(data.get("endpoint", ""))
            owner = PUSH.subscriptions.get(endpoint, {}).get("user", user)
            if owner == user or role == "admin":
                PUSH.remove(endpoint)
            self.send_json({"ok": True})
            return
        sub = data.get("subscription")
        known = isinstance(sub, dict) and sub.get("endpoint") in PUSH.subscriptions
        device_lang = i18n.from_accept_language(self.headers.get("Accept-Language"))
        # приложение переподписывается при каждом открытии (quiet): выбор языка на устройстве доходит до push
        if not PUSH.add(sub, self.headers.get("User-Agent", ""), user, device_lang, self.chosen_lang):
            self.send_error(400)
            return
        if not known and not data.get("quiet"):
            log(f"push: new subscription from {self.client_desc()}, total {PUSH.count()}")
            threading.Thread(target=PUSH.send_all, args=(lambda s: {
                "title": "Zorcam", "body": t(push_lang(s), "push.enabled"), "tag": "zorcam-test",
                "url": "/?tab=live",
            }, log, sub["endpoint"]), daemon=True).start()
        self.send_json({"ok": True})

    def admin_form(self, limit=65536):
        """Форма администратора с CSRF-токеном сессии; None — ответ уже отправлен."""
        token = self.get_session_token()
        if not self.require_auth() or not self.require_role("admin"):
            return None
        form = self.read_form(limit)
        if form is None:
            self.send_error(400)
            return None
        if not same_secret(form.get("csrf", [""])[0], csrf_token(token)):
            self.send_error(403)
            return None
        return form

    def notify_set(self):
        """Переключатель на вкладке «Уведомления»: свой (не чужой) выбор, сразу в users.json. Роль — от viewer, CSRF — токеном
        сессии, как у остальных форм; камера — из доступных этому пользователю. Включение камеры с выключенным тумблером
        «Уведомления о движении» включает и его (cameras.json; как любое изменение файла, перезапускает детектор и go2rtc)."""
        token = self.get_session_token()
        if not self.require_auth() or not self.require_role("viewer"):
            return
        data = self.read_json(4096)
        if data is None or not same_secret(data.get("csrf", ""), csrf_token(token)):
            self.send_error(403)
            return
        cam, on = data.get("camera"), data.get("on")
        allowed = {c["id"] for c in user_cameras(self.user, self.role)}
        if not isinstance(on, bool) or not (cam == "all" or cam in allowed):
            self.send_error(400)
            return
        if on and cam != "all":
            cams = cameras()
            target = next((c for c in cams if c["id"] == cam), None)
            if target is not None and not target["notify_motion"]:  # включили здесь — включаем и в настройках камеры
                target["notify_motion"] = True
                zorcam_config.save_cameras(cams)
                log(f"cameras: {cam} motion notifications switched on by {self.user} from the Alerts tab")
        zorcam_users.set_notify(self.user, cam, on)
        log(f"notifications: {self.user} switched {cam} {'on' if on else 'off'}")
        self.send_json({"ok": True})

    def ptz_camera(self):
        """(камера или None, ID) из /ptz/<ID> для viewer и выше; камера — включённая и доступная пользователю;
        None вместо пары — ответ уже отправлен."""
        if not self.require_auth() or not self.require_role("viewer"):
            return None
        cam_id = unquote(urlparse(self.path).path.rsplit("/", 1)[-1])
        cam = next((c for c in user_cameras(self.user, self.role) if c["id"] == cam_id and c.get("enabled", True)), None)
        return cam, cam_id

    def ptz_status(self):
        """GET /ptz/<ID>: {"ptz": true, "pan", "zoom"}, если камера умеет поворачиваться или зумить (для показа кнопок
        в Live: стрелки — pan, «−» «+» — zoom)."""
        got = self.ptz_camera()
        if got is None:
            return
        if got[0] is None:
            self.send_error(404)
            return
        if not got[0].get("onvif_url"):
            self.send_json({"ptz": False, "reason": "no_onvif_url"})  # к камере не ходим
            return
        cap = ptz_of(got[0])
        self.send_json({"ptz": True, "pan": cap.get("pan", True), "zoom": cap.get("zoom", True)} if cap else {"ptz": False})

    def ptz_command(self):
        """POST /ptz/<ID> {csrf, action: move|stop, dir}: непрерывное движение камеры (с остановкой по таймеру камеры) или
        остановка. Роль — от viewer, CSRF — токеном сессии (как у notify_set), камера — из доступных пользователю."""
        token = self.get_session_token()
        got = self.ptz_camera()
        if got is None:
            return
        data = self.read_json(1024)
        if data is None or not same_secret(data.get("csrf", ""), csrf_token(token)):
            self.send_error(403)
            return
        cam, action, direction = got[0], data.get("action"), data.get("dir")
        if cam is None:
            self.send_error(404)
            return
        if action not in ("move", "stop") or (action == "move" and direction not in PTZ_DIRS):
            self.send_error(400)
            return
        if not cam.get("onvif_url"):
            self.send_json({"ok": False, "error": "no_onvif_url"}, status=409)
            return
        cap = ptz_of(cam)
        # направление, которого камера не умеет (стрелки у камеры с одним зумом и наоборот), — как камера без PTZ
        if cap is None or (action == "move" and not cap.get("zoom" if direction in ("in", "out") else "pan", True)):
            self.send_json({"ok": False, "error": "unsupported"}, status=409)
            return
        if action == "move":
            now = time.monotonic()
            if now - PTZ_LAST.get(cam["id"], 0) < PTZ_MIN_GAP:
                self.send_json({"ok": False, "error": "rate"}, status=429)
                return
            PTZ_LAST[cam["id"]] = now
        user, password = cam["rtsp_user"], zorcam_config.camera_password(cam["id"])
        try:
            if action == "stop":
                onvif.ptz_stop(cap, user, password)
            else:
                x, y, z = (v * PTZ_SPEED for v in PTZ_DIRS[direction])
                onvif.ptz_move(cap, user, password, x, y, z, PTZ_MOVE_SECONDS)
        except onvif.OnvifError as e:
            log(f"ptz: {cam['id']}: {action}: {e}")
            ptz_forget(cam["id"])
            self.send_json({"ok": False, "error": e.key}, status=502)
            return
        self.send_json({"ok": True})

    def storage_delete(self):
        """Кнопки удаления записей камеры на странице «Настройки» (только admin, с CSRF-токеном, как /config)."""
        form = self.admin_form(4096)
        if form is None:
            return
        scope, cam_id = form.get("scope", [""])[0], form.get("cam", [""])[0]
        if scope not in ("frames", "videos", "all") or not camera(cam_id):
            self.send_error(400)
            return
        frames, videos, size = delete_recordings(cam_id, scope)
        log(f"storage: {self.user} deleted {scope} of {cam_id}: {frames} frames, {videos} videos, {size} bytes")
        self.redirect(settings_url("cameras"))  # без страницы «Удалено»: список камер сам покажет нули

    def users_save(self):
        """Вкладка «Польз.»: пароль (пусто — не менять), права и доступ к камерам пользователя; или новый
        пользователь (new=1: имя, права, пароль, камеры). Смена пароля завершает сессии этого пользователя
        на других устройствах."""
        form = self.admin_form(8192)
        if form is None:
            return
        users = zorcam_users.load()
        get = lambda k: form.get(k, [""])[0]  # noqa: E731
        p1, p2, role, cams = get("password"), get("repeat"), get("role"), set(form.get("cam", []))
        new = get("new") == "1"
        if new:
            name, key = get("name").strip(), "new"
            if not zorcam_users.name_ok(name) or name.lower() == zorcam_users.ADMIN:
                err = Msg("users.bad_name", n=zorcam_users.MAX_NAME)
            elif zorcam_users.find(name):  # вход без учёта регистра: Slava и slava — одно имя
                err = Msg("users.name_taken")
            else:
                err = password_error(p1, p2, required=True)
        else:
            name = key = get("user")
            if name not in users:
                self.send_error(404)
                return
            err = password_error(p1, p2, required=False)
        is_admin = name == zorcam_users.ADMIN and not new
        if not is_admin and role not in USER_ROLES:
            self.send_error(400)
            return
        if err:
            raw = {"name": name, "role": role, "cam": sorted(cams)} if new else None
            self.send_html(render_settings(self.role, self.lang, self.user, csrf_token(self.get_session_token()),
                                           tab="users", users_error=(key, err), users_raw=raw,
                                           chosen_lang=self.chosen_lang), status=400)
            return
        if p1:
            zorcam_users.set_user(name, users[name]["role"] if is_admin else role, p1)
            if not new:
                end_sessions(name, keep=self.get_session_token())
            log(f"users: {'created ' + name if new else 'password of ' + name} "
                f"({users[name]['role'] if is_admin else role}) by {self.user} from {self.client_desc()}")
        if not is_admin:
            if not p1 and users[name]["role"] != role:
                zorcam_users.set_role(name, role)
                log(f"users: {name} is now {role} (by {self.user})")
            allowed = {c["id"] for c in cameras()} & cams
            if new or allowed != zorcam_users.cameras_of(name):
                zorcam_users.set_cameras(name, allowed)
                log(f"users: {name} sees: {', '.join(sorted(allowed)) or 'no cameras'} (by {self.user})")
        self.redirect(settings_url("users") + "&saved=" + quote(name))

    def users_delete(self):
        """Удалить пользователя (кроме admin); его сессии сразу перестают действовать."""
        form = self.admin_form(4096)
        if form is None:
            return
        name = form.get("user", [""])[0]
        users = zorcam_users.load()
        if name not in users or users[name]["role"] == "admin":
            self.send_error(400)
            return
        zorcam_users.delete_user(name)
        log(f"users: {name} deleted by {self.user} from {self.client_desc()}")
        self.redirect(settings_url("users"))

    def sessions_others(self):
        """«Выйти на других устройствах»: все сессии этого пользователя, кроме текущей. Любая роль."""
        token = self.get_session_token()
        if not self.require_auth() or not self.require_role("liveviewer"):
            return
        form = self.read_form(4096)
        if form is None:
            self.send_error(400)
            return
        if not same_secret(form.get("csrf", [""])[0], csrf_token(token)):
            self.send_error(403)
            return
        count = end_sessions(self.user, keep=token)
        log(f"sessions: {self.user} signed out {count} other session(s) from {self.client_desc()}")
        self.redirect(settings_url("users") + "&signed_out=1")

    def users_sessions(self):
        """admin завершает все сессии другого пользователя (себе — «Выйти на других устройствах»)."""
        form = self.admin_form(4096)
        if form is None:
            return
        name = form.get("user", [""])[0]
        if name not in zorcam_users.load() or name == self.user:
            self.send_error(400)
            return
        count = end_sessions(name)
        log(f"sessions: {name} signed out everywhere ({count} session(s)) by {self.user} from {self.client_desc()}")
        self.redirect(settings_url("users") + "&ended=" + quote(name))

    def motion_event(self):
        """Сигнал от детектора движения (zorcam-nvr-capture) — только с самого Pi, на порту 80 и с общим секретом
        (X-Zorcam-Internal): иначе его мог бы слать любой локальный процесс или запрос из интернета через прокси."""
        if (self.on_tunnel or self.client_address[0] != "127.0.0.1" or self.headers.get("X-Forwarded-For")
                or not INTERNAL_TOKEN or not same_secret(self.headers.get("X-Zorcam-Internal"), INTERNAL_TOKEN)):
            self.send_error(403)
            return
        data = self.read_json() or {}
        self.send_json({"ok": True})
        cam_id = str(data.get("camera", ""))
        cam = camera(cam_id)
        if cam is not None and YANDEX is not None:  # датчик движения в «Доме с Алисой» — независимо от push
            YANDEX.motion(cam_id)
        if cam is not None and MQTT is not None and cam.get("enabled", True):  # MQTT: все включённые камеры, независимо от push
            MQTT.motion(cam_id)
        if cam is not None and zorcam_cloud.state() == "paired":  # лента движений облака: без снимка, только время
            viewers = sorted(u for u, v in zorcam_users.load().items() if can_see(u, v["role"], cam_id))
            try:
                _cloud_events.put_nowait((cam_id, cam["name"], str(data.get("name", "")), viewers))
            except queue.Full:
                log(f"cloud: motion event {cam_id} dropped (the cloud does not answer)")
        # каждое решение — в журнал: «не пришло уведомление» разбирается по одной строке
        skip = None
        if cam is None:
            skip = "unknown camera"
        elif not cam["notify_motion"]:  # тумблер «Уведомления о движении» камеры
            skip = "motion notifications are off for the camera"
        elif PUSH is None:
            skip = "push is not available"
        elif not PUSH.count():
            skip = "no push subscriptions"
        else:
            now = time.time()
            with _push_lock:  # не чаще раза в motion_cooldown для каждой камеры
                left = PUSH_COOLDOWN - (now - _last_push.get(cam_id, 0.0))
                if left > 0:
                    skip = f"motion_cooldown, {left:.0f} s left"
                else:
                    _last_push[cam_id] = now
        if skip:
            log(f"motion push {cam_id}: skipped ({skip})")
            return
        ts = parse_ts(str(data.get("name", "")))
        body = f"{cam['name']}, {ts.strftime('%H:%M:%S')}" if ts else cam["name"]
        threading.Thread(target=motion_push, args=(cam_id, body, ts.strftime("%Y%m%d_%H%M%S") if ts else None),
                         daemon=True).start()

    def config_save(self):
        form = self.admin_form()
        if form is None:
            return
        values, errors, raw = read_config_form(form)
        if errors:
            self.send_html(render_config_page(csrf_token(self.get_session_token()), self.lang, raw, errors), status=400)
            return
        if values == CFG.values:
            self.send_html(render_config_page(csrf_token(self.get_session_token()), self.lang,
                                              message=("ok", t(self.lang, "cfgpage.no_changes"))))
            return
        zorcam_config.save_config(values)
        log(f"config: saved by {self.user} from {self.client_desc()}")
        lang = self.lang
        self.send_html(settings_page(lang, t(lang, "cfgpage.saved_title"), "/settings", intro=replace_later("/settings", 8),
                                     sections=banner("ok", t(lang, "cfgpage.saved"))))
        restart_self("settings saved on /config")

    # ---- мастер первичной настройки

    def setup_general_save(self):
        """Шаг 1: общие настройки. Изменения — в zorcam.conf (галерея перезапустится), затем шаг 2."""
        if not setup_needed():
            self.redirect("/login")
            return
        if not self.setup_allowed():
            return
        form = self.setup_form()
        if form is None:
            return
        values, errors, raw = read_config_form(form)
        if errors:
            self.send_html(render_setup_general(self.lang, form["csrf"][0], raw, errors), status=400)
            return
        if values == CFG.values:
            self.redirect("/setup/users")
            return
        zorcam_config.save_config(values)
        log(f"setup: general settings saved from {self.client_desc()}")
        lang = self.lang
        self.send_html(settings_page(lang, t(lang, "setup.title"), intro=replace_later("/setup/users", 6),
                                     sections=banner("ok", t(lang, "setup.saved"))))
        restart_self("settings saved in the setup wizard")

    def setup_users_save(self):
        """Шаг 2: пароль admin; затем вход под admin."""
        if not setup_needed():
            self.redirect("/login")
            return
        if not self.setup_allowed():
            return
        form = self.setup_form()
        if form is None:
            return
        p1, p2 = form.get("password", [""])[0], form.get("repeat", [""])[0]
        err = password_error(p1, p2, required=True)
        if err:
            self.send_html(render_setup_users(self.lang, form["csrf"][0], err), status=400)
            return
        zorcam_users.set_user(zorcam_users.ADMIN, "admin", p1)
        log(f"setup: user {zorcam_users.ADMIN} (admin) created from {self.client_desc()}")
        self.redirect("/setup/cameras", session_cookie(create_session(zorcam_users.ADMIN), secure=self.https_front()))

    def after_password(self):
        """Куда ведёт «Смените пароль» после «Сохранить» и «Пропустить»: камер ещё нет — к их добавлению (шаг 3 мастера),
        иначе на главную."""
        return "/" if cameras() else "/setup/cameras"

    def password_save(self):
        """«Смените пароль» → «Сохранить»: свой пароль admin вместо выданного установщиком (метка initial снимается, как
        при любой установке пароля); остальные сессии admin завершаются, как при смене пароля в «Польз.»."""
        form = self.admin_form(8192)
        if form is None:
            return
        p1, p2 = form.get("password", [""])[0], form.get("repeat", [""])[0]
        err = password_error(p1, p2, required=True)
        if err:
            self.send_html(render_password_change(self.lang, csrf_token(self.get_session_token()), err), status=400)
            return
        zorcam_users.set_user(self.user, "admin", p1)
        end_sessions(self.user, keep=self.get_session_token())
        log(f"users: {self.user} replaced the password set by the installer, from {self.client_desc()}")
        self.redirect(self.after_password())

    def password_skip(self):
        """«Смените пароль» → «Пропустить»: пароль от установщика остаётся, метка initial снимается — больше не спрашивать."""
        form = self.admin_form(4096)
        if form is None:
            return
        zorcam_users.clear_initial(self.user)
        log(f"users: {self.user} kept the password set by the installer, from {self.client_desc()}")
        self.redirect(self.after_password())

    # ---- камеры

    def camera_save(self):
        """Добавить или изменить камеру: проверка полей, запрос к камере (RTSP DESCRIBE), cameras.json и пароль."""
        form = self.admin_form()
        if form is None:
            return
        lang, csrf = self.lang, form["csrf"][0]
        edit, setup = form.get("edit", [""])[0], form.get("setup", [""])[0] == "1"
        cams = [dict(c) for c in cameras()]
        old = next((c for c in cams if c["id"] == edit), None) if edit else None
        if edit and old is None:
            self.send_error(404)
            return
        # поля, которых нет в форме (в том числе неактивный список), — как были или по умолчанию у новой камеры
        raw = {k: form[k][0].strip() if k in form else zorcam_config.format_value(old[k] if old else CAMERA_DEFAULTS[k])
               for k in CAMERA_FORM_KEYS}
        for key in CAMERA_SWITCHES:  # выключенный тумблер форма не отправляет
            if key == "enabled" and key not in form:  # форма без него (старая страница, мастер): как было
                continue
            raw[key] = "yes" if "yes" in form.get(key, []) else "no"
        if old:
            raw["id"] = old["id"]  # ID — имя папок записей, не меняется
        token = form.get("onvif", [""])[0]
        cam, errors = zorcam_config.validate_camera(raw, [c["id"] for c in cams if c is not old])
        if errors:
            self.send_html(render_camera_form(lang, csrf, old, raw, errors, setup=setup, onvif_token=token), status=400)
            return
        password = form.get("password", [""])[0] or (onvif_password(token) if token and not old else "")
        if old and not password:
            password = zorcam_config.camera_password(old["id"])  # пусто — не менять
            # сохранённый пароль уйдёт на адреса камеры (проверка, go2rtc, PTZ): на новый хост — только введённый заново
            if password and cam["rtsp_user"] and not camera_address_hosts(cam) <= camera_address_hosts(old):
                log(f"cameras: {old['id']} address changed without a new password, refused for {self.user} "
                    f"from {self.client_desc()}")
                errors = {"password": Msg("cam.password_again")}
                self.send_html(render_camera_form(lang, csrf, old, raw, errors, setup=setup, onvif_token=token), status=400)
                return
        if not cam["rtsp_user"]:
            password = ""
        # камера отвечает до сохранения; пароль остаётся в этом процессе (без командной строки)
        # выключенную камеру не проверяем: её часто выключают как раз потому, что она недоступна
        skip_probe = form.get("force") == ["1"] or not cam["enabled"]
        problem = None if skip_probe else zorcam_config.probe_camera(zorcam_config.rtsp_url(cam, password), 5)
        if not problem and cam["rtsp_sub_url"] and not skip_probe:
            sub_problem = zorcam_config.probe_camera(zorcam_config.rtsp_url(cam, password, sub=True), 5)
            problem = t(lang, "cam.probe_sub", error=sub_problem) if sub_problem else None
        if problem:
            self.send_html(render_camera_form(lang, csrf, old, raw, problem=problem, setup=setup, onvif_token=token),
                           status=400)
            return
        ONVIF_PASSWORDS.pop(token, None)
        if old:
            cams[cams.index(old)] = cam
        else:
            cams.append(cam)
        zorcam_config.save_cameras(cams)
        zorcam_config.set_camera_password(cam["id"], password)
        ptz_forget(cam["id"])
        log(f"cameras: {cam['id']} {'changed' if old else 'added'} by {self.user} from {self.client_desc()}")
        self.redirect("/setup/cameras" if setup else settings_url("cameras") + ("" if old else "&added=" + quote(cam["id"])))

    def onvif_camera(self):
        """Камера из поиска ONVIF: по её логину и паролю — адреса потоков; дальше обычная форма новой камеры."""
        form = self.admin_form(8192)
        if form is None:
            return
        lang, csrf = self.lang, form["csrf"][0]
        get = lambda key: form.get(key, [""])[0].strip()  # noqa: E731
        xaddr, user, name, setup = get("xaddr"), get("rtsp_user"), get("name"), get("setup") == "1"
        password = form.get("password", [""])[0]
        host = urlparse(xaddr).hostname or ""
        if urlparse(xaddr).scheme not in ("http", "https") or not onvif.local_host(host):
            self.send_error(400)
            return
        values = {k: zorcam_config.format_value(d) for k, d, *_ in zorcam_config.CAMERA_FIELDS}
        values.update(id=suggest_camera_id(host), rtsp_user=user, name=name[:40])
        try:
            found = onvif.camera_streams(xaddr, user, password)
        except onvif.OnvifError as e:
            log(f"onvif: {host}: {e}")
            values["rtsp_url"] = f"rtsp://{host}:554/"
            self.send_html(render_camera_form(lang, csrf, form=values, setup=setup,
                                              notices=[("bad", t(lang, "onvif.failed", error=t(lang, "onvif.err." + e.key, detail=e.detail)))]))
            return
        model = " ".join(x for x in (found["manufacturer"], found["model"]) if x)
        values.update(name=(name or model or host)[:40], rtsp_url=found["main"], rtsp_sub_url=found["sub"])
        values["onvif_url"] = xaddr  # найденный адрес ONVIF — для PTZ, вводить его вручную не нужно
        size = lambda s: f"{s[0]}×{s[1]}" if s and s[0] else "?"  # noqa: E731
        text = (t(lang, "onvif.found", main=size(found["main_size"]), sub=size(found["sub_size"])) if found["sub"]
                else t(lang, "onvif.found_one", main=size(found["main_size"])))
        log(f"onvif: {host} ({model or 'unknown model'}): {len(found['sub']) and 2 or 1} stream(s), "
            f"sign-in {found['auth']}, by {self.user}")
        self.send_html(render_camera_form(lang, csrf, form=values, setup=setup, notices=[("ok", text)],
                                          onvif_token=onvif_keep_password(password) if password else ""))

    def camera_enable(self):
        """Тумблер «Камера включена» в списке камер: только это поле, камера не проверяется (выключенную не
        спрашивают, включённую детектор подключит сам). Детектор и go2rtc перезапускаются от изменения cameras.json."""
        form = self.admin_form(4096)
        if form is None:
            return
        cam_id = form.get("id", [""])[0]
        cams = cameras()
        cam = next((c for c in cams if c["id"] == cam_id), None)
        if cam is None:
            self.send_error(404)
            return
        on = form.get("enabled", ["no"])[-1] == "yes"  # скрытое "no", поверх него — "yes" включённого тумблера
        if cam.get("enabled", True) != on:
            cam["enabled"] = on
            zorcam_config.save_cameras(cams)
            log(f"cameras: {cam_id} switched {'on' if on else 'off'} by {self.user} from {self.client_desc()}")
        self.redirect(settings_url("cameras"))

    def camera_zones(self):
        """Сохранить зоны движения камеры в motion_zones.json (не в cameras.json: от него детекторы и go2rtc
        перезапускаются, а процесс камеры сам подхватывает этот файл). Нет закрашенных клеток — зон нет, запись удаляется."""
        form = self.admin_form(4096)
        if form is None:
            return
        cam_id, mode, cells = (form.get(k, [""])[0] for k in ("id", "mode", "cells"))
        if camera(cam_id) is None:
            self.send_error(404)
            return
        if not zorcam_config.valid_zone(mode, cells):
            self.send_error(400)
            return
        zorcam_config.save_zone(cam_id, mode, cells)
        log(f"cameras: {cam_id} motion zones " + (f"set ({mode}, {cells.count('1')} cells)" if "1" in cells else "cleared")
            + f" by {self.user}")
        self.redirect(f"/camera/{quote(cam_id, safe='')}")

    def camera_delete(self):
        """Удалить камеру; recordings=1 — вместе с её записями."""
        form = self.admin_form(4096)
        if form is None:
            return
        cam_id = form.get("id", [""])[0]
        cams = cameras()
        cam = camera(cam_id)
        if cam is None:
            self.send_error(404)
            return
        zorcam_config.save_cameras([c for c in cams if c["id"] != cam_id])
        ptz_forget(cam_id)
        zorcam_config.set_camera_password(cam_id, "")
        with_recordings = form.get("recordings") == ["1"]
        if with_recordings:
            for d in cam_dirs(cam_id):
                shutil.rmtree(os.path.join(BASE_DIR, d), ignore_errors=True)
        zorcam_users.forget_camera(cam_id)
        zorcam_config.save_zone(cam_id, "exclude", None)
        log(f"cameras: {cam_id} deleted by {self.user}" + (" with its recordings" if with_recordings else ""))
        self.redirect(settings_url("cameras"))

    # ---- облако Zorcam

    def storage_request(self):
        """Заявка root-сервису zorcam-nvr-storage (path-юнит запускает его сразу): отформатировать и использовать
        USB-диск, подключить его как есть (ext4) или отключить. Диск сервис проверит по lsblk заново."""
        form = self.admin_form(4096)
        if form is None:
            return
        action, disk = form.get("action", [""])[0], form.get("disk", [""])[0]
        if action not in STORAGE_ACTIONS or (action != "forget" and disk not in {d["path"] for d in usb_disks()}):
            self.send_error(400)
            return
        os.makedirs(zorcam_storage.STORAGE_DIR, exist_ok=True)
        zorcam_storage.write_json(zorcam_storage.REQUEST_FILE, {"action": action, "disk": disk, "user": self.user,
                                                                 "time": time.time()}, 0o600)
        log(f"storage: {action} {disk} asked by {self.user} from {self.client_desc()}")
        self.redirect(settings_url("general") + "#storage")

    def cloud_connect(self):
        form = self.admin_form(4096)
        if form is None:
            return
        server = form.get("server", [""])[0]
        ok, why = zorcam_cloud.start_pairing(server, socket.gethostname() or "Zorcam", VERSION)
        log(f"cloud: pairing with {server} started by {self.user}" if ok else f"cloud: cannot start pairing: {why[0]} {why[1:] or ''}")
        if not ok:
            lang = self.lang
            csrf = csrf_token(self.get_session_token())
            self.send_html(render_settings(self.role, lang, self.user, csrf, chosen_lang=self.chosen_lang,
                                           general=banner("bad", t(lang, "cloud.failed", why=Msg(why[0], **(why[1] if len(why) > 1 else {}))))),
                           status=502)
            return
        self.redirect("/settings")

    def cloud_disconnect(self):
        form = self.admin_form(4096)
        if form is None:
            return
        zorcam_cloud.unlink()
        log(f"cloud: disconnected by {self.user}")
        self.redirect("/settings")

    # ---- «Умный дом Яндекса»

    def yandex_open(self):
        """Адреса для Яндекса отвечают, только пока подключено облако, сохранены ключи навыка и запрос пришёл через
        его туннель (порт TUNNEL_PORT; заголовка X-Zorcam-Identity у запросов Яндекса нет): свой домен и
        домашняя сеть для навыка больше не работают, а устройство без навыка не даёт из интернета проверять пароль
        admin. Иначе ответ как на неизвестный путь."""
        if YANDEX is not None and self.on_tunnel and yandex_cloud_url() and YANDEX.configured():
            return True
        self.send_error(404)
        return False

    def do_HEAD(self):
        """Яндекс проверяет адрес навыка запросом HEAD /yandex/v1.0; iOS при добавлении на экран «Домой»
        может проверять значок запросом HEAD — без ответа 200 рисует вместо него букву."""
        path = urlparse(self.path).path
        if path in STATIC_FILES:
            try:
                size = os.path.getsize(ICON_FILES[path])
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
        elif path.rstrip("/") != "/yandex/v1.0":
            self.send_error(404)
        elif self.yandex_open():
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def yandex_get(self, path, query):
        if not self.yandex_open():
            return
        if path.rstrip("/") == "/yandex/v1.0":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/yandex/v1.0/user/devices":
            self.yandex_api("devices")
        elif path == "/yandex/auth":
            params = {k: v[0] for k, v in parse_qs(query).items()}
            if params.get("client_id") != yandex.CLIENT_ID or not YANDEX.valid_redirect(params.get("redirect_uri")):
                self.send_error(400)
                return
            self.send_html(render_yandex_auth(self.lang, params))
        else:
            self.send_error(404)

    def yandex_failed(self):
        if yandex_register_failed():
            log(f"yandex: {YANDEX_MAX_FAILURES} failed sign-ins in {YANDEX_FAILURE_WINDOW // 60} min, "
                f"the skill addresses answer 429 until the window passes (last from {self.client_desc()})")

    def yandex_auth(self):
        """Связка аккаунтов: вход admin (логин и пароль Zorcam) — и обратно в Яндекс с кодом авторизации."""
        if not self.yandex_open():
            return
        form = {k: v[0] for k, v in (self.read_form(8192) or {}).items()}
        if form.get("client_id") != yandex.CLIENT_ID or not YANDEX.valid_redirect(form.get("redirect_uri")):
            self.send_error(400)
            return
        lang, ip = self.lang, self.client_ip()
        if is_locked_out(ip) or yandex_locked_out():
            self.send_html(render_yandex_auth(lang, form, t(lang, "login.locked")), status=429)
            return
        name, role = check_login(form.get("username", ""), form.get("password", ""))
        if role != "admin":
            register_failed(ip)
            self.yandex_failed()
            self.send_html(render_yandex_auth(lang, form, t(lang, "ya.auth_wrong")), status=401)
            return
        code = YANDEX.issue_code(form["redirect_uri"], name)
        log(f"yandex: account linking allowed by {name} from {self.client_desc()}")
        uri = form["redirect_uri"]
        self.redirect(uri + ("&" if "?" in uri else "?") + urlencode({"code": code, "state": form.get("state", "")}))

    def yandex_token(self):
        if not self.yandex_open():
            return
        ip = self.client_ip()  # через туннель — реальный клиент, не 127.0.0.1
        if is_locked_out(ip) or yandex_locked_out():  # подбор секрета приложения — как подбор пароля
            self.send_json({"error": "slow_down"}, 429)
            return
        status, body = YANDEX.token(self.read_form(8192) or {}, self.headers.get("Authorization", ""))
        if status == 401:
            register_failed(ip)
            self.yandex_failed()
        if status != 200:
            log(f"yandex: token request refused ({body['error']}) from {self.client_desc()}")
        self.send_json(body, status)

    def yandex_api(self, action):
        """API устройств навыка: только с токеном, который Zorcam выдал Яндексу."""
        if not self.yandex_open():
            return
        if not YANDEX.check_access(self.headers.get("Authorization", "")):
            self.send_error(401)
            return
        reply = {"request_id": self.headers.get("X-Request-Id", "")}
        if DEBUG:
            log(f"yandex: {action} (request {reply['request_id']})")
        cams = cameras()
        if action == "devices":
            reply["payload"] = YANDEX.devices(cams, VERSION)
        elif action == "query":
            ids = [str(d.get("id", "")) for d in (self.read_json(65536) or {}).get("devices", []) if isinstance(d, dict)]
            reply["payload"] = YANDEX.query(cams, ids)
        elif action == "action":  # у датчика нечего включать
            devices = (self.read_json(65536) or {}).get("payload", {}).get("devices", [])
            reply["payload"] = {"devices": [{"id": str(d.get("id", "")), "action_result": {
                "status": "ERROR", "error_code": "INVALID_ACTION"}} for d in devices if isinstance(d, dict)]}
        else:
            YANDEX.unlink()
            log("yandex: unlinked in Yandex")
        self.send_json(reply)

    def yandex_save(self):
        form = self.admin_form(8192)
        if form is None:
            return
        if YANDEX is None:
            self.send_error(503)
            return
        if not yandex_cloud_url():  # без облака ключи не принимаются: страница объяснит, что нужно
            self.redirect("/yandex")
            return
        field = {k: v[0] for k, v in form.items()}
        YANDEX.save_settings(field.get("skill_id", ""), field.get("client_secret", ""), field.get("skill_token", ""))
        log(f"yandex: settings saved by {self.user} from {self.client_desc()}")
        self.redirect("/yandex?saved=1")

    def mqtt_save(self):
        form = self.admin_form(8192)
        if form is None:
            return
        if MQTT is None:
            self.send_error(503)
            return
        fields = {k: v[0] for k, v in form.items()}
        with MQTT.lock:
            current = dict(MQTT.cfg)
        cfg, errors = mqtt.validate(fields, current)
        if errors:  # введённое остаётся в форме (кроме пароля)
            values = {k: fields.get(k, "") for k in ("host", "port", "username", "prefix", "device")}
            values.update(enabled="enabled" in fields, tls="tls" in fields, discovery="discovery" in fields)
            self.send_html(render_mqtt_page(self.lang, csrf_token(self.get_session_token()), values=values, errors=errors),
                           status=400)
            return
        MQTT.save(cfg)
        log(f"mqtt: settings saved by {self.user} from {self.client_desc()} "
            f"({'on, ' + cfg['host'] + ':' + str(cfg['port']) if cfg['enabled'] else 'off'})")
        self.redirect("/mqtt?saved=1")

    def yandex_admin_unlink(self):
        form = self.admin_form(4096)
        if form is None:
            return
        if YANDEX is not None:
            YANDEX.unlink()
            log(f"yandex: unlinked by {self.user} from {self.client_desc()}")
        self.redirect("/yandex")

    def foreign_post(self):
        """Запрос, который браузер послал со страницы другого источника. Через облако устройства живут на
        <id>.devices.<домен облака> — для браузера один «сайт», и cookie облака уходят и со страницы чужого устройства.
        Sec-Fetch-Site (все современные браузеры) — только same-origin и none; иначе Origin должен быть адресом
        самого устройства; нет ни того, ни другого (curl, старые клиенты) — пропускаем: формы защищены токенами."""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site:
            return site not in ("same-origin", "none")
        origin = self.headers.get("Origin")
        if origin is None:
            return False
        host = urlparse(origin).netloc.lower()
        ours = {(self.headers.get("Host") or "").strip().lower(),
                (self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip().lower()} - {""}
        return host not in ours

    def do_POST(self):
        path = urlparse(self.path).path
        # машинные адреса (детектор, Яндекс) браузер не вызывает; остальное — только со страниц самого устройства
        if (path != "/internal/motion" and path != "/yandex/token" and not path.startswith("/yandex/v1.0/")
                and self.foreign_post()):
            log(f"post: {path} refused, cross-site request from {self.client_desc()} "
                f"(Sec-Fetch-Site {self.headers.get('Sec-Fetch-Site')}, Origin {self.headers.get('Origin')})")
            self.send_error(403)
            return
        routes = {
            "/config": self.config_save,
            "/internal/motion": self.motion_event,
            "/push/subscribe": lambda: self.push_subscribe(True),
            "/push/unsubscribe": lambda: self.push_subscribe(False),
            "/storage/delete": self.storage_delete,
            "/users/save": self.users_save,
            "/users/delete": self.users_delete,
            "/users/sessions": self.users_sessions,
            "/sessions/others": self.sessions_others,
            "/live/webrtc": self.proxy_webrtc,
            "/live/webrtc-log": self.webrtc_log,
            "/setup": self.setup_general_save,
            "/setup/users": self.setup_users_save,
            PASSWORD_URL: self.password_save,
            PASSWORD_URL + "/skip": self.password_skip,
            "/camera/save": self.camera_save,
            "/onvif": self.onvif_camera,
            "/camera/delete": self.camera_delete,
            "/camera/zones": self.camera_zones,
            "/camera/enable": self.camera_enable,
            "/cloud/connect": self.cloud_connect,
            "/settings/storage": self.storage_request,
            "/settings/notify": self.notify_set,
            "/cloud/disconnect": self.cloud_disconnect,
            "/yandex/save": self.yandex_save,
            "/mqtt/save": self.mqtt_save,
            "/yandex/unlink": self.yandex_admin_unlink,
            "/yandex/auth": self.yandex_auth,
            "/yandex/token": self.yandex_token,
            "/yandex/v1.0/user/devices/query": lambda: self.yandex_api("query"),
            "/yandex/v1.0/user/devices/action": lambda: self.yandex_api("action"),
            "/yandex/v1.0/user/unlink": lambda: self.yandex_api("unlink"),
        }
        if path in routes:
            routes[path]()
            return
        if re.fullmatch(r"/ptz/[^/]+", path):
            self.ptz_command()
            return
        if path != "/login":
            self.send_error(404)
            return
        form = self.read_form(8192) or {}
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        client_ip = self.client_ip()

        if is_locked_out(client_ip) or user_locked_out(username):
            self.send_html(login_page(self.lang, "login.locked"), status=429)
            return

        name, role = check_login(username, password)
        if role:
            clear_user_failed(username)
            log(f"login: {name} ({role}) from {self.client_desc()}")
            token = create_session(name)
            self.send_response(302)
            self.send_header("Location", "/?tab=live")
            self.send_header("Set-Cookie", session_cookie(token, secure=self.https_front()))
            self.end_headers()
            return

        register_failed(client_ip)
        pause = register_user_failed(username)
        if pause:
            # набранное имя — в журнал, только если такой пользователь есть: в поле имени могли набрать пароль
            who = (json.dumps(zorcam_users.find(username.strip()), ensure_ascii=False)
                   if zorcam_users.find(username.strip()) else "an unknown name")
            log(f"login: sign-in as {who} is closed for {pause} s after repeated failures "
                f"(last from {self.client_desc()})")
        self.send_html(login_page(self.lang, "login.wrong"), status=401)


class Server(ThreadingHTTPServer):
    request_queue_size = 64  # сетка превью открывает подключения пачкой; по умолчанию очередь — 5
    max_connections = MAX_CONNECTIONS

    def __init__(self, address, handler, tunnel=False):
        self.tunnel = tunnel  # туннельный порт: см. Handler.on_tunnel
        self.slots = threading.BoundedSemaphore(self.max_connections)
        super().__init__(address, handler)

    def exempt(self, client_address):
        """Сам Pi на порту 80 (проверка /healthz для watchdog systemd, детектор) мимо лимита: иначе соединения из
        сети, занявшие все места, остановили бы WATCHDOG=1 и systemd перезапустил бы галерею. Туннельный порт
        целиком приходит с 127.0.0.1 (frpc), там лимит действует для всех."""
        if self.tunnel:
            return False
        try:
            ip = ipaddress.ip_address(client_address[0])
        except ValueError:
            return False
        return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback

    def process_request(self, request, client_address):
        """Поток на соединение, но не больше max_connections одновременно: остальные закрываются сразу, а не копят
        потоки (память Pi 3), пока их кто-то держит открытыми."""
        if self.exempt(client_address):
            return super().process_request(request, client_address)
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            if not self.exempt(client_address):
                self.slots.release()

    def handle_error(self, request, client_address):
        """Телефон оборвал загрузку (перемотка или закрытие ролика) — обычное дело, не ошибка: без трассировки
        в журнале. Остальные ошибки — как раньше, с трассировкой."""
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


if __name__ == "__main__":
    for e in CFG.errors:
        log(f"config: {e}")
    for name in zorcam_users.migrate_camera_access([c["id"] for c in cameras()]):
        log(f"users: {name} keeps the cameras they saw; a new camera is now closed until switched on for them")
    threading.Thread(target=prune_thumbs, daemon=True).start()
    threading.Thread(target=HEALTH.loop, daemon=True).start()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=config_watch_loop, daemon=True).start()
    if YANDEX is not None:
        threading.Thread(target=YANDEX.loop, daemon=True).start()
    if MQTT is not None:
        threading.Thread(target=MQTT.loop, daemon=True).start()
    threading.Thread(target=cloud_event_loop, daemon=True).start()
    threading.Thread(target=stun.loop, args=(log,), daemon=True).start()
    # туннель облака — отдельный порт только на localhost; заголовкам облака и адресам Яндекса галерея верит только там
    try:
        tunnel_server = Server(("127.0.0.1", TUNNEL_PORT), Handler, tunnel=True)
        threading.Thread(target=tunnel_server.serve_forever, daemon=True).start()
    except OSError as e:  # порт занят: без облака можно жить, без галереи дома — нет
        log(f"tunnel port {TUNNEL_PORT} is not available, the cloud cannot reach the gallery: {e}")
    Server(("0.0.0.0", PORT), Handler).serve_forever()
