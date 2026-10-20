"""Zorcam облако (по желанию владельца): привязка Pi к аккаунту, туннель frpc, вход через облако, события.

Без привязки ничего не делает: галерея и запись работают автономно. Состояние — /var/lib/zorcam-nvr/config/cloud.json
(600): адрес облака, id устройства, закрытый ключ Pi (Ed25519), после привязки — токен туннеля и открытый ключ
облака. Пишут его галерея (подключить, отвязать) и сервис zorcam-nvr-cloud (завершить привязку) — под блокировкой.

Протокол (zorcam-cloud, docs/mvp.md): запросы Pi подписаны её ключом (заголовки X-Zorcam-Device, X-Zorcam-Time,
X-Zorcam-Signature над «МЕТОД\\nпуть\\nвремя\\nsha256 тела»); облако подтверждает вход заголовком X-Zorcam-Identity —
токен v1.<данные>.<подпись> ключом облака, на 60 с, только для этого устройства.
"""
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import zorcam_config

CLOUD_FILE = os.path.join(zorcam_config.CONFIG_DIR, "cloud.json")
LOCK_FILE = CLOUD_FILE + ".lock"
FRPC_CONFIG = "/dev/shm/zorcam_frpc.toml"
# Туннель облака приходит на отдельный порт галереи (только 127.0.0.1): заголовкам облака (X-Zorcam-Identity и др.)
# и адресам Яндекса галерея верит только на нём, а не «всем с 127.0.0.1» — иначе обратный прокси на самом Pi
# превратил бы запросы из интернета в «свои»
TUNNEL_PORT = 8081
HEALTH_FILE = "/dev/shm/zorcam_health.json"  # проверки галереи («Состояние Zorcam»), пишет галерея
CGROUPS = "/sys/fs/cgroup/system.slice"
SERVICES = ("capture", "gallery", "go2rtc", "cloud")
URL_RE = re.compile(r"https://[a-z0-9.-]+(:\d+)?")
ADDRESS_FIELDS = {"url", "app_url", "frps_addr", "frps_port", "subdomain", "stun"}  # приходят из облака со статусом
TUNNEL_NAME_RE = re.compile(r"[a-z0-9.-]+")  # frps_addr и subdomain: попадают в TOML frpc
# STUN облака (host:port, порт 1-65535)
STUN_RE = re.compile(r"[a-z0-9.-]+:(?:[1-9]\d{0,3}|[1-5]\d{4}|6[0-4]\d{3}|65[0-4]\d{2}|655[0-2]\d|6553[0-5])")

try:  # python3-cryptography; без него облако недоступно, остальное работает
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
except ImportError:  # pragma: no cover
    Ed25519PrivateKey = None


