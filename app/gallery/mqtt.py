"""MQTT для Home Assistant: движение каждой камеры в брокер владельца, без библиотек (клиент MQTT 3.1.1, только публикация).

Темы: <префикс>/<устройство>/<камера>/motion — ON/OFF (retain), <префикс>/<устройство>/status — online/offline (retain;
last will: брокер сам ставит offline при обрыве). Home Assistant: описания автообнаружения
homeassistant/binary_sensor/zorcam_<устройство>_<камера>/config (device_class motion), доступность — по теме status
и по теме <камера>/available (online / offline: выключенная камера «недоступна»).

Очереди событий нет: после подключения (и после каждого обрыва) в брокер заново уходят описания, online и ТЕКУЩЕЕ состояние
каждой камеры; что было за время обрыва, не копится. Поток один, галерея в брокер из обработчиков запросов не ходит —
/healthz и watchdog от брокера не зависят.

Настройки — /var/lib/zorcam-nvr/config/mqtt.json (600; логин и пароль брокера там же). Не zorcam.conf и не secrets.env:
их изменение перезапускает go2rtc.
"""
import json
import os
import re
import select
import socket
import ssl
import struct
import threading
import time

import episodes
import zorcam_config
from zorcam_i18n import Msg

STATE_FILE = os.path.join(zorcam_config.CONFIG_DIR, "mqtt.json")
DISCOVERY_PREFIX = "homeassistant"
KEEPALIVE = 60  # секунд; PINGREQ — после KEEPALIVE/2 тишины
IO_TIMEOUT = 10  # на подключение, рукопожатие TLS и каждое чтение/запись
RETRY_MIN, RETRY_MAX = 5, 300  # пауза между попытками подключиться: 5 с, вдвое дольше после каждой неудачи, до 5 минут
IDLE = 5  # самое долгое ожидание потока между проверками (обрыв, новые камеры, конец эпизода — раньше, по событию)
ON, OFF = b"ON", b"OFF"
ONLINE, OFFLINE = b"online", b"offline"

DEFAULTS = {"enabled": False, "host": "", "port": 0, "tls": False, "username": "", "password": "",
            "prefix": "zorcam", "device": "", "discovery": True}
HOST_RE = re.compile(r"^[A-Za-z0-9._:-]{1,253}$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$")
DEVICE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
MAX_PREFIX, MAX_LOGIN = 64, 256

# типы пакетов MQTT 3.1.1
CONNECT, CONNACK, PUBLISH, PUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 4, 12, 13, 14
CONNACK_ERRORS = {1: "mqtt.err.version", 2: "mqtt.err.client_id", 3: "mqtt.err.unavailable",
                  4: "mqtt.err.credentials", 5: "mqtt.err.not_authorized"}


class MqttError(Exception):
    """Ошибка связи с брокером; msg — Msg (переводится при показе), str(error) — английский текст для журнала."""

    def __init__(self, msg_key, /, **kw):
        self.msg = Msg(msg_key, **kw)
        super().__init__(str(self.msg))


# ------------------------------------------------------------ пакеты
def encode_length(n):
    out = bytearray()
    while True:
        n, digit = divmod(n, 128)
        out.append(digit | (0x80 if n else 0))
        if not n:
            return bytes(out)


def encode_str(value):
    data = value if isinstance(value, bytes) else value.encode()
    return struct.pack("!H", len(data)) + data


def packet(first_byte, body):
    return bytes([first_byte]) + encode_length(len(body)) + body


