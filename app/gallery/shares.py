"""Ссылки без входа: Live одной камеры по секретной ссылке /s/<токен> (решение владельца, issue #175).

Сам токен (secrets.token_urlsafe(32)) нигде не хранится и не пишется в журнал: в /var/lib/zorcam-nvr/config/shares.json (600)
лежит только его SHA-256, камера, название, срок и время последнего открытия. Токен виден один раз — сразу после создания.

Зрители: сколько человек одновременно смотрят по ссылкам (все ссылки вместе, вошедшие пользователи не считаются).
Зритель — ссылка + адрес + User-Agent, он «жив» VIEWER_TTL секунд после последнего запроса; считается в памяти, без потока.
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time

import zorcam_config

SHARES_FILE = os.path.join(zorcam_config.CONFIG_DIR, "shares.json")
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # token_urlsafe(32)
EXPIRY_DAYS = (1, 7, 30)  # None — без срока
OPENED_EVERY = 3600  # last_opened пишется на SD не чаще раза в час на ссылку
VIEWER_TTL = 60
MAX_SHARES = 100
LABEL_MAX = 60


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def clean_label(text):
    """Название — свободный текст: без управляющих символов и длиннее LABEL_MAX не бывает."""
    return "".join(ch if ch.isprintable() else " " for ch in str(text)).strip()[:LABEL_MAX].strip()


class Shares:
    def __init__(self, path=SHARES_FILE, log=print, max_viewers=lambda: 3, clock=time.time):
        self.path, self.log, self.max_viewers, self.clock = path, log, max_viewers, clock
        self.lock = threading.Lock()
        self.items = self._load()
        self._index()
        self.viewers = {}  # (id ссылки, клиент) -> когда был последний запрос; только в памяти
        self.viewers_lock = threading.Lock()

    # ------------------------------------------------------------ хранение
    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        listed = data.get("shares") if isinstance(data, dict) else None
        items = []
        for it in listed if isinstance(listed, list) else []:
            if (isinstance(it, dict) and isinstance(it.get("id"), str) and isinstance(it.get("camera"), str)
                    and isinstance(it.get("hash"), str) and re.fullmatch(r"[0-9a-f]{64}", it["hash"])
                    and isinstance(it.get("created"), (int, float))):
                expires, opened = it.get("expires"), it.get("last_opened")
                items.append({"id": it["id"], "hash": it["hash"], "camera": it["camera"],
                              "label": clean_label(it.get("label", "")), "created": it["created"],
                              "expires": expires if isinstance(expires, (int, float)) else None,
                              "last_opened": opened if isinstance(opened, (int, float)) else None})
        return items

    def _index(self):
        self.by_hash = {it["hash"]: it for it in self.items}

    def _save(self):
        # под self.lock
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"shares": self.items}, f)
        os.replace(tmp, self.path)
        self._index()

    # ------------------------------------------------------------ ссылки
    def expired(self, item, now=None):
        return item["expires"] is not None and item["expires"] <= (self.clock() if now is None else now)

    def create(self, camera, label, days=None):
        """Новая ссылка камеры: (запись без хеша, токен). ValueError — ссылок уже слишком много."""
        now = self.clock()
        with self.lock:
            self.items = [it for it in self.items if not self.expired(it, now)]  # истёкшие больше не нужны
            if len(self.items) >= MAX_SHARES:
                self._index()
                raise ValueError("too many shares")
            token = secrets.token_urlsafe(32)
            ids = {it["id"] for it in self.items}
            share_id = secrets.token_hex(4)
            while share_id in ids:
                share_id = secrets.token_hex(4)
            item = {"id": share_id, "hash": token_hash(token), "camera": camera, "label": clean_label(label),
                    "created": now, "expires": now + days * 86400 if days else None, "last_opened": None}
            self.items.append(item)
            self._save()
        return self.public(item), token

    @staticmethod
    def public(item):
        return {k: v for k, v in item.items() if k != "hash"}

    def find(self, token):
        """Действующая ссылка по токену (запись без хеша) или None; токен сверяется по хешу."""
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            return None
        with self.lock:
            item = self.by_hash.get(token_hash(token))
            return self.public(item) if item and not self.expired(item) else None

    def listing(self, camera):
        """Ссылки камеры по порядку создания; у истёкших остаётся запись (admin их видит и удаляет)."""
        with self.lock:
            return [dict(self.public(it), expired=self.expired(it)) for it in self.items if it["camera"] == camera]

    def opened(self, share_id):
        """Отметить открытие; на диск — не чаще раза в час. True, если записали (первое открытие за час)."""
        now = self.clock()
        with self.lock:
            item = next((it for it in self.items if it["id"] == share_id), None)
            if item is None or (item["last_opened"] is not None and now - item["last_opened"] < OPENED_EVERY):
                return False
            item["last_opened"] = now
            try:
                self._save()
            except OSError as e:  # нет места или прав: ссылка работает и так
                self.log(f"shares: cannot save: {e}")
            return True

    def delete(self, share_id, camera):
        """Удалить ссылку камеры; False — такой нет."""
        with self.lock:
            keep = [it for it in self.items if not (it["id"] == share_id and it["camera"] == camera)]
            if len(keep) == len(self.items):
                return False
            self.items = keep
            self._save()
        self._drop_viewers(lambda sid: sid == share_id)
        return True

    def forget_camera(self, camera):
        """Камеру удалили — её ссылки тоже."""
        with self.lock:
            gone = {it["id"] for it in self.items if it["camera"] == camera}
            if not gone:
                return
            self.items = [it for it in self.items if it["camera"] != camera]
            self._save()
        self._drop_viewers(lambda sid: sid in gone)

    # ------------------------------------------------------------ зрители
    def _drop_viewers(self, match):
        with self.viewers_lock:
            for key in [k for k in self.viewers if match(k[0])]:
                del self.viewers[key]

    def admit(self, share_id, client):
        """Можно ли этому зрителю смотреть: он уже считается или есть свободное место. Отказ ничего не запоминает,
        поэтому словарь не больше лимита."""
        now = self.clock()
        with self.viewers_lock:
            for key in [k for k, at in self.viewers.items() if now - at >= VIEWER_TTL]:
                del self.viewers[key]
            key = (share_id, client)
            if key in self.viewers or len(self.viewers) < self.max_viewers():
                self.viewers[key] = now
                return True
            return False
