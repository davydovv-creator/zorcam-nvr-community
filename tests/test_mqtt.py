"""app/gallery/mqtt.py: MQTT 3.1.1 packets, settings, Home Assistant discovery and the whole publishing thread against a
fake broker on localhost (a socket server in a thread). No real network."""
import datetime
import json
import os
import socket
import ssl
import struct
import threading
import time

import pytest

import mqtt
from zorcam_i18n import Msg, t

CAMS = [{"id": "front", "name": "Front door", "enabled": True},
        {"id": "yard", "name": "Yard", "enabled": True}]


# ---------------------------------------------------------------- packets

@pytest.mark.parametrize("n,expected", [(0, b"\x00"), (127, b"\x7f"), (128, b"\x80\x01"), (16383, b"\xff\x7f"),
                                        (16384, b"\x80\x80\x01"), (2097152, b"\x80\x80\x80\x01"),
                                        (268435455, b"\xff\xff\xff\x7f")])
def test_remaining_length(n, expected):
    assert mqtt.encode_length(n) == expected
    if n < 20000:
        buf = b"\x30" + expected + b"x" * n
        pos = [0]

        def read(k):
            chunk = buf[pos[0]:pos[0] + k]
            pos[0] += k
            return chunk
        assert mqtt.read_packet(read) == (3, 0, b"x" * n)


def test_connect_packet_with_will_and_login():
    pkt = mqtt.connect_packet("zorcam-box", 60, "user", "pw", will=("zorcam/box/status", b"offline", 1, True))
    body = (b"\x00\x04MQTT" + bytes([4, 0b11101110]) + struct.pack("!H", 60) + b"\x00\x0azorcam-box"
            + b"\x00\x11zorcam/box/status" + b"\x00\x07offline" + b"\x00\x04user" + b"\x00\x02pw")
    assert pkt == b"\x10" + mqtt.encode_length(len(body)) + body
    # flags: username, password, will retain, will QoS 1, will, clean session


def test_connect_packet_without_login_and_password_without_login():
    pkt = mqtt.connect_packet("c", 30)
    assert pkt == b"\x10\x0d\x00\x04MQTT\x04\x02\x00\x1e\x00\x01c"
    # a password without a login is not allowed by MQTT 3.1.1: it is not sent
    assert mqtt.connect_packet("c", 30, "", "secret") == pkt


def test_publish_packets():
    assert mqtt.publish_packet("a/b", b"ON") == b"\x30\x07\x00\x03a/bON"
    assert mqtt.publish_packet("a/b", b"ON", 0, True) == b"\x31\x07\x00\x03a/bON"
    assert mqtt.publish_packet("a/b", b"x", 1, True, 7) == b"\x33\x08\x00\x03a/b\x00\x07x"
    assert mqtt.publish_packet("a/b", b"") == b"\x30\x05\x00\x03a/b"


def test_connack_codes():
    mqtt.parse_connack(b"\x00\x00")
    for code, key in mqtt.CONNACK_ERRORS.items():
        with pytest.raises(mqtt.MqttError) as e:
            mqtt.parse_connack(bytes([0, code]))
        assert e.value.msg == Msg(key)
    assert "login" in str(Msg("mqtt.err.credentials")) and "authorized" in str(Msg("mqtt.err.not_authorized"))
    with pytest.raises(mqtt.MqttError) as e:
        mqtt.parse_connack(b"\x00\x09")
    assert e.value.msg == Msg("mqtt.err.refused", code=9)
    with pytest.raises(mqtt.MqttError):
        mqtt.parse_connack(b"\x00")


def test_every_error_text_is_translated():
    for key in list(mqtt.CONNACK_ERRORS.values()) + ["mqtt.err.connect", "mqtt.err.timeout", "mqtt.err.closed", "mqtt.err.tls",
                                                    "mqtt.err.protocol", "mqtt.err.internal"]:
        assert t("en", key, error="x") and t("ru", key, error="x")


# ---------------------------------------------------------------- settings

