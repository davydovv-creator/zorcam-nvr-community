"""Web Push (RFC 8030/8291/8292) без внешних библиотек, кроме python3-cryptography.

Ключи VAPID и подписки браузеров хранятся в /var/lib/zorcam-nvr/config (права 600).
"""
import base64
import json
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CONFIG_DIR = os.path.join(os.environ.get("ZORCAM_STATE_DIR", "/var/lib/zorcam-nvr"), "config")
VAPID_KEY_FILE = os.path.join(CONFIG_DIR, "vapid_private.pem")
SUBSCRIPTIONS_FILE = os.path.join(CONFIG_DIR, "push_subscriptions.json")
# контакт владельца для push-сервисов (Apple требует mailto: или https:)
VAPID_SUBJECT = os.environ.get("ZORCAM_PUSH_SUBJECT", "mailto:zorcamnvr@yandex.ru")
GONE_REASONS = {"VapidPkHashMismatch", "BadDeviceToken", "Unregistered", "ExpiredSubscription"}
TTL = 600  # сек: сколько push-сервис хранит уведомление, если телефон не в сети
RECORD_SIZE = 4096


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _write_private(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _raw_public(key):
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _hkdf(salt, ikm, info, length):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload, p256dh, auth):
    """Тело запроса aes128gcm (RFC 8291) для подписки с ключами p256dh/auth (base64url)."""
    ua_public = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    as_key = ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(as_key)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh_secret = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, ecdh_secret, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack(">IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


class PushService:
    def __init__(self):
        self.lock = threading.Lock()
        self.key = self._load_or_create_key()
        self.public_key = b64url(_raw_public(self.key))
        self.subscriptions = self._load_subscriptions()

    # ---------------------------------------------------------------- хранение
    def _load_or_create_key(self):
        try:
            with open(VAPID_KEY_FILE, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None)
        except FileNotFoundError:
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
            _write_private(VAPID_KEY_FILE, pem)
            return key

    def _load_subscriptions(self):
        try:
            with open(SUBSCRIPTIONS_FILE) as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            return {}

    def _save(self):
        # вызывается под self.lock
        _write_private(SUBSCRIPTIONS_FILE, json.dumps(self.subscriptions).encode())

    def add(self, sub, label="", user="", lang="", pick=None):
        """sub — PushSubscription.toJSON() из браузера; lang — язык устройства (en, ru), pick — выбранный
        на устройстве в «Настройках» (None — «авто»).
        Возвращает False, если подписка некорректна."""
        try:
            endpoint = sub["endpoint"]
            keys = {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]}
            if urlparse(endpoint).scheme != "https" or len(b64url_decode(keys["p256dh"])) != 65:
                return False
        except (KeyError, TypeError, ValueError):
            return False
        entry = {"keys": keys, "label": label[:200], "user": user, "lang": lang, **({"pick": pick} if pick else {})}
        with self.lock:
            old = self.subscriptions.get(endpoint)
            # приложение подписывается заново при каждом открытии страницы: без изменений — без записи на SD-карту
            if old is not None and {k: v for k, v in old.items() if k != "created"} == entry:
                return True
            self.subscriptions[endpoint] = {**entry, "created": (old or {}).get("created", int(time.time()))}
            self._save()
        return True

    def remove(self, endpoint):
        with self.lock:
            if self.subscriptions.pop(endpoint, None) is not None:
                self._save()

    def count(self):
        with self.lock:
            return len(self.subscriptions)

    # ---------------------------------------------------------------- отправка
    def _vapid_header(self, endpoint):
        u = urlparse(endpoint)
        header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
        claims = b64url(json.dumps({
            "aud": f"{u.scheme}://{u.netloc}",
            "exp": int(time.time()) + 12 * 3600,
            "sub": VAPID_SUBJECT,
        }).encode())
        signing_input = f"{header}.{claims}".encode()
        r, s = decode_dss_signature(self.key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={header}.{claims}.{signature}, k={self.public_key}"

    def send_one(self, endpoint, keys, message):
        """Отправляет уведомление. Возвращает (HTTP-статус push-сервиса, причина отказа или "");
        статус 0 — сеть недоступна."""
        body = encrypt(json.dumps(message, ensure_ascii=False).encode(), keys["p256dh"], keys["auth"])
        req = urllib.request.Request(endpoint, data=body, method="POST", headers={
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(TTL),
            "Urgency": "high",
            "Authorization": self._vapid_header(endpoint),
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, ""
        except urllib.error.HTTPError as e:
            try:  # Apple объясняет отказ: {"reason": "VapidPkHashMismatch"}
                reason = str(json.loads(e.read(4096) or b"{}").get("reason") or "")
            except (OSError, ValueError, AttributeError):
                reason = ""
            return e.code, re.sub(r"[^A-Za-z0-9_.-]", "", reason)[:64]
        except (urllib.error.URLError, OSError):
            return 0, ""

    def send_all(self, message, log=print, only=None, users=None):
        """Рассылает всем подпискам; only — только подписке с этим endpoint;
        users — только подпискам этих пользователей (подписка без пользователя считается пользователем "").
        message — dict или функция подписки (dict подписки -> dict), чтобы каждый получил текст на своём языке.
        Возвращает (сколько принял push-сервис, скольким подпискам отправляли)."""
        with self.lock:
            targets = [(e, s) for e, s in self.subscriptions.items()
                       if (only is None or e == only) and (users is None or s.get("user", "") in users)]
        delivered = 0
        for endpoint, sub in targets:
            body = message(sub) if callable(message) else message
            status, reason = self.send_one(endpoint, sub["keys"], body)
            # 404/410 — подписка отозвана браузером; VapidPkHashMismatch — сделана с ключом другой установки
            # (новая SD-карта): Apple отказывает ей всегда, iPhone подписку уже снял — повторять незачем
            if status in (404, 410) or reason in GONE_REASONS:
                self.remove(endpoint)
                log(f"push: subscription gone ({status}{' ' + reason if reason else ''}), removed: {sub.get('label', '')}")
            elif not 200 <= status < 300:
                log(f"push: HTTP {status}{' ' + reason if reason else ''} from {urlparse(endpoint).netloc} "
                    f"({sub.get('label', '')})")
            else:
                delivered += 1
        return delivered, len(targets)
