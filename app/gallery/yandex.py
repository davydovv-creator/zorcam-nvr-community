"""«Умный дом Яндекса» через облако Zorcam: каждая камера Zorcam — датчик движения в «Доме с Алисой»,
на его срабатывание владелец вешает сценарии (колонка говорит, включается свет).

Zorcam здесь — «провайдер» приватного навыка умного дома (dialogs.yandex.ru): сервер OAuth для связки аккаунтов
(/yandex/auth, /yandex/token), API устройств (/yandex/v1.0/...) и уведомления о движении в Яндекс
(POST dialogs.yandex.net/api/v1/skills/<skill_id>/callback/state). Видео в Яндекс не уходит.

Работает только при подключённом облаке: запросы Яндекса приходят на адрес Pi в облаке (https://<id>.<домен облака>)
через его туннель; без облака адреса навыка не отвечают, а уведомления не отправляются.

Настройки и выданные Яндексу токены — /var/lib/zorcam-nvr/config/yandex.json (600); токены хранятся только хешами.
"""
import base64
import hashlib
import json
import os
import queue
import secrets
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import zorcam_config

STATE_FILE = os.path.join(zorcam_config.CONFIG_DIR, "yandex.json")
API = os.environ.get("ZORCAM_YANDEX_API", "https://dialogs.yandex.net")
CLIENT_ID = "zorcam"  # «Идентификатор приложения» в связке аккаунтов навыка
# куда Яндекс просит вернуть код авторизации: только его адреса — иначе код (и доступ) можно было бы увести
REDIRECT_HOSTS = ("social.yandex.net",)
CODE_TTL = 600
ACCESS_TTL = 30 * 86400  # Яндекс сам обновляет токен по refresh_token
HOLD = 60  # «движение» держится, пока детектор не молчит HOLD секунд; потом — «нет движения»
EVENTS = ("detected", "not_detected")


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def motion_property(value=None):
    prop = {"type": "devices.properties.event"}
    if value is None:
        prop.update(retrievable=True, reportable=True,
                    parameters={"instance": "motion", "events": [{"value": v} for v in EVENTS]})
    else:
        prop["state"] = {"instance": "motion", "value": value}
    return prop


