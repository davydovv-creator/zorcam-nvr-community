"""app/common/zorcam_cloud.py and the gallery side of the Zorcam cloud: pairing against a fake cloud, the tunnel
settings, sign-in by the cloud's identity token, motion events."""
import base64
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

import app  # noqa: F401
import zorcam_cloud
import zorcam_config
import zorcam_users
from zorcam_i18n import t
from test_gallery import assets, cookies, login, motion_headers, request, server, tunnel  # noqa: F401  (fixtures)

CLOUD_KEY = Ed25519PrivateKey.generate()


def b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def raw_public(key):
    return b64e(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def identity(device_id, user, exp_in=60, key=CLOUD_KEY):
    payload = b64e(json.dumps({"aud": device_id, "user": user, "acct": "a", "iat": 0,
                               "exp": time.time() + exp_in}).encode())
    return f"v1.{payload}.{b64e(key.sign(f'v1.{payload}'.encode()))}"


class FakeCloud(BaseHTTPRequestHandler):
    paired = False
    calls = []
    device_key = None
    owner = "Anna K."  # None: an older cloud that does not send owner_name
    address = {}  # replaces/adds fields of the status reply; a None value drops the field

    def log_message(self, *a):
        pass

    def reply(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def signed_ok(self, body):
        import hashlib
        msg = (f"{self.command}\n{self.path}\n{self.headers['X-Zorcam-Time']}\n"
               f"{hashlib.sha256(body).hexdigest()}").encode()
        try:
            Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(FakeCloud.device_key + "==")).verify(
                base64.urlsafe_b64decode(self.headers["X-Zorcam-Signature"] + "=="), msg)
            return True
        except Exception:
            return False

    def do_GET(self):
        FakeCloud.calls.append(("GET", self.path, self.signed_ok(b"")))
        if FakeCloud.paired:
            reply = {"paired": True, "name": "Dacha", "url": "https://dev1234567.zorcam.test",
                     "app_url": "https://cloud.zorcam.test", "frps_addr": "tunnel.zorcam.test", "frps_port": 443,
                     "subdomain": "dev1234567", "cloud_key": raw_public(CLOUD_KEY),
                     **({"owner_name": FakeCloud.owner} if FakeCloud.owner else {}), **FakeCloud.address}
            self.reply({k: v for k, v in reply.items() if v is not None})
        else:
            self.reply({"paired": False})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/pair/start":
            FakeCloud.device_key = json.loads(body)["public_key"]
            return self.reply({"device_id": "dev1234567", "code": "ABCD-EFGH", "expires_in": 600,
                               "pair_url": "https://cloud.zorcam.test/pair?code=ABCDEFGH"})
        FakeCloud.calls.append(("POST", self.path, self.signed_ok(body), json.loads(body or b"{}")))
        if self.path == "/api/device/token":
            return self.reply({"token": "tunnel-token"})
        self.reply({"ok": True})


@pytest.fixture
def fake_cloud():
    FakeCloud.paired, FakeCloud.calls, FakeCloud.owner, FakeCloud.address = False, [], "Anna K.", {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCloud)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    zorcam_cloud.remove()


def test_pairing_tunnel_and_unlink(fake_cloud, monkeypatch):
    monkeypatch.setattr(zorcam_cloud, "URL_RE", __import__("re").compile(r"https?://.+"))  # http only in the test
    assert zorcam_cloud.state() == "none"
    ok, why = zorcam_cloud.start_pairing(fake_cloud, "pi", "2.6.0")
    assert ok and zorcam_cloud.state() == "pending" and zorcam_cloud.load()["code"] == "ABCD-EFGH"
    import os
    assert os.stat(zorcam_cloud.CLOUD_FILE).st_mode & 0o777 == 0o600
    assert zorcam_cloud.finish_pairing() == "pending"  # the owner has not entered the code yet
    FakeCloud.paired = True
    assert zorcam_cloud.finish_pairing("2.6.0") == "paired"
    assert all(c[2] for c in FakeCloud.calls)  # every Pi request is signed with its key
    data = zorcam_cloud.load()
    assert data["token"] == "tunnel-token" and "code" not in data
    conf = zorcam_cloud.frpc_config(data)
    for line in ('serverAddr = "tunnel.zorcam.test"', "serverPort = 443", 'transport.protocol = "wss"',
                 'metadatas.device = "dev1234567"', 'metadatas.token = "tunnel-token"', 'subdomain = "dev1234567"',
                 f"localPort = {zorcam_cloud.TUNNEL_PORT}"):
        assert line in conf, line
    assert zorcam_cloud.TUNNEL_PORT == 8081 and "localPort = 80\n" not in conf  # not the port of the home network
    # identity: the cloud's signature, this device, not expired
    assert zorcam_cloud.verify_identity(identity("dev1234567", "admin")) == "admin"
    assert zorcam_cloud.verify_identity(identity("other12345", "admin")) is None
    assert zorcam_cloud.verify_identity(identity("dev1234567", "admin", exp_in=-1)) is None
    assert zorcam_cloud.verify_identity(identity("dev1234567", "admin", key=Ed25519PrivateKey.generate())) is None
    assert zorcam_cloud.send_event("front", "Front", "x", ["admin"]) == 200
    assert FakeCloud.calls[-1][3]["viewers"] == ["admin"]
    assert zorcam_cloud.send_health({"version": "2.6.0", "cameras": []}) == 200  # the hourly report, signed
    assert FakeCloud.calls[-1][:3] == ("POST", "/api/device/health", True)
    assert zorcam_cloud.heartbeat()
    FakeCloud.paired = False
    assert not zorcam_cloud.heartbeat()  # unlinked in the app
    zorcam_cloud.unlink()
    assert zorcam_cloud.state() == "none" and FakeCloud.calls[-1][1] == "/api/device/unlink"


def pair_locally():
    with zorcam_cloud.locked():
        zorcam_cloud.save({"server": "https://cloud.zorcam.test", "device_id": "dev1234567", "paired": True,
                          "cloud_key": raw_public(CLOUD_KEY), "private_key": b64e(b"\0" * 32), "url": "https://x"})


def test_gallery_sign_in_through_the_cloud(server, tunnel):  # noqa: F811
    pair_locally()
    try:
        # admin through the tunnel: no password, a normal gallery session for admin
        status, headers, body = request(tunnel, "GET", "/settings",
                                        headers={"X-Zorcam-Identity": identity("dev1234567", "admin")})
        assert status == 200 and "zorcam_session=" in headers["Set-Cookie"] and 'action="/cloud/disconnect"' in body
        assert "; Secure" in headers["Set-Cookie"]  # the cloud is HTTPS only: the cookie never goes over HTTP
        cookie = headers["Set-Cookie"].split(";")[0]
        # the same cookie with a fresh token: no new session
        status, headers, _ = request(tunnel, "GET", "/", cookie=cookie,
                                     headers={"X-Zorcam-Identity": identity("dev1234567", "admin")})
        assert status == 200 and "Set-Cookie" not in headers
        # a viewer gets the viewer's rights, not the cookie's admin
        status, _, body = request(tunnel, "GET", "/config", cookie=cookie,
                                  headers={"X-Zorcam-Identity": identity("dev1234567", "bob")})
        assert status == 403
        # forged, foreign or for a missing user: the usual sign-in page
        for tok in (identity("dev1234567", "admin", key=Ed25519PrivateKey.generate()),
                    identity("other12345", "admin"), identity("dev1234567", "nobody"), "garbage"):
            status, headers, _ = request(tunnel, "GET", "/", headers={"X-Zorcam-Identity": tok})
            assert status == 302 and headers["Location"] == "/login", tok
    finally:
        zorcam_cloud.remove()


def test_admin_through_the_cloud_is_asked_to_change_the_installer_password(server, tunnel):  # noqa: F811
    """The mark of the installer's password leads admin to "Change the password" after signing in through the cloud
    too (the main page decides, not the sign-in form); another user of the cloud is not asked."""
    pair_locally()
    users = zorcam_users.load()
    users["admin"]["initial"] = True
    zorcam_users.save(users)
    try:
        status, headers, _ = request(tunnel, "GET", "/", headers={"X-Zorcam-Identity": identity("dev1234567", "admin")})
        assert status == 303 and headers["Location"] == "/setup/password"
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, body = request(tunnel, "GET", "/setup/password", cookie=cookie,
                                  headers={"X-Zorcam-Identity": identity("dev1234567", "admin")})
        assert status == 200 and t("en", "pw.title") in body
        assert request(tunnel, "GET", "/", headers={"X-Zorcam-Identity": identity("dev1234567", "bob")})[0] == 200
    finally:
        zorcam_users.clear_initial("admin")
        zorcam_cloud.remove()


def test_header_brand_shows_the_viewing_mode(server, tunnel, cookies):  # noqa: F811
    from zorcam_i18n import STRINGS
    for key in ("brand.nvr", "brand.cloud", "brand.tip_nvr", "brand.tip_cloud"):
        assert STRINGS[key]["en"] and STRINGS[key]["ru"]
    # at home: Zorcam.nvr and the device icon on the main page, in Settings and in the sidebar
    for path in ("/", "/settings"):
        body = request(server, "GET", path, cookie=cookies["admin"])[2]
        assert "Zorcam.nvr" in body and 'data-view="nvr"' in body and "Zorcam.cloud" not in body
    body = request(server, "GET", "/", cookie=cookies["admin"] + "; zorcam_lang=ru")[2]
    assert STRINGS["brand.nvr"]["ru"] in body and STRINGS["brand.tip_nvr"]["ru"] in body
    pair_locally()
    try:
        for path in ("/", "/settings"):
            body = request(tunnel, "GET", path, headers={"X-Zorcam-Identity": identity("dev1234567", "admin")})[2]
            assert "Zorcam.cloud" in body and 'data-view="cloud"' in body and "Zorcam.nvr" not in body
    finally:
        zorcam_cloud.remove()


def test_yandex_admin_page_through_the_cloud_sign_in(server, tunnel):  # noqa: F811
    """The cloud lets only the addresses for Yandex pass without its own sign-in; /yandex and its forms come with
    X-Zorcam-Identity: admin works there (CSRF of the cloud session), a viewer does not."""
    import app
    from urllib.parse import urlencode
    pair_locally()
    ya = app.YANDEX
    before = json.loads(json.dumps(ya.data))
    admin = {"X-Zorcam-Identity": identity("dev1234567", "admin")}
    try:
        status, headers, page = request(tunnel, "GET", "/yandex", headers=admin)
        assert status == 200 and 'action="/yandex/save"' in page and "https://x/yandex/token" in page
        cookie = headers["Set-Cookie"].split(";")[0]
        csrf = re.search(r'action="/yandex/save"><input type="hidden" name="csrf" value="([0-9a-f]+)"', page).group(1)
        assert request(tunnel, "GET", "/yandex", headers={"X-Zorcam-Identity": identity("dev1234567", "bob")})[0] == 403
        status, headers, _ = request(tunnel, "POST", "/yandex/save", urlencode({"csrf": csrf, "skill_id": "s-1"}),
                                     cookie=cookie, headers=admin)
        assert status == 303 and headers["Location"] == "/yandex?saved=1" and ya.status()["skill_id"] == "s-1"
        assert request(tunnel, "POST", "/yandex/save", urlencode({"csrf": "bad", "skill_id": "s-2"}),
                       cookie=cookie, headers=admin)[0] == 403
        status, headers, _ = request(tunnel, "POST", "/yandex/unlink", urlencode({"csrf": csrf}),
                                     cookie=cookie, headers=admin)
        assert status == 303 and headers["Location"] == "/yandex"
        assert ya.status()["skill_id"] == "s-1"
    finally:
        zorcam_cloud.remove()
        with ya.lock:
            ya.data = before
            ya._save()


def test_cloud_sign_in_does_not_make_a_session_per_request(server, tunnel):  # noqa: F811
    """The cloud asks without a cookie (camera list, posters): one gallery session per user, not one per request."""
    import app
    pair_locally()
    try:
        counts = []
        for _ in range(3):
            assert request(tunnel, "GET", "/cloud/v1/cameras",
                           headers={"X-Zorcam-Identity": identity("dev1234567", "bob")})[0] == 200
            counts.append(len(app.SESSIONS))
        assert counts[0] == counts[1] == counts[2]
        # the tunnel cannot reach the detector's endpoint, not even with its secret
        assert request(tunnel, "POST", "/internal/motion", "{}", headers={
            "X-Zorcam-Identity": identity("dev1234567", "admin"), "Content-Type": "application/json"})[0] == 403
        assert request(tunnel, "POST", "/internal/motion", "{}", headers=motion_headers())[0] == 403
    finally:
        zorcam_cloud.remove()


def test_connect_errors_are_translated(server, cookies):  # noqa: F811
    import re as _re
    page = request(server, "GET", "/settings", cookie=cookies["admin"] + "; zorcam_lang=ru")[2]
    csrf = _re.search(r'action="/cloud/connect"><input type="hidden" name="csrf" value="([0-9a-f]+)"', page).group(1)
    from urllib.parse import urlencode
    status, _, body = request(server, "POST", "/cloud/connect", urlencode({"csrf": csrf, "server": "ftp://x"}),
                              cookie=cookies["admin"] + "; zorcam_lang=ru")
    from zorcam_i18n import t
    assert status == 502 and t("ru", "cloud.why.address") in body


def test_identity_is_trusted_only_on_the_tunnel_port(server, tunnel, cookies):  # noqa: F811
    """A reverse proxy on the device (HTTPS by DDNS) also comes from 127.0.0.1, but to port 80: the cloud's headers
    mean nothing there, and the setup wizard is closed on the tunnel in any case."""
    import app
    pair_locally()
    try:
        via = {"X-Zorcam-Identity": identity("dev1234567", "admin")}
        assert request(tunnel, "GET", "/settings", headers=via)[0] == 200
        status, headers, _ = request(server, "GET", "/settings", headers=via)
        assert status == 302 and headers["Location"] == "/login" and "Set-Cookie" not in headers
        assert request(server, "GET", "/cloud/v1/cameras", headers=via)[0] in (302, 401, 403)
        body = request(server, "GET", "/", cookie=cookies["admin"], headers=via)[2]
        assert "Zorcam.nvr" in body and "Zorcam.cloud" not in body  # not shown as the cloud
        # no users yet: the wizard is for the home network, and the tunnel is not it
        users = zorcam_users.load()
        saved = app.setup_needed
        app.setup_needed = lambda: True
        try:
            assert request(server, "GET", "/setup")[0] == 200
            assert request(tunnel, "GET", "/setup")[0] == 403
            assert request(tunnel, "GET", "/setup", headers={"X-Forwarded-For": "192.168.1.5"})[0] == 403
            assert request(tunnel, "POST", "/setup", "csrf=x")[0] == 403
            assert request(tunnel, "POST", "/setup/users", "csrf=x")[0] == 403
        finally:
            app.setup_needed = saved
        assert zorcam_users.load() == users
    finally:
        zorcam_cloud.remove()


def test_not_paired_ignores_the_header(server, tunnel):  # noqa: F811
    status, headers, _ = request(tunnel, "GET", "/", headers={"X-Zorcam-Identity": identity("dev1234567", "admin")})
    assert status == 302 and headers["Location"] == "/login"


def test_cloud_camera_list_follows_the_user(server, cookies):  # noqa: F811
    zorcam_users.set_cameras("guest", {"yard"})
    try:
        cams = json.loads(request(server, "GET", "/cloud/v1/cameras", cookie=cookies["guest"])[2])["cameras"]
        assert [c["id"] for c in cams] == ["yard"]
        assert request(server, "GET", "/cloud/v1/poster/front", cookie=cookies["guest"])[0] == 403  # no snapshots
        assert request(server, "GET", "/cloud/v1/poster/front", cookie=cookies["admin"])[0] == 200
    finally:
        zorcam_users.set_cameras("guest", {"front", "yard"})


def test_installed_folders_import(tmp_path):
    """Each /opt/zorcam-nvr/<service> folder gets only what install.sh copies there: every program must start from it
    (zorcam-nvr-cloud once failed: zorcam_config needs zorcam_i18n, which was not copied)."""
    import shutil
    import subprocess
    import sys
    repo = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
    install = open(f"{repo}/app/install.sh").read()
    folders = {}
    for src, dst in re.findall(r'^copy "\$SRC/([\w/]+\.py)"\s+"\$PREFIX/(\w+)/[\w.]+"', install, re.M):
        folders.setdefault(dst, []).append(src)
    assert set(folders) >= {"capture", "gallery", "cloud"}
    for folder, files in folders.items():
        target = tmp_path / folder
        target.mkdir()
        for src in files:
            shutil.copy(f"{repo}/app/{src}", target)
        if folder == "gallery":  # the page markup, copied by loops in install.sh
            for sub in ("templates", "static"):
                assert f'"$SRC"/gallery/{sub}/*' in install, sub
                shutil.copytree(f"{repo}/app/gallery/{sub}", target / sub)
        for src in files:
            if src.startswith(("capture/", "gallery/", "cloud/")) and src.endswith(("daemon.py", "app.py")):
                module = src.rsplit("/", 1)[1][:-3]
                # import only: a module under its own name does not run main()
                code = f"import sys; sys.path.insert(0, {str(target)!r}); import {module}"
                p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                                   env={"ZORCAM_STATE_DIR": str(tmp_path / "state"), "PATH": "/usr/bin:/bin",
                                        "HOME": str(tmp_path)})
                assert p.returncode == 0, f"{folder}/{module}: {p.stderr[-500:]}"