def test_default_device_and_validate(tmp_path):
    assert mqtt.default_device("Pi-Box.local") == "pi-box-local"
    assert mqtt.default_device("") == "zorcam" and mqtt.default_device("---") == "zorcam"
    base = dict(mqtt.DEFAULTS)
    cfg, errors = mqtt.validate({"enabled": "yes", "host": "broker.lan", "username": "u", "password": "p"}, base)
    assert not errors and cfg["port"] == 1883 and cfg["prefix"] == "zorcam" and cfg["device"] == ""
    assert cfg["discovery"] is False  # a checkbox that is not in the form is off
    cfg, errors = mqtt.validate({"host": "b", "tls": "yes", "discovery": "yes"}, base)
    assert cfg["port"] == 8883 and cfg["tls"] and cfg["discovery"]
    for fields, field in [({"enabled": "yes"}, "host"), ({"host": "a b"}, "host"), ({"host": "http://x/"}, "host"),
                          ({"host": "b", "port": "0"}, "port"), ({"host": "b", "port": "70000"}, "port"),
                          ({"host": "b", "port": "x"}, "port"), ({"host": "b", "prefix": "a b"}, "prefix"),
                          ({"host": "b", "prefix": "a/#"}, "prefix"), ({"host": "b", "prefix": "/a"}, "prefix"),
                          ({"host": "b", "prefix": "a" * 65}, "prefix"), ({"host": "b", "device": "Box"}, "device"),
                          ({"host": "b", "device": "a/b"}, "device"), ({"host": "b", "username": "u" * 257}, "username")]:
        assert field in mqtt.validate(fields, base)[1], fields
    ok = mqtt.validate({"host": "b", "prefix": "home/zorcam", "device": "cam-1_a"}, base)
    assert not ok[1] and ok[0]["prefix"] == "home/zorcam"
    # the default name is stored as "follow the hostname"
    assert mqtt.validate({"host": "b", "device": mqtt.default_device()}, base)[0]["device"] == ""


def test_password_is_kept_when_empty_and_dropped_without_login(tmp_path):
    cur = dict(mqtt.DEFAULTS, username="u", password="old")
    assert mqtt.validate({"host": "b", "username": "u", "password": ""}, cur)[0]["password"] == "old"
    assert mqtt.validate({"host": "b", "username": "u", "password": "new"}, cur)[0]["password"] == "new"
    assert mqtt.validate({"host": "b", "username": "", "password": "x"}, cur)[0]["password"] == ""


def test_save_is_atomic_600_and_survives_a_restart(tmp_path):
    path = tmp_path / "mqtt.json"
    m = mqtt.Mqtt(path=str(path), hostname="Box")
    assert m.settings()["device"] == "box" and m.settings()["port"] == 1883 and not m.enabled()
    cfg, _ = mqtt.validate({"enabled": "yes", "host": "broker", "username": "u", "password": "pw", "discovery": "yes"}, m.cfg)
    m.save(cfg)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600" and not os.path.exists(str(path) + ".tmp")
    again = mqtt.Mqtt(path=str(path), hostname="Box")
    assert again.settings()["host"] == "broker" and again.settings()["password"] == "pw" and again.enabled()
    form = again.form_values()
    assert "password" not in form and form["has_password"]
    path.write_text("{broken")
    assert mqtt.Mqtt(path=str(path)).settings()["prefix"] == "zorcam"
    path.write_text(json.dumps({"host": 5, "port": "x", "enabled": "yes", "prefix": "p"}))
    loaded = mqtt.Mqtt(path=str(path)).settings()
    assert loaded["host"] == "" and loaded["prefix"] == "p" and not loaded["enabled"]  # wrong types are ignored


# ---------------------------------------------------------------- discovery and the state to publish

def make(tmp_path, cams=None, **kw):
    m = mqtt.Mqtt(path=str(tmp_path / "mqtt.json"), cameras=lambda: cams if cams is not None else CAMS, version="9.9.9",
                  log=kw.pop("log", lambda msg: None), hostname="Box", **kw)
    return m


def configure(m, **fields):
    base = {"enabled": "yes", "host": "127.0.0.1", "discovery": "yes", "device": "box", "username": "u", "password": "pw"}
    cfg, errors = mqtt.validate({k: v for k, v in {**base, **fields}.items() if v is not None}, m.cfg)  # None: the box is off
    assert not errors, errors
    m.save(cfg)