class Yandex:
    def __init__(self, path=STATE_FILE, api=API, log=print, enabled=lambda: True):
        # enabled: уведомления идут, только пока подключено облако Zorcam; без него не шлём и не копим
        self.path, self.api, self.log, self.enabled = path, api, log, enabled
        self.lock = threading.Lock()
        self.data = self._load()
        self.codes = {}  # хеш кода -> {"exp", "redirect_uri", "user"}; только в памяти: живут 10 минут
        self.last_motion = {}  # камера -> время последнего сигнала детектора
        self.active = set()  # камеры, о которых Яндексу сказано «движение»
        self.outbox = queue.Queue(maxsize=100)
        self.last_result = ""

    # ------------------------------------------------------------ хранение
    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("tokens", {})
                return data
        except (OSError, ValueError):
            pass
        return {"tokens": {}}

    def _save(self):
        # под self.lock
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    def status(self):
        """Что показать на странице: без секретов, только есть ли они."""
        with self.lock:
            now = time.time()
            return {"skill_id": self.data.get("skill_id", ""), "has_secret": bool(self.data.get("client_secret")),
                    "has_token": bool(self.data.get("skill_token")),
                    "linked": any(v["kind"] == "refresh" for v in self.data["tokens"].values()),
                    "linked_since": min((v["created"] for v in self.data["tokens"].values()
                                         if v["kind"] == "refresh" and v.get("created")), default=0) or None,
                    "last_result": self.last_result, "now": now}

    def save_settings(self, skill_id, client_secret="", skill_token=""):
        """Пустой секрет или токен — оставить прежний (на страницу они не выводятся)."""
        with self.lock:
            self.data["skill_id"] = skill_id.strip()
            if client_secret.strip():
                self.data["client_secret"] = client_secret.strip()
            if skill_token.strip():
                self.data["skill_token"] = skill_token.strip()
            self._save()

    def ready(self):
        """Связан с Яндексом и есть чем слать уведомления."""
        with self.lock:
            return bool(self.data.get("skill_id") and self.data.get("skill_token")
                        and any(v["kind"] == "refresh" for v in self.data["tokens"].values()))

    # ------------------------------------------------------------ OAuth
    def valid_redirect(self, uri):
        if not uri or any(c in uri for c in "\r\n\t "):  # urlparse молча вырезает их — а адрес уйдёт в Location
            return False
        u = urlparse(uri)
        return u.scheme == "https" and (u.hostname or "") in REDIRECT_HOSTS and not u.username

    def issue_code(self, redirect_uri, user):
        code = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.codes = {k: v for k, v in self.codes.items() if v["exp"] > now}
            self.codes[_hash(code)] = {"exp": now + CODE_TTL, "redirect_uri": redirect_uri, "user": user}
        return code

    def token(self, form, authorization=""):
        """POST /yandex/token: (HTTP-статус, JSON-ответ). Клиент — из формы или из заголовка Basic."""
        field = {k: (v[0] if isinstance(v, list) else v) for k, v in form.items()}
        client_id, secret = field.get("client_id", ""), field.get("client_secret", "")
        if authorization.lower().startswith("basic "):
            try:
                client_id, _, secret = base64.b64decode(authorization[6:]).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                return 401, {"error": "invalid_client"}
        now = time.time()
        with self.lock:
            stored = self.data.get("client_secret", "")
            if client_id != CLIENT_ID or not stored or not secrets.compare_digest(secret.encode(), stored.encode()):
                return 401, {"error": "invalid_client"}
            grant = field.get("grant_type", "")
            if grant == "authorization_code":
                entry = self.codes.pop(_hash(field.get("code", "")), None)
                if not entry or entry["exp"] < now or (field.get("redirect_uri")
                                                       and field["redirect_uri"] != entry["redirect_uri"]):
                    return 400, {"error": "invalid_grant"}
                refresh = secrets.token_urlsafe(32)
                self.data["tokens"][_hash(refresh)] = {"kind": "refresh", "created": now}
                self.data["user"] = entry["user"]
            elif grant == "refresh_token":
                refresh = field.get("refresh_token", "")
                if self.data["tokens"].get(_hash(refresh), {}).get("kind") != "refresh":
                    return 400, {"error": "invalid_grant"}
            else:
                return 400, {"error": "unsupported_grant_type"}
            access = secrets.token_urlsafe(32)
            self.data["tokens"] = {k: v for k, v in self.data["tokens"].items()
                                   if v["kind"] == "refresh" or v["exp"] > now}
            self.data["tokens"][_hash(access)] = {"kind": "access", "exp": now + ACCESS_TTL}
            self._save()
        return 200, {"access_token": access, "token_type": "bearer", "expires_in": ACCESS_TTL,
                     "refresh_token": refresh}

    def check_access(self, authorization):
        if not authorization.lower().startswith("bearer "):
            return False
        with self.lock:
            entry = self.data["tokens"].get(_hash(authorization[7:].strip()))
        return bool(entry) and entry["kind"] == "access" and entry["exp"] > time.time()

    def unlink(self):
        """Пользователь отвязал навык в «Доме с Алисой» (или admin — на странице): все токены — в мусор."""
        with self.lock:
            self.data["tokens"] = {}
            self.active.clear()
            self.last_motion.clear()
            self._save()

    # ------------------------------------------------------------ устройства
    def user_id(self):
        return self.data.get("user") or "admin"

    def devices(self, cams, version=""):
        return {"user_id": self.user_id(), "devices": [{
            "id": c["id"], "name": c["name"], "type": "devices.types.sensor.motion",
            "status_info": {"reportable": True},
            "description": "Zorcam",
            "properties": [motion_property()],
            "device_info": {"manufacturer": "Zorcam", "model": "NVR camera", "sw_version": version},
        } for c in cams]}

    def state(self, cam_id):
        return "detected" if cam_id in self.active else "not_detected"

    def query(self, cams, ids):
        known = {c["id"] for c in cams}
        return {"devices": [{"id": i, "properties": [motion_property(self.state(i))]} if i in known
                            else {"id": i, "error_code": "DEVICE_NOT_FOUND", "error_message": "no such camera"} for i in ids]}

    # ------------------------------------------------------------ движение -> Яндекс
    def motion(self, cam_id, now=None):
        """Сигнал детектора: Яндексу — «движение», только когда его ещё не было (сценарий срабатывает один раз
        на эпизод, а не на каждый ролик)."""
        if not self.enabled():
            return
        now = now or time.time()
        ready = self.ready()
        with self.lock:  # active меняют потоки запросов и фоновый поток
            self.last_motion[cam_id] = now
            if cam_id in self.active or not ready:
                return
            self.active.add(cam_id)
        self._enqueue(cam_id, "detected")

    def tick(self, now=None):
        now = now or time.time()
        with self.lock:
            quiet = [c for c in self.active if now - self.last_motion.get(c, 0) >= HOLD]
            self.active.difference_update(quiet)
        for cam_id in quiet:
            self._enqueue(cam_id, "not_detected")

    def _enqueue(self, cam_id, value):
        if not self.enabled():
            return
        try:
            self.outbox.put_nowait((cam_id, value))
        except queue.Full:
            self.log(f"yandex: queue is full, {cam_id} {value} dropped")

    def send(self, cam_id, value):
        with self.lock:
            skill_id, token = self.data.get("skill_id", ""), self.data.get("skill_token", "")
        body = json.dumps({"ts": time.time(), "payload": {"user_id": self.user_id(), "devices": [
            {"id": cam_id, "properties": [motion_property(value)]}]}}).encode()
        req = urllib.request.Request(f"{self.api}/api/v1/skills/{skill_id}/callback/state", data=body,
                                     method="POST", headers={"Content-Type": "application/json",
                                                             "Authorization": f"OAuth {token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            result = f"HTTP {e.code} {e.read(300).decode(errors='replace')}"
        except (urllib.error.URLError, OSError) as e:
            result = f"no connection ({getattr(e, 'reason', e)})"
        self.last_result = f"{time.strftime('%d.%m %H:%M:%S')} {cam_id} {value}: {result}"
        self.log(f"yandex: {cam_id} motion {value} -> {result}")

    def loop(self):
        """Фоновый поток галереи: уведомления по одному и «нет движения» через HOLD с."""
        while True:
            try:
                cam_id, value = self.outbox.get(timeout=5)
                if self.enabled():  # облако отключили, пока уведомление ждало в очереди
                    self.send(cam_id, value)
            except queue.Empty:
                pass
            except Exception as e:  # поток не должен умереть из-за одного запроса
                self.log(f"yandex: {e}")
            try:
                self.tick()
            except Exception as e:
                self.log(f"yandex: {e}")