def test_health_report(tmp_path, monkeypatch):
    """The hourly report for the cloud's admin page: numbers, states and the gallery's checks — no camera names."""
    import zorcam_config
    cams = [{"id": "front", "name": "Secret name"}, {"id": "yard", "name": "Yard"}]
    monkeypatch.setattr(zorcam_config, "load_cameras", lambda path=None: cams)
    status = {"front": tmp_path / "front.json", "yard": tmp_path / "missing.json"}
    monkeypatch.setattr(zorcam_config, "capture_status_file", lambda cam_id: str(status[cam_id]))
    net = {"via": "wifi", "wifi": True, "bluetooth": False}
    monkeypatch.setattr(zorcam_config, "network_state", lambda *a, **k: net)
    now = time.time()
    status["front"].write_text(json.dumps({"last_frame": now - 4, "reconnects": [now - 90000, now - 60, now - 30]}))
    cg = tmp_path / "cg" / "zorcam-nvr-capture.service"
    cg.mkdir(parents=True)
    (cg / "memory.events").write_text("low 0\nhigh 12\nmax 0\noom 0\noom_kill 1\n")
    summary = tmp_path / "health.json"
    summary.write_text(json.dumps({"checked": now, "checks": {"disk": {"ok": True},
                                                              "frames:yard": {"ok": False, "text": "no frames yet"}}}))
    r = zorcam_cloud.health_report("2.6.0", now, cgroups=str(tmp_path / "cg"), health_file=str(summary))
    assert r["version"] == "2.6.0" and r["edition"] == "community"
    assert r["memory_events"] == {"capture": {"high": 12, "max": 0, "oom_kill": 1}}
    assert r["cameras"] == [{"id": "front", "frame_age": 4, "reconnects": 2},
                            {"id": "yard", "frame_age": None, "reconnects": 0}]
    assert r["checks"]["frames:yard"] == {"ok": False, "text": "no frames yet"} and "Secret name" not in json.dumps(r)
    assert "disk" in r and "mem" in r  # from this machine
    assert r["network"] == net  # how it is connected and the radios: no network name, addresses or interface names