def test_desired_topics_and_discovery(tmp_path):
    m = make(tmp_path)
    configure(m)
    cfg = m.settings()
    want = m.desired(cfg)
    assert list(want)[-3:] == ["zorcam/box/status", "zorcam/box/front/motion", "zorcam/box/yard/motion"]
    assert want["zorcam/box/status"] == (b"online", 1) and want["zorcam/box/front/motion"] == (b"OFF", 0)
    assert want["zorcam/box/front/available"] == (b"online", 1)
    payload, qos = want["homeassistant/binary_sensor/zorcam_box_front/config"]
    assert qos == 1
    assert json.loads(payload) == {
        "name": "Front door", "unique_id": "zorcam_box_front", "device_class": "motion",
        "state_topic": "zorcam/box/front/motion", "payload_on": "ON", "payload_off": "OFF",
        "availability": [{"topic": "zorcam/box/status"}, {"topic": "zorcam/box/front/available"}],
        "availability_mode": "all",
        "device": {"identifiers": ["zorcam_box"], "name": "Zorcam box", "manufacturer": "Zorcam", "model": "NVR",
                   "sw_version": "9.9.9"}}
    m.motion("front", 1000)
    assert m.desired(cfg)["zorcam/box/front/motion"] == (b"ON", 0)
    assert m.desired(cfg)["zorcam/box/yard/motion"] == (b"OFF", 0)


def test_disabled_camera_is_unavailable_and_discovery_can_be_off(tmp_path):
    cams = [dict(CAMS[0], enabled=False), CAMS[1]]
    m = make(tmp_path, cams)
    configure(m)
    want = m.desired(m.settings())
    assert want["zorcam/box/front/available"] == (b"offline", 1) and want["zorcam/box/yard/available"] == (b"online", 1)
    assert want["zorcam/box/front/motion"] == (b"OFF", 0)
    configure(m, discovery=None)
    off = m.desired(m.settings())
    assert not [k for k in off if k.startswith("homeassistant/") or k.endswith("/available")]
    assert "zorcam/box/status" in off and "zorcam/box/front/motion" in off


def test_motion_is_ignored_while_mqtt_is_off(tmp_path):
    m = make(tmp_path)
    m.motion("front", 1000)
    assert not m.episodes.active and not m.episodes.last_motion and not m.wake.is_set()


def test_episode_is_on_for_hold_seconds_after_the_last_signal(tmp_path):
    now = [1000.0]
    m = make(tmp_path, clock=lambda: now[0])
    configure(m)
    m.motion("front")
    now[0] += 59
    m.motion("front")  # a new clip: the episode goes on, nothing new
    assert m.episodes.tick(now[0] + 59) == [] and m.episodes.tick(now[0] + 60) == ["front"]
    assert m.desired(m.settings())["zorcam/box/front/motion"] == (b"OFF", 0)


class Recorder:
    keepalive = 60

    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload, qos, retain))


def test_sync_sends_changes_only_and_clears_what_disappeared(tmp_path):
    cams = list(CAMS)
    m = make(tmp_path, cams)
    configure(m)
    rec, sent = Recorder(), {}
    m.sync(rec, m.settings(), sent)
    topics = [s[0] for s in rec.sent]
    assert len(topics) == len(set(topics)) == 2 * 3 + 1  # per camera: description, available, motion; the status
    assert all(s[3] for s in rec.sent)  # everything is retained
    assert topics.index("zorcam/box/status") > topics.index("homeassistant/binary_sensor/zorcam_box_front/config")
    assert topics.index("zorcam/box/status") < topics.index("zorcam/box/front/motion")
    rec.sent.clear()
    m.sync(rec, m.settings(), sent)
    assert rec.sent == []  # nothing changed, nothing sent
    m.motion("yard", 1000)
    m.sync(rec, m.settings(), sent)
    assert rec.sent == [("zorcam/box/yard/motion", b"ON", 0, True)]
    rec.sent.clear()
    cams[0] = dict(cams[0], name="Door", enabled=False)  # renamed and switched off
    m.sync(rec, m.settings(), sent)
    assert sorted(s[0] for s in rec.sent) == ["homeassistant/binary_sensor/zorcam_box_front/config", "zorcam/box/front/available"]
    rec.sent.clear()
    del cams[0]  # removed: empty retained descriptions
    m.sync(rec, m.settings(), sent)
    assert ("homeassistant/binary_sensor/zorcam_box_front/config", b"", 1, True) in rec.sent
    assert all(s[1] == b"" for s in rec.sent) and "zorcam/box/front/motion" not in sent
    rec.sent.clear()
    m.cfg["discovery"] = False
    m.sync(rec, m.settings(), sent)
    assert ("homeassistant/binary_sensor/zorcam_box_yard/config", b"", 1, True) in rec.sent
    assert all(not k.startswith("homeassistant") for k in sent)


