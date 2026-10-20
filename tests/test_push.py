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
        sub = {"endpoint": f"https://web.push.apple.com/{n}", "keys": {"p256dh": key, "auth": "AAAAAAAAAAAAAAAAAAAAAA"}}
        assert service.add(sub, user="u", lang=lang)
    sent = {}
    monkeypatch.setattr(service, "send_one", lambda endpoint, keys, message: (sent.update({endpoint: message}), (201, ""))[1])
    assert service.send_all(lambda sub: {"title": sub.get("lang") or "legacy"}) == (3, 3)  # (delivered, targets)
    assert sent == {"https://web.push.apple.com/1": {"title": "en"}, "https://web.push.apple.com/2": {"title": "ru"},
                    "https://web.push.apple.com/3": {"title": "legacy"}}
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
    monkeypatch.setattr(push, "allowed_endpoint", lambda endpoint: True)  # the fake service is on localhost
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
    sub = {"endpoint": "https://web.push.apple.com/1", "keys": {"p256dh": key, "auth": "AAAAAAAAAAAAAAAAAAAAAA"}}
    assert service.add(sub, "iPhone", "bob", "en") and len(saves) == 1
    created = service.subscriptions[sub["endpoint"]]["created"]
    assert service.add(sub, "iPhone", "bob", "en") and len(saves) == 1
    monkeypatch.setattr(push.time, "time", lambda: created + 100)
    assert service.add(sub, "iPhone", "bob", "en", "ru") and len(saves) == 2
    assert service.subscriptions[sub["endpoint"]]["pick"] == "ru" and service.subscriptions[sub["endpoint"]]["created"] == created


def test_only_known_push_services_are_accepted():
    ok = ("https://fcm.googleapis.com/fcm/send/abc", "https://web.push.apple.com/QW", "https://WEB.PUSH.APPLE.COM/x",
          "https://updates.push.services.mozilla.com/wpush/v2/abc", "https://wns2-par02p.notify.windows.com/w/?token=x",
          "https://fcm.googleapis.com:443/x")
    bad = ("http://fcm.googleapis.com/x", "https://192.168.1.10/x", "https://127.0.0.1/x", "https://evil.example/x",
           "https://fcm.googleapis.com.evil.example/x", "https://evil.example/?h=fcm.googleapis.com",
           "https://user@fcm.googleapis.com/x", "https://fcm.googleapis.com@evil.example/x",
           "https://evilpush.apple.com/x", "https://push.apple.com/x", "https://fcm.googleapis.com:8443/x",
           "https://web.push.apple.com.evil.example/x", "", None, 5)
    for endpoint in ok:
        assert push.allowed_endpoint(endpoint), endpoint
    for endpoint in bad:
        assert not push.allowed_endpoint(endpoint), endpoint


def test_a_subscription_for_another_host_is_refused_dropped_and_never_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "VAPID_KEY_FILE", str(tmp_path / "vapid.pem"))
    monkeypatch.setattr(push, "SUBSCRIPTIONS_FILE", str(tmp_path / "subs.json"))
    key = push.b64url(push._raw_public(ec.generate_private_key(ec.SECP256R1())))
    keys = {"p256dh": key, "auth": "AAAAAAAAAAAAAAAAAAAAAA"}
    service = push.PushService()
    assert not service.add({"endpoint": "https://192.168.1.10/x", "keys": keys})
    # a file written by an older version: the foreign host goes away when the service starts
    good = "https://fcm.googleapis.com/fcm/send/a"
    (tmp_path / "subs.json").write_text(json.dumps({good: {"keys": keys}, "https://192.168.1.10/x": {"keys": keys}}))
    service = push.PushService()
    assert list(service.subscriptions) == [good]
    assert list(json.loads((tmp_path / "subs.json").read_text())) == [good]
    # slipped in afterwards: not sent, removed at the first send
    service.subscriptions["https://evil.example/x"] = {"keys": keys, "label": "x"}
    sent, logged = [], []
    monkeypatch.setattr(service, "send_one", lambda *a, **k: (sent.append(a), (201, ""))[1])
    assert service.send_all({"title": "x"}, log=logged.append) == (1, 2)
    assert [a[0] for a in sent] == [good] and "https://evil.example/x" not in service.subscriptions
    assert any("evil.example removed: not a known push service" in line for line in logged)


def test_the_push_service_cannot_redirect_the_request(monkeypatch):
    """A 3xx from the push service is an answer, not a new address to send the notification to."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    hits = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/start":
                self.send_response(307)
                self.send_header("Location", "/inner")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    key = push.b64url(push._raw_public(ec.generate_private_key(ec.SECP256R1())))
    service = push.PushService.__new__(push.PushService)
    service.key = ec.generate_private_key(ec.SECP256R1())
    service.public_key = push.b64url(push._raw_public(service.key))
    status, _ = service.send_one(f"http://127.0.0.1:{srv.server_address[1]}/start",
                                 {"p256dh": key, "auth": "AAAAAAAAAAAAAAAAAAAAAA"}, {"title": "x"})
    srv.shutdown()
    assert status == 307 and hits == ["/start"]