def test_the_cloud_admin_asks_for_the_health_now(server, tunnel):  # noqa: F811
    pair_locally()
    try:
        status, _, body = request(tunnel, "GET", "/cloud/v1/health",
                                  headers={"X-Zorcam-Identity": identity("dev1234567", "admin")})
        report = json.loads(body)
        assert status == 200 and report["version"] and report["edition"] == "community" and report["ts"]
        assert set(report["network"]) == {"via", "wifi", "bluetooth"}
        assert request(tunnel, "GET", "/cloud/v1/health",
                       headers={"X-Zorcam-Identity": identity("dev1234567", "bob")})[0] == 403
    finally:
        zorcam_cloud.remove()


def test_gallery_writes_the_check_summary(tmp_path, monkeypatch):
    import app
    from zorcam_i18n import Msg
    monkeypatch.setattr(zorcam_cloud, "HEALTH_FILE", str(tmp_path / "health.json"))
    h = app.Health()
    h.problems["temp"] = {"since": 100.0, "notified": 100.0}
    h.write_summary({"disk": (True, Msg("health.running")), "temp": (False, app.Raw("85.0 °C"))}, 200.0)
    data = json.loads((tmp_path / "health.json").read_text())
    assert data == {"checked": 200.0, "checks": {"disk": {"ok": True},
                                                 "temp": {"ok": False, "text": "85.0 °C", "since": 100.0}}}