# ---------------------------------------------------------------- a fake broker

class FakeBroker:
    """MQTT 3.1.1 broker for tests: CONNECT/PUBLISH/PINGREQ/DISCONNECT, keeps retained messages, publishes the last will of
    a connection that ended without DISCONNECT. Every received packet goes to self.events."""

    def __init__(self, connack=0, context=None, silent=False):
        self.connack, self.context, self.silent = connack, context, silent
        self.retained, self.events, self.conns = {}, [], []
        self.lock = threading.Lock()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock):
        will, clean = None, False
        try:
            if self.context:
                sock = self.context.wrap_socket(sock, server_side=True)
            sock.settimeout(10)
            with self.lock:
                self.conns.append(sock)
            if self.silent:
                time.sleep(3)
                return

            def read(n):
                data = b""
                while len(data) < n:
                    chunk = sock.recv(n - len(data))
                    if not chunk:
                        raise mqtt.MqttError("mqtt.err.closed")
                    data += chunk
                return data
            while True:
                ptype, flags, body = mqtt.read_packet(read)
                if ptype == mqtt.CONNECT:
                    will = self._connect(body)
                    sock.sendall(bytes([0x20, 2, 0, self.connack]))
                    if self.connack:  # refused: no session, no will
                        return
                elif ptype == mqtt.PUBLISH:
                    qos, retain = flags >> 1 & 3, bool(flags & 1)
                    n = struct.unpack("!H", body[:2])[0]
                    topic, rest = body[2:2 + n].decode(), body[2 + n:]
                    if qos:
                        pid, rest = rest[:2], rest[2:]
                        sock.sendall(b"\x40\x02" + pid)
                    self._publish(topic, rest, qos, retain)
                elif ptype == mqtt.PINGREQ:
                    self.events.append(("ping",))
                    sock.sendall(b"\xd0\x00")
                elif ptype == mqtt.DISCONNECT:
                    clean = True
                    self.events.append(("disconnect",))
                    return
        except (mqtt.MqttError, OSError):
            pass
        finally:
            if will and not clean:
                self._publish(will[0], will[1], will[2], will[3], will=True)
            try:
                sock.close()
            except OSError:
                pass

    def _connect(self, body):
        pos = 2 + struct.unpack("!H", body[:2])[0]  # "MQTT"
        level, flags, keepalive = body[pos], body[pos + 1], struct.unpack("!H", body[pos + 2:pos + 4])[0]
        pos += 4
        fields = []
        for _ in range(1 + 2 * bool(flags & 0x04) + bool(flags & 0x80) + bool(flags & 0x40)):
            n = struct.unpack("!H", body[pos:pos + 2])[0]
            fields.append(body[pos + 2:pos + 2 + n])
            pos += 2 + n
        info = {"level": level, "flags": flags, "keepalive": keepalive, "client_id": fields.pop(0).decode()}
        will = None
        if flags & 0x04:
            will = (fields.pop(0).decode(), fields.pop(0), flags >> 3 & 3, bool(flags & 0x20))
            info["will"] = will
        if flags & 0x80:
            info["username"] = fields.pop(0).decode()
        if flags & 0x40:
            info["password"] = fields.pop(0).decode()
        self.events.append(("connect", info))
        return will

    def _publish(self, topic, payload, qos, retain, will=False):
        with self.lock:
            self.events.append(("will" if will else "publish", topic, payload, qos, retain))
            if retain:
                if payload:
                    self.retained[topic] = payload
                else:
                    self.retained.pop(topic, None)

    def drop(self):
        """Break every connection without DISCONNECT (the power went off)."""
        with self.lock:
            conns, self.conns = self.conns, []
        for sock in conns:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def connects(self):
        return [e[1] for e in self.events if e[0] == "connect"]

    def published(self, topic):
        return [e[2] for e in list(self.events) if e[0] == "publish" and e[1] == topic]

    def close(self):
        self.server.close()
        self.drop()