def b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@contextmanager
def locked():
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    with open(LOCK_FILE, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load():
    try:
        with open(CLOUD_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    tmp = CLOUD_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, CLOUD_FILE)


def remove():
    for path in (CLOUD_FILE, FRPC_CONFIG):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def state():
    """none — не подключено; pending — ждём, пока владелец введёт код; paired — подключено."""
    data = load()
    if data.get("paired"):
        return "paired"
    if data.get("device_id"):
        return "pending" if data.get("code_exp", 0) > time.time() else "expired"
    return "none"


def private_key(data):
    return Ed25519PrivateKey.from_private_bytes(b64d(data["private_key"]))


def sign_headers(data, method, path, body=b""):
    ts = str(int(time.time()))
    msg = f"{method}\n{path}\n{ts}\n{hashlib.sha256(body).hexdigest()}".encode()
    return {"X-Zorcam-Device": data["device_id"], "X-Zorcam-Time": ts,
            "X-Zorcam-Signature": b64e(private_key(data).sign(msg))}


def call(data, method, path, body=None, signed=True, version=""):
    """Запрос к облаку: (статус, JSON или {}); 0 — облако недоступно."""
    raw = json.dumps(body).encode() if body is not None else b""
    headers = {"Content-Type": "application/json", "User-Agent": "zorcam"}
    if version:
        headers["X-Zorcam-Version"] = version
    if signed:
        headers.update(sign_headers(data, method, path, raw))
    req = urllib.request.Request(data["server"] + path, data=raw if method == "POST" else None, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return 0, {}


def start_pairing(server, name, version):
    """Галерея: «Подключить». Новый ключ Pi, код для владельца. (True, None) или (False, (ключ текста, поля))."""
    if Ed25519PrivateKey is None:
        return False, ("cloud.why.crypto",)
    server = server.strip().rstrip("/")
    if not URL_RE.fullmatch(server):
        return False, ("cloud.why.address",)
    key = Ed25519PrivateKey.generate()
    data = {"server": server,
            "private_key": b64e(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                  serialization.NoEncryption()))}
    public = b64e(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    status, reply = call(data, "POST", "/api/pair/start", {"public_key": public, "name": name, "version": version,
                                                          "edition": zorcam_config.edition()},
                         signed=False)
    if status != 200 or not reply.get("device_id"):
        return False, ("cloud.why.http", {"status": status}) if status else ("cloud.why.offline",)
    data.update(device_id=reply["device_id"], code=reply["code"], pair_url=reply.get("pair_url", ""),
                code_exp=time.time() + int(reply.get("expires_in", 600)))
    with locked():
        save(data)
    return True, None


def finish_pairing(version=""):
    """Сервис zorcam-nvr-cloud: владелец ввёл код? Тогда — токен туннеля и ключ облака. Возвращает state()."""
    with locked():
        data = load()
        if not data.get("device_id") or data.get("paired"):
            return state()
        status, reply = call(data, "GET", "/api/device/status", version=version)
        if status == 200 and reply.get("paired"):
            status, tok = call(data, "POST", "/api/device/token", {})
            if status == 200 and tok.get("token"):
                for k in ("url", "app_url", "frps_addr", "frps_port", "subdomain", "stun", "cloud_key", "name", "owner_name"):
                    data[k] = reply.get(k)
                data.update(paired=True, token=tok["token"])
                for k in ("code", "code_exp", "pair_url"):
                    data.pop(k, None)
                save(data)
    return state()


def heartbeat(version=""):
    """Раз в минуту: облако знает, что Pi на связи. False — облако устройство забыло (отвязали в приложении)."""
    data = load()
    if not data.get("paired"):
        return True
    status, reply = call(data, "GET", "/api/device/status", version=version)
    if status == 401 or (status == 200 and not reply.get("paired")):
        return False
    if status != 200:
        return True
    # имя устройства и отображаемое имя владельца аккаунта меняются в облаке: подтягиваем (старое облако owner_name не шлёт)
    changed = {k: reply[k] for k in ("name", "owner_name")
               if isinstance(reply.get(k), str) and reply[k] and reply[k] != data.get(k)}
    # адрес устройства в облаке тоже может смениться (облако переехало на другой домен); негодное и пропавшее не берём
    changed.update({k: reply[k] for k in ("url", "app_url")
                    if isinstance(reply.get(k), str) and URL_RE.fullmatch(reply[k]) and reply[k] != data.get(k)})
    for k in ("frps_addr", "subdomain"):
        if isinstance(reply.get(k), str) and TUNNEL_NAME_RE.fullmatch(reply[k]) and reply[k] != data.get(k):
            changed[k] = reply[k]
    if isinstance(reply.get("stun"), str) and STUN_RE.fullmatch(reply["stun"]) and reply["stun"] != data.get("stun"):
        changed["stun"] = reply["stun"]  # старое облако поля не шлёт: ничего не меняется
    port = reply.get("frps_port")
    if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536 and port != data.get("frps_port"):
        changed["frps_port"] = port
    if changed:
        with locked():
            fresh = load()
            fresh.update(changed)
            save(fresh)
        moved = sorted(changed.keys() & ADDRESS_FIELDS)
        if moved:  # в журнал сервиса zorcam-nvr-cloud (stdout); значения без токена туннеля, только адреса
            print(f"cloud: the address from the cloud changed: {', '.join(f'{k}={changed[k]}' for k in moved)}", flush=True)
    return True


def unlink():
    """Галерея: «Отвязать». Облако забывает Pi (если доступно), файл удаляется в любом случае."""
    with locked():
        data = load()
        if data.get("device_id"):
            call(data, "POST", "/api/device/unlink", {})
        remove()


def send_event(camera, camera_name, name, viewers, ts=None):
    data = load()
    if not data.get("paired"):
        return None
    status, _ = call(data, "POST", "/api/device/events", {"camera": camera, "camera_name": camera_name,
                                                          "name": name, "ts": ts or time.time(), "viewers": viewers})
    return status


def verify_identity(token, data=None):
    """Имя локального пользователя из X-Zorcam-Identity, если токен подписан облаком, для этого Pi и не истёк."""
    data = data if data is not None else load()
    if not token or not data.get("paired") or not data.get("cloud_key") or Ed25519PrivateKey is None:
        return None
    try:
        version, payload, sig = token.split(".")
        if version != "v1":
            return None
        Ed25519PublicKey.from_public_bytes(b64d(data["cloud_key"])).verify(b64d(sig), f"v1.{payload}".encode())
        claims = json.loads(b64d(payload))
    except (ValueError, InvalidSignature):
        return None
    if claims.get("aud") != data.get("device_id") or not claims.get("exp", 0) > time.time():
        return None
    user = claims.get("user")
    return user if isinstance(user, str) and user else None


def frpc_config(data):
    """Настройки frpc: туннель по wss через 443 (как обычный HTTPS)."""
    q = json.dumps  # строки TOML в кавычках, как JSON
    return "\n".join([
        f"serverAddr = {q(data['frps_addr'])}",
        f"serverPort = {int(data.get('frps_port') or 443)}",
        "loginFailExit = false",
        # wss через 443: TLS облака (Let's Encrypt), frpc проверяет его по системным сертификатам Pi;
        # второй слой TLS frps со своим самоподписанным сертификатом не нужен
        'transport.protocol = "wss"',
        "transport.tls.enable = false",
        f"metadatas.device = {q(data['device_id'])}",
        f"metadatas.token = {q(data['token'])}",
        'log.to = "console"',
        'log.level = "warn"',
        "",
        "[[proxies]]",
        f"name = {q(data['device_id'] + '-web')}",
        'type = "http"',
        'localIP = "127.0.0.1"',
        f"localPort = {TUNNEL_PORT}",
        f"subdomain = {q(data['subdomain'])}",
        "",
    ])


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def health_report(version="", now=None, cgroups=CGROUPS, health_file=HEALTH_FILE):
    """Самочувствие Pi для админки облака: система, сервисы, камеры, проверки галереи, сеть (через что подключена, включены
    ли Wi-Fi и Bluetooth). Без названий камер, имени сети, адресов, названий интерфейсов, паролей и записей — только числа,
    состояния и тексты проблем."""
    now = now or time.time()
    report = {"version": version, "edition": zorcam_config.edition(), "ts": now}
    uptime, loadavg = _read("/proc/uptime"), _read("/proc/loadavg")
    if uptime:
        report["uptime"] = int(float(uptime.split()[0]))
    if loadavg:
        report["load"] = [float(x) for x in loadavg.split()[:3]]
    meminfo = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        key, _, value = line.partition(":")
        if value.split():
            meminfo[key] = int(value.split()[0]) // 1024
    if meminfo:
        report["mem"] = {"total_mb": meminfo.get("MemTotal"), "available_mb": meminfo.get("MemAvailable")}
    try:
        du = shutil.disk_usage(zorcam_config.STATE_DIR)
        report["disk"] = {"total_gb": round(du.total / 2**30, 1), "free_gb": round(du.free / 2**30, 1)}
    except OSError:
        pass
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    if temp and temp.strip().isdigit():
        report["temp"] = round(int(temp) / 1000, 1)
    report["network"] = zorcam_config.network_state()

    try:
        states = subprocess.run(["systemctl", "is-active", *(f"zorcam-nvr-{s}" for s in SERVICES)], capture_output=True,
                                text=True, timeout=10, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}).stdout.split()
    except (OSError, subprocess.SubprocessError):
        states = []
    if len(states) == len(SERVICES):
        report["services"] = dict(zip(SERVICES, states, strict=True))
    events = {}
    for svc in SERVICES:
        text = _read(os.path.join(cgroups, f"zorcam-nvr-{svc}.service", "memory.events"))
        if text:
            values = dict(line.split()[:2] for line in text.splitlines() if len(line.split()) >= 2)
            events[svc] = {k: int(values[k]) for k in ("high", "max", "oom_kill") if values.get(k, "").isdigit()}
    if events:
        report["memory_events"] = events

    cams = []
    for cam in zorcam_config.load_cameras():
        if not cam.get("enabled", True):
            continue
        try:
            with open(zorcam_config.capture_status_file(cam["id"])) as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        last = st.get("last_frame")
        cams.append({"id": cam["id"], "frame_age": round(now - last) if last else None,
                     "reconnects": len([t for t in st.get("reconnects") or [] if t > now - 86400])})
    report["cameras"] = cams
    try:
        with open(health_file) as f:
            gallery = json.load(f)
        report["checks"] = gallery.get("checks") or {}
        report["checked"] = gallery.get("checked")
    except (OSError, ValueError, AttributeError):
        pass
    return report


def send_health(report):
    """Раз в час — в админку облака. Статус ответа или None, если Pi не привязана."""
    data = load()
    if not data.get("paired"):
        return None
    status, _ = call(data, "POST", "/api/device/health", report)
    return status