def test_a_motion_event_is_sent_again_while_the_cloud_is_away(monkeypatch):
    """No connection or 5xx: the event is sent again after 5, 30 and 120 s with the time of the motion;
    a refusal of the cloud (4xx) or a Pi not paired is not repeated."""
    import app
    answers, sent, slept = [], [], []

    def send(*args, ts=None):
        sent.append(ts)
        return answers.pop(0)
    monkeypatch.setattr(app.zorcam_cloud, "send_event", send)
    answers[:] = [0, 502, 200]
    assert app.deliver_cloud_event("front", "Front", "x", ["admin"], sleep=slept.append) == 200
    assert slept == [5, 30] and len(sent) == 3 and len(set(sent)) == 1
    for refusal in (403, None):
        answers[:], sent[:], slept[:] = [refusal], [], []
        assert app.deliver_cloud_event("front", "Front", "x", ["admin"], sleep=slept.append) == refusal
        assert len(sent) == 1 and not slept
    answers[:], sent[:], slept[:] = [0, 0, 0, 0], [], []
    assert app.deliver_cloud_event("front", "Front", "x", ["admin"], sleep=slept.append) == 0
    assert slept == [5, 30, 120] and len(sent) == 4


def test_turn_servers_of_the_cloud_reach_the_live_page(server, tunnel, cookies):  # noqa: F811
    """Through the cloud the page gets its TURN servers (X-Zorcam-Ice) for RTCPeerConnection; at home, a forged
    header or a strange value gives none."""
    import app
    turn = [{"urls": ["turn:turn.zorcam.test:3478?transport=udp", "turn:turn.zorcam.test:3478?transport=tcp"],
             "username": "1791200000:dev1234567", "credential": "c2VjcmV0+/="}]
    assert app.cloud_ice(json.dumps(turn)) == turn
    for bad in ("garbage", "{}", json.dumps([{"urls": ["http://x"], "username": "u", "credential": "c"}]),
                json.dumps([{"urls": ["turn:x</script>"], "username": "u", "credential": "c"}]),
                json.dumps([{"urls": ["turn:x"], "username": "u\n", "credential": "c"}]), json.dumps([turn[0]] * 5)):
        assert app.cloud_ice(bad) == [], bad
    pair_locally()
    try:
        status, _, body = request(tunnel, "GET", "/?tab=live", headers={
            "X-Zorcam-Identity": identity("dev1234567", "admin"), "X-Zorcam-Ice": json.dumps(turn)})
        assert status == 200 and json.loads(re.search(r"window\.PAGE = (.*);", body).group(1))["iceServers"] == turn
        assert "new RTCPeerConnection({ iceServers: ICE_SERVERS })" in assets(server, body)
        _, _, body = request(server, "GET", "/?tab=live", cookie=cookies["admin"],
                             headers={"X-Zorcam-Ice": json.dumps(turn)})
        # not through the tunnel: the header is ignored
        assert json.loads(re.search(r"window\.PAGE = (.*);", body).group(1))["iceServers"] == []
    finally:
        zorcam_cloud.remove()


# ---------------------------------------------------------------- timelapse of the snapshots (only through the cloud)