def until(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def broker():
    b = FakeBroker()
    yield b
    b.close()


@pytest.fixture
def fast_retry(monkeypatch):
    monkeypatch.setattr(mqtt, "RETRY_MIN", 0.05)
    monkeypatch.setattr(mqtt, "IDLE", 0.1)  # how soon a broken connection is noticed


@pytest.fixture
def running(tmp_path, fast_retry):
    started = []

    def start(broker_port, cams=None, **fields):
        now = [1000.0]
        logs = []
        m = make(tmp_path, cams if cams is not None else list(CAMS), clock=lambda: now[0], log=logs.append)
        configure(m, port=str(broker_port), **fields)
        m.now, m.logs = now, logs
        threading.Thread(target=m.loop, daemon=True).start()
        started.append(m)
        return m
    yield start
    for m in started:
        m.stop()


def test_connects_with_will_and_login_and_publishes_everything(broker, running):
    m = running(broker.port)
    assert until(lambda: broker.retained.get("zorcam/box/status") == b"online")
    assert until(lambda: "zorcam/box/yard/motion" in broker.retained)
    info = broker.connects()[0]
    assert info["level"] == 4 and info["client_id"] == "zorcam-box" and info["username"] == "u" and info["password"] == "pw"
    assert info["will"] == ("zorcam/box/status", b"offline", 1, True) and info["flags"] & 0x02
    assert broker.retained["zorcam/box/front/motion"] == b"OFF"
    assert broker.retained["zorcam/box/front/available"] == b"online"
    cfg = json.loads(broker.retained["homeassistant/binary_sensor/zorcam_box_front/config"])
    assert cfg["name"] == "Front door" and cfg["device_class"] == "motion"
    st = m.status()
    assert st["state"] == "connected" and st["error"] is None and st["port"] == broker.port
    assert [x for x in m.logs if "connected" in x] == [f"mqtt: connected to 127.0.0.1:{broker.port}"]


def test_motion_on_and_off_after_the_hold(broker, running):
    m = running(broker.port)
    assert until(lambda: broker.retained.get("zorcam/box/front/motion") == b"OFF")
    m.motion("front")
    assert until(lambda: broker.retained.get("zorcam/box/front/motion") == b"ON")
    assert broker.retained["zorcam/box/yard/motion"] == b"OFF"
    m.now[0] += mqtt.episodes.HOLD - 1
    m.wake.set()
    time.sleep(0.2)
    assert broker.retained["zorcam/box/front/motion"] == b"ON"
    m.now[0] += 2
    m.wake.set()
    assert until(lambda: broker.retained.get("zorcam/box/front/motion") == b"OFF")
    assert broker.published("zorcam/box/front/motion") == [b"OFF", b"ON", b"OFF"]
    # QoS 0 for the motion, QoS 1 for the status and the descriptions
    qos = {e[1]: e[3] for e in broker.events if e[0] == "publish"}
    assert qos["zorcam/box/front/motion"] == 0 and qos["zorcam/box/status"] == 1
    assert qos["homeassistant/binary_sensor/zorcam_box_front/config"] == 1


def test_reconnects_after_a_break_and_sends_the_current_state_only(broker, running):
    m = running(broker.port)
    assert until(lambda: broker.retained.get("zorcam/box/status") == b"online")
    m.motion("front")
    assert until(lambda: broker.retained.get("zorcam/box/front/motion") == b"ON")
    broker.drop()
    assert until(lambda: broker.retained.get("zorcam/box/status") == b"offline")  # the last will
    assert until(lambda: len(broker.connects()) == 2 and broker.retained.get("zorcam/box/status") == b"online")
    assert until(lambda: broker.retained.get("zorcam/box/front/motion") == b"ON")
    # the same again, with the motion that ended while the broker was away: no old events, only the state now
    broker.connack = 3  # the broker is "unavailable" for a while
    broker.drop()
    assert until(lambda: m.status()["state"] == "error")
    broker.events.clear()
    m.now[0] += 100
    m.motion("yard")  # an episode that starts and ends while there is no connection
    m.now[0] += 100
    m.episodes.tick(m.now[0])
    m.motion("front")  # and one that is still going when the broker is back
    broker.connack = 0
    assert until(lambda: broker.retained.get("zorcam/box/status") == b"online" and broker.published("zorcam/box/yard/motion"))
    assert until(lambda: broker.retained.get("zorcam/box/front/motion") == b"ON")
    assert broker.published("zorcam/box/yard/motion") == [b"OFF"] and broker.published("zorcam/box/front/motion") == [b"ON"]
    # the journal has the changes of the connection only (two breaks), not a line per attempt
    assert [x.split(":")[1].split()[0] for x in m.logs] == ["connected", "no", "connected", "no", "connected"]


def test_removed_and_disabled_cameras(broker, running):
    cams = list(CAMS)
    m = running(broker.port, cams)
    assert until(lambda: "homeassistant/binary_sensor/zorcam_box_yard/config" in broker.retained)
    cams[1] = dict(cams[1], enabled=False)
    m.wake.set()
    assert until(lambda: broker.retained.get("zorcam/box/yard/available") == b"offline")
    assert "homeassistant/binary_sensor/zorcam_box_yard/config" in broker.retained  # still there, only unavailable
    del cams[1]
    m.wake.set()
    assert until(lambda: "homeassistant/binary_sensor/zorcam_box_yard/config" not in broker.retained)
    assert "zorcam/box/yard/available" not in broker.retained
    assert "homeassistant/binary_sensor/zorcam_box_front/config" in broker.retained
    cams.append({"id": "garage", "name": "Garage", "enabled": True})  # a new camera appears by itself
    m.wake.set()
    assert until(lambda: "homeassistant/binary_sensor/zorcam_box_garage/config" in broker.retained)
    assert json.loads(broker.retained["homeassistant/binary_sensor/zorcam_box_garage/config"])["name"] == "Garage"


def test_switching_discovery_off_removes_the_descriptions(broker, running):
    m = running(broker.port)
    assert until(lambda: "homeassistant/binary_sensor/zorcam_box_front/config" in broker.retained)
    configure(m, port=str(broker.port), discovery=None)
    m.wake.set()
    assert until(lambda: not [k for k in broker.retained if k.startswith("homeassistant/")])
    assert broker.retained["zorcam/box/front/motion"] == b"OFF" and broker.retained["zorcam/box/status"] == b"online"
    assert len(broker.connects()) == 1  # the same connection


def test_switching_mqtt_off_says_offline_and_disconnects_cleanly(broker, running):
    m = running(broker.port)
    assert until(lambda: broker.retained.get("zorcam/box/status") == b"online")
    cfg = dict(m.cfg, enabled=False)
    m.save(cfg)
    assert until(lambda: broker.retained.get("zorcam/box/status") == b"offline")
    assert until(lambda: ("disconnect",) in broker.events)
    assert not [e for e in broker.events if e[0] == "will"]  # a clean disconnect: the broker has no will to send
    assert until(lambda: m.status()["state"] == "off")
    assert "homeassistant/binary_sensor/zorcam_box_front/config" in broker.retained  # sensors only become unavailable


def test_renaming_the_device_clears_the_old_topics(broker, running):
    m = running(broker.port)
    assert until(lambda: "homeassistant/binary_sensor/zorcam_box_front/config" in broker.retained)
    configure(m, port=str(broker.port), device="garage")
    assert until(lambda: "homeassistant/binary_sensor/zorcam_garage_front/config" in broker.retained
                 and broker.retained.get("zorcam/garage/status") == b"online")
    assert broker.retained.get("zorcam/box/status") == b"offline"
    assert "homeassistant/binary_sensor/zorcam_box_front/config" not in broker.retained
    assert not [k for k in broker.retained if k.startswith("zorcam/box/") and k != "zorcam/box/status"]


def test_wrong_credentials_are_shown_and_logged_once(tmp_path, running):
    b = FakeBroker(connack=4)
    try:
        m = running(b.port)
        assert until(lambda: len(b.connects()) >= 4)  # it keeps trying
        st = m.status()
        assert st["state"] == "error" and st["error"] == Msg("mqtt.err.credentials")
        assert len([x for x in m.logs if "no connection" in x]) == 1 and "wrong login or password" in m.logs[0]
        # the next attempt after the broker has been fixed
        b.connack = 0
        assert until(lambda: m.status()["state"] == "connected")
        assert m.status()["error"] is None
    finally:
        b.close()


def test_backoff_doubles_up_to_five_minutes_and_a_save_retries_at_once(tmp_path, monkeypatch):
    waits = []

    class Dead:
        def __init__(self, *a, **kw):
            pass

        def connect(self):
            raise mqtt.MqttError("mqtt.err.connect", error="Connection refused")
    m = make(tmp_path, client_class=Dead)
    configure(m)
    real_wait = m.changed.wait

    def fake_wait(timeout=None):
        waits.append(timeout)
        if len(waits) == 9:
            m.stop()
        return real_wait(0)
    m.changed.wait = fake_wait
    m.loop()
    assert waits[:9] == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert m.status()["error"] == Msg("mqtt.err.connect", error="Connection refused")


def test_the_loop_survives_an_unexpected_exception(tmp_path, fast_retry):
    class Broken:
        calls = 0

        def __init__(self, *a, **kw):
            Broken.calls += 1

        def connect(self):
            raise ZeroDivisionError("boom")
    m = make(tmp_path, client_class=Broken)
    configure(m)
    th = threading.Thread(target=m.loop, daemon=True)
    th.start()
    assert until(lambda: Broken.calls >= 3)
    assert th.is_alive() and m.status()["error"] == Msg("mqtt.err.internal", error="ZeroDivisionError: boom")
    m.stop()
    th.join(5)
    assert not th.is_alive()


def test_silent_broker_times_out():
    b = FakeBroker(silent=True)
    try:
        c = mqtt.Client("127.0.0.1", b.port, timeout=0.3)
        with pytest.raises(mqtt.MqttError) as e:
            c.connect()
        assert e.value.msg == Msg("mqtt.err.timeout") and c.sock is None
    finally:
        b.close()


def test_refused_port_and_a_broker_that_is_not_mqtt():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(mqtt.MqttError) as e:
        mqtt.Client("127.0.0.1", port, timeout=1).connect()
    assert e.value.msg.key == "mqtt.err.connect"
    junk = socket.socket()
    junk.bind(("127.0.0.1", 0))
    junk.listen(1)

    def serve():
        conn, _ = junk.accept()
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n" + b"x" * 200)
        time.sleep(1)
        conn.close()
    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(mqtt.MqttError) as e:
        mqtt.Client("127.0.0.1", junk.getsockname()[1], timeout=1).connect()
    assert e.value.msg.key in ("mqtt.err.protocol", "mqtt.err.refused")
    junk.close()


def test_keepalive_ping_and_closed_connection_is_noticed(broker):
    c = mqtt.Client("127.0.0.1", broker.port, keepalive=2, timeout=2)
    c.connect()
    assert not c.ping_due()
    time.sleep(1.1)
    assert c.ping_due()
    c.ping()
    assert ("ping",) in broker.events and not c.ping_due()
    c.poll()  # nothing to read
    c.publish("a/b", b"x", 1, True)
    assert broker.retained["a/b"] == b"x"
    broker.drop()
    time.sleep(0.1)
    with pytest.raises(mqtt.MqttError):
        c.poll()
    c.close(clean=False)


# ---------------------------------------------------------------- TLS

def self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import ipaddress
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "broker")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256()))
    crt, pem = tmp_path / "broker.crt", tmp_path / "broker.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pem.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    return str(crt), str(pem)


def test_tls_checks_the_certificate(tmp_path, monkeypatch):
    crt, key = self_signed(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(crt, key)
    b = FakeBroker(context=server_ctx)
    try:
        # a self-signed certificate is refused by default, with a reason a person can read
        with pytest.raises(mqtt.MqttError) as e:
            mqtt.Client("127.0.0.1", b.port, tls=True, timeout=3).connect()
        assert e.value.msg.key == "mqtt.err.tls" and "self-signed" in str(e.value)
        # the owner's own certificate in the trusted ones: works
        trusted = ssl.create_default_context(cafile=crt)
        monkeypatch.setattr(mqtt, "tls_context", lambda: trusted)
        c = mqtt.Client("127.0.0.1", b.port, tls=True, timeout=3)
        c.connect()
        c.publish("a/b", b"tls", 1, True)
        assert b.retained["a/b"] == b"tls"
        c.poll()
        c.close()
    finally:
        b.close()