def connect_packet(client_id, keepalive=KEEPALIVE, username="", password="", will=None):
    """will — (тема, байты, qos, retain). Пароль без логина в 3.1.1 нельзя: его не передаём."""
    flags, payload = 0x02, encode_str(client_id)  # clean session
    if will:
        topic, message, qos, retain = will
        flags |= 0x04 | qos << 3 | (0x20 if retain else 0)
        payload += encode_str(topic) + encode_str(message)
    if username:
        flags |= 0x80
        payload += encode_str(username)
        if password:
            flags |= 0x40
            payload += encode_str(password)
    return packet(CONNECT << 4, encode_str("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive) + payload)


def publish_packet(topic, payload, qos=0, retain=False, packet_id=0):
    body = encode_str(topic) + (struct.pack("!H", packet_id) if qos else b"") + payload
    return packet(PUBLISH << 4 | qos << 1 | int(retain), body)


def read_packet(read):
    """(тип, флаги, тело) очередного пакета; read(n) отдаёт ровно n байт или бросает MqttError."""
    first = read(1)[0]
    length, shift = 0, 0
    for _ in range(4):
        digit = read(1)[0]
        length += (digit & 0x7F) << shift
        if not digit & 0x80:
            break
        shift += 7
    else:
        raise MqttError("mqtt.err.protocol")
    return first >> 4, first & 0x0F, read(length) if length else b""


def parse_connack(body):
    """Код ответа брокера на CONNECT; не 0 — MqttError с понятной причиной."""
    if len(body) != 2:
        raise MqttError("mqtt.err.protocol")
    code = body[1]
    if code:
        if code in CONNACK_ERRORS:
            raise MqttError(CONNACK_ERRORS[code])
        raise MqttError("mqtt.err.refused", code=code)


def tls_context():
    return ssl.create_default_context()  # сертификат брокера проверяется по системным CA; свой CA — в системное хранилище


class Client:
    """Одно соединение с брокером: connect, publish (QoS 0 и 1), ping, poll, close. Сам не переподключается."""

    def __init__(self, host, port, tls=False, username="", password="", client_id="zorcam", will=None,
                 keepalive=KEEPALIVE, timeout=IO_TIMEOUT):
        self.host, self.port, self.tls, self.username, self.password = host, port, tls, username, password
        self.client_id, self.will, self.keepalive, self.timeout = client_id, will, keepalive, timeout
        self.sock = None
        self.packet_id = 0
        self.last_send = time.monotonic()

    # ---- ввод-вывод
    def _read(self, n):
        data = b""
        while len(data) < n:
            try:
                chunk = self.sock.recv(n - len(data))
            except TimeoutError:
                raise MqttError("mqtt.err.timeout") from None
            except (ssl.SSLEOFError, ssl.SSLZeroReturnError):
                raise MqttError("mqtt.err.closed") from None
            except ssl.SSLError as e:
                raise MqttError("mqtt.err.tls", error=e.reason or e) from None
            except OSError as e:
                raise MqttError("mqtt.err.connect", error=e.strerror or e) from None
            if not chunk:
                raise MqttError("mqtt.err.closed")
            data += chunk
        return data

    def _send(self, data):
        try:
            self.sock.sendall(data)
        except TimeoutError:
            raise MqttError("mqtt.err.timeout") from None
        except OSError as e:
            raise MqttError("mqtt.err.connect", error=e.strerror or e) from None
        self.last_send = time.monotonic()

    def _wait_for(self, ptype, packet_id=None):
        """Читает пакеты, пока не придёт нужный (другие — PINGRESP, чужие PUBLISH — пропускаются)."""
        while True:
            got, _, body = read_packet(self._read)
            if got == ptype and (packet_id is None or body[:2] == struct.pack("!H", packet_id)):
                return body

    # ---- протокол
    def connect(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            if self.tls:
                try:
                    sock = tls_context().wrap_socket(sock, server_hostname=self.host)
                except BaseException:
                    sock.close()
                    raise
        except ssl.SSLError as e:
            raise MqttError("mqtt.err.tls", error=getattr(e, "verify_message", None) or e.reason or e) from None
        except TimeoutError:
            raise MqttError("mqtt.err.timeout") from None
        except OSError as e:
            raise MqttError("mqtt.err.connect", error=e.strerror or e) from None
        self.sock = sock
        try:
            sock.settimeout(self.timeout)
            self._send(connect_packet(self.client_id, self.keepalive, self.username, self.password, self.will))
            ptype, _, body = read_packet(self._read)
            if ptype != CONNACK:  # первым должен прийти CONNACK; иначе это не MQTT-брокер (чаще — не тот порт)
                raise MqttError("mqtt.err.protocol")
            parse_connack(body)
        except BaseException:
            self.close(clean=False)
            raise

    def publish(self, topic, payload, qos=0, retain=False):
        """QoS 1 — с ожиданием PUBACK (без него MqttError: соединение считается потерянным)."""
        if not qos:
            self._send(publish_packet(topic, payload, 0, retain))
            return
        self.packet_id = self.packet_id % 65535 + 1
        self._send(publish_packet(topic, payload, 1, retain, self.packet_id))
        self._wait_for(PUBACK, self.packet_id)

    def ping_due(self):
        return time.monotonic() - self.last_send >= self.keepalive / 2

    def ping(self):
        self._send(bytes([PINGREQ << 4, 0]))
        self._wait_for(PINGRESP)

    def poll(self):
        """Не блокируя: прочитать, что брокер прислал (чаще всего ничего), и заметить, что он закрыл соединение."""
        while getattr(self.sock, "pending", lambda: 0)() or select.select([self.sock], [], [], 0)[0]:
            read_packet(self._read)  # EOF здесь — MqttError("closed")

    def close(self, clean=True):
        if self.sock is None:
            return
        try:
            if clean:
                self.sock.sendall(bytes([DISCONNECT << 4, 0]))  # с DISCONNECT брокер last will не публикует
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None


# ------------------------------------------------------------ настройки и состояние
def default_device(hostname=None):
    """Имя устройства в темах: hostname в нижнем регистре, из допустимых символов."""
    name = re.sub(r"[^a-z0-9_-]+", "-", (hostname if hostname is not None else socket.gethostname()).lower()).strip("-")
    return name[:32].strip("-") or "zorcam"


def default_port(tls):
    return 8883 if tls else 1883


def validate(fields, current):
    """Настройки из формы -> (настройки, {поле: Msg}). Пустой пароль — прежний; без логина пароля нет.
    fields — {имя: строка}; галочки — наличие имени (как у форм браузера)."""
    cfg, errors = dict(current), {}
    cfg["enabled"] = "enabled" in fields
    cfg["tls"] = "tls" in fields
    cfg["discovery"] = "discovery" in fields
    host = fields.get("host", "").strip()
    cfg["host"] = host
    if host and not HOST_RE.fullmatch(host):
        errors["host"] = Msg("mqtt.bad.host")
    elif cfg["enabled"] and not host:
        errors["host"] = Msg("mqtt.bad.host_empty")
    port = fields.get("port", "").strip()
    if not port:
        cfg["port"] = default_port(cfg["tls"])
    elif port.isdecimal() and 0 < int(port) < 65536:
        cfg["port"] = int(port)
    else:
        errors["port"] = Msg("mqtt.bad.port")
    prefix = fields.get("prefix", "").strip() or DEFAULTS["prefix"]
    cfg["prefix"] = prefix
    if len(prefix) > MAX_PREFIX or not PREFIX_RE.fullmatch(prefix):
        errors["prefix"] = Msg("mqtt.bad.prefix")
    device = fields.get("device", "").strip()
    if device == default_device():
        device = ""  # как предложено: имя и дальше следует за hostname
    cfg["device"] = device
    if device and not DEVICE_RE.fullmatch(device):
        errors["device"] = Msg("mqtt.bad.device")
    username, password = fields.get("username", ""), fields.get("password", "")
    if len(username) > MAX_LOGIN or len(password) > MAX_LOGIN or "\0" in username + password:
        errors["username"] = Msg("mqtt.bad.login")
    cfg["username"] = username
    if not username:
        cfg["password"] = ""
    elif password:
        cfg["password"] = password
    return cfg, errors


def client_key(cfg):
    """Что нельзя поменять на живом соединении: куда и под чем подключены, last will (тема status)."""
    return tuple(cfg[k] for k in ("host", "port", "tls", "username", "password", "prefix", "device"))


class Mqtt:
    def __init__(self, path=STATE_FILE, cameras=lambda: [], version="", log=print, hostname=None, clock=time.time,
                 client_class=Client):
        self.path, self.cameras, self.version, self.log = path, cameras, version, log
        self.hostname, self.clock, self.client_class = hostname, clock, client_class
        self.lock = threading.Lock()
        self.cfg = self._load()
        self.episodes = episodes.Episodes()
        self.wake = threading.Event()  # движение, новые настройки: потоку пора проснуться
        self.changed = threading.Event()  # только новые настройки: повторить подключение без ожидания паузы
        self.state, self.error, self.connected_at = "off", None, None
        self._reported = None
        self.stopped = False  # только для тестов: поток галереи работает, пока жив процесс

    def stop(self):
        self.stopped = True
        self.changed.set()
        self.wake.set()

    # ---- хранение
    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        cfg = dict(DEFAULTS)
        if isinstance(data, dict):
            for key, default in DEFAULTS.items():
                if isinstance(data.get(key), type(default)):
                    cfg[key] = data[key]
        return cfg

    def settings(self):
        """Действующие настройки: порт и имя устройства по умолчанию уже подставлены."""
        with self.lock:
            cfg = dict(self.cfg)
        cfg["port"] = cfg["port"] or default_port(cfg["tls"])
        cfg["device"] = cfg["device"] or default_device(self.hostname)
        return cfg

    def form_values(self):
        """Для формы: то же, но без пароля (в форму он не возвращается), с признаком, что он задан."""
        cfg = self.settings()
        cfg["has_password"] = bool(cfg.pop("password"))
        return cfg

    def save(self, cfg):
        """Атомарно, 600; потоку — новые настройки (переподключится сам, сервисы не перезапускаются)."""
        cfg = {k: cfg[k] for k in DEFAULTS}
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f)
        os.replace(tmp, self.path)
        with self.lock:
            was_enabled = self.cfg["enabled"]
            self.cfg = cfg
        if not was_enabled:
            self.episodes.clear()  # пока MQTT был выключен, сигналы не учитывались: начинаем с «нет движения»
        self.changed.set()
        self.wake.set()

    def enabled(self):
        with self.lock:
            return self.cfg["enabled"]

    def status(self):
        """Для «Статуса»: state off / connecting / connected / error, брокер host:port и последняя ошибка (Msg)."""
        cfg = self.settings()
        with self.lock:
            return {"state": self.state if cfg["enabled"] else "off", "host": cfg["host"], "port": cfg["port"],
                    "error": self.error}

    def _report(self, state, error=None):
        """Состояние подключения; в журнал — только когда оно (или причина ошибки) изменилось: повторные попытки
        с той же ошибкой журнал не засоряют."""
        with self.lock:
            if state == "connecting" and self.state != "off":
                return  # после ошибки «Статус» держит её до удачи, а не мигает «подключение…»
            self.state, self.error = state, error
            self.connected_at = time.monotonic() if state == "connected" else None
            if state in ("off", "connecting"):
                if state == "off":
                    self._reported = None
                return
            mark = (state, str(error) if error else "")
            changed, self._reported = mark != self._reported, mark
        if changed:
            cfg = self.settings()
            where = f"{cfg['host']}:{cfg['port']}"
            self.log(f"mqtt: connected to {where}" if state == "connected" else f"mqtt: no connection to {where}: {error}")

    # ---- движение
    def motion(self, cam_id, now=None):
        """Сигнал детектора (вызывает обработчик запроса: быстро, в сеть не ходит)."""
        if not self.enabled():
            return
        if self.episodes.signal(cam_id, now or self.clock()):
            self.wake.set()

    # ---- что должно лежать в брокере
    def topics(self, cfg):
        base = f"{cfg['prefix']}/{cfg['device']}"
        return base, f"{base}/status"

    def desired(self, cfg):
        """{тема: (содержимое, qos)} — всё, что сейчас должно быть в брокере (все retain), в порядке публикации:
        описания и доступность камер, online, состояния. Темы, которых здесь больше нет, у брокера очищаются."""
        base, status = self.topics(cfg)
        discovery, available, states = {}, {}, {}
        for cam in self.cameras():
            cam_id, on = cam["id"], cam.get("enabled", True)
            states[f"{base}/{cam_id}/motion"] = (ON if on and self.episodes.is_active(cam_id) else OFF, 0)
            if cfg["discovery"]:
                available[f"{base}/{cam_id}/available"] = (ONLINE if on else OFFLINE, 1)
                discovery[self.discovery_topic(cfg, cam_id)] = (self.discovery_payload(cfg, cam), 1)
        return {**discovery, **available, status: (ONLINE, 1), **states}

    def discovery_topic(self, cfg, cam_id):
        return f"{DISCOVERY_PREFIX}/binary_sensor/zorcam_{cfg['device']}_{cam_id}/config"

    def discovery_payload(self, cfg, cam):
        """Описание датчика движения для Home Assistant. Камеру выключили — её тема available = offline:
        датчик «недоступен» (общая доступность устройства — по теме status, обе должны быть online)."""
        base, status = self.topics(cfg)
        dev = cfg["device"]
        return json.dumps({
            "name": cam["name"], "unique_id": f"zorcam_{dev}_{cam['id']}", "device_class": "motion",
            "state_topic": f"{base}/{cam['id']}/motion", "payload_on": "ON", "payload_off": "OFF",
            "availability": [{"topic": status}, {"topic": f"{base}/{cam['id']}/available"}],
            "availability_mode": "all",
            "device": {"identifiers": [f"zorcam_{dev}"], "name": f"Zorcam {dev}", "manufacturer": "Zorcam",
                       "model": "NVR", "sw_version": self.version},
        }, sort_keys=True, ensure_ascii=False).encode()

    def sync(self, client, cfg, sent):
        """Довести брокера до desired(): отправить изменившееся, очистить исчезнувшее. sent — что уже в брокере
        на этом соединении (пустой после подключения — тогда уходит всё)."""
        want = self.desired(cfg)
        for topic, (payload, qos) in want.items():
            if sent.get(topic, (None,))[0] != payload:
                client.publish(topic, payload, qos, retain=True)
                sent[topic] = (payload, qos)
        for topic in [t for t in sent if t not in want]:  # удалённая камера, выключенный discovery
            client.publish(topic, b"", sent[topic][1], retain=True)
            del sent[topic]

    def _will(self, cfg):
        return self.topics(cfg)[1], OFFLINE, 1, True

    def _client(self, cfg):
        return self.client_class(cfg["host"], cfg["port"], cfg["tls"], cfg["username"], cfg["password"],
                                 client_id=f"zorcam-{cfg['device']}", will=self._will(cfg))

    def _goodbye(self, client, old, new, sent):
        """Настройки изменились: брокер узнаёт offline (с DISCONNECT его last will не сработает). Устройство
        переименовали или перенесли к другому брокеру — старые темы очищаются, чтобы датчики не висели."""
        status = self.topics(old)[1]
        try:
            if new["enabled"]:
                for topic in [t for t in sent if t != status]:
                    client.publish(topic, b"", sent[topic][1], retain=True)
            client.publish(status, OFFLINE, 1, retain=True)
        except MqttError:
            pass
        client.close()

    def _next_wait(self, client):
        wait, end = IDLE, self.episodes.next_end()
        if end is not None:
            wait = min(wait, end - self.clock())
        return max(0.05, min(wait, client.keepalive / 2))

    def _session(self, cfg):
        """Одно соединение: от подключения до обрыва (MqttError) или смены настроек (возврат)."""
        client = self._client(cfg)
        self._report("connecting")
        client.connect()
        self._report("connected")
        sent = {}
        try:
            while True:
                self.wake.clear()  # события после этого места разбудят wait внизу
                if self.stopped:
                    client.close()
                    return
                self.episodes.tick(self.clock())
                new = self.settings()
                if not new["enabled"] or client_key(new) != client_key(cfg):
                    self._goodbye(client, cfg, new, sent)
                    return
                cfg = new
                self.sync(client, cfg, sent)
                client.poll()
                if client.ping_due():
                    client.ping()
                self.wake.wait(self._next_wait(client))
        except BaseException:
            client.close(clean=False)
            raise

    def loop(self):
        """Фоновый поток галереи. Исключения его не роняют: ошибка — в «Статус», пауза и новая попытка."""
        delay = RETRY_MIN
        while not self.stopped:
            try:
                cfg = self.settings()
                if not cfg["enabled"] or not cfg["host"]:
                    self._report("off")
                    self.changed.wait(IDLE)
                    self.changed.clear()
                    delay = RETRY_MIN
                    continue
                try:
                    self._session(cfg)
                    self._report("off")  # настройки изменились: «Статус» не держит старое подключение
                    delay = RETRY_MIN
                    continue
                except MqttError as e:
                    error = e.msg
                except Exception as e:  # ошибка в самом коде: поток живёт дальше
                    error = Msg("mqtt.err.internal", error=f"{type(e).__name__}: {e}")
                if self.connected_at is not None:  # соединение было и оборвалось: заново с короткой паузы
                    delay = RETRY_MIN
                self.changed.clear()  # уже сохранённые настройки были и у оборвавшегося соединения
                self._report("error", error)
                if self.changed.wait(delay):  # настройки изменили: пробуем сразу
                    self.changed.clear()
                    delay = RETRY_MIN
                else:
                    delay = min(delay * 2, RETRY_MAX)
            except Exception as e:  # сюда не должно попасть; не плодим журнал при сбое
                self.log(f"mqtt: {type(e).__name__}: {e}")
                time.sleep(RETRY_MIN)