@pytest.fixture
def frames_dir(tmp_path, monkeypatch):
    """A separate recordings folder of the camera "front": a snapshot every 4 s for the last 90 minutes (the first one
    2 s old, so that the edges of the periods do not depend on the second the test runs in), a few with the motion
    mark, one 30 hours old and one 3 days old, and files that are not snapshots."""
    from datetime import datetime, timedelta

    import app
    monkeypatch.setattr(app, "BASE_DIR", str(tmp_path))
    app._timelapse_cache.clear()
    raw = tmp_path / "front"
    raw.mkdir()
    now = datetime.now()
    for i in range(1350):
        mark = "_m" if i in (10, 11, 12, 1000) else ""  # 1000 is 67 minutes ago
        (raw / (zorcam_config.frame_name(now - timedelta(seconds=4 * i + 2)) + mark + ".jpg")).write_bytes(b"x")
    for age in (timedelta(hours=30), timedelta(days=3)):
        (raw / (zorcam_config.frame_name(now - age) + ".jpg")).write_bytes(b"x")
    for junk in ("notes.jpg", "20260101_000000_000.jpg.tmp", "20269999_000000_000.jpg"):
        (raw / junk).write_bytes(b"x")
    return raw, now


def app_tz():
    import app
    return app.pi_timezone()


def test_timelapse_is_for_viewers_and_their_cameras(server, tunnel, cookies, frames_dir):  # noqa: F811
    pair_locally()
    try:
        via = lambda user: {"X-Zorcam-Identity": identity("dev1234567", user)}  # noqa: E731
        url = "/timelapse/front?period="
        # at home and through the cloud alike: the snapshot rules; the live-only user has no snapshots
        assert request(server, "GET", url + "hour")[0] == 302  # not signed in
        assert request(server, "GET", url + "hour", cookie=cookies["guest"])[0] == 403
        assert request(tunnel, "GET", url + "hour", headers=via("guest"))[0] == 403
        for user in ("bob", "admin"):
            for port, kwargs in ((server, {"cookie": cookies[user]}), (tunnel, {"headers": via(user)})):
                status, headers, _ = request(port, "GET", url + "hour", **kwargs)
                assert status == 200 and headers["Content-Type"] == "application/json", user
                assert headers["Cache-Control"] == "no-store"
        assert request(tunnel, "GET", "/timelapse/nope?period=hour", headers=via("bob"))[0] == 404
        for gone in ("month", "day", "week"):  # the periods 24 h and Week are removed: unknown like any other
            assert request(tunnel, "GET", url + gone, headers=via("bob"))[0] == 400, gone
        assert request(tunnel, "GET", "/timelapse/front", headers=via("bob"))[0] == 400
        # a camera that is not switched on for the user is closed, as everywhere
        zorcam_users.set_cameras("bob", {"yard"})
        try:
            assert request(tunnel, "GET", url + "hour", headers=via("bob"))[0] == 404
            assert request(server, "GET", url + "hour", cookie=cookies["bob"])[0] == 404
            assert request(tunnel, "GET", url + "hour", headers=via("admin"))[0] == 200
        finally:
            zorcam_users.set_cameras("bob", {"front", "yard"})
    finally:
        zorcam_cloud.remove()


def test_timelapse_reply_has_every_frame_as_a_name(server, cookies, frames_dir):  # noqa: F811
    raw, now = frames_dir
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    url = "/timelapse/front?period="

    def get(period):
        status, headers, body = request(server, "GET", url + period, cookie=cookies["bob"])
        assert status == 200 and "Content-Encoding" not in headers and int(headers["Content-Length"]) == len(body.encode())
        return json.loads(body)
    hour = get("hour")
    assert set(hour) == {"camera", "period", "base", "full", "tz", "frames", "now", "videos", "clip"}
    assert hour["camera"] == "front" and hour["period"] == "hour" and hour["base"] == "/thumb/front/"
    assert hour["full"] == "/img/front/"
    assert hour["tz"] == {k: app_tz()[k] for k in ("abbr", "offset", "label")}  # the Pi's zone of these times
    assert hour["tz"]["offset"].startswith("UTC") and hour["tz"]["label"]
    frames = hour["frames"]
    assert all(isinstance(f, str) and (raw / f).exists() for f in frames)
    assert len(frames) == 900 and frames == sorted(frames)  # an hour of snapshots, none dropped
    motion = [f for f in frames if f.endswith("_m.jpg")]  # snapshots 10..12 (the one 67 minutes ago is out of the hour)
    assert len(motion) == 3 and all(zorcam_config.parse_frame_name(f)[1] for f in motion)
    assert get("today")["frames"] == sorted(
        f.name for f in raw.iterdir() if zorcam_config.parse_frame_name(f.name)
        and zorcam_config.parse_frame_name(f.name)[0] >= midnight)


