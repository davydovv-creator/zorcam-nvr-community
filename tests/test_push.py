"""Web Push encryption (RFC 8291, aes128gcm): decrypt the way a browser does."""
import json
import struct

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import push


def test_encrypt_decrypts_like_a_browser():
    ua_key = ec.generate_private_key(ec.SECP256R1())
    ua_public = push._raw_public(ua_key)
    auth = b"0123456789abcdef"
    payload = '{"title": "Motion \u2014 \u0414\u0432\u0438\u0436\u0435\u043d\u0438\u0435", "url": "/?tab=live"}'.encode()

    body = push.encrypt(payload, push.b64url(ua_public), push.b64url(auth))

    salt = body[:16]
    record_size, key_len = struct.unpack(">IB", body[16:21])
    as_public = body[21:21 + key_len]
    assert record_size == push.RECORD_SIZE and key_len == 65
    as_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    ecdh_secret = ua_key.exchange(ec.ECDH(), as_key)
    ikm = push._hkdf(auth, ecdh_secret, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = push._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = push._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, body[21 + key_len:], None)
    assert plain == payload + b"\x02"  # the last record: delimiter 0x02


def test_b64url_roundtrip():
    for data in (b"", b"\x00", b"\xff" * 33):
        assert push.b64url_decode(push.b64url(data)) == data


def test_send_all_uses_the_language_of_each_subscription(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "VAPID_KEY_FILE", str(tmp_path / "vapid.pem"))
    monkeypatch.setattr(push, "SUBSCRIPTIONS_FILE", str(tmp_path / "subs.json"))
    service = push.PushService()
    key = push.b64url(push._raw_public(ec.generate_private_key(ec.SECP256R1())))
    for n, lang in ((1, "en"), (2, "ru"), (3, "")):
        assert service.add({"endpoint": f"https://push.example/{n}", "keys": {"p256dh": key, "auth": "AAAAAAAAAAAAAAAAAAAAAA"}},
                           user="u", lang=lang)
    sent = {}
    monkeypatch.setattr(service, "send_one", lambda endpoint, keys, message: (sent.update({endpoint: message}), (201, ""))[1])
    assert service.send_all(lambda sub: {"title": sub.get("lang") or "legacy"}) == (3, 3)  # (delivered, targets)
    assert sent == {"https://push.example/1": {"title": "en"}, "https://push.example/2": {"title": "ru"},
                    "https://push.example/3": {"title": "legacy"}}
    assert service.send_all({"title": "x"}, users={"someone else"}) == (0, 0)


def test_a_subscription_of_another_key_is_dropped(tmp_path, monkeypatch):
    """Apple answers 400 VapidPkHashMismatch to a subscription made with the key of another install (a new SD card):
    it is removed, not retried at every motion; any other refusal is only logged with its reason."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Apple(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"reason": "VapidPkHashMismatch" if self.path == "/old" else "BadJwtToken"}).encode()
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Apple)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(push, "VAPID_KEY_FILE", str(tmp_path / "vapid.pem"))
    monkeypatch.setattr(push, "SUBSCRIPTIONS_FILE", str(tmp_path / "subs.json"))
    service = push.PushService()
    key = push.b64url(push._raw_public(ec.generate_private_key(ec.SECP256R1())))
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    for path in ("/old", "/other"):  # http only in this test: add() takes https endpoints
        service.subscriptions[base + path] = {"keys": {"p256dh": key, "auth": "AAAAAAAAAAAAAAAAAAAAAA"}, "label": path}
    logged = []
    assert service.send_all({"title": "x"}, log=logged.append) == (0, 2)
    srv.shutdown()
    assert service.count() == 1 and list(service.subscriptions) == [base + "/other"]
    assert any("gone (400 VapidPkHashMismatch), removed" in line for line in logged)
    assert any("HTTP 400 BadJwtToken from 127.0.0.1" in line for line in logged)


def test_subscribing_again_writes_only_changes(tmp_path, monkeypatch):
    """The app subscribes again on every page open: an unchanged subscription is not written to the SD card again;
    a change (the language picked on the device) is, and the subscription keeps its creation time."""
    monkeypatch.setattr(push, "VAPID_KEY_FILE", str(tmp_path / "vapid.pem"))
    monkeypatch.setattr(push, "SUBSCRIPTIONS_FILE", str(tmp_path / "subs.json"))
    service = push.PushService()
    saves = []
    real_save = service._save
    monkeypatch.setattr(service, "_save", lambda: (saves.append(1), real_save()))
    key = push.b64url(push._raw_public(ec.generate_private_key(ec.SECP256R1())))
    sub = {"endpoint": "https://push.example/1", "keys": {"p256dh": key, "auth": "AAAAAAAAAAAAAAAAAAAAAA"}}
    assert service.add(sub, "iPhone", "bob", "en") and len(saves) == 1
    created = service.subscriptions[sub["endpoint"]]["created"]
    assert service.add(sub, "iPhone", "bob", "en") and len(saves) == 1
    monkeypatch.setattr(push.time, "time", lambda: created + 100)
    assert service.add(sub, "iPhone", "bob", "en", "ru") and len(saves) == 2
    assert service.subscriptions[sub["endpoint"]]["pick"] == "ru" and service.subscriptions[sub["endpoint"]]["created"] == created