def test_timelapse_reply_is_gzipped_when_asked(server, cookies, frames_dir):  # noqa: F811
    import gzip
    import http.client

    def get(accept):
        conn = http.client.HTTPConnection("127.0.0.1", server, timeout=15)
        headers = {"Cookie": cookies["bob"]}
        if accept:
            headers["Accept-Encoding"] = accept
        conn.request("GET", "/timelapse/front?period=hour", headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        # keep-alive: the length is the length of what is sent, and the same connection takes the next request
        assert int(resp.getheader("Content-Length")) == len(body) and not resp.will_close
        conn.request("GET", "/healthz")
        assert conn.getresponse().status == 200
        conn.close()
        return resp, body
    resp, plain = get("")
    assert resp.getheader("Content-Encoding") is None
    for accept in ("gzip", "deflate, gzip;q=0.8, br"):
        resp, packed = get(accept)
        assert resp.getheader("Content-Encoding") == "gzip" and resp.getheader("Cache-Control") == "no-store"
        assert gzip.decompress(packed) == plain and len(packed) * 3 < len(plain)
    for accept in ("gzip;q=0", "identity", "br"):
        resp, body = get(accept)
        assert resp.getheader("Content-Encoding") is None and body == plain, accept


def test_timelapse_cache_keeps_bytes_and_is_bounded(frames_dir, monkeypatch):
    import app
    app._timelapse_cache.clear()
    first = app.timelapse_reply("front", "hour")
    assert isinstance(first, bytes) and app.timelapse_reply("front", "hour") is first  # the same bytes, no new read
    packed = app.timelapse_reply("front", "hour", gzipped=True)
    assert packed is app.timelapse_reply("front", "hour", gzipped=True) and packed != first  # compressed once
    for period in ("today", "days"):
        app.timelapse_reply("front", period)
    for n in (2, 3, 4):
        app.timelapse_reply("front", None, day=app.parse_timelapse_day(f"2026-01-0{n}"))
    assert len(app._timelapse_cache) == app.TIMELAPSE_CACHE_ENTRIES  # the day list and a day count like the periods
    monkeypatch.setattr(app, "TIMELAPSE_CACHE_SECONDS", 0)  # expired: read again
    assert app.timelapse_reply("front", "hour") is not first
    assert len(app._timelapse_cache) <= app.TIMELAPSE_CACHE_ENTRIES
    for period in ("today", "hour", "today", "days"):
        app.timelapse_reply("front", period)
    for n in range(1, 12):  # a user paging through the days does not grow the cache
        app.timelapse_reply("front", None, day=app.parse_timelapse_day(f"2026-02-{n:02d}"))
    assert len(app._timelapse_cache) <= app.TIMELAPSE_CACHE_ENTRIES
    assert [app.accepts_gzip(h) for h in (None, "", "gzip", "br, gzip", "gzip;q=0", "gzip; q=0.0", "identity")] == [
        False, False, True, True, False, False, False]


def test_timelapse_player_on_the_page(server, tunnel):  # noqa: F811
    """The Timelapse tab has the player: canvas, slider with the strip of motion, day
    and hour marks, speeds; no hint and no reload of the page every minute under it."""
    pair_locally()
    try:
        for lang, cookie_lang in (("en", ""), ("ru", "zorcam_lang=ru")):
            status, _, body = request(tunnel, "GET", "/?tab=all&cam=front&mode=grid&horizon=60",
                                      headers={"X-Zorcam-Identity": identity("dev1234567", "bob"), "Cookie": cookie_lang})
            assert status == 200 and "[[" not in body
            panel = body[body.index('data-panel="all"'):body.index("<script")]
            js = assets(server, body)
            assert 'class="tl" data-cam="front" data-api="/timelapse/front"' in panel and "tl-hint" not in panel
            assert "data-period" not in panel
            assert re.findall(r'data-speed="([\d.]+)">×([\d.]+)<', panel) == [
                (n, n) for n in ("0.25", "0.5", "1", "2", "4")]
            assert 'class="tl-cal"' in panel and 'class="grid"' not in panel and 'class="pager"' not in panel
            assert 'class="horizon-pills modes"' not in panel
            for part in ('<canvas class="tl-canvas"', 'class="tl-range"', 'class="tl-strip"', 'class="tl-play"',
                         'class="tl-time"'):
                assert part in panel, part
            assert t(lang, "tl.empty") in body and t(lang, "tl.error") in body  # the strings of the script
            assert "timelapse && current === 'all'" in js  # no reload of the page under the player
            assert "data.tz.label" in js  # the time of the frame carries the Pi's time zone
            assert "tl-tick" in js and "tl-lab" in js and "function layoutMarks" in js  # day and hour marks
            assert "frames[j].name" not in js  # the reply is plain names, the time comes from them
            # the calendar of days under the player: hidden until the list arrives, buttons built by the script
            assert 'class="tl-cal" hidden' in panel and 'class="tl-days"' in panel and panel.count("tl-calnav") == 2
            assert t(lang, "tl.days") in panel and t(lang, "tl.keys") in panel and "[ " in t(lang, "tl.keys")
            assert "?days=1" in js and "'?day=' + d" in js and "zorcam_tl_day" not in js and "BracketLeft" in js
            # a pause in the recording is walked on a grey screen, not jumped; the newest day with snapshots opens
            assert "function showGap" in js and "gapAt(v)" in js and "it.frames > 0" in js
            assert "removeAttribute('poster')" in js  # the clip over the timelapse plays at once
            for key in ("tl.day_frames", "tl.day_clips", "tl.day_none", "tl.day_no_clips", "tl.no_frames"):
                assert json.dumps(t(lang, key), ensure_ascii=False) in body, key  # window.T
            assert "'<i class=\"tm'" in js and "'<i class=\"vd'" in js and "TIMELAPSE" in js  # two fixed slots
    finally:
        zorcam_cloud.remove()


def test_timelapse_one_day_and_the_list_of_days(server, cookies, frames_dir):  # noqa: F811
    from datetime import datetime, timedelta
    raw, now = frames_dir
    video = raw.parent / "front_videos"
    video.mkdir()
    today = now.date()
    two = today - timedelta(days=2)
    five = today - timedelta(days=5)
    stamp = lambda d, hms: d.strftime("%Y%m%d") + "_" + hms  # noqa: E731
    for f in raw.glob(stamp(two, "") + "*"):  # before 06:00 the fixture's frame 30 hours old falls on that day too
        f.unlink()
    for hms in ("000000", "235959"):  # the edges of the calendar day belong to it
        (raw / f"{stamp(two, hms)}_000.jpg").write_bytes(b"x")
    (raw / f"{stamp(two, '120000')}_000_m.jpg").write_bytes(b"x")
    (video / f"{stamp(five, '101010')}.jpg").write_bytes(b"x")  # a clip: poster and mp4 are one
    (video / f"{stamp(five, '101010')}.mp4").write_bytes(b"x")
    (video / f"{stamp(five, '111111')}.jpg").write_bytes(b"x")  # only a poster: the clip is being recorded, not counted
    (video / f"{stamp(five, '121212')}.jpg").write_bytes(b"x")  # failed: nothing to watch, not counted
    (video / f"{stamp(five, '121212')}.failed").write_bytes(b"")
    (video / f"{stamp(five, '131313')}.json").write_bytes(b"{}")
    (video / f"{stamp(two, '090000')}.mp4").write_bytes(b"x")  # an mp4 without a poster is a clip as well (counted)
    (video / "notes.jpg").write_bytes(b"x")
    cookie = cookies["bob"]

    def get(query, **kw):
        return request(server, "GET", "/timelapse/front?" + query, cookie=cookie, **kw)
    status, headers, body = get(f"day={two.isoformat()}")
    reply = json.loads(body)
    assert status == 200 and reply["day"] == two.isoformat() and reply["period"] is None
    assert reply["frames"] == [f"{stamp(two, '000000')}_000.jpg", f"{stamp(two, '120000')}_000_m.jpg",
                               f"{stamp(two, '235959')}_000.jpg"]  # exactly that day, in order
    assert reply["base"] == "/thumb/front/" and reply["tz"]["label"]
    assert json.loads(get(f"day={today.isoformat()}")[2])["frames"] == sorted(
        f.name for f in raw.iterdir() if f.name.startswith(stamp(today, "")[:9]) and zorcam_config.parse_frame_name(f.name))
    assert json.loads(get("day=2001-01-01")[2])["frames"] == []  # a day with nothing: an empty list
    for bad in ("tomorrow", "2026-13-01", "2026-02-30", "20261011", "2026-1-5", "2026-10-11x", "../x"):
        assert get("day=" + bad)[0] == 400, bad

    status, headers, body = get("days=1")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    data = json.loads(body)
    assert set(data) == {"days", "today"} and data["today"] == today.isoformat()
    days = data["days"]
    assert days[0]["d"] <= five.isoformat()  # from the oldest day with frames or clips
    assert days[-1]["d"] == today.isoformat()  # today is last
    by = {d["d"]: d for d in days}
    assert [d["d"] for d in days] == sorted(by) and len(by) == len(days)
    first = datetime.strptime(days[0]["d"], "%Y-%m-%d").date()
    assert len(days) == (today - first).days + 1  # no holes: the empty days between are in
    assert by[five.isoformat()] == {"d": five.isoformat(), "frames": 0, "clips": 1}  # only a ready mp4 counts
    assert by[two.isoformat()] == {"d": two.isoformat(), "frames": 3, "clips": 1}
    assert by[(today - timedelta(days=4)).isoformat()] == {"d": (today - timedelta(days=4)).isoformat(), "frames": 0, "clips": 0}
    assert by[today.isoformat()]["frames"] == len(json.loads(get(f"day={today.isoformat()}")[2])["frames"])
    assert "20269999" not in body and "notes" not in body  # junk names are no days
    # gzip, like the frame lists
    import gzip
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=15)
    for query in ("days=1", f"day={two.isoformat()}"):
        conn.request("GET", "/timelapse/front?" + query, headers={"Cookie": cookie, "Accept-Encoding": "gzip"})
        resp = conn.getresponse()
        packed = resp.read()
        assert resp.getheader("Content-Encoding") == "gzip" and int(resp.getheader("Content-Length")) == len(packed)
        assert json.loads(gzip.decompress(packed)) == json.loads(get(query)[2])
    conn.close()


def test_timelapse_days_are_capped_and_cheap(frames_dir, monkeypatch):
    from datetime import timedelta

    import app
    raw, now = frames_dir
    old = now - timedelta(days=1000)
    (raw / (zorcam_config.frame_name(old) + ".jpg")).write_bytes(b"x")
    days = app.timelapse_days("front", now)["days"]
    assert len(days) == app.TIMELAPSE_DAYS_MAX and days[-1]["d"] == now.date().isoformat()
    assert days[0]["d"] == (now.date() - timedelta(days=app.TIMELAPSE_DAYS_MAX - 1)).isoformat()
    # nothing at all (a new camera without folders): just today
    monkeypatch.setattr(app, "BASE_DIR", str(raw.parent / "elsewhere"))
    assert app.timelapse_days("front", now) == {"days": [{"d": now.date().isoformat(), "frames": 0, "clips": 0}],
                                                 "today": now.date().isoformat()}


def test_timelapse_day_queries_have_the_same_access(server, tunnel, cookies, frames_dir):  # noqa: F811
    pair_locally()
    try:
        via = lambda user: {"X-Zorcam-Identity": identity("dev1234567", user)}  # noqa: E731
        for query in ("day=2026-10-11", "days=1"):
            url = "/timelapse/front?" + query
            assert request(server, "GET", url)[0] == 302
            assert request(server, "GET", url, cookie=cookies["guest"])[0] == 403
            assert request(tunnel, "GET", url, headers=via("guest"))[0] == 403
            for user in ("bob", "admin"):
                assert request(server, "GET", url, cookie=cookies[user])[0] == 200
                assert request(tunnel, "GET", url, headers=via(user))[0] == 200
            assert request(tunnel, "GET", "/timelapse/nope?" + query, headers=via("bob"))[0] == 404
            zorcam_users.set_cameras("bob", {"yard"})
            try:
                assert request(tunnel, "GET", url, headers=via("bob"))[0] == 404
                assert request(tunnel, "GET", url, headers=via("admin"))[0] == 200
            finally:
                zorcam_users.set_cameras("bob", {"front", "yard"})
    finally:
        zorcam_cloud.remove()


def test_the_owner_name_comes_with_the_pairing_and_follows_the_cloud(fake_cloud, monkeypatch):
    """GET /api/device/status gives owner_name (the account's display name): kept at pairing, renewed by the heartbeat
    when it changes; an older cloud without it leaves nothing (no empty value)."""
    monkeypatch.setattr(zorcam_cloud, "URL_RE", re.compile(r"https?://.+"))
    assert zorcam_cloud.start_pairing(fake_cloud, "pi", "2.6.0")[0]
    FakeCloud.paired = True
    assert zorcam_cloud.finish_pairing("2.6.0") == "paired"
    assert zorcam_cloud.load()["owner_name"] == "Anna K." and zorcam_cloud.load()["name"] == "Dacha"
    FakeCloud.owner = "Anna Kuznetsova"
    assert zorcam_cloud.heartbeat()
    assert zorcam_cloud.load()["owner_name"] == "Anna Kuznetsova" and zorcam_cloud.load()["name"] == "Dacha"
    FakeCloud.owner = None  # the cloud stops sending it: the last known name stays, nothing breaks
    assert zorcam_cloud.heartbeat() and zorcam_cloud.load()["owner_name"] == "Anna Kuznetsova"
    FakeCloud.owner = 5  # not a text
    assert zorcam_cloud.heartbeat() and zorcam_cloud.load()["owner_name"] == "Anna Kuznetsova"


def test_the_device_address_follows_the_cloud(fake_cloud, monkeypatch, capsys):
    """The cloud moved the devices to <id>.devices.<domain>: the heartbeat renews url/app_url (the Yandex skill address
    follows) and the tunnel settings, logs it; a bad value is not saved, a missing field does not erase the old one."""
    monkeypatch.setattr(zorcam_cloud, "URL_RE", re.compile(r"https?://[a-z0-9.:/-]+"))  # http: the fake cloud
    assert zorcam_cloud.start_pairing(fake_cloud, "pi", "2.6.0")[0]
    FakeCloud.paired = True
    FakeCloud.address = {"stun": "turn.zorcam.test:3478"}
    assert zorcam_cloud.finish_pairing("2.6.0") == "paired"
    assert zorcam_cloud.load()["stun"] == "turn.zorcam.test:3478"  # pairing copies it with the other addresses
    zorcam_cloud.save({**zorcam_cloud.load(), "stun": None})  # an old cloud sends none: nothing is left over
    FakeCloud.address = {}
    assert app.yandex_cloud_url() == "https://dev1234567.zorcam.test"
    capsys.readouterr()
    assert zorcam_cloud.heartbeat() and "address" not in capsys.readouterr().out  # nothing changed: no log line
    good = {"url": "https://dev1234567.devices.zorcam.test", "app_url": "https://cloud2.zorcam.test",
            "frps_addr": "tunnel2.zorcam.test", "frps_port": 8443, "subdomain": "dev7654321",
            "stun": "turn.zorcam.test:3478"}
    FakeCloud.address = good
    assert zorcam_cloud.heartbeat()
    data = zorcam_cloud.load()
    assert (data["url"], data["app_url"]) == ("https://dev1234567.devices.zorcam.test", "https://cloud2.zorcam.test")
    assert (data["frps_addr"], data["frps_port"], data["subdomain"]) == ("tunnel2.zorcam.test", 8443, "dev7654321")
    assert data["token"] == "tunnel-token" and data["name"] == "Dacha" and data["stun"] == "turn.zorcam.test:3478"
    assert app.yandex_cloud_url() == "https://dev1234567.devices.zorcam.test"
    assert "tunnel2.zorcam.test" in zorcam_cloud.frpc_config(data)  # the daemon sees the new config and restarts frpc
    assert "the address from the cloud changed" in capsys.readouterr().out
    # a bad or missing value changes nothing
    for bad in ({"url": "javascript:alert(1)", "app_url": "ftp://x"}, {"url": 5, "app_url": ["https://a.test"]},
                {"url": "", "frps_addr": "tunnel.test\"\nx = 1", "frps_port": "443", "subdomain": 7},
                {"frps_port": True}, {"frps_port": 70000},
                {"stun": "turn.zorcam.test"}, {"stun": "turn.zorcam.test:0"}, {"stun": "turn.zorcam.test:70000"},
                {"stun": "Turn.x:3478"}, {"stun": "a\nb:3478"}, {"stun": 3478}, {"stun": None},
                {"url": None, "app_url": None, "frps_addr": None, "frps_port": None, "subdomain": None}):
        FakeCloud.address = {**good, **bad}
        assert zorcam_cloud.heartbeat()
        assert zorcam_cloud.load() == data, bad
    assert app.yandex_cloud_url() == "https://dev1234567.devices.zorcam.test"


def test_cloud_section_shows_the_owner(server, cookies):  # noqa: F811
    """Settings -> General -> the cloud: the "Owner" row with the name on the right, only when the cloud sent it."""
    def section():
        return request(server, "GET", "/settings", cookie=cookies["admin"] + "; zorcam_lang=ru")[2]
    pair_locally()
    try:
        assert t("ru", "cloud.owner") not in section()  # an older cloud: no row
        with zorcam_cloud.locked():
            data = zorcam_cloud.load()
            data["owner_name"] = "Anna <b>K.</b>"
            zorcam_cloud.save(data)
        page = section()
        assert (f'<div class="row"><div class="k">{t("ru", "cloud.owner")}</div>'
                '<div class="v">Anna &lt;b&gt;K.&lt;/b&gt;</div></div>') in page
        assert "Owner" not in request(server, "GET", "/settings", cookie=cookies["admin"] + "; zorcam_lang=ru")[2]
        assert f'<div class="k">{t("en", "cloud.owner")}</div>' in request(server, "GET", "/settings",
                                                                        cookie=cookies["admin"])[2]
    finally:
        zorcam_cloud.remove()


def test_sign_out_on_other_devices_through_the_cloud(server, tunnel):  # noqa: F811
    """Pressed through the cloud: the user's cloud session (one for all his cloud clients) stays and so does it
    working, the sessions at home end. Pressed at home: the home session stays, the cloud one is dropped (the next
    request through the cloud signs in again by the cloud's identity)."""
    import app
    from urllib.parse import urlencode
    from test_gallery import csrf_of_session
    pair_locally()
    via = {"X-Zorcam-Identity": identity("dev1234567", "bob")}
    try:
        home = [login(server, "bob"), login(server, "bob")]
        status, headers, _ = request(tunnel, "GET", "/settings?tab=users", headers=via)
        assert status == 200
        cloud = headers["Set-Cookie"].split(";")[0]
        shared = app._cloud_sessions["bob"]
        csrf = csrf_of_session(cloud)
        status, headers, _ = request(tunnel, "POST", "/sessions/others", urlencode({"csrf": csrf}), cookie=cloud, headers=via)
        assert status == 303
        assert [request(server, "GET", "/settings", cookie=c)[0] for c in home] == [302, 302]
        assert app._cloud_sessions["bob"] == shared and app.check_session(shared)[0] == "bob"
        assert request(tunnel, "GET", "/settings", headers=via)[0] == 200
        assert request(tunnel, "GET", "/settings", cookie=cloud, headers=via)[0] == 200
        # at home
        here = login(server, "bob")
        status, _, _ = request(server, "POST", "/sessions/others", urlencode({"csrf": csrf_of_session(here)}), cookie=here)
        assert status == 303 and "bob" not in app._cloud_sessions and app.check_session(shared)[0] is None
        assert request(server, "GET", "/settings", cookie=here)[0] == 200
        status, headers, _ = request(tunnel, "GET", "/settings", headers=via)  # the cloud signs in again, nothing breaks
        assert status == 200 and app._cloud_sessions["bob"] != shared
    finally:
        zorcam_cloud.remove()
        app._cloud_sessions.pop("bob", None)
