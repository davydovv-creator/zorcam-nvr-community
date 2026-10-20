"""Gallery smoke test: the real app.Handler HTTP server on a free port, two cameras with fake frames and clips,
sign-in with every role and a check of which pages each role can open, in English and in Russian;
the setup wizard, the camera pages and the per-camera recording buttons."""
import calendar
import contextlib
import copy
import time
import html
import http.client
import io
import json
import os
import re
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

import pytest
from PIL import Image

import zorcam_config
import zorcam_users
from conftest import HOME
from test_onvif import PTZ_SPACES

import app  # noqa: E402  (after conftest: temporary HOME)
from zorcam_i18n import Msg, t

PASSWORDS = {"admin": "admin-pass", "bob": "viewer-pass", "guest": "live-pass"}
N_VIDEOS = 35
FRAME = "20260926_120000_000.jpg"
CAMS = [{"id": "front", "name": "Front door", "rtsp_url": "rtsp://127.0.0.1:1/live", "rtsp_user": ""},
        {"id": "yard", "name": "Yard", "rtsp_url": "rtsp://127.0.0.1:1/yard", "rtsp_user": ""}]


def jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (640, 360), (40, 90, 160)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def server():
    zorcam_users.set_user("admin", "admin", PASSWORDS["admin"])
    zorcam_users.set_user("bob", "viewer", PASSWORDS["bob"])
    zorcam_users.set_user("guest", "liveviewer", PASSWORDS["guest"])
    for name in ("bob", "guest"):
        zorcam_users.set_cameras(name, {"front", "yard"})
    zorcam_config.save_cameras(CAMS)
    raw = os.path.join(HOME, "recordings", "front")
    vids = os.path.join(HOME, "recordings", "front_videos")
    os.makedirs(raw, exist_ok=True)
    os.makedirs(vids, exist_ok=True)
    data = jpeg()
    with open(os.path.join(raw, FRAME), "wb") as f:
        f.write(data)
    for i in range(N_VIDEOS):
        name = f"20260926_1{i // 60:01d}{i % 60:02d}00_000"
        with open(os.path.join(vids, name + ".jpg"), "wb") as f:
            f.write(data)
        with open(os.path.join(vids, name + ".mp4"), "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
    srv = app.Server(("127.0.0.1", 0), app.Handler)
    tun = app.Server(("127.0.0.1", 0), app.Handler, tunnel=True)  # the cloud tunnel port (app.TUNNEL_PORT)
    TUNNEL_PORTS[srv.server_address[1]] = tun.server_address[1]
    for one in (srv, tun):
        threading.Thread(target=one.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    tun.shutdown()


@pytest.fixture(autouse=True)
def quiet_network(monkeypatch):
    """Settings -> Status shows a Network row when the machine is on Wi-Fi or has a radio on; the machine the tests run
    on must not decide that: by default the cable and no radio."""
    monkeypatch.setattr(zorcam_config, "network_state", lambda *a, **k: {"via": "ethernet", "wifi": False, "bluetooth": False})


TUNNEL_PORTS = {}  # port of the gallery -> port of its tunnel twin


@pytest.fixture(scope="module")
def tunnel(server):
    """The port the cloud tunnel (frpc) connects to: only there X-Zorcam-Identity, X-Zorcam-Ice, X-Zorcam-Client-IP
    and the Yandex addresses are trusted."""
    return TUNNEL_PORTS[server]


def motion_headers(extra=None):
    """What the detector sends to /internal/motion: JSON and the shared secret."""
    return {"Content-Type": "application/json", "X-Zorcam-Internal": app.INTERNAL_TOKEN, **(extra or {})}


def request(port, method, path, body=None, cookie=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    hdrs = dict(headers or {})
    if cookie:
        hdrs["Cookie"] = cookie
    if body is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, resp.headers, data.decode("utf-8", errors="replace")


def login(port, user):
    status, headers, _ = request(port, "POST", "/login", urlencode({"username": user, "password": PASSWORDS[user]}))
    assert status == 302, user
    assert headers["Location"] == "/?tab=live"
    return headers["Set-Cookie"].split(";")[0]


@pytest.fixture(scope="module")
def cookies(server):
    return {user: login(server, user) for user in PASSWORDS}


def assets(server, body):
    """The CSS and JS a page links to (they are files now, not part of the page), fetched the way a browser does:
    without signing in, by the address with ?v=<hash>."""
    names = re.findall(r'(?:href|src)="(/static/[\w.]+\?v=\w+)"', body)
    assert names, "the page links no CSS or JS"
    return "\n".join(request(server, "GET", name)[2] for name in names)


def csrf_of(body, action):
    return re.search(r'action="' + re.escape(action) + r'".*?name="csrf" value="([0-9a-f]+)"', body, re.S).group(1)


def csrf_of_session(cookie):
    """The CSRF token of the session a cookie ("zorcam_session=TOKEN" or with more cookies) belongs to."""
    return app.csrf_token(re.search(app.SESSION_COOKIE + r"=([^;\s]+)", cookie).group(1))


def test_public_endpoints(server):
    assert request(server, "GET", "/healthz")[0] == 200
    assert request(server, "GET", "/sw.js")[0] == 200
    assert request(server, "GET", "/manifest.webmanifest")[0] == 200
    assert request(server, "GET", "/apple-touch-icon.png")[0] == 200
    assert request(server, "GET", "/apple-touch-icon-precomposed.png")[0] == 200
    status, headers, body = request(server, "HEAD", "/apple-touch-icon.png?v=1")
    assert status == 200 and headers["Content-Type"] == "image/png" and int(headers["Content-Length"]) > 0 and body == ""
    assert request(server, "GET", "/login")[0] == 200


def test_anonymous_is_redirected_to_login(server):
    for path in ("/", "/settings", "/config", "/watch/front/x", "/push/key", "/camera/new", "/setup/cameras"):
        status, headers, _ = request(server, "GET", path)
        assert status == 302 and headers["Location"] == "/login", path
    # the wizard steps without a session, once users exist: to the sign-in page
    for path in ("/setup", "/setup/users"):
        status, headers, _ = request(server, "GET", path)
        assert status == 303 and headers["Location"] == "/login", path


def test_wrong_password(server):
    status, _, body = request(server, "POST", "/login", urlencode({"username": "bob", "password": "nope"}),
                              headers={"X-Test": "1"})
    assert status == 401 and t("en", "login.wrong") in body


VIDEO = "20260926_100000_000"
ACCESS = [
    # path,                                       liveviewer, viewer, admin
    ("/?tab=live",                                200, 200, 200),
    ("/static/app.css",                           200, 200, 200),  # the CSS and JS of the pages: public, like the icons
    ("/?tab=live&cam=front&embed=1",              200, 200, 200),  # one camera's player for the cloud grid
    (f"/watch/front/{VIDEO}",                     403, 200, 200),
    (f"/vid/front/{VIDEO}.mp4",                   403, 200, 200),
    (f"/thumb/front_videos/{VIDEO}.jpg",          403, 200, 200),
    (f"/img/front/{FRAME}",                       403, 200, 200),
    (f"/watch/front/{VIDEO}?frag=1",              403, 200, 200),  # the clip window over the timelapse
    ("/?tab=all",                                 200, 200, 200),  # snapshots: the timelapse only (a liveviewer gets Live)
    ("/settings",                                 200, 200, 200),
    ("/settings?tab=notify",                      200, 200, 200),  # a liveviewer gets the General tab instead
    ("/config",                                   403, 403, 200),
    ("/camera/new",                               403, 403, 200),
    ("/camera/front",                             403, 403, 200),
    ("/camera/front/zones",                       403, 403, 200),  # motion zones of a camera (admin only)
    ("/onvif",                                    403, 403, 200),
    ("/yandex",                                   403, 403, 200),
    ("/mqtt",                                     403, 403, 200),
    ("/cloud/v1/cameras",                         200, 200, 200),
    ("/cloud/v1/poster/front",                    403, 200, 200),
    ("/camera-frame/front",                       403, 403, 200),  # the recent frame in the camera form (admin only)
    ("/cloud/v1/health",                          403, 403, 200),
    ("/timelapse/front?period=hour",              403, 200, 200),  # snapshots: a viewer and above (test_cloud.py)
    ("/setup/cameras",                            403, 403, 200),
    ("/setup/password",                           403, 403, 200),  # "Change the password"; POSTs: test_installer_password_*
    ("/ptz/front",                                403, 200, 200),  # does the camera turn (JSON); POST: test_ptz_*
    # POST /sessions/others (every role), /users/sessions (admin only): test_sign_out_*, test_admin_ends_all_sessions
]
ROLES_ORDER = ["guest", "bob", "admin"]  # liveviewer, viewer, admin


@pytest.mark.parametrize("path,expected", [(p, list(r)) for p, *r in ACCESS])
def test_access_by_role(server, cookies, path, expected):
    got = [request(server, "GET", path, cookie=cookies[u])[0] for u in ROLES_ORDER]
    assert got == expected, path


def test_camera_frame_is_the_newest_one_for_admin(server, cookies):
    status, headers, _ = request(server, "GET", "/camera-frame/front", cookie=cookies["admin"])
    assert status == 200 and headers["Content-Type"] == "image/jpeg"
    assert headers["Cache-Control"].startswith("private") and "max-age" in headers["Cache-Control"]
    assert request(server, "GET", "/camera-frame/nope", cookie=cookies["admin"])[0] == 404  # not a camera
    assert request(server, "GET", "/camera-frame/..%2Fetc", cookie=cookies["admin"])[0] == 404
    assert request(server, "GET", "/camera-frame/front")[0] in (302, 401, 403)  # not signed in


def test_camera_frame_without_frames_is_404_and_the_pages_have_the_plate(server, cookies, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "BASE_DIR", str(tmp_path))
    assert request(server, "GET", "/camera-frame/front", cookie=cookies["admin"])[0] == 404
    form = request(server, "GET", "/camera/front", cookie=cookies["admin"])[2]
    assert 'class="camframe"' in form and 'src="/camera-frame/front"' in form and t("en", "cams.no_frame") in form
    lst = request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    assert 'class="camframe small"' in lst and 'src="/camera-frame/yard"' in lst
    new = request(server, "GET", "/camera/new", cookie=cookies["admin"])[2]
    assert "camframe" not in new  # a new camera has no frame


def test_liveviewer_sees_only_live(server, cookies):
    _, _, body = request(server, "GET", "/?tab=videos", cookie=cookies["guest"])
    assert 'data-tab="live"' in body
    assert 'data-tab="videos"' not in body and 'id="bell"' not in body


def test_header_has_no_language_or_sign_out(server, cookies):
    """The language and sign-out are in Settings, which every role can open from the tab bar; the header has only the
    app name (and the badge of a non-prod stand), no version and no tabs (the tabs are the bar at the bottom)."""
    for user in ROLES_ORDER:
        body = request(server, "GET", "/", cookie=cookies[user])[2]
        header = body[body.index('<div class="topbar">'):body.index('<div class="content">')]
        bar = body[body.index('<nav class="tabbar">'):body.index("</nav>", body.index('<nav class="tabbar">'))]
        assert 'href="/settings"' in bar and 'href="/settings"' not in header, user
        assert 'class="ver"' not in body and 'class="tabs"' not in body, user
        assert '<span class="appname"><span>Zorcam.nvr</span>' in header and "envbadge" not in body.split("</head>")[0], user
        assert "data-lang-pick" not in body and 'href="/logout"' not in body, user


def test_settings_of_viewers_have_only_language_and_sign_out(server, cookies):
    """liveviewer and viewer: the General tab (the language) and the Users tab (who is signed in, Sign out), a viewer
    also Notifications; the admin tabs fall back to General."""
    for user in ("guest", "bob"):  # liveviewer, viewer
        body = request(server, "GET", "/settings", cookie=cookies[user])[2]
        assert re.findall(r'href="/settings\?tab=(\w+)"', body) == (
            ["general", "users"] if user == "guest" else ["general", "notify", "users"]), user
        assert re.findall(r"<section><h2>(.*?)</h2>", body) == [t("en", "section.language")], user
        assert "data-lang-pick" in body and 'href="/logout"' not in body
        body = request(server, "GET", "/settings?tab=users", cookie=cookies[user])[2]
        assert re.findall(r"<section><h2>(.*?)</h2>", body) == [], user  # no group label
        assert f'{t("en", "account.signed_in", user="<b>" + user + "</b>")}' in body
        assert '<div class="signout"><a href="/logout">' in body and t("en", "users.devices") not in body
        assert 'href="/logout"' in body and 'action="/users/save"' not in body
        for tab in ("cameras", "status"):
            body = request(server, "GET", f"/settings?tab={tab}", cookie=cookies[user])[2]
            assert "data-lang-pick" in body and "/storage/delete" not in body and 'href="/camera/' not in body


def test_iphone_install_hint(server, cookies):
    """Safari on iPhone: the app icon in the header shows how to add Zorcam to the Home Screen (hidden until JS
    finds iOS outside the Home Screen app)."""
    body = request(server, "GET", "/", cookie=cookies["guest"])[2]
    assert '<button class="appbtn" id="appbtn" hidden' in body and '<div class="apphint" id="apphint" hidden>' in body
    button = body[body.index('id="appbtn"'):body.index("</button>", body.index('id="appbtn"'))]
    assert 'fill="currentColor"><path d="M12.152 6.896' in button and "<img" not in button  # the Apple logo
    assert t("en", "app.step2") in body and "navigator.standalone" in assets(server, body)
    body = request(server, "GET", "/", cookie=cookies["guest"], headers=RU)[2]
    assert t("ru", "app.step2") in body


def test_liveviewer_cannot_subscribe_to_push(server, cookies):
    status = request(server, "POST", "/push/subscribe", "{}", cookie=cookies["guest"],
                     headers={"Content-Type": "application/json"})[0]
    assert status == 403


def push_sub(endpoint="https://web.push.apple.com/t"):
    key = app.webpush.b64url(app.webpush._raw_public(app.webpush.ec.generate_private_key(app.webpush.ec.SECP256R1())))
    return {"endpoint": endpoint, "keys": {"p256dh": key, "auth": "AAAAAAAAAAAAAAAAAAAAAA"}}


def push_post(server, cookie, path, data, headers=None):
    return request(server, "POST", path, app.json.dumps(data), cookie=cookie,
                   headers={"Content-Type": "application/json", **(headers or {})})[0]


def test_push_needs_the_csrf_token(server, cookies):
    sub = push_sub("https://web.push.apple.com/csrf")
    own = {"subscription": sub, "quiet": True}
    try:
        assert push_post(server, cookies["bob"], "/push/subscribe", own) == 403  # no token
        assert push_post(server, cookies["bob"], "/push/subscribe", {**own, "csrf": "x"}) == 403
        assert push_post(server, cookies["bob"], "/push/subscribe",
                         {**own, "csrf": csrf_of_session(cookies["admin"])}) == 403  # a token of another session
        assert sub["endpoint"] not in app.PUSH.subscriptions
        assert push_post(server, cookies["bob"], "/push/unsubscribe", {"endpoint": sub["endpoint"]}) == 403
        assert push_post(server, cookies["bob"], "/push/subscribe", {**own, "csrf": csrf_of_session(cookies["bob"])}) == 200
        assert sub["endpoint"] in app.PUSH.subscriptions
    finally:
        app.PUSH.remove(sub["endpoint"])


def test_push_endpoint_only_of_a_known_push_service(server, cookies):
    csrf = csrf_of_session(cookies["bob"])
    for endpoint in ("https://192.168.1.10/x", "https://evil.example/x", "http://fcm.googleapis.com/x",
                     "https://user@fcm.googleapis.com/x"):
        assert push_post(server, cookies["bob"], "/push/subscribe",
                         {"subscription": push_sub(endpoint), "quiet": True, "csrf": csrf}) == 400, endpoint
        assert endpoint not in app.PUSH.subscriptions


def test_push_unsubscribe_removes_only_own_subscriptions(server, cookies):
    mine, other = push_sub("https://web.push.apple.com/mine"), push_sub("https://fcm.googleapis.com/fcm/send/other")
    try:
        for cookie, sub in ((cookies["bob"], mine), (cookies["admin"], other)):
            assert push_post(server, cookie, "/push/subscribe",
                             {"subscription": sub, "quiet": True, "csrf": csrf_of_session(cookie)}) == 200
        # bob names admin's endpoint: answered as usual, nothing removed
        bob = csrf_of_session(cookies["bob"])
        assert push_post(server, cookies["bob"], "/push/unsubscribe", {"endpoint": other["endpoint"], "csrf": bob}) == 200
        assert other["endpoint"] in app.PUSH.subscriptions
        assert push_post(server, cookies["bob"], "/push/unsubscribe", {"endpoint": mine["endpoint"], "csrf": bob}) == 200
        assert mine["endpoint"] not in app.PUSH.subscriptions
        # admin removes any
        assert push_post(server, cookies["bob"], "/push/subscribe", {"subscription": mine, "quiet": True, "csrf": bob}) == 200
        admin = csrf_of_session(cookies["admin"])
        assert push_post(server, cookies["admin"], "/push/unsubscribe", {"endpoint": mine["endpoint"], "csrf": admin}) == 200
        assert mine["endpoint"] not in app.PUSH.subscriptions
    finally:
        for sub in (mine, other):
            app.PUSH.remove(sub["endpoint"])


def test_posts_from_another_origin_are_refused(server, cookies):
    """The cloud puts all devices on one site: the browser sends the cloud cookie from a stranger's page too.
    Sec-Fetch-Site decides when the browser sends it, otherwise Origin must be this device's own address."""
    sub = push_sub("https://web.push.apple.com/origin")
    data = {"subscription": sub, "quiet": True, "csrf": csrf_of_session(cookies["bob"])}
    host = f"127.0.0.1:{server}"
    try:
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                        {"Origin": "https://other123.zorcam.test"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site", "Origin": f"http://{host}"},
                        {"Origin": "https://other.zorcam.test", "X-Forwarded-Host": "mine.zorcam.test"}):
            assert push_post(server, cookies["bob"], "/push/subscribe", data, headers) == 403, headers
            assert request(server, "POST", "/login", urlencode({"username": "bob", "password": PASSWORDS["bob"]}),
                           headers=headers)[0] == 403, headers
        assert sub["endpoint"] not in app.PUSH.subscriptions
        for headers in ({"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}, {"Origin": f"http://{host}"},
                        {"Origin": "https://mine.zorcam.test", "X-Forwarded-Host": "mine.zorcam.test"}, {}):
            assert push_post(server, cookies["bob"], "/push/subscribe", data, headers) == 200, headers
        assert sub["endpoint"] in app.PUSH.subscriptions
        # the machine addresses are not for browsers and are not judged by the origin
        assert request(server, "POST", "/internal/motion", "{}",
                       headers=motion_headers({"Sec-Fetch-Site": "cross-site"}))[0] == 200
    finally:
        app.PUSH.remove(sub["endpoint"])


def live_panel(body):
    """The Live panel of the main page (the next panel, Videos, starts after it)."""
    start = body.index('data-panel="live"')
    return body[start:body.index('data-panel="videos"', start)] if 'data-panel="videos"' in body else body[start:]


def test_live_shows_every_camera_with_its_name(server, cookies):
    for user in ROLES_ORDER:
        body = request(server, "GET", "/?tab=live", cookie=cookies[user])[2]
        live = body[body.index('data-panel="live"'):]
        assert live.index('<h2 class="camera-name link"><a href="/?tab=live&amp;cam=front"') < live.index('data-cam="front"') \
            < live.index('<h2 class="camera-name link"><a href="/?tab=live&amp;cam=yard"') < live.index('data-cam="yard"'), user
        assert "Front door</span>" in live and "Yard</span>" in live, user
        header = body[body.index('<div class="topbar">'):body.index('<div class="content">')]
        assert "Front door" not in header
    # the tab strip is always there: "All" (this grid) is open, then the cameras; tabs first, then the column choice
    for user in ROLES_ORDER:
        live = live_panel(request(server, "GET", "/?tab=live", cookie=cookies[user])[2])
        assert re.findall(r'<a class="(pill[ a-z]*)" href="([^"]+)">([^<]+)</a>', live) == [
            ("pill active", "/?tab=live", "All"), ("pill", "/?tab=live&amp;cam=front", "Front door"),
            ("pill", "/?tab=live&amp;cam=yard", "Yard")], user
        assert live.index("camtabs") < live.index("livecols") < live.index("live-grid"), user
    # Records keep their own camera tab active, Live tabs do not follow it
    rec = request(server, "GET", "/?tab=all&cam=yard", cookie=cookies["admin"])[2]
    assert '<a class="pill active" href="/?tab=live">All</a>' in live_panel(rec)
    # the 44 px touch area is the link's own: on the h2 it would cover the link and a tap would lead nowhere
    css = assets(server, body)
    assert ".camera-name.link a::after" in css and ".camera-name.link::after" not in css


def test_live_column_choice(server, cookies):
    """Several cameras: 1, 2 or 3 per row above Live (one column until the device has chosen); the choice is kept
    in the browser of the device, so the page itself always starts with one column."""
    body = request(server, "GET", "/?tab=live", cookie=cookies["guest"])[2]
    live = body[body.index('data-panel="live"'):]
    assert live.index('class="horizon-pills livecols"') < live.index('<div class="live-grid" data-cols="1">') \
        < live.index('data-cam="front"')
    # 4 is only for a wide screen (CSS hides 1 there and 4 on a phone)
    assert re.findall(r'data-cols="(\d)" aria-label="([^"]+)"', live) == [
        ("1", "1 per row"), ("2", "2 per row"), ("3", "3 per row"), ("4", "4 per row")]
    assert "localStorage.setItem('zorcam_live_cols'" in assets(server, body)
    ru = request(server, "GET", "/?tab=live", cookie=cookies["guest"] + "; zorcam_lang=ru")[2]
    assert f'aria-label="{t("ru", "live.columns", n=2)}"' in ru and t("ru", "live.columns", n=2) != "2 per row"


def test_live_one_camera_mode(server, cookies):
    """?tab=live&cam=ID: one player, the tab strip with "All" first, no column choice; the name is no link there."""
    for user in ROLES_ORDER:
        body = request(server, "GET", "/?tab=live&cam=yard", cookie=cookies[user])[2]
        live = live_panel(body)
        assert re.findall(r'class="live-wrap" data-cam="(\w+)"', live) == ["yard"], user
        assert '<h2 class="camera-name">Yard</h2>' in live and "camera-name link" not in live, user
        assert "live-grid" not in live and "data-cols" not in live and 'id="live-reload"' in live, user
        tabs = re.findall(r'<a class="(pill[ a-z]*)" href="([^"]+)">([^<]+)</a>', live)
        assert tabs == [("pill", "/?tab=live", "All"), ("pill", "/?tab=live&amp;cam=front", "Front door"),
                        ("pill active", "/?tab=live&amp;cam=yard", "Yard")], user
        assert 'data-panel="live" data-single' in body, user
    ru = request(server, "GET", "/?tab=live&cam=yard", cookie=cookies["bob"] + "; zorcam_lang=ru")[2]
    assert f">{t('ru', 'live.all_cams')}</a>" in ru and t("ru", "live.all_cams") != "All"
    # the Records tabs still take cam for themselves
    assert "data-single" not in request(server, "GET", "/?tab=all&cam=yard", cookie=cookies["bob"])[2]


def test_live_one_camera_mode_hides_unavailable_cameras(server, cookies):
    """A camera that is closed to the user or does not exist: the usual Live of the cameras the user may see."""
    zorcam_users.set_cameras("guest", {"front"})
    try:
        for user, path in (("guest", "/?tab=live&cam=yard"), ("admin", "/?tab=live&cam=nope")):
            body = request(server, "GET", path, cookie=cookies[user])[2]
            live = live_panel(body)
            assert "data-single" not in body, path
            if user == "guest":  # one camera left: as it is without the parameter, and nothing about Yard
                assert re.findall(r'live-wrap" data-cam="(\w+)"', live) == ["front"] and "Yard" not in live
                assert "camera-name link" not in live and "camtabs" not in live
            else:  # "All"
                assert re.findall(r'live-wrap" data-cam="(\w+)"', live) == ["front", "yard"] and 'class="live-grid"' in live
                assert '<a class="pill active" href="/?tab=live">All</a>' in live
    finally:
        zorcam_users.set_cameras("guest", {"front", "yard"})


def test_embedded_live_is_one_player_only(server, cookies):
    """embed=1: the player of one camera and nothing else; a frame can show it (no frame-blocking headers)."""
    for user in ROLES_ORDER:
        status, headers, body = request(server, "GET", "/?tab=live&cam=yard&embed=1", cookie=cookies[user])
        assert status == 200, user
        assert re.findall(r'data-cam="(\w+)"', body) == ["yard"], user
        assert '<h2 class="camera-name">Yard</h2>' in body and "Front door" not in body, user
        for gone in ('class="tabbar"', 'class="topbar"', 'class="large"', "livecols", "data-cols", 'id="bell"', "apphint"):
            assert gone not in body.split("</head>")[1].split("<script")[0], (user, gone)
        assert '<body class="embed">' in body and "/live/webrtc" in assets(server, body)
        low = {k.lower() for k in headers}
        assert "x-frame-options" not in low and "frame-ancestors 'self'" in headers["Content-Security-Policy"]
    assert 'data-cam=' in request(server, "GET", "/?tab=live&cam=yard", cookie=cookies["admin"])[2]  # without embed: as before
    assert 'class="tabbar"' in request(server, "GET", "/?tab=live&cam=yard", cookie=cookies["admin"])[2]


def test_security_headers_on_every_answer(server, tunnel, cookies):
    """Pages, static files, JSON, redirects and errors: no sniffing, referrer only to the same site, no plugins, no
    <base>, framing only by the page itself; the Server header names no software version."""
    answers = [request(server, "GET", path, cookie=cookie)[:2] for path, cookie in (
        ("/", cookies["admin"]), ("/login", None), ("/healthz", None), ("/sw.js", None), ("/", None),
        ("/apple-touch-icon.png", None), ("/cloud/v1/cameras", cookies["admin"]), ("/nope", cookies["admin"]),
        ("/camera-frame/front", cookies["admin"]))]
    answers.append(request(server, "POST", "/login", "username=x&password=y")[:2])
    answers.append(request(server, "HEAD", "/apple-touch-icon.png")[:2])
    page = request(server, "GET", "/", cookie=cookies["admin"])[2]
    css = re.search(r'href="(/static/app\.css\?v=\w+)"', page).group(1)
    answers.append(request(server, "GET", css)[:2])
    answers.append(request(tunnel, "GET", "/login")[:2])
    assert len(answers) >= 12
    import socket
    with socket.create_connection(("127.0.0.1", server), timeout=5) as sock:  # a request line that cannot be parsed
        sock.sendall(b"NONSENSE\r\n\r\n")
        raw = b""
        while chunk := sock.recv(4096):
            raw += chunk
    assert raw.startswith(b"HTTP/1.") and b" 400 " in raw.split(b"\r\n")[0]
    assert b"X-Content-Type-Options: nosniff" in raw and b"frame-ancestors 'self'" in raw
    for status, headers in answers:
        assert headers["X-Content-Type-Options"] == "nosniff", status
        assert headers["Referrer-Policy"] == "same-origin", status
        assert headers["Content-Security-Policy"] == "frame-ancestors 'self'; object-src 'none'; base-uri 'none'", status
        assert headers["Server"] == "zorcam", status


def test_frame_ancestors_allow_the_cloud_app_for_the_tunnel_and_embed(server, tunnel, cookies):
    """The cloud app shows the device's Live in a frame: its address (app_url, given at pairing) is allowed through the
    tunnel and for embed=1, nowhere else; nothing strange from the file gets into the header."""
    import zorcam_cloud

    def csp(port, path="/login", **kwargs):
        return request(port, "GET", path, **kwargs)[1]["Content-Security-Policy"].split(";")[0]

    def pair(app_url):
        with zorcam_cloud.locked():
            zorcam_cloud.save({"server": "https://cloud.zorcam.test", "device_id": "dev1234567", "paired": True,
                               "url": CLOUD_URL, "app_url": app_url})
    try:
        assert csp(tunnel) == "frame-ancestors 'self'" and csp(server, "/?tab=live&embed=1", cookie=cookies["bob"]) \
            == "frame-ancestors 'self'"  # not paired
        pair("https://app.zorcam.test")
        assert csp(tunnel) == "frame-ancestors 'self' https://app.zorcam.test"
        assert csp(server, "/?tab=live&cam=front&embed=1", cookie=cookies["bob"]) \
            == "frame-ancestors 'self' https://app.zorcam.test"
        assert csp(server) == "frame-ancestors 'self'"  # the home page is not framed by the cloud
        assert csp(server, "/?tab=live", cookie=cookies["bob"]) == "frame-ancestors 'self'"
        pair("https://app.zorcam.test:8443/path?x=1")
        assert csp(tunnel) == "frame-ancestors 'self' https://app.zorcam.test:8443"
        for bad in ("javascript:alert(1)", "https://x.test; script-src *", "https://x.test https://evil.test", "", "//x.test"):
            pair(bad)
            assert csp(tunnel) == "frame-ancestors 'self'", bad
    finally:
        zorcam_cloud.remove()


def test_csrf_with_non_ascii_text_is_refused_not_a_server_error(server, cookies):
    """compare_digest on str fails for non-ASCII text: every place that checks a token must answer 403, not 500."""
    bad = "\u0436" * 8
    admin, bob = cookies["admin"], cookies["bob"]
    assert request(server, "POST", "/users/delete", urlencode({"csrf": bad, "user": "nobody"}), cookie=admin)[0] == 403
    assert request(server, "POST", "/camera/delete", urlencode({"csrf": bad, "id": "front"}), cookie=admin)[0] == 403
    assert push_post(server, bob, "/push/subscribe", {"csrf": bad}) == 403
    assert push_post(server, bob, "/settings/notify", {"csrf": bad, "camera": "all", "on": True}) == 403
    assert push_post(server, cookies["admin"], "/ptz/front", {"csrf": bad, "action": "stop"}) == 403
    assert push_post(server, bob, "/push/subscribe", {"csrf": "\ud800"}) == 403  # a lone surrogate from JSON
    # the wizard before the first sign-in: the cookie and the field
    original = app.setup_needed
    app.setup_needed = lambda: True
    try:
        for cookie in ("zorcam_setup=" + "a" * 32, None):
            status = request(server, "POST", "/setup", urlencode({"csrf": bad}), cookie=cookie)[0]
            assert status == 403, cookie
    finally:
        app.setup_needed = original


def test_session_cookie_is_secure_behind_an_https_proxy(server, monkeypatch):
    """A reverse proxy with HTTPS says so in X-Forwarded-Proto: the session cookie gets Secure at sign-in and when it
    is renewed; without the header (the home network) it is as before."""
    form = urlencode({"username": "bob", "password": PASSWORDS["bob"]})
    for headers, secure in (({}, False), ({"X-Forwarded-Proto": "https"}, True), ({"X-Forwarded-Proto": "https, http"}, True),
                            ({"X-Forwarded-Proto": "http"}, False)):
        cookie = request(server, "POST", "/login", form, headers=headers)[1]["Set-Cookie"]
        assert ("; Secure" in cookie) == secure, headers
    session = cookie.split(";")[0]
    monkeypatch.setattr(app, "SESSION_REFRESH", -10)  # every request renews the session, and the cookie with it
    for headers, secure in (({}, False), ({"X-Forwarded-Proto": "https"}, True)):
        status, answer, _ = request(server, "GET", "/settings", cookie=session, headers=headers)
        assert status == 200 and ("; Secure" in answer["Set-Cookie"]) == secure, headers
    assert not app.session_cookie("t").endswith("Secure") and app.session_cookie("t", secure=True).endswith("; Secure")


def test_connections_are_limited(monkeypatch):
    """Not more than max_connections at once: the rest are closed at once; a finished one frees its place. (The tunnel
    port: all of its clients are 127.0.0.1, and the limit holds for them too.)"""
    import socket
    monkeypatch.setattr(app.Handler, "timeout", 30)
    monkeypatch.setattr(app.Server, "max_connections", 2)
    srv = app.Server(("127.0.0.1", 0), app.Handler, tunnel=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    held = []
    try:
        for _ in range(2):  # connected and silent: the handler waits for the request line
            held.append(socket.create_connection(("127.0.0.1", port), timeout=5))
        deadline = time.time() + 5
        while srv.slots._value and time.time() < deadline:  # both places are taken
            time.sleep(0.02)
        third = socket.create_connection(("127.0.0.1", port), timeout=5)
        third.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        assert third.recv(100) == b""  # closed without an answer
        third.close()
        held.pop().close()  # one place is free again
        deadline = time.time() + 5
        while not srv.slots._value and time.time() < deadline:
            time.sleep(0.02)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/healthz")
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        for sock in held:
            sock.close()
        srv.shutdown()
        srv.server_close()


def test_the_device_itself_is_not_held_by_the_connection_limit(monkeypatch):
    """Port 80 full of connections from the network must not stop /healthz from 127.0.0.1 (the watchdog pings it:
    a refused ping restarts the gallery); the local connections do not use up places either."""
    srv = app.Server.__new__(app.Server)
    srv.tunnel = False
    for local in ("127.0.0.1", "127.0.0.5", "::1", "::ffff:127.0.0.1"):
        assert srv.exempt((local, 1)), local
    for other in ("192.168.1.5", "203.0.113.7", "2001:db8::1", "garbage"):
        assert not srv.exempt((other, 1)), other
    srv.tunnel = True
    assert not srv.exempt(("127.0.0.1", 1))  # the tunnel port: frpc is local, its clients are not
    monkeypatch.setattr(app.Server, "max_connections", 2)
    port = app.Server(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=port.serve_forever, daemon=True).start()
    try:
        for _ in range(2):  # every place taken, as 96 silent phones of the LAN would do
            assert port.slots.acquire(blocking=False)
        for _ in range(5):
            conn = http.client.HTTPConnection("127.0.0.1", port.server_address[1], timeout=5)
            conn.request("GET", "/healthz")
            assert conn.getresponse().status == 200
            conn.close()
        deadline = time.time() + 5
        while port.slots._value and time.time() < deadline:
            time.sleep(0.02)
        assert port.slots._value == 0  # nothing of the local answers was returned to the pool, nor taken from it
    finally:
        port.shutdown()
        port.server_close()


def test_host_cache_is_bounded(monkeypatch):
    """The Host header comes from the client: the name cache keeps the newest HOST_IPS_MAX names, not every one."""
    monkeypatch.setattr(app, "_host_ips", {})
    monkeypatch.setattr(app.socket, "getaddrinfo", lambda host, *a: [(2, 1, 6, "", ("203.0.113.5", 0))])
    for i in range(app.HOST_IPS_MAX + 50):
        assert app.host_ip(f"h{i}.example.org") == "203.0.113.5"
    assert len(app._host_ips) == app.HOST_IPS_MAX
    assert f"h{app.HOST_IPS_MAX + 49}.example.org" in app._host_ips and "h0.example.org" not in app._host_ips
    assert app.host_ip("192.0.2.1") == "192.0.2.1" and len(app._host_ips) == app.HOST_IPS_MAX  # addresses are not cached


def test_embedded_live_unavailable_camera(server, cookies, monkeypatch):
    """No camera, an unknown one, one closed to the user or a switched-off one: no player, the same short message."""
    zorcam_users.set_cameras("guest", {"front"})
    try:
        for lang, hdr in (("en", {}), ("ru", RU)):
            msg = t(lang, "live.embed_unavailable")
            assert msg != t("en" if lang == "ru" else "ru", "live.embed_unavailable")
            pages = [request(server, "GET", path, cookie=cookies[u], headers=hdr)[2] for u, path in (
                ("admin", "/?tab=live&embed=1"), ("admin", "/?tab=live&cam=nope&embed=1"),
                ("guest", "/?tab=live&cam=yard&embed=1"))]
            assert len({re.sub(r"\s+", " ", p) for p in pages}) == 1  # closed and missing look the same
            for page in pages:
                assert msg in page and "data-cam=" not in page and "Yard" not in page and 'class="tabbar"' not in page
    finally:
        zorcam_users.set_cameras("guest", {"front", "yard"})
    monkeypatch.setattr(app, "user_cameras", lambda user, role: [dict(CAMS[0], enabled=False)])
    body = app.render_embed("admin", "admin", "en", "front")
    assert t("en", "live.embed_unavailable") in body and "data-cam=" not in body


def test_live_with_one_camera_has_no_column_choice(monkeypatch):
    monkeypatch.setattr(app, "user_cameras", lambda user, role: CAMS[:1])
    body = app.render("live", role="admin", user="admin")
    live = live_panel(body)
    assert 'data-cam="front"' in live and "live-grid" not in live and "data-cols" not in live  # no column choice
    assert 'id="live-reload"' in live  # the reconnect button stays
    assert "camera-name link" not in live and "camtabs" not in live  # nothing to choose between


def test_camera_tabs_on_videos_and_frames(server, cookies):
    """Videos and Timelapse: a camera sub-tab (name only, no counts) inside the screen; the tab bar has no counts."""
    body = request(server, "GET", "/?tab=videos", cookie=cookies["bob"])[2]
    bar = body[body.index('<nav class="tabbar">'):body.index("</nav>", body.index('<nav class="tabbar">'))]
    assert 'class="badge"' not in bar and "Front door" not in bar
    videos = body[body.index('data-panel="videos"'):body.index('data-panel="all"')]
    assert ('<a class="pill active" href="/?tab=videos&amp;cam=front">Front door</a>'
            in videos)
    assert '<a class="pill" href="/?tab=videos&amp;cam=yard">Yard</a>' in videos
    frames = body[body.index('data-panel="all"'):]
    assert 'href="/?tab=all&amp;cam=yard">Yard</a>' in frames
    body = request(server, "GET", "/?tab=videos&cam=yard", cookie=cookies["bob"])[2]
    assert t("en", "videos.empty") in body and 'href="/watch/' not in body
    assert '<a class="pill active" href="/?tab=videos&amp;cam=yard">Yard</a>' in body


def test_watch_page_swipes_to_neighbour_clips(server, cookies):
    """Swiping on the player: left to the next (older) clip of the camera, right to the previous (newer) one,
    in the grid order; the newest has no newer one, the oldest no older one."""
    videos = app.list_videos("front")
    assert len(videos) == N_VIDEOS and videos == sorted(videos, reverse=True)

    def neighbours(name):
        body = request(server, "GET", f"/watch/front/{name}", cookie=cookies["bob"])[2]
        return re.search(r'data-newer="([^"]*)" data-older="([^"]*)"', body).groups()
    assert neighbours(videos[1]) == (f"/watch/front/{videos[0]}", f"/watch/front/{videos[2]}")
    assert neighbours(videos[0]) == ("", f"/watch/front/{videos[1]}")
    assert neighbours(videos[-1]) == (f"/watch/front/{videos[-2]}", "")


def test_clip_controls_hide_a_second_after_the_last_touch(server, cookies):
    """The clip page (and the window over the timelapse, same script) hides the system controls of the playing clip
    a second after the last touch; a tap brings them back, a pause keeps them."""
    name = app.list_videos("front")[0]
    js = assets(server, request(server, "GET", f"/watch/front/{name}", cookie=cookies["bob"])[2])
    for piece in ("var CONTROLS_HIDE_MS = 1000;", "if (!video.paused) video.controls = false;",
                  "root.querySelectorAll('video[controls]').forEach(autoHideControls);",
                  "video.addEventListener('pause', function() { clearTimeout(timer); video.controls = true; });"):
        assert piece in js, piece


def test_video_thumbnail_url_changes_with_the_poster(server, cookies):
    """The browser keeps thumbnails for a day: a poster replaced by a placeholder gets a new thumbnail URL."""
    name = app.list_videos("front")[0]
    poster = os.path.join(app.BASE_DIR, app.cam_dirs("front")[1], name + ".jpg")
    old = os.stat(poster)

    def thumb_url():
        body = request(server, "GET", "/?tab=videos&cam=front", cookie=cookies["bob"])[2]
        return re.search(r'src="(/thumb/[^"]*' + name + r'\.jpg[^"]*)"', body).group(1)
    before = thumb_url()
    try:
        os.utime(poster, (old.st_atime, old.st_mtime + 100))
        after = thumb_url()
        assert before != after and after.startswith(before.split("?")[0] + "?v=")
        assert request(server, "GET", after, cookie=cookies["bob"])[0] == 200
    finally:
        os.utime(poster, (old.st_atime, old.st_mtime))


def test_videos_are_paginated(server, cookies):
    per_page = app.VIDEOS_PER_PAGE
    body = request(server, "GET", "/?tab=videos&cam=front", cookie=cookies["bob"])[2]
    assert body.count('href="/watch/front/') == per_page
    assert f"1–{per_page} of {N_VIDEOS}" in body and "cam=front&amp;vpage=2" in body
    body = request(server, "GET", "/?tab=videos&cam=front&vpage=2", cookie=cookies["bob"])[2]
    assert body.count('href="/watch/front/') == N_VIDEOS - per_page
    body = request(server, "GET", "/?tab=videos&vpage=99", cookie=cookies["bob"])[2]  # past the end: the last page
    assert body.count('href="/watch/front/') == N_VIDEOS - per_page


def test_path_traversal_and_unknown_cameras_are_rejected(server, cookies):
    for path in ("/vid/front/..%2F..%2F.config%2Fzorcam%2Fusers.json", "/img/..%2F.config/users.json",
                 f"/watch/nope/{VIDEO}", f"/watch/yard/{VIDEO}",
                 f"/thumb/nope_videos/{VIDEO}.jpg", "/live/nope/stream.m3u8", "/live/front/x.ts"):
        assert request(server, "GET", path, cookie=cookies["admin"])[0] == 404, path
    assert request(server, "POST", "/live/webrtc?cam=nope", "v=0", cookie=cookies["admin"],
                   headers={"Content-Type": "application/sdp"})[0] == 404


def test_the_version_is_in_the_status_only(server, cookies):
    """The version number is not in the header of the gallery or the sign-in page: it is a row of Settings -> Status."""
    for path in ("/?tab=live", "/login"):
        assert f">{app.VERSION}<" not in request(server, "GET", path, cookie=cookies["admin"])[2], path
    body = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])[2]
    assert f'<div class="k">{t("en", "st.version")}</div><div class="v ">{app.VERSION}</div>' in body


def test_config_save_requires_csrf_and_validates(server, cookies):
    cookie = cookies["admin"]
    page = request(server, "GET", "/config", cookie=cookie)[2]
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    form = {f"{s}.{k}": zorcam_config.format_value(app.CFG.get(s, k)) for s, k, *_ in zorcam_config.PARAMS}

    assert request(server, "POST", "/config", urlencode({**form, "csrf": "bad"}), cookie=cookie)[0] == 403
    status, _, body = request(server, "POST", "/config",
                              urlencode({**form, "csrf": csrf, "gallery.videos_per_page": "45"}), cookie=cookie)
    assert status == 400 and t("en", "cfgpage.errors", n=1) in body
    assert not os.path.exists(zorcam_config.CONFIG_FILE)

    status, _, body = request(server, "POST", "/config",
                              urlencode({**form, "csrf": csrf, "gallery.videos_per_page": "10"}), cookie=cookie)
    try:
        assert status == 200 and t("en", "cfgpage.saved") in body
        assert zorcam_config.load().get("gallery", "videos_per_page") == 10
    finally:
        os.remove(zorcam_config.CONFIG_FILE)


def test_config_forbidden_for_viewer_post(server, cookies):
    assert request(server, "POST", "/config", "csrf=x", cookie=cookies["bob"])[0] == 403


def test_internal_motion_rejects_forwarded(server):
    status = request(server, "POST", "/internal/motion", "{}", headers=motion_headers({"X-Forwarded-For": "1.2.3.4"}))[0]
    assert status == 403


def test_internal_motion_needs_the_shared_secret(server, tunnel):
    """Only the detector (the secret in config/internal_token) may report motion: not a local process without it, not a
    wrong or non-ASCII one, and not through the tunnel port even with the right secret."""
    assert os.stat(zorcam_config.INTERNAL_TOKEN_FILE).st_mode & 0o777 == 0o600
    assert zorcam_config.internal_token() == app.INTERNAL_TOKEN
    post = {"Content-Type": "application/json"}
    assert request(server, "POST", "/internal/motion", "{}", headers=post)[0] == 403
    for bad in ("x", "\u0436" * 8, app.INTERNAL_TOKEN[:-1], app.INTERNAL_TOKEN + "0"):
        assert request(server, "POST", "/internal/motion", "{}",
                       headers={**post, "X-Zorcam-Internal": bad.encode("utf-8").decode("latin-1")})[0] == 403, bad
    assert request(server, "POST", "/internal/motion", "{}", headers=motion_headers())[0] == 200
    assert request(tunnel, "POST", "/internal/motion", "{}", headers=motion_headers())[0] == 403


def test_motion_push_names_the_camera(server, monkeypatch):
    sent = []
    monkeypatch.setattr(app, "_last_push", {})
    monkeypatch.setattr(app.PUSH, "count", lambda: 1)
    monkeypatch.setattr(app.PUSH, "send_all", lambda message, log, users=None: (sent.append(message), (1, 1))[1])
    for cam in ("yard", "yard", "front", "nope"):  # the cooldown is per camera; an unknown camera is ignored
        body = json.dumps({"name": "20260926_120000_000", "camera": cam})
        assert request(server, "POST", "/internal/motion", body, headers=motion_headers())[0] == 200
    time.sleep(0.3)
    assert sorted(m({"lang": "en"})["body"] for m in sent) == ["Front door, 12:00:00", "Yard, 12:00:00"]


def test_motion_push_opens_the_timelapse_at_the_moment(server, monkeypatch):
    sent = []
    monkeypatch.setattr(app, "_last_push", {})
    monkeypatch.setattr(app.PUSH, "count", lambda: 1)
    monkeypatch.setattr(app.PUSH, "send_all", lambda message, log, users=None: (sent.append(message), (1, 1))[1])
    body = json.dumps({"name": "20260926_120007_123", "camera": "front"})
    assert request(server, "POST", "/internal/motion", body, headers=motion_headers())[0] == 200
    time.sleep(0.3)
    msg = sent[0]({"lang": "en"})
    assert msg["url"] == "/?tab=all&cam=front&at=20260926_120007" and msg["tag"] == "motion-front"
    assert app.motion_push_url("a b/c") == "/?tab=all&cam=a%20b%2Fc"


def test_timelapse_takes_the_moment_of_a_push(server, cookies):
    """?cam=&at= from the push: the moment goes to the player (only a real YYYYMMDD_HHMMSS and only for a camera the user
    sees); an unknown or closed camera opens the ordinary Records."""
    at = "20260926_120007"
    body = request(server, "GET", f"/?tab=all&cam=front&at={at}", cookie=cookies["bob"])[2]
    assert f'data-cam="front" data-api="/timelapse/front" data-at="{at}"' in body
    for bad in ("2026", "20260926_120007x", "20261326_120007", "20260926_250007", "<b>", ""):
        body = request(server, "GET", f"/?tab=all&cam=front&at={bad}", cookie=cookies["bob"])[2]
        assert "data-at" not in body, bad
    body = request(server, "GET", f"/?tab=all&cam=nope&at={at}", cookie=cookies["bob"])
    assert body[0] == 200 and "data-at" not in body[2]
    zorcam_users.set_cameras("bob", {"front"})
    try:
        body = request(server, "GET", f"/?tab=all&cam=yard&at={at}", cookie=cookies["bob"])
        assert body[0] == 200 and "data-at" not in body[2] and 'data-cam="yard"' not in body[2]
    finally:
        zorcam_users.set_cameras("bob", {"front", "yard"})


def test_clip_window_over_the_timelapse(server, cookies):
    """The links over the strip open the clip over the player: the fragment of the watch page (video, frame, badge, scale)
    for viewers; the timelapse markup has the window with Close and Download (?dl=1 gives the file as an attachment),
    both texts in two languages; the script gets the notice texts."""
    vids = os.path.join(app.BASE_DIR, app.cam_dirs("front")[1])
    meta = "20260926_103400_000"
    path = os.path.join(vids, meta + ".json")
    try:
        with open(path, "w") as f:
            json.dump({"box": [0.25, 0.5, 0.5, 0.75], "motion_at": 3.4}, f)
        frag = request(server, "GET", f"/watch/front/{meta}?frag=1", cookie=cookies["bob"])
        assert frag[0] == 200 and "<html" not in frag[2] and "viewbar" not in frag[2]
        assert 'class="vframe" data-motion="3.4"' in frag[2] and 'class="mbadge"' in frag[2] and 'class="mscale"' in frag[2]
        assert request(server, "GET", f"/watch/front/{meta}?frag=1", cookie=cookies["guest"])[0] == 403
        assert request(server, "GET", f"/watch/nope/{meta}?frag=1", cookie=cookies["bob"])[0] == 404
    finally:
        os.remove(path)
    plain = request(server, "GET", f"/vid/front/{VIDEO}.mp4", cookie=cookies["bob"])
    assert plain[0] == 200 and "Content-Disposition" not in plain[1]
    dl = request(server, "GET", f"/vid/front/{VIDEO}.mp4?dl=1", cookie=cookies["bob"])
    assert dl[0] == 200 and dl[1]["Content-Disposition"] == f'attachment; filename="front_{VIDEO}.mp4"'
    assert request(server, "GET", f"/vid/front/{VIDEO}.mp4?dl=1", cookie=cookies["guest"])[0] == 403
    body = request(server, "GET", "/?tab=all&cam=front", cookie=cookies["bob"])[2]
    assert 'class="tl-modal" hidden' in body and 'class="tl-modal-x"' in body
    assert f'class="tl-modal-dl" download>{t("en", "tl.clip_download")}<' in body
    for cls, key in (("prev", "tl.clip_prev"), ("next", "tl.clip_next")):
        assert f'class="tl-clip-arrow tl-clip-{cls}" hidden aria-label="{t("en", key)}"' in body
        assert t("ru", key) != t("en", key)
    tljs = open(os.path.join(os.path.dirname(__file__), "..", "app", "gallery", "static", "timelapse.js")).read()
    for bit in ("function turnClip", "'ArrowLeft'", "touchend", "{ passive: true }", "batch.length"):
        assert bit in tljs
    ru = request(server, "GET", "/?tab=all&cam=front", cookie=cookies["bob"] + "; zorcam_lang=ru")[2]
    assert t("ru", "tl.clip_close") in ru and t("ru", "tl.clip_download") in ru
    assert t("ru", "tl.deleted") != t("en", "tl.deleted") and "{when}" in t("ru", "tl.deleted")
    assert t("en", "tl.deleted").format(when="26.09 12:00") == "Recordings for 26.09 12:00 have been deleted"
    assert json.dumps(t("en", "tl.deleted")) in body


def test_login_ignores_the_letter_case(server):
    """BOB, Bob and bob sign in the same user; the session belongs to the stored name."""
    status, headers, _ = request(server, "POST", "/login", urlencode({"username": "BOB", "password": PASSWORDS["bob"]}))
    assert status == 302
    cookie = headers["Set-Cookie"].split(";")[0]
    body = request(server, "GET", "/settings?tab=users", cookie=cookie)[2]
    assert "Signed in as <b>bob</b>" in body
    assert request(server, "POST", "/login", urlencode({"username": "BOB", "password": "wrong"}))[0] == 401


def test_logout(server):
    cookie = login(server, "bob")
    assert request(server, "GET", "/logout", cookie=cookie)[0] == 302
    status, headers, _ = request(server, "GET", "/", cookie=cookie)
    assert status == 302 and headers["Location"] == "/login"


# ---------------------------------------------------------------- languages (issue #5)

CYRILLIC = re.compile("[\u0400-\u04ff]")
RU = {"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
PAGES = ["/?tab=live", "/?tab=videos", "/?tab=all", "/settings", "/config", f"/watch/front/{VIDEO}",
         "/camera/new", "/camera/front", "/setup/cameras", "/setup/password"]


def visible_text(body):
    """Page text without language names, which are always in their own language."""
    for name in app.i18n.LANG_NAMES.values():
        body = body.replace(name, "")
    return body


def test_english_by_default_everywhere(server, cookies):
    """No Accept-Language (or an unsupported one): every page is English, including scripts and the login form."""
    for headers in ({}, {"Accept-Language": "de-DE,de;q=0.9"}):
        status, _, body = request(server, "GET", "/login", headers=headers)
        assert status == 200 and '<html lang="en">' in body and not CYRILLIC.search(body)
        for path in PAGES:
            status, _, body = request(server, "GET", path, cookie=cookies["admin"], headers=headers)
            assert status == 200, path
            assert '<html lang="en">' in body and "[[" not in body, path
            found = CYRILLIC.search(visible_text(body))
            assert not found, f"{path}: ...{body[max(0, found.start() - 60):found.start() + 60]}..."


def test_russian_from_the_device_language(server, cookies):
    status, _, body = request(server, "GET", "/login", headers=RU)
    assert '<html lang="ru">' in body and t("ru", "login.submit") in body
    body = request(server, "GET", "/?tab=videos", cookie=cookies["bob"], headers=RU)[2]
    assert t("ru", "tab.videos") in body and t("ru", "js.note_webrtc") in body
    per_page = app.VIDEOS_PER_PAGE
    assert t("ru", "pager.info", first=1, last=per_page, total=N_VIDEOS, page=1, pages=2) in body
    body = request(server, "GET", "/settings", cookie=cookies["admin"], headers=RU)[2]
    assert t("ru", "settings.title") in body and t("ru", "cloud.title") in body
    body = request(server, "GET", "/config", cookie=cookies["bob"], headers=RU)[2]
    assert t("ru", "denied.text") in body
    body = request(server, "GET", "/camera/new", cookie=cookies["admin"], headers=RU)[2]
    assert t("ru", "cam.tips_title") in body and t("ru", "cam.label.clip_seconds") in body


def test_language_setting_overrides_the_device(server, cookies, monkeypatch):
    monkeypatch.setattr(app, "LANG_SETTING", "ru")
    assert '<html lang="ru">' in request(server, "GET", "/login")[2]
    monkeypatch.setattr(app, "LANG_SETTING", "en")
    assert '<html lang="en">' in request(server, "GET", "/login", headers=RU)[2]


def test_config_page_offers_the_language(server, cookies):
    body = request(server, "GET", "/config", cookie=cookies["admin"], headers=RU)[2]
    assert '<select name="language.language">' in body
    assert t("ru", "cfgpage.lang.auto") in body and all(n in body for n in app.i18n.LANG_NAMES.values())
    assert "rtsp_url" not in body and "rtsp_user" not in body  # cameras are not in zorcam.conf any more


def test_config_errors_are_translated(server, cookies):
    cookie = cookies["admin"]
    page = request(server, "GET", "/config", cookie=cookie, headers=RU)[2]
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    form = {f"{s}.{k}": zorcam_config.format_value(app.CFG.get(s, k)) for s, k, *_ in zorcam_config.PARAMS}
    body = request(server, "POST", "/config", urlencode({**form, "csrf": csrf, "capture.stall_timeout": "x"}),
                   cookie=cookie, headers=RU)[2]
    assert t("ru", "cfg.not_a_number") in body
    body = request(server, "POST", "/config", urlencode({**form, "csrf": csrf, "capture.stall_timeout": "x"}),
                   cookie=cookie)[2]
    assert t("en", "cfg.not_a_number") in body


def test_push_subscription_remembers_the_device_language(server, cookies):
    key = app.webpush.b64url(app.webpush._raw_public(app.webpush.ec.generate_private_key(app.webpush.ec.SECP256R1())))
    endpoint = "https://web.push.apple.com/ru-phone"
    body = app.json.dumps({"subscription": {"endpoint": endpoint, "keys": {"p256dh": key, "auth": "AAAAAAAAAAAAAAAAAAAAAA"}},
                           "quiet": True, "csrf": csrf_of_session(cookies["bob"])})
    status = request(server, "POST", "/push/subscribe", body, cookie=cookies["bob"],
                     headers={**RU, "Content-Type": "application/json"})[0]
    try:
        assert status == 200
        assert app.PUSH.subscriptions[endpoint]["lang"] == "ru"
    finally:
        app.PUSH.remove(endpoint)


def test_push_language(monkeypatch):
    monkeypatch.setattr(app, "LANG_SETTING", "auto")
    assert app.push_lang({"user": "bob", "lang": "en"}) == "en"
    assert app.push_lang({"user": "bob", "lang": "ru"}) == "ru"
    assert app.push_lang({"user": "bob"}) == "en"  # no language anywhere: English
    monkeypatch.setattr(app, "LANG_SETTING", "en")
    assert app.push_lang({"user": "bob", "lang": "ru"}) == "en"
    # the language picked on that device wins over the default and the device language
    assert app.push_lang({"user": "bob", "lang": "en", "pick": "ru"}) == "ru"


def test_health_events_follow_the_language_and_send_no_push(server, monkeypatch):
    sent = []
    monkeypatch.setattr(app.PUSH, "send_all", lambda message, log, users=None: sent.append(message))
    health = app.Health()
    health.evaluate({"disk": (False, app.Msg("health.free", free=lambda lang: app.fmt_bytes(10 * 1048576, lang))),
                     "frames:yard": (False, app.Msg("health.no_frames_yet"))})
    health.evaluate({"disk": (True, app.Msg("health.running"))})  # the yard camera was deleted meanwhile
    assert sent == []  # problems and recoveries are only in Status, never a push
    assert "frames:yard" not in health.problems
    events = [(title, text, ok) for _ts, title, text, ok in health.events]
    assert len(events) == 3
    assert events[0][0].text("en") == "Recovered: Disk space" and events[0][2] is True
    assert events[0][1].text("en") == t("en", "health.running")
    problem = [e for e in events if e[0].text("en") == "Last problem: Disk space"]
    assert problem and problem[0][1].text("en") == "10.0 MB free" and not problem[0][2]
    assert "10.0 " + t("ru", "units.mb") in problem[0][1].text("ru")
    assert events[-1][0].text("ru") == t("ru", "health.event_problem", title=t("ru", "check.disk")) and not events[-1][2]


def test_detector_check_says_how_long_without_frames(server, monkeypatch):
    """The camera check is about the motion detector (not saved snapshots): "Front door: motion detector:
    no frames for 40 min" when it gets no frames, the time of the last one otherwise."""
    health = app.Health()
    now = time.time()
    for age, en, key in ((40 * 60, "no frames for 40 min", "health.no_frames_for"), (30, "last frame 30 s ago",
                                                                                       "health.last_frame")):
        monkeypatch.setattr(health, "capture_status", lambda cam_id, age=age: {"last_frame": now - age})
        monkeypatch.setattr(app, "run_quiet", lambda cmd: "")
        checks = health.run_checks()
        ok, text = checks["frames:" + app.cameras()[0]["id"]]
        assert ok == (age < 60) and text.text("en") == en
        assert text.text("ru") == t("ru", key, ago=app.fmt_ago(age, "ru"))
    assert app.check_title("frames:" + app.cameras()[0]["id"]).text("en").endswith(": motion detector")


def test_status_events_show_the_problem_in_red(server, cookies, monkeypatch):
    """Status: "Last problem: <check>" with the problem text in red under it, "Recovered: <check>" in green."""
    now = time.time()
    monkeypatch.setattr(app.HEALTH, "events", app.collections.deque([
        (now, app.Msg("health.event_recovered", title=app.Msg("check.disk")), app.Msg("health.running"), True),
        (now - 60, app.Msg("health.event_problem", title=app.check_title("frames:front")),
         app.Msg("health.no_frames_yet"), False)]))
    body = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"], headers=RU)[2]
    title = t("ru", "health.event_problem", title=t("ru", "check.frames_of", name="Front door"))
    assert f'<div class="k">{title}<span class="sub bad">{t("ru", "health.no_frames_yet")}</span></div>' in body
    title = t("ru", "health.event_recovered", title=t("ru", "check.disk"))
    assert f'<div class="k">{title}<span class="sub ok">{t("ru", "health.running")}</span></div>' in body


def test_status_last_camera_error_spans_the_row(server, cookies, monkeypatch):
    """Status: "Last camera error" and its time are a line of their own, the long ffmpeg text is under it full width."""
    at = time.time()
    monkeypatch.setattr(app.HEALTH, "capture_status", lambda cam_id: {
        "state": "reconnecting", "last_error_at": at, "last_error": "could not connect: camera: Network is unreachable"})
    body = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"], headers=RU)[2]
    assert (f'<div class="row wide"><div class="wide-head"><div class="k">{t("ru", "st.last_error")}</div>'
            f'<div class="v">{app.fmt_clock(at)}</div></div>'
            '<div class="wide-text">could not connect: camera: Network is unreachable</div></div>') in body


def test_capture_errors_are_translated():
    st = {"last_error": "no frames for 15 s: x", "last_error_msg": {
        "reason": {"key": "capture.stall", "kw": {"seconds": 15}}, "detail": "Connection refused"}}
    assert app.capture_error(st).text("en") == "no frames for 15 s: Connection refused"
    assert app.capture_error(st).text("ru") == t("ru", "capture.stall", seconds=15) + ": Connection refused"
    assert app.capture_error({"last_error": "old detector text"}).text("ru") == "old detector text"
    assert app.capture_error({}) is None


def test_language_is_the_first_section(server, cookies):
    for path in ("/settings", "/config"):
        for headers, lang in (({}, "en"), (RU, "ru")):
            body = request(server, "GET", path, cookie=cookies["admin"], headers=headers)[2]
            first = re.search(r'<section><h2>(.*?)</h2>(.*?)</section>', body, re.S)
            assert first.group(1) == t(lang, "section.language"), path
            if path == "/config":
                assert first.group(2).count('class="row field') == 1  # a single setting: the language


def lang_cookie(cookie, value):
    """The session cookie plus the language picked on the device (the cookie Settings sets)."""
    return f"{cookie}; {app.LANG_COOKIE}={value}" if cookie else f"{app.LANG_COOKIE}={value}"


def test_language_is_kept_on_the_device(server, cookies):
    """The language picked on Settings is a cookie of the device (the app or the browser), not a setting on
    the Pi: it applies to every page of that device, even before sign-in, and not to other devices of the user."""
    body = request(server, "GET", "/settings", cookie=cookies["guest"])[2]
    assert f'data-lang-pick data-cookie="{app.LANG_COOKIE}"' in body and "max-age=31536000" in assets(server, body)
    assert "fetch('/language'" not in body
    assert request(server, "POST", "/language", "{}", cookie=cookies["guest"],
                   headers={"Content-Type": "application/json"})[0] == 404  # nothing is stored on the Pi
    ru = lang_cookie(cookies["guest"], "ru")
    for headers in ({}, {"Accept-Language": "en-US"}):  # whatever the device language is
        assert '<html lang="ru">' in request(server, "GET", "/?tab=live", cookie=ru, headers=headers)[2]
    assert '<html lang="ru">' in request(server, "GET", "/login", cookie=lang_cookie(None, "ru"))[2]
    assert '<html lang="en">' in request(server, "GET", "/?tab=live", cookie=cookies["guest"])[2]  # another device
    assert '<html lang="ru">' in request(server, "GET", "/", cookie=cookies["guest"], headers=RU)[2]  # auto
    assert '<html lang="en">' in request(server, "GET", "/", cookie=lang_cookie(cookies["guest"], "de"))[2]


def test_language_pickers(server, cookies):
    for user in ROLES_ORDER:  # on Settings for everyone, liveviewer included
        body = request(server, "GET", "/settings", cookie=cookies[user])[2]
        assert "data-lang-pick" in body and '<option value="auto" selected>' in body, user
    body = request(server, "GET", "/settings", cookie=lang_cookie(cookies["admin"], "ru"))[2]
    section = re.search(r'<section><h2>(.*?)</h2>(.*?)</section>', body, re.S)
    assert section.group(1) == t("ru", "section.language")
    assert '<option value="ru" selected>' in section.group(2) and 'class="src"' not in section.group(2)


def test_push_subscription_keeps_the_language_of_the_device(server, cookies):
    """The app subscribes again (quietly) every time it opens: the language picked on the device reaches push."""
    key = app.webpush.b64url(app.webpush._raw_public(app.webpush.ec.generate_private_key(app.webpush.ec.SECP256R1())))
    sub = {"endpoint": "https://web.push.apple.com/device-lang", "keys": {"p256dh": key, "auth": "AAAAAAAAAAAAAAAAAAAAAA"}}
    try:
        for cookie, pick in ((lang_cookie(cookies["bob"], "ru"), "ru"), (cookies["bob"], None)):
            status = request(server, "POST", "/push/subscribe", app.json.dumps({"subscription": sub, "quiet": True,
                                                                          "csrf": csrf_of_session(cookie)}),
                             cookie=cookie, headers={"Content-Type": "application/json"})[0]
            assert status == 200 and app.PUSH.subscriptions[sub["endpoint"]].get("pick") == pick
    finally:
        app.PUSH.remove(sub["endpoint"])


# ---------------------------------------------------------------- recordings of each camera

@pytest.fixture
def recordings(tmp_path, monkeypatch):
    """A separate BASE_DIR: frames and clips of both cameras 30 min, 5 h, 2 days and 10 days old."""
    from datetime import datetime, timedelta
    monkeypatch.setattr(app, "BASE_DIR", str(tmp_path))
    dirs = {}
    for cam in ("front", "yard"):
        raw, vids = tmp_path / cam, tmp_path / f"{cam}_videos"
        raw.mkdir()
        vids.mkdir()
        for age in (timedelta(minutes=30), timedelta(hours=5), timedelta(days=2), timedelta(days=10)):
            name = (datetime.now() - age).strftime("%Y%m%d_%H%M%S_000")
            (raw / f"{name}.jpg").write_bytes(b"f" * 100)
            (vids / f"{name}.jpg").write_bytes(b"p" * 10)
            (vids / f"{name}.mp4").write_bytes(b"v" * 1000)
        dirs[cam] = (raw, vids)
    return dirs


def count(path):
    return len(list(path.iterdir()))


def delete(port, cookie, cam, scope, csrf):
    return request(port, "POST", "/storage/delete", urlencode({"csrf": csrf, "cam": cam, "scope": scope}), cookie=cookie)


def settings_csrf(port, cookie):
    return csrf_of(request(port, "GET", "/settings?tab=cameras", cookie=cookie)[2], "/storage/delete")


def test_each_camera_has_its_recordings_section(server, cookies, recordings):
    """One «Recordings» group; in it each camera — a head row with its name, the counters and two «Delete all»."""
    app.RECORDINGS_STATS.clear()
    placeholder = request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    assert placeholder.count(f'<div class="v">{t("en", "rec.counting")}</div>') == 2 * len(CAMS)  # not counted yet
    app.HEALTH.cleanup()
    body = request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    section = re.search(r"<section><h2>" + re.escape(t("en", "rec.section")) + r"</h2>(.*?)</section>", body, re.S).group(1)
    parts = section.split('<div class="row all">')[1:]
    assert len(parts) == len(CAMS)
    for cam, part in zip(CAMS, parts, strict=True):
        assert part.startswith(f'<div class="k">{cam["name"]}</div>')
        assert part.count('action="/storage/delete"') == 3  # snapshots, videos, both
        assert part.count(f">{t('en', 'rec.delete')}</button>") == 2
        assert part.count(f">{t('en', 'rec.delete_all')}</button>") == 1
        assert part.index('value="frames"') < part.index('value="videos"') < part.index('value="all"')
        assert f'name="cam" value="{cam["id"]}"' in part
        assert f'<div class="rc"><div class="k">{t("en", "tab.all")}</div><div class="v">4 (' in part
        assert f'<div class="rc"><div class="k">{t("en", "tab.videos")}</div><div class="v">4 (' in part
        assert part.index('class="rc"') < part.index("<form")  # values on top, delete buttons under them
    assert "confirm(" in body


def test_cameras_tab_does_not_walk_the_recordings(server, cookies, recordings):
    """Hundreds of thousands of frames: the tab shows what the cleanup counted and never scans a folder itself;
    a delete zeroes the list at once."""
    app.HEALTH.cleanup()

    def scan(*_a, **_k):
        raise AssertionError("the Cameras tab must not scan the recordings")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app.os, "scandir", scan)
        body = request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    assert body.count('<div class="v">4 (') >= 2 and t("en", "rec.counting") not in body
    assert delete(server, cookies["admin"], "front", "all", csrf_of(body, "/storage/delete"))[0] == 303
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app.os, "scandir", scan)
        body = request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    assert app.RECORDINGS_STATS["front"][:2] == (0, 0) and app.RECORDINGS_STATS["front_videos"][:2] == (0, 0)
    assert '<div class="v">0 (' in body


SETTINGS_TABS = {
    "general": [t("en", "section.language"), t("en", "cloud.title"), t("en", "ya.title"), t("en", "mqtt.title")],
    "notify": [t("en", "notify.section")],
    "cameras": [t("en", "cams.section"), t("en", "rec.section")],
    "status": [t("en", "st.section.community")],
    "users": [t("en", "setup.user_title.admin"), "bob", "guest", t("en", "users.new_title")],
}


def test_settings_tabs_of_admin(server, cookies):
    """Settings are split into tabs: General, Notifications, Cameras, Status, Users; Sign out is on the last one.
    Nothing hidden on a wide screen (only laid out in two columns), no link to /config."""
    for tab, sections in SETTINGS_TABS.items():
        body = request(server, "GET", f"/settings?tab={tab}", cookie=cookies["admin"])[2]
        assert re.findall(r'<a (?:class="(active)" )?href="/settings\?tab=(\w+)">', body) == [
            ("active" if key == tab else "", key) for key in SETTINGS_TABS], tab
        assert re.findall(r"<section(?: data-csrf=\"\w+\")?><h2>(.*?)</h2>", body) == sections, tab
        assert ('<div class="signout"><a href="/logout">' in body) == (tab == "users"), tab
        assert ('id="ptr"' in body) == (tab in ("cameras", "status")), tab  # pull to refresh
        assert (t("en", "users.devices") in body) == (tab == "users"), tab
        assert ('<a class="row action" href="/camera/new">' in body) == (tab == "cameras"), tab
        assert "desk" not in body and 'href="/config"' not in body and 'class="intro' not in body
        # the only width query is the wide screen from 1024 px (and hover); below it the page is the phone's one
        assert 'class="src"' not in body and ".src" not in body
        assert not re.search(r"@media\s*\((?!min-width: 1024px)", assets(server, body))
        # a phone-wide column
        assert '<div class="viewbar"><div class="bar">' in body and "max-width:560px" in assets(server, body)
    # the header clock may tick between the two requests
    def page(path):
        return re.sub(r">\d\d:\d\d:\d\d<", ">00:00:00<", request(server, "GET", path, cookie=cookies["admin"])[2])
    assert page("/settings") == page("/settings?tab=nope").replace("tab=nope", "tab=general")


def user_section(body, name):
    """The Users tab section of the user (by the login shown in it)."""
    for section in re.findall(r"<section>(.*?)</section>", body, re.S):
        if f'name="user" value="{name}"' in section:
            return section
    return None


def test_users_tab_shows_every_user(server, cookies):
    """Users (admin): admin first (a password only), then every user by name: access (Live and the gallery
    or Live only), a password to set, a switch for each camera, Delete; then New user."""
    body = request(server, "GET", "/settings?tab=users", cookie=cookies["admin"], headers=RU)[2]
    assert t("ru", "account.signed_in", user="<b>admin</b>") in body
    for name, role in (("admin", "admin"), ("bob", "viewer"), ("guest", "liveviewer")):
        section = user_section(body, name)
        title = t("ru", "setup.user_title.admin") if role == "admin" else name
        assert f"<h2>{title}</h2>" in section and f'<div class="v">{name}</div>' in section, name
        assert f'placeholder="{t("ru", "cfgpage.password_placeholder")}"' in section, name
        if role == "admin":
            assert t("ru", "role.admin") in section and 'name="role"' not in section
        else:
            assert f'<option value="{role}" selected>{t("ru", "role." + role)}</option>' in section, name
        switches = re.findall(r'name="cam" value="(\w+)" checked', section)
        assert switches == ([] if role == "admin" else ["front", "yard"]), name
        assert ('action="/users/delete"' in section) == (role != "admin"), name
    assert body.index(t("ru", "users.new_title")) > body.index('name="user" value="guest"')
    assert body.index(t("ru", "account.signed_in", user="<b>admin</b>")) < body.index('href="/logout"') \
        < body.index(t("ru", "users.devices")) \
        < body.index('name="user" value="admin"')  # Sign out is in the "Signed in as" block, then the users group


def users_form(port, cookie, **fields):
    page = request(port, "GET", "/settings?tab=users", cookie=cookie)[2]
    return request(port, "POST", "/users/save", urlencode({"csrf": csrf_of(page, "/users/save"), **fields}, doseq=True),
                   cookie=cookie)


def test_camera_access_per_user(server, cookies, recordings, monkeypatch):
    """A camera switched off for a user disappears from Live, Videos and Timelapse, its streams and files answer 404,
    motion on it sends this user no push. admin sees everything."""
    status, headers, _ = users_form(server, cookies["admin"], user="bob", role="viewer", cam=["front"])
    try:
        assert status == 303 and headers["Location"] == "/settings?tab=users&saved=bob"
        assert zorcam_users.cameras_of("bob") == {"front"}
        body = request(server, "GET", "/settings?tab=users&saved=bob", cookie=cookies["admin"])[2]
        assert t("en", "users.saved", name="bob") in body
        assert re.findall(r'name="cam" value="(\w+)"( checked)?', user_section(body, "bob")) == [("front", " checked"),
                                                                                               ("yard", "")]
        live = request(server, "GET", "/?tab=live", cookie=cookies["bob"])[2]
        assert 'data-cam="front"' in live and 'data-cam="yard"' not in live
        videos = request(server, "GET", "/?tab=videos&cam=yard", cookie=cookies["bob"])[2]
        assert "cam=yard" not in videos and "/yard_videos/" not in videos and "cam=front" in videos
        raw, vids = recordings["yard"]
        frame, clip = sorted(os.listdir(raw))[0], sorted(n for n in os.listdir(vids) if n.endswith(".mp4"))[0]
        for path in ("/live/yard/stream.m3u8", f"/watch/yard/{clip[:-4]}", f"/vid/yard/{clip}",
                     f"/img/yard/{frame}", f"/thumb/yard_videos/{clip[:-4]}.jpg"):
            assert request(server, "GET", path, cookie=cookies["bob"])[0] == 404, path
            if not path.startswith("/live/"):  # (HLS would start ffmpeg)
                assert request(server, "GET", path, cookie=cookies["admin"])[0] == 200, path
        assert request(server, "POST", "/live/webrtc?cam=yard", "v=0", cookie=cookies["bob"],
                       headers={"Content-Type": "application/sdp"})[0] == 404
        assert "bob" not in app.push_users("yard") and "bob" in app.push_users("front")
        assert "yard" in [c["id"] for c in app.user_cameras("admin", "admin")]
    finally:
        zorcam_users.set_cameras("bob", {"front", "yard"})
    assert "bob" in app.push_users("yard")


def test_no_camera_left_for_a_user(server, cookies):
    zorcam_users.set_cameras("guest", set())
    try:
        live = request(server, "GET", "/?tab=live", cookie=cookies["guest"], headers=RU)[2]
        assert t("ru", "cams.no_access") in live and "data-cam=" not in live
    finally:
        zorcam_users.set_cameras("guest", {"front", "yard"})


def test_a_new_password_ends_the_other_sessions(server, cookies):
    PASSWORDS["carol"] = "carol-pass"
    zorcam_users.set_user("carol", "viewer", PASSWORDS["carol"])
    try:
        carol = login(server, "carol")
        status, _, body = users_form(server, cookies["admin"], user="carol", role="viewer", password="new-pass", repeat="other")
        assert status == 400 and t("en", "setup.mismatch") in user_section(body, "carol")
        status, _, body = users_form(server, cookies["admin"], user="carol", role="viewer", password="short", repeat="short")
        assert status == 400 and t("en", "setup.short") in body
        assert request(server, "GET", "/?tab=live", cookie=carol)[0] == 200  # nothing changed yet
        status, _, _ = users_form(server, cookies["admin"], user="carol", role="viewer", password="new-pass", repeat="new-pass",
                                  cam=["front", "yard"])
        assert status == 303 and zorcam_users.verify("carol", "new-pass") == "viewer"
        assert request(server, "GET", "/?tab=live", cookie=carol)[0] == 302  # signed out on the other device
        # admin changes the own password: the own session stays
        status, _, _ = users_form(server, cookies["admin"], user="admin", password="admin-new", repeat="admin-new")
        assert status == 303 and request(server, "GET", "/settings", cookie=cookies["admin"])[0] == 200
    finally:
        zorcam_users.set_user("admin", "admin", PASSWORDS["admin"])
        zorcam_users.delete_user("carol")
        del PASSWORDS["carol"]


def test_create_change_and_delete_a_user(server, cookies):
    """New user: a name, Live and the gallery or Live only, a password, the cameras (all by default).
    admin is the only administrator: no second one, the name admin is taken. Then the role changes without
    the password; Delete removes the user."""
    try:
        body = request(server, "GET", "/settings?tab=users", cookie=cookies["admin"], headers=RU)[2]
        new = body[body.index(t("ru", "users.new_title")):]
        assert 'name="name" value=""' in new and 'name="new" value="1"' in new
        assert re.findall(r'<option value="(\w+)"', new) == ["viewer", "liveviewer"]  # no admin to choose
        assert re.findall(r'name="cam" value="(\w+)" checked', new) == ["front", "yard"]
        for name, err in (("admin", "users.bad_name"), ("Admin", "users.bad_name"), ("with space", "users.bad_name"),
                          ("bob", "users.name_taken"), ("Bob", "users.name_taken")):
            status, _, body = users_form(server, cookies["admin"], new="1", name=name, role="viewer",
                                         password="nanny-pass", repeat="nanny-pass")
            assert status == 400 and t("en", err, n=32) in body, name
        status, _, body = users_form(server, cookies["admin"], new="1", name="nanny", role="liveviewer",
                                     password="", repeat="", cam=["yard"])
        assert status == 400 and t("en", "setup.short") in body and zorcam_users.role_of("nanny") is None
        new = body[body.index(t("en", "users.new_title")):]  # what was entered stays, but the password
        assert 'name="name" value="nanny"' in new and '<option value="liveviewer" selected>' in new
        assert re.findall(r'name="cam" value="(\w+)"( checked)?', new) == [("front", ""), ("yard", " checked")]
        assert users_form(server, cookies["admin"], new="1", name="nanny", role="admin", password="nanny-pass",
                          repeat="nanny-pass")[0] == 400  # no second administrator
        status, headers, _ = users_form(server, cookies["admin"], new="1", name="nanny", role="liveviewer",
                                        password="nanny-pass", repeat="nanny-pass", cam=["yard"])
        assert status == 303 and headers["Location"] == "/settings?tab=users&saved=nanny"
        assert zorcam_users.verify("nanny", "nanny-pass") == "liveviewer" and zorcam_users.cameras_of("nanny") == {"yard"}
        # the role changes, the password stays
        status, _, _ = users_form(server, cookies["admin"], user="nanny", role="viewer", cam=["front", "yard"])
        assert status == 303 and zorcam_users.verify("nanny", "nanny-pass") == "viewer"
        assert zorcam_users.cameras_of("nanny") == {"front", "yard"}
        assert users_form(server, cookies["admin"], user="nanny", role="admin")[0] == 400
        assert users_form(server, cookies["admin"], user="admin", password="", repeat="")[0] == 303  # admin stays admin
        assert zorcam_users.role_of("admin") == "admin"
        assert request(server, "POST", "/users/save", urlencode({"csrf": "x", "new": "1", "name": "x"}),
                       cookie=cookies["bob"])[0] == 403
        page = request(server, "GET", "/settings?tab=users", cookie=cookies["admin"])[2]
        csrf = csrf_of(page, "/users/delete")
        assert request(server, "POST", "/users/delete", urlencode({"csrf": csrf, "user": "admin"}),
                       cookie=cookies["admin"])[0] == 400  # admin cannot be deleted
        status, headers, _ = request(server, "POST", "/users/delete", urlencode({"csrf": csrf, "user": "nanny"}),
                                     cookie=cookies["admin"])
        assert status == 303 and headers["Location"] == "/settings?tab=users" and zorcam_users.role_of("nanny") is None
    finally:
        zorcam_users.delete_user("nanny")


def test_delete_is_protected(server, cookies, recordings):
    csrf = settings_csrf(server, cookies["admin"])
    assert delete(server, cookies["admin"], "front", "frames", "bad")[0] == 403
    assert delete(server, cookies["admin"], "front", "forever", csrf)[0] == 400
    assert delete(server, cookies["admin"], "front", "1w", csrf)[0] == 400  # no "older than" deletion any more
    assert delete(server, cookies["admin"], "nope", "frames", csrf)[0] == 400
    assert delete(server, cookies["bob"], "front", "frames", csrf)[0] == 403
    assert delete(server, None, "front", "frames", csrf)[0] == 302
    assert all(count(raw) == 4 and count(vids) == 8 for raw, vids in recordings.values())


def test_delete_frames_or_videos_of_one_camera(server, cookies, recordings):
    csrf = settings_csrf(server, cookies["admin"])
    status, headers, body = delete(server, cookies["admin"], "front", "frames", csrf)
    assert status == 303 and headers["Location"] == "/settings?tab=cameras" and body == ""  # no page "Deleted"
    assert count(recordings["front"][0]) == 0 and count(recordings["front"][1]) == 8
    status, headers, _ = delete(server, cookies["admin"], "yard", "videos", csrf)
    assert status == 303 and headers["Location"] == "/settings?tab=cameras"
    assert count(recordings["yard"][0]) == 4 and count(recordings["yard"][1]) == 0


def test_delete_all_recordings_of_one_camera(server, cookies, recordings):
    """«Delete all» under both columns: snapshots and videos of that camera, the other camera keeps its own."""
    csrf = settings_csrf(server, cookies["admin"])
    status, headers, _ = delete(server, cookies["admin"], "front", "all", csrf)
    assert status == 303 and headers["Location"] == "/settings?tab=cameras"
    assert count(recordings["front"][0]) == 0 and count(recordings["front"][1]) == 0
    assert count(recordings["yard"][0]) == 4 and count(recordings["yard"][1]) == 8


def test_cleanup_covers_every_camera_folder(recordings, monkeypatch):
    cams = [dict(c) for c in app.cameras()]
    yard = next(c for c in cams if c["id"] == "yard")
    yard.update(frames_days=3, videos_days=1)  # per camera; front keeps 1 day of snapshots and the default 5 days of videos
    next(c for c in cams if c["id"] == "front")["frames_days"] = 1
    monkeypatch.setattr(app, "cameras", lambda: cams)
    old = recordings["yard"][0].parent / "removed_camera"  # a camera deleted from cameras.json: the defaults
    old.mkdir()
    (old / "20200101_000000_000.jpg").write_bytes(b"x")
    os.utime(old / "20200101_000000_000.jpg", (0, 0))
    for raw, vids in recordings.values():
        for f in list(raw.iterdir()) + list(vids.iterdir()):
            age = time.time() - app.parse_ts(f.name).timestamp()
            os.utime(f, (time.time() - age, time.time() - age))
    app.Health().cleanup()
    assert count(old) == 0
    assert count(recordings["front"][0]) == 2 and count(recordings["front"][1]) == 6  # 1 day, 5 days
    assert count(recordings["yard"][0]) == 3 and count(recordings["yard"][1]) == 4   # 3 days, 1 day


def test_back_buttons_do_not_use_history(server, cookies, recordings):
    """Issue #10: Settings has the tab bar (its Live tab leads to Live), /config goes Back to Settings; result pages
    replace themselves in history (deleting recordings has none: it goes straight back)."""
    def back(body):
        return re.search(r'<a class="back" href="([^"]+)"', body).group(1)
    for user in ("admin", "bob"):
        body = request(server, "GET", "/settings", cookie=cookies[user])[2]
        assert 'class="back"' not in body and '<a class="tab" href="/?tab=live">' in body, user
    assert back(request(server, "GET", "/config", cookie=cookies["admin"])[2]) == "/settings"
    assert back(request(server, "GET", "/camera/front", cookie=cookies["admin"])[2]) == "/settings?tab=cameras"
    assert back(request(server, "GET", "/config", cookie=cookies["bob"])[2]) == "/?tab=live"  # access denied
    status, headers, _ = delete(server, cookies["admin"], "front", "videos", settings_csrf(server, cookies["admin"]))
    assert status == 303 and headers["Location"] == "/settings?tab=cameras"  # no result page to go back from
    assert "history.back" not in app.TEMPLATES["settings"] + app.ASSETS["settings.js"][0].decode()


# ---------------------------------------------------------------- cameras

def fake_camera(user, password):
    from test_config import _fake_camera
    return _fake_camera(user, password, "digest")


def camera_form(port, cookie, **fields):
    page = request(port, "GET", "/camera/new", cookie=cookie)[2]
    form = {"csrf": csrf_of(page, "/camera/save"), "edit": "", "setup": "", "id": "", "name": "", "rtsp_url": "",
            "rtsp_user": "", "password": "", **fields}
    return request(port, "POST", "/camera/save", urlencode(form), cookie=cookie)


@pytest.fixture
def restore_cameras():
    yield
    zorcam_config.save_cameras(CAMS)
    for key in [k for k in zorcam_config.read_secrets() if k.startswith("ZORCAM_CAMERA_PASSWORD_")]:
        zorcam_config.remove_secret(key)


def test_switch_a_camera_off_and_on(server, cookies, restore_cameras):
    """«Camera on»: off — no Live, no cloud tile, no frame check; its recordings stay; a form without the switch
    (an old page) keeps it as it was."""
    def save(enabled, force=False):
        page = request(server, "GET", "/camera/yard", cookie=cookies["admin"])[2]
        form = [("csrf", csrf_of(page, "/camera/save")), ("edit", "yard"), ("setup", ""), ("id", "yard"),
                ("name", "Yard"), ("rtsp_url", "rtsp://127.0.0.1:1/yard"), ("rtsp_user", ""), ("password", "")]
        form += [("enabled", v) for v in enabled] + ([("force", "1")] if force else [])
        return request(server, "POST", "/camera/save", urlencode(form), cookie=cookies["admin"])

    page = request(server, "GET", "/camera/yard", cookie=cookies["admin"])[2]
    assert '<input type="hidden" name="enabled" value="no"><input type="checkbox" class="switch" name="enabled" ' \
           'value="yes" checked>' in page
    assert save(["no"])[0] in (302, 303)  # the switch unticked: only the hidden "no" is sent
    assert not app.camera("yard")["enabled"]
    body = request(server, "GET", "/?tab=live", cookie=cookies["admin"])[2]
    live = body[body.index('data-panel="live"'):body.index('data-panel="videos"')]
    assert 'data-cam="front"' in live and 'data-cam="yard"' not in live
    assert "cam=yard" in body  # its recordings tabs stay
    assert [c["id"] for c in json.loads(request(server, "GET", "/cloud/v1/cameras", cookie=cookies["admin"])[2])
            ["cameras"]] == ["front"]
    assert "Yard<span class=\"sub\">127.0.0.1 · <span class=\"off\">switched off</span></span>" in request(
        server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    assert save([])[0] in (302, 303) and not app.camera("yard")["enabled"]  # no switch in the form: as it was
    assert save(["no", "yes"])[0] == 400  # switched on again: the camera is checked first (none at this address)
    assert save(["no", "yes"], force=True)[0] in (302, 303) and app.camera("yard")["enabled"]


def test_camera_list_shows_whether_a_camera_works(server, cookies, monkeypatch):
    """Camera list: an enabled camera is "working" (green) or "not working" (red) by the monitor's frames check;
    undecided or switched off - no such status."""
    def page():
        return request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    ok, bad = (True, app.Msg("health.running")), (False, app.Msg("health.no_frames_yet"))
    monkeypatch.setattr(app.HEALTH, "checks", {})
    monkeypatch.setattr(app.HEALTH, "capture_status", lambda cam_id: {"last_frame": time.time()})
    assert 'class="working"' not in page() and 'class="broken"' not in page()  # monitor has not decided yet
    monkeypatch.setattr(app.HEALTH, "checks", {"frames:front": ok})
    assert '<span class="working">working</span>' in page()
    monkeypatch.setattr(app.HEALTH, "capture_status", lambda cam_id: None)
    assert 'class="working"' not in page()  # no frame yet
    monkeypatch.setattr(app.HEALTH, "checks", {"frames:front": bad})
    assert '<span class="broken">not working</span>' in page()
    cams = app.cameras()
    monkeypatch.setattr(app, "cameras", lambda: [{**c, "enabled": False} for c in cams])
    body = page()
    assert 'class="broken"' not in body and 'class="working"' not in body and 'switched off' in body


def test_sound_in_live_as_the_camera_sends_it(server, cookies, restore_cameras, monkeypatch):
    """"Sound in Live": a sound button on that camera's player, WebRTC asks for the sound (go2rtc adds it only
    as it is: G.711, Opus); HLS copies only AAC, anything else stays silent — nothing is re-encoded on the Pi."""
    page = request(server, "GET", "/camera/yard", cookie=cookies["admin"])[2]
    assert 'name="audio" value="yes">' in page and t("en", "cam.hint.audio") in page
    cams = app.cameras()
    monkeypatch.setattr(app, "cameras", lambda: [{**c, "audio": c["id"] == "yard"} for c in cams])
    body = request(server, "GET", "/?tab=live", cookie=cookies["admin"])[2]
    assert '<div class="live-wrap" data-cam="yard" data-audio>' in body
    assert '<div class="live-wrap" data-cam="front">' in body and body.count('class="live-sound"') == 1
    js = assets(server, body)
    assert "if (withAudio) conn.addTransceiver('audio'" in js and "media.addTrack(e.track)" in js

    probed = []
    monkeypatch.setattr(app, "camera_audio_codec", lambda cam_id: probed.append(cam_id) or codec[0])
    codec = ["aac"]
    assert app.hls_audio_args({"id": "yard", "audio": True}) == ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy"]
    for codec[0] in ("pcma", None):
        assert app.hls_audio_args({"id": "yard", "audio": True}) == ["-an", "-c:v", "copy"]
    probed.clear()
    assert app.hls_audio_args({"id": "front", "audio": False}) == ["-an", "-c:v", "copy"] and not probed


def test_audio_codec_from_go2rtc(monkeypatch):
    import http.server
    import threading

    answer = {"text": ""}

    class Go2rtc(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = answer["text"].encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    fake = http.server.HTTPServer(("127.0.0.1", 0), Go2rtc)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    monkeypatch.setattr(app, "GO2RTC_PROBE_URL", f"http://127.0.0.1:{fake.server_address[1]}/api/streams?src=")
    try:
        answer["text"] = ('{"producers":[{"receivers":[{"codec":{"codec_name":"h264"}},'
                          '{"codec":{"codec_name": "pcma","clock_rate":8000}}]}]}')
        assert app.camera_audio_codec("yard") == "pcma"
        answer["text"] = '{"producers":[{"receivers":[{"codec":{"codec_name":"h264"}}]}]}'
        assert app.camera_codecs("yard", max_age=300) == ("h264", "pcma")  # the last answer, go2rtc not asked
        assert app.camera_audio_codec("yard") is None and app.camera_codecs("yard") == ("h264", None)
        # sound listed first, named pcm_alaw: still sound, the video codec is the video one
        answer["text"] = ('{"producers":[{"receivers":[{"codec":{"codec_name":"pcm_alaw"}},'
                          '{"codec":{"codec_name":"h264"}}]}]}')
        assert app.camera_codecs("yard") == ("h264", "pcm_alaw")
    finally:
        fake.shutdown()
        fake.server_close()
    assert app.camera_audio_codec("yard") is None  # go2rtc does not answer


def test_camera_list_switches_a_camera_at_once(server, cookies, restore_cameras):
    """The camera list has a "Camera on" switch on every row: it is saved at once, only by admin, with CSRF."""
    page = request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    assert page.count('action="/camera/enable"') == len(app.cameras())
    assert 'name="id" value="yard"><input type="hidden" name="enabled" value="no"><input type="checkbox" ' \
           'class="switch" name="enabled" value="yes" checked' in page and 'onchange="this.form.submit()"' in page
    csrf = csrf_of(page, "/camera/enable")
    off = urlencode([("csrf", csrf), ("id", "yard"), ("enabled", "no")])
    assert request(server, "POST", "/camera/enable", off, cookie=cookies["bob"])[0] == 403
    assert request(server, "POST", "/camera/enable", urlencode([("csrf", "x"), ("id", "yard")]),
                   cookie=cookies["admin"])[0] == 403
    status, headers, _ = request(server, "POST", "/camera/enable", off, cookie=cookies["admin"])
    assert status == 303 and headers["Location"] == "/settings?tab=cameras" and not app.camera("yard")["enabled"]
    page = request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    assert "127.0.0.1 · <span class=\"off\">switched off</span>" in page and 'name="enabled" value="yes" aria-label' in page
    on = urlencode([("csrf", csrf), ("id", "yard"), ("enabled", "no"), ("enabled", "yes")])
    assert request(server, "POST", "/camera/enable", on, cookie=cookies["admin"])[0] == 303  # no check of the camera
    assert app.camera("yard")["enabled"]
    assert request(server, "POST", "/camera/enable", urlencode([("csrf", csrf), ("id", "nope")]),
                   cookie=cookies["admin"])[0] == 404


def test_add_a_camera_checks_it_first(server, cookies, restore_cameras):
    cam_srv = fake_camera("cam", "right-pass")
    url = f"rtsp://127.0.0.1:{cam_srv.server_address[1]}/live/0/MAIN"
    try:
        status, _, body = camera_form(server, cookies["admin"], id="garage", name="Garage", rtsp_url=url,
                                      rtsp_user="cam", password="wrong")
        assert status == 400 and t("en", "cam.probe_failed", error="401 Unauthorized") in body
        assert "wrong" not in body and 'name="force"' in body  # the password is not sent back
        assert app.camera("garage") is None
        status, headers, _ = camera_form(server, cookies["admin"], id="garage", name="Garage", rtsp_url=url,
                                         rtsp_user="cam", password="right-pass")
        assert status == 303 and headers["Location"] == "/settings?tab=cameras&added=garage"
    finally:
        cam_srv.shutdown()
        cam_srv.server_close()
    assert [c["id"] for c in zorcam_config.load_cameras()] == ["front", "yard", "garage"]
    assert zorcam_config.camera_password("garage") == "right-pass"
    assert "right-pass" not in open(zorcam_config.CAMERAS_FILE, encoding="utf-8").read()
    body = request(server, "GET", "/?tab=live", cookie=cookies["admin"])[2]
    assert 'data-cam="garage"' in body and "Garage" in body  # no gallery restart needed
    # a new camera is closed to users until admin switches it on for them
    assert 'data-cam="garage"' not in request(server, "GET", "/?tab=live", cookie=cookies["guest"])[2]
    body = request(server, "GET", "/settings?tab=cameras&added=garage", cookie=cookies["admin"], headers=RU)[2]
    assert t("ru", "cams.added_closed", name="Garage") in body
    try:
        zorcam_users.set_cameras("guest", {"front", "yard", "garage"})
        assert 'data-cam="garage"' in request(server, "GET", "/?tab=live", cookie=cookies["guest"])[2]
    finally:
        zorcam_users.set_cameras("guest", {"front", "yard"})


def test_camera_form_errors_and_force(server, cookies, restore_cameras):
    status, _, body = camera_form(server, cookies["admin"], id="front", name="", rtsp_url="http://x")
    assert status == 400 and t("en", "cfg.camera_id_taken") in body and t("en", "cfg.camera_name") in body
    # an unreachable camera can still be saved with "Save anyway"
    status, _, body = camera_form(server, cookies["admin"], id="off", name="Switched off", rtsp_url="rtsp://127.0.0.1:1/x")
    assert status == 400 and t("en", "cam.force") in body
    status, _, _ = camera_form(server, cookies["admin"], id="off", name="Switched off", rtsp_url="rtsp://127.0.0.1:1/x",
                               force="1")
    assert status == 303 and app.camera("off")["name"] == "Switched off"


def test_second_stream_is_checked_and_shown(server, cookies, restore_cameras, monkeypatch):
    """The camera form has the detector's second stream; saving checks it too; Status says which stream the
    detector reads."""
    page = request(server, "GET", "/camera/yard", cookie=cookies["admin"])[2]
    assert 'name="rtsp_sub_url"' in page and t("en", "cam.label.rtsp_sub_url") in page
    probed = []
    monkeypatch.setattr(zorcam_config, "probe_camera",
                        lambda url, timeout: probed.append(url) or ("404 Not Found" if url.endswith("/SUB") else None))
    status, _, body = camera_form(server, cookies["admin"], id="gate", name="Gate", rtsp_url="rtsp://10.0.0.9/MAIN",
                                  rtsp_sub_url="rtsp://10.0.0.9/SUB")
    assert status == 400 and probed == ["rtsp://10.0.0.9/MAIN", "rtsp://10.0.0.9/SUB"]
    assert html.escape(t("en", "cam.probe_failed", error=t("en", "cam.probe_sub", error="404 Not Found"))) in body
    status, _, _ = camera_form(server, cookies["admin"], id="gate", name="Gate", rtsp_url="rtsp://10.0.0.9/MAIN",
                               rtsp_sub_url="rtsp://10.0.0.9/SUB", force="1")
    assert status == 303 and app.camera("gate")["rtsp_sub_url"] == "rtsp://10.0.0.9/SUB"
    states = {"gate": {"state": "streaming", "detector_stream": "main"}, "yard": {"state": "streaming"}}
    monkeypatch.setattr(app.HEALTH, "capture_status", lambda cam_id: states.get(cam_id))
    _, cam_rows = app.status_rows("en")
    assert ("Detector stream", t("en", "st.stream.main"), "bad", None, "") in cam_rows
    assert sum(r[0] == "Detector stream" for r in cam_rows) == 1  # Yard has no second stream set
    states["gate"]["detector_stream"] = "sub"
    assert ("Detector stream", t("en", "st.stream.sub"), "", None, "") in app.status_rows("en")[1]


def test_edit_keeps_the_id_and_the_password(server, cookies, restore_cameras):
    zorcam_config.set_camera_password("yard", "old-pass")
    page = request(server, "GET", "/camera/yard", cookie=cookies["admin"])[2]
    # ID of an existing camera is a row, not a field; the server takes it from "edit" and ignores "id"
    save_form = page[page.index('action="/camera/save"'):page.index('action="/camera/delete"')]
    assert 'name="id"' not in save_form and '<div class="v">yard</div>' in save_form and "old-pass" not in page
    assert '<details class="tips"><summary>' in page and "<details class=\"tips\" open" not in page
    assert 'name="edit" value="yard"' in save_form
    form = {"csrf": csrf_of(page, "/camera/save"), "edit": "yard", "setup": "", "id": "hacked", "name": "Back yard",
            "rtsp_url": "rtsp://127.0.0.1:1/yard", "rtsp_user": "u", "password": "", "clip_seconds": "15", "force": "1"}
    assert request(server, "POST", "/camera/save", urlencode(form), cookie=cookies["admin"])[0] == 303
    cam = app.camera("yard")
    assert cam["name"] == "Back yard" and cam["clip_seconds"] == 15 and app.camera("hacked") is None
    assert zorcam_config.camera_password("yard") == "old-pass"


def test_new_camera_address_needs_the_password_again(server, cookies, restore_cameras, monkeypatch):
    """An empty password field means "keep the saved one" only while the camera stays on the same hosts: with another
    address in any field the saved password would go to a server of somebody else (the check, go2rtc, PTZ)."""
    probed = []
    monkeypatch.setattr(zorcam_config, "probe_camera", lambda url, timeout: probed.append(url) and None)
    assert camera_form(server, cookies["admin"], id="gate", name="Gate", rtsp_url="rtsp://Cam.local/MAIN",
                       rtsp_sub_url="rtsp://cam.local/SUB", onvif_url="http://cam.local:8000/onvif/device_service",
                       rtsp_user="u", password="old-pass")[0] == 303
    page = request(server, "GET", "/camera/gate", cookie=cookies["admin"])[2]
    csrf = csrf_of(page, "/camera/save")

    def edit(password="", **fields):
        form = {"csrf": csrf, "edit": "gate", "setup": "", "name": "Gate", "rtsp_url": "rtsp://cam.local/MAIN",
                "rtsp_sub_url": "rtsp://cam.local/SUB", "onvif_url": "http://cam.local:8000/onvif/device_service",
                "rtsp_user": "u", "password": password, **fields}
        del probed[:]
        return request(server, "POST", "/camera/save", urlencode(form), cookie=cookies["admin"])
    again = html.escape(t("en", "cam.password_again"))
    # every address that moves to another host, one at a time
    for changed in ({"rtsp_url": "rtsp://evil.example/MAIN"}, {"rtsp_sub_url": "rtsp://10.9.9.9/SUB"},
                    {"onvif_url": "http://evil.example/onvif"}):
        status, _, body = edit(**changed)
        assert status == 400 and again in body and "old-pass" not in body, changed
        assert probed == [] and zorcam_config.camera_password("gate") == "old-pass", changed
        assert app.camera("gate")["rtsp_url"] == "rtsp://Cam.local/MAIN"
    # the same hosts (other path, port and case), the password stays
    status, _, _ = edit(rtsp_url="rtsp://CAM.local:8554/other", force="1", name="Gate 2")
    assert status == 303 and zorcam_config.camera_password("gate") == "old-pass" and app.camera("gate")["name"] == "Gate 2"
    # an address for the second stream or ONVIF removed: fewer hosts is fine
    assert edit(rtsp_sub_url="", onvif_url="", force="1")[0] == 303
    assert zorcam_config.camera_password("gate") == "old-pass"
    # a new host with a password typed in: saved, and the typed one is used
    status, _, _ = edit("new-pass", rtsp_url="rtsp://other.local/MAIN", rtsp_sub_url="", onvif_url="")
    assert status == 303 and probed == ["rtsp://u:new-pass@other.local/MAIN"]
    assert zorcam_config.camera_password("gate") == "new-pass"
    assert app.camera("gate")["rtsp_url"] == "rtsp://other.local/MAIN"
    # no login or nothing saved: there is no password to give away
    assert edit(rtsp_url="rtsp://third.local/MAIN", rtsp_user="", force="1")[0] == 303
    assert zorcam_config.camera_password("gate") == ""


def test_delete_a_camera(server, cookies, restore_cameras, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "BASE_DIR", str(tmp_path))
    for d in ("yard", "yard_videos", "front"):
        (tmp_path / d).mkdir()
    zorcam_config.set_camera_password("yard", "x")
    page = request(server, "GET", "/camera/yard", cookie=cookies["admin"])[2]
    form = {"csrf": csrf_of(page, "/camera/delete"), "id": "yard", "recordings": "1"}
    assert request(server, "POST", "/camera/delete", urlencode(form), cookie=cookies["bob"])[0] == 403
    status, headers, _ = request(server, "POST", "/camera/delete", urlencode(form), cookie=cookies["admin"])
    assert status == 303 and headers["Location"] == "/settings?tab=cameras"
    assert [c["id"] for c in zorcam_config.load_cameras()] == ["front"]
    assert sorted(os.listdir(tmp_path)) == ["front"] and zorcam_config.camera_password("yard") == ""


@pytest.fixture
def zones_file(tmp_path, monkeypatch):
    monkeypatch.setattr(zorcam_config, "MOTION_ZONES_FILE", str(tmp_path / "motion_zones.json"))
    return tmp_path / "motion_zones.json"


def test_zones_page_and_save(server, cookies, zones_file):
    admin = cookies["admin"]
    form = request(server, "GET", "/camera/front", cookie=admin)[2]
    assert 'href="/camera/front/zones"' in form and t("en", "zones.link") in form
    assert 'href="/camera/front/zones"' not in request(server, "GET", "/camera/new", cookie=admin)[2]
    status, _, page = request(server, "GET", "/camera/front/zones", cookie=admin)
    assert status == 200 and 'class="zonegrid"' in page and 'name="cells" value="' + "0" * 144 + '"' in page
    assert '/static/zones.js?v=' in page and '/static/zones.css?v=' in page and 'src="/camera-frame/front"' in page
    assert request(server, "GET", "/camera/nope/zones", cookie=admin)[0] == 404
    assert request(server, "GET", "/camera/front/zones")[0] in (302, 401, 403)

    csrf = csrf_of(page, "/camera/zones")
    cells = "1" * 16 + "0" * 128
    body = lambda **kw: urlencode({"csrf": csrf, "id": "front", "mode": "exclude", "cells": cells, **kw})  # noqa: E731
    assert request(server, "POST", "/camera/zones", body(), cookie=cookies["bob"])[0] == 403  # a viewer
    assert request(server, "POST", "/camera/zones", body(csrf="x"), cookie=admin)[0] == 403
    assert request(server, "POST", "/camera/zones", body(id="nope"), cookie=admin)[0] == 404
    for bad in ({"mode": "all"}, {"cells": "1" * 143}, {"cells": "2" * 144}, {"cells": ""}):
        assert request(server, "POST", "/camera/zones", body(**bad), cookie=admin)[0] == 400, bad
    assert not zones_file.exists()
    status, headers, _ = request(server, "POST", "/camera/zones", body(mode="include"), cookie=admin)
    assert status == 303 and headers["Location"] == "/camera/front"
    assert zorcam_config.load_zones() == {"front": {"mode": "include", "cells": cells}}
    assert oct(zones_file.stat().st_mode & 0o777) == "0o600"
    page = request(server, "GET", "/camera/front/zones", cookie=admin)[2]
    assert f'value="{cells}"' in page and '<option value="include" selected>' in page
    # nothing painted: no zones, the entry is gone
    assert request(server, "POST", "/camera/zones", body(cells="0" * 144), cookie=admin)[0] == 303
    assert zorcam_config.load_zones() == {}


def test_zones_leave_cameras_json_alone_and_go_with_the_camera(server, cookies, restore_cameras, zones_file, tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(app, "BASE_DIR", str(tmp_path))
    before = zorcam_config.signature()
    zorcam_config.save_zone("yard", "exclude", "1" + "0" * 143)
    zorcam_config.save_zone("front", "include", "1" * 144)
    assert zorcam_config.signature() == before  # the detectors and go2rtc restart on those files only
    page = request(server, "GET", "/camera/yard", cookie=cookies["admin"])[2]
    form = {"csrf": csrf_of(page, "/camera/delete"), "id": "yard", "recordings": ""}
    assert request(server, "POST", "/camera/delete", urlencode(form), cookie=cookies["admin"])[0] == 303
    assert list(zorcam_config.load_zones()) == ["front"]


# ---------------------------------------------------------------- setup wizard

@pytest.fixture
def fresh_install(tmp_path, monkeypatch):
    """No users (a fresh installation) and no cameras."""
    monkeypatch.setattr(zorcam_users, "USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setattr(zorcam_config, "CAMERAS_FILE", str(tmp_path / "cameras.json"))


def setup_cookie(headers):
    return next(c.split(";")[0] for c in headers.get_all("Set-Cookie") if c.startswith("zorcam_setup="))


def test_setup_wizard(server, fresh_install):
    for path in ("/", "/login", "/settings", "/?tab=videos"):
        status, headers, _ = request(server, "GET", path)
        assert status == 303 and headers["Location"] == "/setup", path

    # step 1: general settings; nothing changed -> straight to step 2 without a restart
    status, headers, page = request(server, "GET", "/setup")
    assert status == 200 and t("en", "setup.step", n=1) in page
    assert '<details><summary>' in page and 'name="gallery.session_days"' in page and 'class="back"' not in page
    first = page[page.index("<form"):page.index("<details")]  # a short question: the language
    assert t("en", "setup.lang_label") in first and "(each user picks a language" not in first
    cookie = setup_cookie(headers)
    csrf = cookie.split("=", 1)[1]
    form = {f"{s}.{k}": zorcam_config.format_value(app.CFG.get(s, k)) for s, k, *_ in zorcam_config.PARAMS}
    assert request(server, "POST", "/setup", urlencode({**form, "csrf": "0" * 32}), cookie=cookie)[0] == 403
    status, _, body = request(server, "POST", "/setup", urlencode({**form, "csrf": csrf, "gallery.videos_per_page": "7"}),
                              cookie=cookie)
    assert status == 400 and t("en", "cfgpage.errors", n=1) in body
    status, headers, _ = request(server, "POST", "/setup", urlencode({**form, "csrf": csrf}), cookie=cookie)
    assert status == 303 and headers["Location"] == "/setup/users"

    # step 2: users
    page = request(server, "GET", "/setup/users", cookie=cookie)[2]
    assert t("en", "setup.step", n=2) in page and '<div class="v">admin</div>' in page  # the name is fixed
    assert 'name="password"' in page and 'name="name"' not in page and 'name="role"' not in page
    users = {"csrf": csrf, "password": "secret1", "repeat": "other"}
    status, _, body = request(server, "POST", "/setup/users", urlencode(users), cookie=cookie)
    assert status == 400 and t("en", "setup.mismatch") in body and "secret1" not in body
    assert zorcam_users.load() == {}
    status, headers, _ = request(server, "POST", "/setup/users", urlencode({**users, "repeat": "secret1"}),
                                 cookie=cookie)
    assert status == 303 and headers["Location"] == "/setup/cameras"
    assert {n: v["role"] for n, v in zorcam_users.load().items()} == {"admin": "admin"}
    session = headers["Set-Cookie"].split(";")[0]  # signed in as admin right away

    # step 3: cameras
    page = request(server, "GET", "/setup/cameras", cookie=session)[2]
    assert t("en", "setup.ready") in page and 'href="/camera/new?setup=1"' in page
    page = request(server, "GET", "/camera/new?setup=1", cookie=session)[2]
    assert t("en", "cam.tips_title") in page and 'name="setup" value="1"' in page
    assert '<details class="tips"><summary>' in page and 'name="id" value=""' in page  # a new camera: ID is editable
    assert f'<div class="k">{t("en", "cam.label.id")}</div>' in page
    assert f'<div class="k">{t("en", "cam.label.name")}</div>' in page
    assert 'placeholder="rtsp://192.168.1.50:554/live/0/MAIN"' in page  # the example in grey inside the field
    # dimmer than a typed value
    assert "::placeholder { color:var(--btn); opacity:1; font-style:italic; }" in assets(server, page)
    assert f'placeholder="{t("en", "cam.password_none")}"' in page
    assert f'<span class="sub">{t("en", "cam.hint.rtsp_user")}</span>' in page  # grey, on the phone too
    assert 'name="rtsp_user" value="admin"' in page and "ZORCAM_CAMERA_PASSWORD" not in page
    form = {"csrf": csrf_of(page, "/camera/save"), "edit": "", "setup": "1", "id": "gate", "name": "Gate",
            "rtsp_url": "rtsp://127.0.0.1:1/gate", "rtsp_user": "", "password": "", "force": "1"}
    status, headers, _ = request(server, "POST", "/camera/save", urlencode(form), cookie=session)
    assert status == 303 and headers["Location"] == "/setup/cameras"
    page = request(server, "GET", "/setup/cameras", cookie=session)[2]
    assert "Gate" in page and t("en", "setup.more") in page and 'href="/?tab=live"' in page

    # the wizard is over: its first steps are gone
    assert request(server, "GET", "/setup", cookie=session)[1]["Location"] == "/setup/cameras"
    assert request(server, "POST", "/setup/users", urlencode(users), cookie=cookie)[0] == 303
    assert set(zorcam_users.load()) == {"admin"}


def test_setup_is_for_the_home_network_only(server, fresh_install):
    status, _, body = request(server, "GET", "/setup", headers={"X-Forwarded-For": "93.184.216.34"})
    assert status == 403 and t("en", "setup.local_only") in body
    assert app.is_local_client("192.168.1.20") and app.is_local_client("127.0.0.1", "10.0.0.5")
    assert not app.is_local_client("127.0.0.1", "93.184.216.34") and not app.is_local_client("8.8.8.8")


def test_no_cameras_yet(server, cookies, monkeypatch, tmp_path):
    monkeypatch.setattr(zorcam_config, "CAMERAS_FILE", str(tmp_path / "none.json"))
    body = request(server, "GET", "/?tab=live", cookie=cookies["admin"])[2]
    assert t("en", "cams.none") in body and 'href="/camera/new"' in body
    body = request(server, "GET", "/?tab=videos", cookie=cookies["bob"])[2]
    assert t("en", "cams.none") in body and 'href="/camera/new"' not in body


def test_debug_mode_logs_every_request(server, cookies, capsys, monkeypatch):
    """app/debug.sh on sets ZORCAM_DEBUG=1: the gallery logs each request with its status and time."""
    monkeypatch.setattr(app, "DEBUG", True)
    assert request(server, "GET", "/healthz")[0] == 200
    time.sleep(0.2)
    out = capsys.readouterr().out
    assert "debug: GET /healthz -> 200 in " in out
    assert "-> None" not in out  # closing a keep-alive connection is not a request
    monkeypatch.setattr(app, "DEBUG", False)
    request(server, "GET", "/healthz")
    time.sleep(0.2)
    assert "debug:" not in capsys.readouterr().out


@pytest.mark.parametrize("host, public", [
    ("myhome.example.net", "myhome.example.net"), ("MyHome.Example.net:8443", "myhome.example.net"),
    ("93.184.216.34", "93.184.216.34"), ("93.184.216.34:80", "93.184.216.34"),
    ("192.168.1.10", None), ("127.0.0.1:8080", None), ("localhost", None), ("raspberrypi", None),
    ("raspberrypi.local", None), ("[::1]:80", None), ("", None), ("bad host", None),
])
def test_public_host(host, public):
    assert app.public_host(host) == public


ANSWER = ("v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nt=0 0\r\n"
          "m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=mid:0\r\na=candidate:1 1 udp 2130706431 192.168.1.10 8555 typ host\r\n"
          "m=audio 9 UDP/TLS/RTP/SAVPF 8\r\na=mid:1\r\n")


HLS_JS_SHA384 = "sha384-cciJ0zi8d1uMKC2zJd7jvPY4HQt7W4ByUI/FlMkltvBi31aW61rcpVBhpmW8/NwX"  # dist/hls.min.js of hls.js@1.7.3


def test_hls_js_is_served_by_the_device_not_by_a_cdn(server, cookies):
    """hls.js is a file of the gallery (/static/hls.min.js, the unchanged build of hls.js 1.7.3 with its licence next to it),
    so that Live in HLS fetches nothing from the internet."""
    import base64
    import hashlib
    body = request(server, "GET", "/", cookie=cookies["bob"])[2]
    page = json.loads(re.search(r"window\.PAGE = (.*);", body).group(1))
    assert page["hlsUrl"] == f"/static/hls.min.js?v={app.ASSETS['hls.min.js'][2]}" and "hlsSri" not in page
    status, headers, text = request(server, "GET", page["hlsUrl"])  # no cookie, like every file of static/
    assert status == 200 and headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "sha384-" + base64.b64encode(hashlib.sha384(text.encode()).digest()).decode() == HLS_JS_SHA384
    js = assets(server, body)
    assert "s.src = PAGE.hlsUrl" in js and "integrity" not in js and "jsdelivr" not in js
    with open(os.path.join(app.STATIC_DIR, "hls.LICENSE.txt"), encoding="utf-8") as f:
        licence = f.read()
    assert "Apache License" in licence and HLS_JS_SHA384 in licence


def test_webrtc_answer_gets_the_address_the_gallery_was_opened_at():
    """From the internet the browser gets one more candidate: the address it opened the gallery at, port 8555."""
    sdp = app.add_candidates(ANSWER, "203.0.113.10")
    video, audio = sdp.split("m=audio")
    for section in (video, audio):
        assert "a=candidate:zorcam1 1 udp 2130706431 203.0.113.10 8555 typ host\r\n" in section
        assert "a=candidate:zorcam2 1 tcp 2130706430 203.0.113.10 8555 typ host tcptype passive\r\n" in section
    assert sdp.startswith("v=0\r\n") and sdp.endswith("\r\n") and sdp.count("m=") == 2
    assert app.add_candidates(sdp, "203.0.113.10") == sdp  # already there
    assert app.host_ip("203.0.113.10") == "203.0.113.10" and app.host_ip("localhost") == "127.0.0.1"
    assert app.host_ip("no-such-host.invalid") is None
    with open(os.path.join(os.path.dirname(app.__file__), "..", "go2rtc", "go2rtc.yaml"), encoding="utf-8") as f:
        assert f'listen: ":{app.WEBRTC_PORT}"' in f.read()


def test_webrtc_signalling_adds_the_candidate(server, tunnel, cookies, monkeypatch):
    """The SDP answer of go2rtc passes the gallery: opened at a public address, the answer gets it as a candidate;
    from the home network or through the cloud, it stays as go2rtc made it."""
    import http.server
    import threading

    class Go2rtc(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = ANSWER.encode()
            self.send_response(201)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    fake = http.server.HTTPServer(("127.0.0.1", 0), Go2rtc)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    monkeypatch.setattr(app, "GO2RTC_WEBRTC_URL", f"http://127.0.0.1:{fake.server_address[1]}/api/webrtc?src=")
    try:
        def offer(host, port=server, **extra):
            return request(port, "POST", "/live/webrtc?cam=front", "v=0", cookie=cookies["bob"],
                           headers={"Content-Type": "application/sdp", "Host": host, **extra})
        status, _, body = offer("93.184.216.34")
        assert status == 201 and body.count(" 93.184.216.34 8555 typ host") == 4  # udp and tcp, both sections
        assert offer("192.168.1.10")[2] == ANSWER
        # through the cloud tunnel: <id>.devices.zorcam.ru leads to the cloud server, it has no port 8555
        assert offer("localhost", tunnel, **{"X-Forwarded-Host": "93.184.216.34", "X-Zorcam-Identity": "x"})[2] == ANSWER
        # at home the header is nothing: the page address is a candidate as usual
        assert offer("localhost", **{"X-Forwarded-Host": "93.184.216.34", "X-Zorcam-Identity": "x"})[2] != ANSWER
    finally:
        fake.shutdown()
        fake.server_close()


def test_webrtc_answer_gets_the_stun_address(server, tunnel, cookies, monkeypatch):
    """The external IP found by STUN goes into every answer (at home, from the internet, through the cloud); the page
    address is added too when it is public and different, a domain resolving to a private IP (KeenDNS at home) is not."""
    import http.server
    import threading
    import stun

    class Go2rtc(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = ANSWER.encode()
            self.send_response(201)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    fake = http.server.HTTPServer(("127.0.0.1", 0), Go2rtc)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    monkeypatch.setattr(app, "GO2RTC_WEBRTC_URL", f"http://127.0.0.1:{fake.server_address[1]}/api/webrtc?src=")
    monkeypatch.setitem(stun._state, "ip", "93.184.216.34")
    monkeypatch.setattr(stun, "_cloud_hosts", [])
    monkeypatch.setattr(app, "host_ip", lambda host: {"home.keenetic.link": "192.168.1.1",
                                                      "cam.example.org": "151.101.1.1"}.get(host))
    try:
        def offer(host, port=server, **extra):
            return request(port, "POST", "/live/webrtc?cam=front", "v=0", cookie=cookies["bob"],
                           headers={"Content-Type": "application/sdp", "Host": host, **extra})
        body = offer("192.168.1.10")[2]
        assert body.count(" 93.184.216.34 8555 typ host") == 4 and "151.101.1.1" not in body
        body = offer("home.keenetic.link")[2]  # resolves to the router at home: not a candidate
        assert body.count(" 93.184.216.34 8555 typ host") == 4 and "192.168.1.1 " not in body
        body = offer("cam.example.org")[2]
        assert body.count(" 93.184.216.34 8555 typ host") == 4 and body.count(" 151.101.1.1 8555 typ host") == 4
        assert offer("93.184.216.34")[2].count(" 93.184.216.34 8555 typ host") == 4  # no duplicates
        ice = json.dumps([{"urls": ["turn:turn.example.org:3480"], "username": "u", "credential": "c"}])
        through_cloud = offer("localhost", tunnel, **{"X-Zorcam-Identity": "x", "X-Zorcam-Ice": ice})[2]
        assert through_cloud.count(" 93.184.216.34 8555 typ host") == 4
        assert ("turn.example.org", 3480) in stun.servers()
        monkeypatch.setitem(stun._state, "ip", None)
        assert offer("192.168.1.10")[2] == ANSWER  # not determined: as go2rtc made it
    finally:
        fake.shutdown()
        fake.server_close()


def test_status_shows_the_external_address(server, cookies, monkeypatch):
    """Settings -> Status: the STUN address and server; not determined - amber (warn), not a problem for the summary."""
    import stun
    monkeypatch.setattr(stun, "_state", {"ip": "93.184.216.34", "at": 1, "server": "stun.cloudflare.com:3478",
                                          "checked": 1, "why": None, "logged": None})
    body = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])[2]
    assert (f'<div class="k">{t("en", "st.external")}</div><div class="v ">'
            f'{t("en", "st.external_ok", ip="93.184.216.34", server="stun.cloudflare.com")}</div>' in body)
    assert "93.184.216.34\nSTUN stun.cloudflare.com</div>" in body  # the address, then the STUN host without the port
    monkeypatch.setitem(stun._state, "ip", None)
    monkeypatch.setitem(stun._state, "server", None)
    body = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])[2]
    assert f'<div class="v warn">{t("en", "st.external_none")}</div>' in body and t("en", "st.external_why") in body
    monkeypatch.setitem(stun._state, "why", "100.64.1.2")
    assert t("en", "st.external_why_cgnat") in request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])[2]
    ru = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"], headers={"Cookie": "zorcam_lang=ru"})
    assert ru[0] == 200
    monkeypatch.setitem(stun._state, "checked", None)  # no check yet: no row
    assert t("en", "st.external") not in request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])[2]


def test_no_external_ip_setting(server, cookies):
    """The external address is not set by hand: STUN, the address the app is opened at, TURN of the cloud."""
    page = request(server, "GET", "/settings", cookie=cookies["admin"])[2]
    assert "live.public_ip" not in page and "live.public_ip" not in request(server, "GET", "/config", cookie=cookies["admin"])[2]
    assert request(server, "POST", "/settings/general", "csrf=x", cookie=cookies["admin"])[0] == 404


def test_login_without_password_means_no_authentication(server, cookies, restore_cameras):
    """The login is prefilled with admin; saved without a password, the camera is used without authentication."""
    status, _, _ = camera_form(server, cookies["admin"], id="porch", name="Porch", rtsp_url="rtsp://127.0.0.1:1/p",
                               rtsp_user="admin", password="", force="1")
    assert status == 303
    assert zorcam_config.camera_password("porch") == ""
    assert zorcam_config.rtsp_url(app.camera("porch")) == "rtsp://127.0.0.1:1/p"
    body = request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    assert 'Porch<span class="sub">127.0.0.1</span>' in body  # only the host under the name, on the phone too


def test_camera_status_is_a_separate_section(server, cookies, monkeypatch):
    """Settings: the frames, detector and reconnects of each camera are in Camera status, not in Zorcam status."""
    monkeypatch.setattr(app.HEALTH, "checks", {"frames:front": (True, app.Msg("health.no_frames_yet")),
                                               "disk": (True, app.Msg("health.running"))})
    monkeypatch.setattr(app.HEALTH, "checked", time.time())
    monkeypatch.setattr(app.HEALTH, "capture_status", lambda cam_id: {"state": "streaming", "reconnects": [1, 2],
                                                                    "keyframe_fps": 2.48} if cam_id == "front" else None)
    body = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])[2]

    def section(title):
        return re.search(r"<section><h2>" + re.escape(title) + r"</h2>(.*?)</section>", body, re.S).group(1)
    main, cams = section(t("en", "st.section.community")), section(t("en", "st.cameras"))
    assert t("en", "check.disk") in main and "Front door" not in main
    head = '<div class="row all"><div class="k">Front door</div></div>'
    assert head in cams and cams.index(head) < cams.index(t("en", "st.frames"))
    for label in (t("en", "st.frames"), t("en", "st.detector"), t("en", "st.reconnects")):
        assert label in cams, label
    assert "Front door:" not in cams
    assert f'{t("en", "st.keyframes")}</div><div class="v ">2.5</div>' in cams
    assert re.findall(r"<section><h2>(.*?)</h2>", body) == [t("en", "st.section.community"), t("en", "st.cameras")]


def test_status_title_names_the_edition(server, cookies, monkeypatch):
    for edition, title in (("community", "Zorcam NVR status"), ("box", "Zorcam Box status")):
        monkeypatch.setattr(app.zorcam_config, "edition", lambda edition=edition: edition)
        body = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])[2]
        assert f"<section><h2>{title}</h2>" in body


def test_motion_notifications_toggle_per_camera(server, cookies, restore_cameras, monkeypatch):
    """A camera's Motion notifications switch: on by default; off, its motion sends no push, the others still do."""
    page = request(server, "GET", "/camera/new", cookie=cookies["admin"])[2]
    assert '<input type="checkbox" class="switch" name="notify_motion" value="yes" checked>' in page
    assert app.camera("front")["notify_motion"] is True  # cameras.json without the field: on
    status, _, _ = camera_form(server, cookies["admin"], id="quiet", name="Quiet", rtsp_url="rtsp://127.0.0.1:1/q",
                               force="1")  # the switch is off: the form does not send notify_motion
    assert status == 303 and app.camera("quiet")["notify_motion"] is False
    page = request(server, "GET", "/camera/quiet", cookie=cookies["admin"])[2]
    assert 'name="notify_motion" value="yes">' in page  # shown off
    sent = []
    monkeypatch.setattr(app, "_last_push", {})
    monkeypatch.setattr(app.PUSH, "count", lambda: 1)
    monkeypatch.setattr(app.PUSH, "send_all", lambda message, log, users=None: (sent.append(message), (1, 1))[1])
    logged = []
    monkeypatch.setattr(app, "log", logged.append)
    for cam in ("quiet", "front", "front"):
        body = json.dumps({"name": "20260926_120000_000", "camera": cam})
        assert request(server, "POST", "/internal/motion", body, headers=motion_headers())[0] == 200
    time.sleep(0.3)
    assert [m({"lang": "en"})["body"] for m in sent] == ["Front door, 12:00:00"]
    # every decision is in the gallery log: why a notification did not come
    motion = sorted(line for line in logged if line.startswith("motion push"))
    assert motion[0] == "motion push front: delivered to 1 of 1 subscriptions"
    assert motion[1].startswith("motion push front: skipped (motion_cooldown, ")
    assert motion[2] == "motion push quiet: skipped (motion notifications are off for the camera)"


def test_camera_recording_settings(server, cookies, restore_cameras):
    """Per camera: save videos (switch), video length, keep videos, save snapshots (switch), keep snapshots."""
    page = request(server, "GET", "/camera/new", cookie=cookies["admin"], headers=RU)[2]
    for key in ("save_videos", "save_frames"):
        assert f'<input type="checkbox" class="switch" name="{key}" value="yes" checked>' in page
    for key, value in (("clip_seconds", "10"), ("videos_days", "5"), ("frames_days", "10")):
        assert t("ru", "cam.label." + key) in page and f'name="{key}" value="{value}" inputmode="numeric"' in page
    status, _, body = camera_form(server, cookies["admin"], id="rec", name="Rec", rtsp_url="rtsp://127.0.0.1:1/r",
                                  force="1", clip_seconds="500")
    assert status == 400 and t("en", "cfg.clip_seconds") in body
    status, _, _ = camera_form(server, cookies["admin"], id="rec", name="Rec", rtsp_url="rtsp://127.0.0.1:1/r",
                               force="1", save_frames="yes", clip_seconds="20", videos_days="30", frames_days="2")
    cam = app.camera("rec")
    assert status == 303 and cam["save_videos"] is False and cam["save_frames"] is True
    assert (cam["clip_seconds"], cam["videos_days"], cam["frames_days"]) == (20, 30, 2)


RECORDING_FIELDS = {"save_videos": ("clip_seconds", "videos_days"),
                    "save_frames": ("frames_interval", "frames_days")}


def test_recording_fields_are_inactive_while_their_switch_is_off(server, cookies, restore_cameras):
    """Save motion videos off: its resolution, length and days are inactive; Save snapshots off: the snapshot
    interval and days. Inactive fields are not sent, so their values stay."""
    page = request(server, "GET", "/camera/new", cookie=cookies["admin"], headers=RU)[2]
    assert t("ru", "cam.label.frames_interval") in page
    assert 'name="frames_interval" value="5" inputmode="numeric" data-needs="save_frames" autocomplete="off">' in page
    for switch, keys in RECORDING_FIELDS.items():
        for key in keys:
            assert re.search(rf'name="{key}"[^>]* data-needs="{switch}"[ >]', page), key
    # both switches are on for a new camera: every field is active; there is no video mode or resolution any more
    assert page.count(" disabled") == 0 and page.count('field off"') == 0
    assert 'name="record_mode"' not in page and 'name="video_height"' not in page
    status, _, body = camera_form(server, cookies["admin"], id="rec", name="Rec", rtsp_url="rtsp://127.0.0.1:1/r",
                                  force="1", save_frames="yes", frames_interval="0")
    assert status == 400 and t("en", "cfg.frames_interval") in body
    status, _, _ = camera_form(server, cookies["admin"], id="rec", name="Rec", rtsp_url="rtsp://127.0.0.1:1/r",
                               force="1", save_videos="yes", save_frames="yes", clip_seconds="20",
                               videos_days="30", frames_interval="30", frames_days="2")
    assert status == 303 and app.camera("rec")["frames_interval"] == 30
    # both switches off: the browser sends none of the inactive fields, the saved values stay
    page = request(server, "GET", "/camera/rec", cookie=cookies["admin"])[2]
    form = {"csrf": csrf_of(page, "/camera/save"), "edit": "rec", "setup": "", "id": "rec", "name": "Rec",
            "rtsp_url": "rtsp://127.0.0.1:1/r", "rtsp_user": "", "password": "", "force": "1"}
    assert request(server, "POST", "/camera/save", urlencode(form), cookie=cookies["admin"])[0] == 303
    cam = app.camera("rec")
    assert cam["save_videos"] is False and cam["save_frames"] is False
    assert [cam[k] for keys in RECORDING_FIELDS.values() for k in keys] == [20, 30, 30, 2]
    page = request(server, "GET", "/camera/rec", cookie=cookies["admin"])[2]
    for switch, keys in RECORDING_FIELDS.items():
        for key in keys:
            assert re.search(rf'name="{key}"[^>]* data-needs="{switch}" disabled[ >]', page), key
    assert page.count('class="row field off"') == 4


def test_clips_being_encoded_are_listed_only_as_pending(recordings):
    """A clip still being recorded or encoded (poster only) is not a playable video (swipes, watch page),
    but the grid lists it as pending."""
    _raw, vids = recordings["front"]
    listed = app.list_videos("front")
    (vids / "20990101_000000_000.jpg").write_bytes(b"p")  # poster written, the video still encoding
    (vids / "20990101_000000_000.mp4.part").write_bytes(b"v")
    assert app.list_videos("front") == listed and listed == sorted(listed, reverse=True) and len(listed) == 4
    assert app.list_videos("front", pending=True) == [("20990101_000000_000", "pending")] + [(n, "ready") for n in listed]
    (vids / "20990101_000000_000.failed").write_bytes(b"")  # not saved: no video, the poster has the text
    assert app.list_videos("front", pending=True)[0] == ("20990101_000000_000", "failed")
    assert app.list_videos("front") == listed


def test_videos_grid_shows_the_poster_until_the_clip_is_ready(server, cookies):
    """The poster is written on motion: the grid shows it with "Preparing video…" (not clickable), and the page
    checks the video file to turn the card into a playable one."""
    vids = os.path.join(app.BASE_DIR, app.cam_dirs("front")[1])
    name = "20990101_000000_000"
    poster = os.path.join(vids, name + ".jpg")
    with open(poster, "wb") as f:
        f.write(b"p")
    try:
        body = request(server, "GET", "/?tab=videos&cam=front", cookie=cookies["bob"])[2]
        assert (f'<a class="card pending" href="/watch/front/{name}" data-vid="/vid/front/{name}.mp4">'
                in body and t("en", "videos.preparing") in body)
        assert body.count('class="card pending"') == 1  # the encoded clips are playable
        assert 'class="badge"' not in body
        assert request(server, "GET", f"/vid/front/{name}.mp4", cookie=cookies["bob"])[0] == 404
        with open(os.path.join(vids, name + ".failed"), "wb"):
            pass  # not saved: the poster stays, not clickable, without "Preparing" and the play icon
        body = request(server, "GET", "/?tab=videos&cam=front", cookie=cookies["bob"])[2]
        assert f'<a class="card failed" href="/watch/front/{name}">' in body and 'class="card pending"' not in body
        assert 'class="badge"' not in body
    finally:
        os.remove(poster)
        if os.path.exists(os.path.join(vids, name + ".failed")):
            os.remove(os.path.join(vids, name + ".failed"))


def write_meta(vids, name, data):
    (vids / f"{name}.json").write_text(data if isinstance(data, str) else json.dumps(data))


def test_video_meta_is_checked_before_it_reaches_the_page(tmp_path, monkeypatch):
    """<name>.json comes from the detector: no file (old clips), garbage or out-of-range numbers show nothing."""
    monkeypatch.setattr(app, "BASE_DIR", str(tmp_path))
    vids = tmp_path / "front_videos"
    vids.mkdir()
    meta = lambda name: app.video_meta("front_videos", name)  # noqa: E731
    assert meta("none") is None
    for name, data in (("junk", "not json"), ("list", "[1]"), ("empty", {"box": None, "motion_at": None}),
                       ("range", {"box": [0.1, 0.1, 1.5, 0.5], "motion_at": -1}),
                       ("flat", {"box": [0.5, 0.1, 0.5, 0.5], "motion_at": "3"}),
                       ("text", {"box": ["a", 0, 1, 1], "motion_at": True})):
        write_meta(vids, name, data)
        assert meta(name) is None, name
    write_meta(vids, "both", {"box": [0.1, 0.2, 0.5, 0.6], "motion_at": 3.4})
    assert meta("both") == {"box": [0.1, 0.2, 0.5, 0.6], "motion_at": 3.4}
    write_meta(vids, "half", {"box": [0.1, 0.2, 0.5, 0.6], "motion_at": None})  # clip not glued yet
    assert meta("half") == {"box": [0.1, 0.2, 0.5, 0.6], "motion_at": None}
    write_meta(vids, "at", {"box": None, "motion_at": 0})
    assert meta("at") == {"box": None, "motion_at": 0.0}


def test_motion_frame_and_time_show_only_with_the_detectors_json(server, cookies):
    """Videos: the orange frame lies over the poster of a clip with a box (not over a clip without .json, and not
    over one not saved); the watch page gets the frame, the Motion badge and the scale with the mark."""
    vids = os.path.join(app.BASE_DIR, app.cam_dirs("front")[1])
    meta, bare = "20260926_103400_000", "20260926_103300_000"  # the newest ones: the first page of the grid
    path = os.path.join(vids, meta + ".json")
    try:
        grid = request(server, "GET", "/?tab=videos&cam=front", cookie=cookies["bob"])[2]
        watch = request(server, "GET", f"/watch/front/{meta}", cookie=cookies["bob"])[2]
        assert 'class="mbox"' not in grid and 'class="vwrap"' not in watch and "<video src=" in watch
        with open(path, "w") as f:
            json.dump({"box": [0.25, 0.5, 0.5, 0.75], "motion_at": 3.4}, f)
        grid = request(server, "GET", "/?tab=videos&cam=front", cookie=cookies["bob"])[2]
        card = re.search(r'<a class="card" href="/watch/front/' + meta + r'">(.*?)</a>', grid, re.S).group(1)
        assert ('<span class="mbox" data-box="[0.25, 0.5, 0.5, 0.75]" style="left:25%;top:50%;width:25%;height:25%">'
                in card)
        assert grid.count('class="mbox"') == 1  # the other clips have no .json
        watch = request(server, "GET", f"/watch/front/{meta}", cookie=cookies["bob"])[2]
        assert '<div class="vframe" data-motion="3.4"><video src="/vid/front/' + meta + '.mp4"' in watch
        assert 'data-box="[0.25, 0.5, 0.5, 0.75]"' in watch
        assert f'<span class="mbadge">{t("en", "watch.motion")}</span>' in watch
        assert f'<button type="button" class="mtick" aria-label="{t("en", "watch.jump")}"' in watch
        ru = request(server, "GET", f"/watch/front/{meta}", cookie=cookies["bob"], headers=RU)[2]
        assert t("ru", "watch.motion") in ru and t("ru", "watch.jump") in ru
        assert 'class="mbox"' not in request(server, "GET", f"/watch/front/{bare}", cookie=cookies["bob"])[2]
        # no box (the frame covered the whole picture, or no difference): the time and the badge stay
        with open(path, "w") as f:
            json.dump({"box": None, "motion_at": 5}, f)
        watch = request(server, "GET", f"/watch/front/{meta}", cookie=cookies["bob"])[2]
        assert 'data-motion="5.0"' in watch and 'class="mbadge"' in watch and 'class="mbox"' not in watch
        grid = request(server, "GET", "/?tab=videos&cam=front", cookie=cookies["bob"])[2]
        assert 'class="mbox"' not in grid
        # a clip not saved (no .mp4, the marker) is not opened; its poster has no frame
        failed = "20260926_103500_000"
        for ext, data in ((".jpg", b"p"), (".failed", b""), (".json", b'{"box": [0.1, 0.1, 0.2, 0.2], "motion_at": null}')):
            with open(os.path.join(vids, failed + ext), "wb") as f:
                f.write(data)
        grid = request(server, "GET", "/?tab=videos&cam=front", cookie=cookies["bob"])[2]
        assert f'<a class="card failed" href="/watch/front/{failed}">' in grid and 'class="mbox"' not in grid
    finally:
        for name, ext in ((meta, ".json"), ("20260926_103500_000", ".jpg"), ("20260926_103500_000", ".failed"),
                          ("20260926_103500_000", ".json")):
            if os.path.exists(os.path.join(vids, name + ext)):
                os.remove(os.path.join(vids, name + ext))


def test_motion_meta_follows_the_access_to_the_clip(server, cookies, recordings):
    """The metadata rides in the watch page: whoever may not see the camera or the recordings gets no page."""
    users_form(server, cookies["admin"], user="bob", role="viewer", cam=["front"])
    try:
        raw, vids = recordings["yard"]
        name = sorted(n[:-4] for n in os.listdir(vids) if n.endswith(".mp4"))[0]
        write_meta(vids, name, {"box": [0.1, 0.1, 0.4, 0.4], "motion_at": 2})
        assert request(server, "GET", f"/watch/yard/{name}", cookie=cookies["bob"])[0] == 404
        assert request(server, "GET", f"/watch/yard/{name}", cookie=cookies["guest"])[0] == 403
        status, _, body = request(server, "GET", f"/watch/yard/{name}", cookie=cookies["admin"])
        assert status == 200 and 'data-motion="2.0"' in body and 'class="mbox"' in body
    finally:
        zorcam_users.set_cameras("bob", {"front", "yard"})


def test_json_goes_with_its_clip(server, cookies, recordings):
    """Deleting recordings, the cleanup by age and the cleanup by free space take the clip's .json with it, and the
    count of clips in Settings is the number of posters, whatever sits next to them."""
    for _raw, vids in recordings.values():
        for poster in vids.glob("*.jpg"):
            write_meta(vids, poster.stem, {"box": [0.1, 0.1, 0.4, 0.4], "motion_at": 1})
    app.HEALTH.cleanup()
    body = request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    assert f'<div class="k">{t("en", "tab.videos")}</div><div class="v">4 (' in body
    csrf = settings_csrf(server, cookies["admin"])
    assert delete(server, cookies["admin"], "yard", "videos", csrf)[0] == 303
    assert count(recordings["yard"][1]) == 0 and count(recordings["front"][1]) == 12
    front = recordings["front"][1]
    for f in front.iterdir():
        os.utime(f, (1000, 1000))
    removed, _ = app.free_disk(str(recordings["front"][0].parent), 10**9)
    assert count(front) == 0 and removed >= 12
    old = recordings["yard"][1]
    (old / "20200101_000000_000.jpg").write_bytes(b"p")
    (old / "20200101_000000_000.json").write_text("{}")
    os.utime(old / "20200101_000000_000.jpg", (0, 0))
    os.utime(old / "20200101_000000_000.json", (0, 0))
    app.Health().cleanup()
    assert count(old) == 0


def test_a_foreign_cookie_does_not_break_the_session(server, cookies):
    """Another program on the same address may set a cookie with a name Python rejects (a@b): the session works."""
    status, _, body = request(server, "GET", "/settings?tab=users", cookie=f"a@b=1; {cookies['bob']}; x(y)=2")
    assert status == 200 and "Signed in as <b>bob</b>" in body


def test_a_silent_client_does_not_hold_a_thread(server, monkeypatch):
    """A phone that drops off the network mid-request: the server gives up after Handler.timeout."""
    import socket
    assert app.Handler.timeout and app.Handler.timeout <= 120  # the gallery's own limit, shortened for the test
    monkeypatch.setattr(app.Handler, "timeout", 0.5)
    with socket.create_connection(("127.0.0.1", server), timeout=5) as s:
        s.sendall(b"GET /healthz HTTP/1.0\r\n")  # the request never ends
        assert s.recv(100) == b""  # closed by the server, long before the 5 s of this client


def test_login_attempts_keep_only_addresses_that_failed(monkeypatch):
    monkeypatch.setattr(app, "FAILED_ATTEMPTS", {})
    assert not app.is_locked_out("198.51.100.1") and app.FAILED_ATTEMPTS == {}
    for _ in range(app.MAX_ATTEMPTS):
        app.register_failed("198.51.100.2")
    assert app.is_locked_out("198.51.100.2") and list(app.FAILED_ATTEMPTS) == ["198.51.100.2"]


def test_log_writes_each_line_at_once(monkeypatch):
    """print() writes the text and the newline apart: lines of request, push and health threads got glued."""
    import sys
    writes = []

    class Out:
        def write(self, text):
            writes.append(text)

        def flush(self):
            pass
    monkeypatch.setattr(sys, "stdout", Out())
    app.log("motion push front: delivered to 1 of 1 subscriptions")
    assert len(writes) == 1 and writes[0].endswith(" subscriptions\n") and writes[0].count("\n") == 1


def test_settings_pages_ask_before_leaving_unsaved_changes(server, cookies):
    """Changes entered but not saved: Back, Cancel or another tab asks first (in the page language)."""
    for path in ("/settings?tab=users", "/camera/front", "/config"):
        page = request(server, "GET", path, cookie=cookies["admin"], headers=RU)[2]
        assert "state() === saved" in assets(server, page) and json.dumps(t("ru", "js.unsaved"), ensure_ascii=False) in page, path


def test_a_dropped_download_is_not_logged_as_an_error(capsys):
    """The iPhone drops a video download (seeking, closing the clip): no traceback in the journal; other errors keep it."""
    import sys
    srv = app.Server.__new__(app.Server)
    for exc in (ConnectionResetError(104, "Connection reset by peer"), BrokenPipeError(32, "Broken pipe")):
        try:
            raise exc
        except OSError:
            srv.handle_error(None, ("192.168.1.1", 1))
    assert capsys.readouterr().err == ""
    try:
        raise ValueError("a bug")
    except ValueError:
        srv.handle_error(None, ("192.168.1.1", 1))
    assert "ValueError: a bug" in capsys.readouterr().err and sys.exc_info()[0] is None


def test_memory_check_reports_detector_memory_pressure(tmp_path, monkeypatch):
    """The capture service's memory.events high counter: no data at the first check; a few slowdowns are fine;
    hundreds since the last check — a problem (and a push to admin) with what to do."""
    events = tmp_path / "memory.events"
    monkeypatch.setattr(app, "CAPTURE_MEMORY_EVENTS", str(events))
    health = app.Health()
    events.write_text("low 0\nhigh 1000\nmax 0\noom 0\noom_kill 0\n")
    assert health.memory_check(0) is None
    events.write_text("low 0\nhigh 1010\nmax 0\n")
    ok, text = health.memory_check(60)
    assert ok and text.text("en") == "enough"
    events.write_text("low 0\nhigh 9010\nmax 0\n")
    ok, text = health.memory_check(180)
    assert not ok and "8000 times in 2 min" in text.text("en")
    events.write_text("high 5\n")  # the service restarted: the counter starts again
    assert health.memory_check(240) is None
    monkeypatch.setattr(app, "CAPTURE_MEMORY_EVENTS", str(tmp_path / "missing"))
    assert health.memory_check(300) is None


def test_status_shows_the_camera_codec(server, monkeypatch):
    """Status: the codec of each camera's main stream as go2rtc sees it, with its sound; H.265 is marked (browsers
    do not show it over WebRTC). go2rtc is asked at most every 5 minutes per camera."""
    st = {"state": "streaming", "reconnects": []}
    monkeypatch.setattr(app.HEALTH, "capture_status", lambda cam_id: st)
    asked = []
    answers = {"front": ("h264", "pcma"), "yard": ("h265", None)}
    monkeypatch.setattr(app, "camera_codecs", lambda cam_id, max_age=0: asked.append((cam_id, max_age)) or answers[cam_id])
    _, cam_rows = app.status_rows("en")
    assert ("Camera codec", "H.264 · sound G.711 (PCMA)", "", None, "") in cam_rows
    assert ("Camera codec", "H.265", "", t("en", "st.codec_h265"), "warn") in cam_rows
    assert sorted(asked) == [("front", 300), ("yard", 300)]


def test_recordings_disk_from_the_settings(server, cookies, monkeypatch, tmp_path):
    """Settings -> General -> Recordings disk (admin): the USB disks, format or connect as it is (ext4), disconnect.
    The gallery only leaves a request for the root service; a disk not in the list or a viewer gets nothing."""
    import zorcam_storage
    for name in ("STORAGE_DIR", "DISK_FILE", "STATE_FILE", "REQUEST_FILE"):
        monkeypatch.setattr(zorcam_storage, name, str(tmp_path / ("" if name == "STORAGE_DIR" else name.lower())))
    disks = [{"path": "/dev/sda", "size": 256 * 10**9, "model": "Samsung T7", "serial": "S1", "uuids": ["u-1"],
              "ext4": {"path": "/dev/sda1", "uuid": "u-1", "label": "zorcam"}, "mounted_here": False},
             {"path": "/dev/sdb", "size": 64 * 10**9, "model": "Flash", "serial": "S2", "uuids": [], "ext4": None,
              "mounted_here": False}]
    monkeypatch.setattr(app, "usb_disks", lambda: disks)
    page = request(server, "GET", "/settings", cookie=cookies["admin"])[2]
    section = page[page.index('<section id="storage">'):]
    assert t("en", "storage.sd") in section and "Samsung T7" in section and "/dev/sdb" in section
    assert t("en", "storage.connect", model="Samsung T7") in section  # ext4: without formatting
    assert t("en", "storage.connect", model="Flash") not in section and t("en", "storage.format", model="Flash") in section
    assert '<section id="storage">' not in request(server, "GET", "/settings", cookie=cookies["bob"])[2]
    csrf = csrf_of(page, "/settings/storage")
    ask = lambda form, who="admin": request(server, "POST", "/settings/storage", urlencode({"csrf": csrf, **form}),  # noqa: E731
                                            cookie=cookies[who])
    assert ask({"action": "use", "disk": "/dev/sda"}, "bob")[0] == 403
    assert ask({"action": "use", "disk": "/dev/mmcblk0"})[0] == 400 and ask({"action": "rm"})[0] == 400
    status, headers, _ = ask({"action": "use", "disk": "/dev/sdb"})
    assert status == 303 and headers["Location"] == "/settings?tab=general#storage"
    req = json.loads((tmp_path / "request_file").read_text())
    assert (req["action"], req["disk"], req["user"]) == ("use", "/dev/sdb", "admin")
    page = request(server, "GET", "/settings", cookie=cookies["admin"])[2]
    assert t("en", "storage.busy") in page and 'data-reload="5000"' in page  # the request is being done: the page reloads itself
    (tmp_path / "request_file").unlink()

    # the disk chosen but not connected: the recordings go to the SD card, Status shows the problem
    zorcam_storage.write_json(str(tmp_path / "disk_file"), {"uuid": "u-9", "model": "Samsung T7", "size": 1, "serial": ""})
    page = request(server, "GET", "/settings", cookie=cookies["admin"])[2]
    assert t("en", "storage.missing", model="Samsung T7") in page and t("en", "storage.forget") in page
    ok, text = app.HEALTH.run_checks()["storage"]
    assert not ok and text.text("en") == t("en", "health.storage_missing", model="Samsung T7")


def test_pager_earlier_on_the_left_later_on_the_right():
    """Records go from the newest: the next page is earlier in time, so its button is on the left, as on a timeline."""
    first = app.pager(1, 3, 130, 60, "en", "/?p=")
    assert first == ('<div class="pager"><a class="pill" href="/?p=2">‹ Earlier</a><span class="pager-info">'
                     f'{t("en", "pager.info", first=1, last=60, total=130, page=1, pages=3)}</span>'
                     '<span class="pill disabled">Later ›</span></div>')
    last = app.pager(3, 3, 130, 60, "en", "/?p=")
    assert '<span class="pill disabled">‹ Earlier</span>' in last and '<a class="pill" href="/?p=2">Later ›</a>' in last
    assert t("ru", "pager.older") != t("ru", "pager.newer")


def test_live_recovers_by_itself_and_has_a_reconnect_button(server, cookies):
    """Live: a camera stuck without picture (Buffering after Wi-Fi -> LTE) restarts by itself, a change of network
    restarts all, and a button above the players reconnects them (pull to refresh does not work over a player)."""
    body = request(server, "GET", "/?tab=live", cookie=cookies["admin"])[2]
    assert 'id="live-reload"' in body and f'aria-label="{t("en", "live.reload")}"' in body
    js = assets(server, body)
    assert "window.addEventListener('online', restartLive)" in js and "var STALL_MS = " in js
    assert "p.check();" in js and "liveReload.addEventListener('click', restartLive)" in js


def test_free_disk_removes_the_oldest_records_first(tmp_path):
    """Low disk: the oldest records of every camera go first, a clip together with its poster; a clip still being
    encoded (.part) is not touched."""
    frames, videos = tmp_path / "cam", tmp_path / "cam_videos"
    frames.mkdir()
    videos.mkdir()
    files = {frames / "a.jpg": 100, videos / "b.mp4": 300, videos / "b.jpg": 50, frames / "c.jpg": 100,
             videos / "d.mp4.part": 999, frames / "e.jpg": 100, videos / "f.jpg": 40, videos / "f.failed": 0}
    for i, (path, size) in enumerate(files.items()):
        path.write_bytes(b"x" * size)
        os.utime(path, (1000 + i, 1000 + i))
    removed, freed = app.free_disk(str(tmp_path), 120, batch=1)
    assert (removed, freed) == (3, 450)  # a.jpg, then b.mp4 with its poster
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["c.jpg", "d.mp4.part", "e.jpg", "f.failed",
                                                                           "f.jpg"]
    # everything but the clip being glued; a clip not saved goes with its marker
    assert app.free_disk(str(tmp_path), 10**9) == (4, 240)
    assert (videos / "d.mp4.part").exists()


def test_low_disk_cleans_up_without_push(monkeypatch):
    """Below min_free_disk_mb the oldest records are removed up to the limit plus a margin; every cleanup goes to
    the event list, nobody gets a push."""
    mb = 1048576
    limit = app.CFG.get("health", "min_free_disk_mb")
    calls, pushes = [], []
    monkeypatch.setattr(app.PUSH, "send_all", lambda message, log, users=None: pushes.append(message))
    monkeypatch.setattr(app.shutil, "disk_usage", lambda path: types.SimpleNamespace(free=(limit - 100) * mb))
    monkeypatch.setattr(app, "free_disk", lambda base, need: calls.append(need) or (42, need))
    health = app.Health()
    health.free_space()
    assert calls == [(100 + app.FREE_MARGIN_MB) * mb]
    assert "42 files" in health.events[0][2].text("en") and "removed" in health.events[0][2].text("en")
    assert health.events[0][1].text("en") == t("en", "health.event_disk_cleanup")
    health.free_space()
    assert len(calls) == 2 and not pushes and len(health.events) == 2
    monkeypatch.setattr(app.shutil, "disk_usage", lambda path: types.SimpleNamespace(free=(limit + 1) * mb))
    health.free_space()
    assert len(calls) == 2  # enough space: nothing is removed


def test_onvif_search_fills_the_camera_form(server, cookies, restore_cameras, monkeypatch, fake_camera):
    """Add a camera -> Find cameras (ONVIF): the list marks the cameras already added; the login and password of
    one read its streams into the form of a new camera; the password waits on the Pi, not in the page."""
    xaddr, _ = fake_camera
    page = request(server, "GET", "/camera/new", cookie=cookies["admin"])[2]
    assert f'href="/onvif" data-busy="{t("en", "onvif.searching")}">{t("en", "onvif.find")}' in page  # with a spinner
    monkeypatch.setattr(app.onvif, "discover", lambda timeout, subnet="": [
        {"host": "127.0.0.1", "xaddr": xaddr, "name": "Hall cam", "hardware": "C2"}])
    page = request(server, "GET", "/onvif", cookie=cookies["admin"])[2]
    assert "Hall cam" in page and t("en", "onvif.added") in page  # front and yard are on 127.0.0.1
    assert '<section class="added">' in page  # a camera already added: its panel is marked
    assert f'data-busy="{t("en", "onvif.reading")}"' in page and f'data-busy="{t("en", "onvif.searching")}"' in page
    form = {"csrf": csrf_of(page, "/onvif"), "xaddr": xaddr, "name": "Hall cam", "setup": "", "rtsp_user": "admin"}
    status, _, body = request(server, "POST", "/onvif", urlencode(dict(form, password="wrong")), cookie=cookies["admin"])
    assert status == 200 and t("en", "onvif.failed", error=t("en", "onvif.err.auth", detail="Sender not Authorized")) in body
    status, _, body = request(server, "POST", "/onvif", urlencode(dict(form, password="secret")), cookie=cookies["admin"])
    assert status == 200 and "secret" not in body
    assert 'name="rtsp_url" value="rtsp://127.0.0.1:554/stream1"' in body
    assert 'name="rtsp_sub_url" value="rtsp://127.0.0.1:554/stream2?ch=1"' in body
    assert t("en", "onvif.found", main="1920×1080", sub="640×360") in body
    token = re.search(r'name="onvif" value="([^"]+)"', body).group(1)
    cam_id = re.search(r'name="id" value="([^"]+)"', body).group(1)
    assert cam_id == "cam1" and app.onvif_password(token) == "secret"
    save = {"csrf": csrf_of(body, "/camera/save"), "edit": "", "setup": "", "onvif": token, "id": cam_id,
            "name": "Hall cam", "rtsp_url": "rtsp://127.0.0.1:554/stream1", "rtsp_sub_url": "", "rtsp_user": "admin",
            "password": "", "force": "1"}
    assert request(server, "POST", "/camera/save", urlencode(save), cookie=cookies["admin"])[0] == 303
    assert app.camera(cam_id)["rtsp_url"] == "rtsp://127.0.0.1:554/stream1"
    assert zorcam_config.camera_password(cam_id) == "secret" and app.onvif_password(token) == ""
    form["xaddr"] = "http://8.8.8.8/onvif/device_service"  # only the home network
    assert request(server, "POST", "/onvif", urlencode(dict(form, password="x")), cookie=cookies["admin"])[0] == 400


def test_header_mode_tip_links_to_the_cloud(server, cookies, monkeypatch):
    """Viewed directly on the device, the hint of the mode icon links to the cloud when the device is connected to it."""
    monkeypatch.setattr(app, "yandex_cloud_url", lambda: "https://dev1.devices.zorcam.ru")
    body = request(server, "GET", "/", cookie=cookies["admin"])[2]
    assert f'<a href="https://dev1.devices.zorcam.ru/">{t("en", "brand.open_cloud")}</a>' in body
    assert 'class="modetip" hidden' in body
    monkeypatch.setattr(app, "yandex_cloud_url", lambda: "")
    body = request(server, "GET", "/", cookie=cookies["admin"])[2]
    assert t("en", "brand.open_cloud") not in body and t("en", "brand.tip_nvr") in body


def test_onvif_search_in_another_subnet_is_kept_on_the_device(server, cookies, monkeypatch):
    """Search again with a subnet: the probe goes to its addresses too; the subnet is kept in a cookie of this
    device, an empty field forgets it, a public or too large one is refused with a message."""
    asked = []
    monkeypatch.setattr(app.onvif, "discover", lambda timeout, subnet="": asked.append(subnet) or
                        (subnet and app.onvif.subnet_hosts(subnet) and []) or [])
    body = request(server, "GET", "/onvif", cookie=cookies["admin"])[2]  # nothing chosen yet: the field has 192.168.1.0
    assert asked[-1] == "192.168.1.0" and 'value="192.168.1.0"' in body
    status, headers, body = request(server, "GET", "/onvif?subnet=192.168.2.0%2F24", cookie=cookies["admin"])
    assert status == 200 and asked[-1] == "192.168.2.0/24" and 'value="192.168.2.0/24"' in body
    assert "zorcam_onvif_subnet=192.168.2.0/24" in headers["Set-Cookie"] and "Max-Age=31536000" in headers["Set-Cookie"]
    body = request(server, "GET", "/onvif", cookie=cookies["admin"] + "; zorcam_onvif_subnet=192.168.2.0/24")[2]
    assert asked[-1] == "192.168.2.0/24" and 'value="192.168.2.0/24"' in body
    status, headers, _ = request(server, "GET", "/onvif?subnet=", cookie=cookies["admin"])
    assert asked[-1] == "" and "zorcam_onvif_subnet=;" in headers["Set-Cookie"]  # an emptied field is remembered too
    assert "Max-Age=31536000" in headers["Set-Cookie"]
    body = request(server, "GET", "/onvif", cookie=cookies["admin"] + "; zorcam_onvif_subnet=")[2]
    assert asked[-1] == "" and 'value="192.168.1.0"' not in body
    body = request(server, "GET", "/onvif?subnet=8.8.8.0%2F24", cookie=cookies["admin"])[2]
    assert html.escape(t("en", "onvif.subnet_bad", subnet="8.8.8.0/24")) in body and asked[-1] == ""


# ---------------------------------------------------------------- Yandex Smart Home

YA_REDIRECT = "https://social.yandex.net/broker/redirect"
CLOUD_URL = "https://dev1234567.zorcam.test"


@contextlib.contextmanager
def paired_cloud():
    """The Pi is connected to the cloud (cloud.json in the temporary HOME); disconnected again afterwards."""
    import zorcam_cloud
    with zorcam_cloud.locked():
        zorcam_cloud.save({"server": "https://cloud.zorcam.test", "device_id": "dev1234567", "paired": True,
                           "url": CLOUD_URL})
    try:
        yield
    finally:
        zorcam_cloud.remove()


@pytest.fixture
def yandex_cloud():
    with paired_cloud():
        yield


@pytest.fixture
def yandex_skill(yandex_cloud):
    """Cloud connected and the skill keys saved; the settings are put back afterwards."""
    ya = app.YANDEX
    before = copy.deepcopy(ya.data)
    ya.save_settings("skill-1", "s3cret", "oauth-tok")
    app.FAILED_ATTEMPTS.clear()
    del app.YANDEX_FAILURES[:]
    try:
        yield ya
    finally:
        with ya.lock:
            ya.data = before
            ya._save()
        app.FAILED_ATTEMPTS.clear()
        del app.YANDEX_FAILURES[:]


def test_yandex_smart_home_link_devices_and_motion(server, tunnel, cookies, tmp_path, yandex_skill):
    """The whole path: admin saves the skill keys, Yandex sends the owner to /yandex/auth, gets a code, swaps it
    for tokens, lists the cameras as motion sensors, asks their state; motion from the detector goes to the
    callback once per event; unlink drops the tokens."""
    import json as _json
    calls = []

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            calls.append((self.path, self.headers["Authorization"],
                          _json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(202)
            self.end_headers()
    fake = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    ya = app.YANDEX
    ya.api = f"http://127.0.0.1:{fake.server_address[1]}"
    try:
        page = request(server, "GET", "/yandex", cookie=cookies["admin"])[2]
        assert t("en", "ya.not_linked") in page and yandex_client_id() in page
        for url in ("/yandex", "/yandex/auth", "/yandex/token"):  # the cloud address, whatever the page was opened by
            assert f"{CLOUD_URL}{url}</span>" in page
        assert "<form" in page and t("en", "ya.need_cloud") not in page
        form = {"csrf": csrf_of(page, "/yandex/save"), "skill_id": "skill-1", "client_secret": "s3cret",
                "skill_token": "oauth-tok"}
        status, headers, _ = request(server, "POST", "/yandex/save", urlencode(form), cookie=cookies["admin"])
        assert status == 303 and headers["Location"] == "/yandex?saved=1"
        page = request(server, "GET", "/yandex?saved=1", cookie=cookies["admin"])[2]
        assert "s3cret" not in page and "oauth-tok" not in page and t("en", "ya.keep") in page  # never shown back
        assert os.stat(ya.path).st_mode & 0o777 == 0o600

        # the authorization page: only Yandex's own redirect address
        params = {"client_id": "zorcam", "redirect_uri": YA_REDIRECT, "state": "st-1", "response_type": "code"}
        assert request(tunnel, "GET", "/yandex/auth?" + urlencode(params))[0] == 200
        bad = dict(params, redirect_uri="https://evil.example.com/cb")
        assert request(tunnel, "GET", "/yandex/auth?" + urlencode(bad))[0] == 400
        assert request(tunnel, "POST", "/yandex/auth", urlencode(dict(bad, username="admin",
                                                                      password=PASSWORDS["admin"])))[0] == 400
        # a viewer cannot link, nor a wrong password
        for user, pw in (("bob", PASSWORDS["bob"]), ("admin", "nope")):
            assert request(tunnel, "POST", "/yandex/auth", urlencode(dict(params, username=user, password=pw)),
                           headers={"X-Test": "ya"})[0] == 401
        app.FAILED_ATTEMPTS.clear()
        status, headers, _ = request(tunnel, "POST", "/yandex/auth",
                                     urlencode(dict(params, username="admin", password=PASSWORDS["admin"])))
        assert status == 303 and headers["Location"].startswith(YA_REDIRECT + "?code=")
        code = re.search(r"code=([^&]+)", headers["Location"]).group(1)
        assert "state=st-1" in headers["Location"]

        def token(**form):
            st, _, body = request(tunnel, "POST", "/yandex/token", urlencode(form))
            return st, _json.loads(body)
        assert token(grant_type="authorization_code", code=code, client_id="zorcam", client_secret="wrong")[0] == 401
        app.FAILED_ATTEMPTS.clear()
        # only admin saves the keys or unlinks
        for user in ("bob", "guest"):
            for path in ("/yandex/save", "/yandex/unlink"):
                assert request(server, "POST", path, urlencode({"csrf": "x", "skill_id": "y"}),
                               cookie=cookies[user])[0] == 403, (user, path)
        st, tok = token(grant_type="authorization_code", code=code, client_id="zorcam", client_secret="s3cret",
                        redirect_uri=YA_REDIRECT)
        assert st == 200 and tok["token_type"] == "bearer" and tok["access_token"] and tok["refresh_token"]
        assert token(grant_type="authorization_code", code=code, client_id="zorcam",
                     client_secret="s3cret")[0] == 400  # a code works once
        st, again = token(grant_type="refresh_token", refresh_token=tok["refresh_token"], client_id="zorcam",
                          client_secret="s3cret")
        assert st == 200 and again["access_token"] != tok["access_token"]

        bearer = {"Authorization": "Bearer " + again["access_token"], "X-Request-Id": "req-1"}
        assert request(tunnel, "GET", "/yandex/v1.0/user/devices")[0] == 401
        assert request(tunnel, "GET", "/yandex/v1.0/user/devices", headers={"Authorization": "Bearer x"})[0] == 401
        assert request(tunnel, "HEAD", "/yandex/v1.0")[0] == 200
        st, _, body = request(tunnel, "GET", "/yandex/v1.0/user/devices", headers=bearer)
        reply = _json.loads(body)
        assert st == 200 and reply["request_id"] == "req-1" and reply["payload"]["user_id"] == "admin"
        devs = reply["payload"]["devices"]
        assert [(d["id"], d["name"], d["type"]) for d in devs] == [
            ("front", "Front door", "devices.types.sensor.motion"), ("yard", "Yard", "devices.types.sensor.motion")]
        assert devs[0]["status_info"] == {"reportable": True}
        assert devs[0]["properties"][0]["parameters"] == {
            "instance": "motion", "events": [{"value": "detected"}, {"value": "not_detected"}]}
        assert "rtsp" not in body

        def state(cam):
            st, _, body = request(tunnel, "POST", "/yandex/v1.0/user/devices/query",
                                  _json.dumps({"devices": [{"id": cam}, {"id": "nope"}]}),
                                  headers=dict(bearer, **{"Content-Type": "application/json"}))
            devices = _json.loads(body)["payload"]["devices"]
            assert devices[1]["error_code"] == "DEVICE_NOT_FOUND"
            return devices[0]["properties"][0]["state"]["value"]
        assert state("front") == "not_detected"

        # motion: one "detected" per event, "not_detected" after HOLD of quiet
        for _ in range(2):
            assert request(server, "POST", "/internal/motion", _json.dumps({"camera": "front", "name": "x"}),
                           headers=motion_headers())[0] == 200
        assert state("front") == "detected" and ya.outbox.qsize() == 1
        ya.send(*ya.outbox.get_nowait())
        path, auth, sent = calls[-1]
        assert path == "/api/v1/skills/skill-1/callback/state" and auth == "OAuth oauth-tok"
        assert sent["payload"] == {"user_id": "admin", "devices": [{"id": "front", "properties": [
            {"type": "devices.properties.event", "state": {"instance": "motion", "value": "detected"}}]}]}
        ya.tick(time.time() + app.yandex.HOLD + 1)
        ya.send(*ya.outbox.get_nowait())
        assert calls[-1][2]["payload"]["devices"][0]["properties"][0]["state"]["value"] == "not_detected"
        assert state("front") == "not_detected"
        assert "HTTP 202" in request(server, "GET", "/yandex", cookie=cookies["admin"])[2]

        # unlink from Yandex: the tokens are gone
        assert request(tunnel, "POST", "/yandex/v1.0/user/unlink", "{}", headers=bearer)[0] == 200
        assert request(tunnel, "GET", "/yandex/v1.0/user/devices", headers=bearer)[0] == 401
        request(server, "POST", "/internal/motion", _json.dumps({"camera": "front"}),
                headers=motion_headers())
        assert ya.outbox.empty()  # not linked: nothing goes to Yandex
    finally:
        fake.shutdown()
        ya.unlink()


def test_yandex_needs_the_cloud(server, tunnel, cookies, tmp_path):
    """Not connected to the cloud: the section and the page only explain it, the addresses for Yandex do not answer
    (own domain and home network no longer work), nothing is queued for Yandex; the keys stay for later."""
    ya = app.YANDEX
    before = copy.deepcopy(ya.data)
    ya.save_settings("skill-1", "s3cret", "oauth-tok")
    try:
        need = html.escape(t("en", "ya.need_cloud"), quote=False)
        section = request(server, "GET", "/settings", cookie=cookies["admin"])[2]
        assert t("en", "ya.title") in section and need in section and 'href="/yandex"' not in section
        page = request(server, "GET", "/yandex", cookie=cookies["admin"])[2]
        assert need in page and "<form" not in page and "/yandex/token" not in page and "YOUR-DOMAIN" not in page
        assert f'href="{app.settings_url("general")}"' in page
        params = urlencode({"client_id": "zorcam", "redirect_uri": YA_REDIRECT, "response_type": "code"})
        assert request(tunnel, "GET", "/yandex/auth?" + params)[0] == 404
        assert request(tunnel, "GET", "/yandex/v1.0")[0] == 404
        assert request(tunnel, "HEAD", "/yandex/v1.0")[0] == 404
        assert request(tunnel, "GET", "/yandex/v1.0/user/devices", headers={"Authorization": "Bearer x"})[0] == 404
        for path in ("/yandex/token", "/yandex/auth", "/yandex/v1.0/user/unlink"):
            assert request(server, "POST", path, urlencode({"client_id": "zorcam"}))[0] == 404, path
        # saving without the cloud changes nothing and sends the admin back to the explanation
        status, headers, _ = request(server, "POST", "/yandex/save", urlencode({
            "csrf": csrf_of(request(server, "GET", "/settings", cookie=cookies["admin"])[2], "/cloud/connect"),
            "skill_id": "other"}), cookie=cookies["admin"])
        assert status == 303 and headers["Location"] == "/yandex" and ya.status()["skill_id"] == "skill-1"
        request(server, "POST", "/internal/motion", json.dumps({"camera": "front"}),
                headers=motion_headers())
        assert ya.outbox.empty() and not ya.active
        assert ya.status()["has_secret"]  # kept
    finally:
        with ya.lock:
            ya.data = before
            ya._save()


def test_yandex_addresses_only_through_the_cloud_tunnel(server, tunnel, cookies, yandex_skill):
    """With the cloud: HEAD /yandex/v1.0 answers; every path that starts with /yandex (also encoded, with ".."
    or another case) goes to the Yandex handlers only, so the cloud letting /yandex* through opens nothing else."""
    assert request(tunnel, "HEAD", "/yandex/v1.0")[0] == 200
    # not the tunnel port (the home network, a reverse proxy on the device): the addresses are not there at all
    for method, path in (("HEAD", "/yandex/v1.0"), ("GET", "/yandex/v1.0/user/devices"), ("GET", "/yandex/auth"),
                         ("POST", "/yandex/auth"), ("POST", "/yandex/token")):
        assert request(server, method, path, headers={"X-Zorcam-Client-IP": "203.0.113.9"})[0] == 404, path
    assert request(tunnel, "HEAD", "/yandex/v1.0/")[0] == 200
    assert request(tunnel, "HEAD", "/yandex/v1.0/user")[0] == 404
    for path in ("/yandex/../config", "/yandex/..%2fconfig", "/yandex%2F..%2Fconfig", "/yandex/%2e%2e/settings",
                 "/%79andex/../config", "/yandex/", "/Yandex/v1.0", "/yandexfoo", "/yandex/save",
                 "/yandex/unlink", "/yandex/auth/x"):
        for cookie in (None, cookies["admin"]):
            status, _, body = request(tunnel, "GET", path, cookie=cookie)
            assert status == 404 and "csrf" not in body, (path, status)
    # the admin page and its forms stay behind the admin role
    assert request(server, "GET", "/yandex")[0] == 302
    assert request(server, "GET", "/yandex", cookie=cookies["bob"])[0] == 403
    assert request(server, "POST", "/yandex/save", "csrf=x")[0] == 302  # to the sign-in page


def test_yandex_lockout_follows_the_client_ip_of_the_tunnel(server, tunnel, yandex_skill, monkeypatch):
    """Through the tunnel every request comes from 127.0.0.1: the cloud names the real client in X-Zorcam-Client-IP,
    and wrong guesses are counted (and blocked) per that address, not for everybody at once."""
    monkeypatch.setattr(app, "MAX_ATTEMPTS", 5)  # the limit of one address is milder than the whole device's: look at it alone
    app.FAILED_ATTEMPTS.clear()
    try:
        def token(ip):
            return request(tunnel, "POST", "/yandex/token", urlencode({"client_id": "zorcam", "client_secret": "x"}),
                           headers={"X-Zorcam-Client-IP": ip} if ip else None)[0]
        for _ in range(app.MAX_ATTEMPTS):
            assert token("203.0.113.7") == 401
        assert token("203.0.113.7") == 429
        assert token("203.0.113.8") == 401  # another client of the same tunnel
        assert token("2001:db8::1") == 401
        assert "127.0.0.1" not in app.FAILED_ATTEMPTS
        # no header, or not an address: counted for 127.0.0.1 as before
        assert token(None) == 401 and token("not-an-ip") == 401 and token("1.2.3.4:5") == 401
        assert len(app.FAILED_ATTEMPTS["127.0.0.1"]) == 3
    finally:
        app.FAILED_ATTEMPTS.clear()


def test_yandex_addresses_do_not_answer_without_skill_keys(server, tunnel, yandex_cloud):
    """The cloud lets /yandex* through to every paired device: without the saved skill keys the device must not
    work as a password oracle for admin (or answer anything else) — the paths look unknown, also through the tunnel."""
    ya = app.YANDEX
    before = copy.deepcopy(ya.data)
    app.FAILED_ATTEMPTS.clear()
    with ya.lock:
        ya.data = {"tokens": {}}
    try:
        assert not ya.configured()
        params = {"client_id": "zorcam", "redirect_uri": YA_REDIRECT, "response_type": "code"}
        via = {"X-Zorcam-Client-IP": "203.0.113.9"}
        assert request(tunnel, "GET", "/yandex/auth?" + urlencode(params), headers=via)[0] == 404
        assert request(tunnel, "POST", "/yandex/auth", urlencode(dict(params, username="admin", password="admin")),
                       headers=via)[0] == 404
        assert request(tunnel, "POST", "/yandex/token", urlencode({"client_id": "zorcam"}), headers=via)[0] == 404
        assert request(tunnel, "HEAD", "/yandex/v1.0")[0] == 404
        assert request(tunnel, "GET", "/yandex/v1.0/user/devices", headers={"Authorization": "Bearer x"})[0] == 404
        assert request(tunnel, "POST", "/yandex/v1.0/user/unlink", "{}", headers=via)[0] == 404
        assert not app.FAILED_ATTEMPTS and not app.YANDEX_FAILURES  # nothing was counted: nothing was checked
        ya.save_settings("skill-1")  # the secret is still missing
        assert not ya.configured() and request(tunnel, "HEAD", "/yandex/v1.0")[0] == 404
        ya.save_settings("skill-1", "s3cret")
        assert ya.configured() and request(tunnel, "HEAD", "/yandex/v1.0")[0] == 200
    finally:
        with ya.lock:
            ya.data = before
            ya._save()


def test_yandex_failures_are_limited_for_the_whole_device(server, tunnel, yandex_skill):
    """Guesses from many addresses are one budget: after YANDEX_MAX_FAILURES wrong passwords or secrets within the
    window, /yandex/auth answers 429 and /yandex/token slow_down to everybody — even with the right password."""
    logged = []
    orig_log = app.log
    app.log = logged.append
    try:
        params = {"client_id": "zorcam", "redirect_uri": YA_REDIRECT, "response_type": "code"}

        def auth(ip, password):
            return request(tunnel, "POST", "/yandex/auth", urlencode(dict(params, username="admin", password=password)),
                           headers={"X-Zorcam-Client-IP": ip})[0]

        def token(ip):
            return request(tunnel, "POST", "/yandex/token", urlencode({"client_id": "zorcam", "client_secret": "x"}),
                           headers={"X-Zorcam-Client-IP": ip})
        half = app.YANDEX_MAX_FAILURES // 2
        for i in range(half):
            assert auth(f"198.51.100.{i}", "wrong") == 401
        for i in range(app.YANDEX_MAX_FAILURES - half):
            assert token(f"203.0.113.{i}")[0] == 401
        assert len([m for m in logged if "failed sign-ins" in m]) == 1
        # a new address, the real password: still refused
        assert auth("192.0.2.77", "wrong") == 429
        status, _, body = token("192.0.2.78")
        assert status == 429 and json.loads(body) == {"error": "slow_down"}
        assert len([m for m in logged if "failed sign-ins" in m]) == 1  # once per window
        # the window passes: answers again
        app.YANDEX_FAILURES[:] = [t - app.YANDEX_FAILURE_WINDOW - 1 for t in app.YANDEX_FAILURES]
        assert auth("192.0.2.77", "wrong") == 401
    finally:
        app.log = orig_log


def test_tunnel_client_ip_is_trusted_only_on_the_tunnel_port():
    def ip_of(on_tunnel, value):
        handler = types.SimpleNamespace(on_tunnel=on_tunnel, client_address=("127.0.0.1", 1),
                                        headers={"X-Zorcam-Client-IP": value})
        return app.Handler.tunnel_client_ip(handler)
    assert ip_of(True, "203.0.113.7") == "203.0.113.7"
    assert ip_of(True, " 2001:DB8::1 ") == "2001:db8::1"
    assert ip_of(True, "127.0.0.1") is None  # not a client of the tunnel: it would share the counter with local requests
    assert ip_of(False, "203.0.113.7") is None  # port 80 (also a reverse proxy on the device): the header is nothing
    for bad in ("", "x", "1.2.3.4, 5.6.7.8", "[2001:db8::1]", "1.2.3.4:80"):
        assert ip_of(True, bad) is None, bad


def test_yandex_notifications_stop_with_the_cloud(tmp_path):
    """Queued notifications are dropped and new ones are not queued once the cloud is gone."""
    import yandex
    on = [True]
    ya = yandex.Yandex(path=str(tmp_path / "yandex.json"), log=lambda m: None, enabled=lambda: on[0])
    ya.save_settings("skill", "sec", "tok")
    ya.data["tokens"]["x"] = {"kind": "refresh", "created": 1}
    ya.motion("front")
    assert ya.outbox.qsize() == 1
    on[0] = False
    ya.motion("yard")
    ya.tick(time.time() + yandex.HOLD + 1)
    assert ya.outbox.qsize() == 1 and "yard" not in ya.last_motion  # nothing new, not even a state to keep


# ---------------------------------------------------------------- MQTT

@pytest.fixture
def mqtt_settings():
    """The MQTT settings of the gallery, switched off again (and the motion forgotten) after the test."""
    yield app.MQTT
    app.MQTT.save(dict(app.mqtt.DEFAULTS))
    app.MQTT.episodes.clear()
    app.MQTT.state, app.MQTT.error = "off", None


MQTT_FORM = {"enabled": "yes", "host": "broker.lan", "port": "", "username": "ha", "password": "s3cret-broker",
             "prefix": "home", "device": "box", "discovery": "yes"}


def test_mqtt_page_saves_without_returning_the_password(server, cookies, mqtt_settings):
    """Settings -> General has the MQTT row; /mqtt saves mqtt.json (600), applied without a restart; the password never
    comes back into the form, an empty field keeps it."""
    general = request(server, "GET", "/settings?tab=general", cookie=cookies["admin"])[2]
    assert t("en", "mqtt.title") in general and 'href="/mqtt"' in general and t("en", "mqtt.off") in general
    page = request(server, "GET", "/mqtt", cookie=cookies["admin"])[2]
    assert 'action="/mqtt/save"' in page and 'name="password"' in page and 'value="zorcam"' in page
    csrf = csrf_of(page, "/mqtt/save")
    status, headers, _ = request(server, "POST", "/mqtt/save", urlencode({"csrf": csrf, **MQTT_FORM}), cookie=cookies["admin"])
    assert status == 303 and headers["Location"] == "/mqtt?saved=1"
    path = os.path.join(HOME, "config", "mqtt.json")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    saved = json.load(open(path))
    assert saved["host"] == "broker.lan" and saved["port"] == 1883 and saved["password"] == "s3cret-broker"
    assert saved["enabled"] and saved["discovery"] and saved["prefix"] == "home" and saved["device"] == "box"
    page = request(server, "GET", "/mqtt?saved=1", cookie=cookies["admin"])[2]
    assert t("en", "mqtt.saved") in page and 's3cret-broker' not in page
    assert 'value="broker.lan"' in page and 'value="ha"' in page and t("en", "mqtt.keep") in page
    assert "On: broker.lan:1883" in request(server, "GET", "/settings?tab=general", cookie=cookies["admin"])[2]
    # the password field empty: the old one stays; TLS without a port: 8883
    form = {"csrf": csrf, **MQTT_FORM, "password": "", "tls": "yes"}
    assert request(server, "POST", "/mqtt/save", urlencode(form), cookie=cookies["admin"])[0] == 303
    saved = json.load(open(path))
    assert saved["password"] == "s3cret-broker" and saved["port"] == 8883 and saved["tls"]
    assert app.MQTT.settings()["port"] == 8883 and app.MQTT.changed.is_set()  # the thread reconnects by itself
    # the Russian page
    ru = request(server, "GET", "/mqtt", cookie=cookies["admin"] + "; zorcam_lang=ru")[2]
    assert t("ru", "mqtt.title") in ru and "s3cret-broker" not in ru


def test_mqtt_page_keeps_the_input_on_errors(server, cookies, mqtt_settings):
    csrf = csrf_of(request(server, "GET", "/mqtt", cookie=cookies["admin"])[2], "/mqtt/save")
    bad = {"csrf": csrf, **MQTT_FORM, "host": "bro ker", "port": "99999", "prefix": "a b", "device": "Box"}
    status, _, page = request(server, "POST", "/mqtt/save", urlencode(bad), cookie=cookies["admin"])
    assert status == 400 and t("en", "mqtt.invalid") in page
    for key in ("mqtt.bad.host", "mqtt.bad.port", "mqtt.bad.prefix", "mqtt.bad.device"):
        assert t("en", key) in page
    assert 'value="bro ker"' in page and "s3cret-broker" not in page
    assert not os.path.exists(os.path.join(HOME, "config", "mqtt.json")) or not app.MQTT.enabled()
    no_host = {"csrf": csrf, **{k: v for k, v in MQTT_FORM.items() if k != "host"}}
    status, _, page = request(server, "POST", "/mqtt/save", urlencode(no_host), cookie=cookies["admin"])
    assert status == 400 and t("en", "mqtt.bad.host_empty") in page


def test_mqtt_save_needs_the_admin_and_the_csrf_token(server, cookies, mqtt_settings):
    csrf = csrf_of(request(server, "GET", "/mqtt", cookie=cookies["admin"])[2], "/mqtt/save")
    body = urlencode({"csrf": csrf, **MQTT_FORM})
    assert request(server, "POST", "/mqtt/save", urlencode({"csrf": "0" * 32, **MQTT_FORM}), cookie=cookies["admin"])[0] == 403
    assert request(server, "POST", "/mqtt/save", urlencode(MQTT_FORM), cookie=cookies["admin"])[0] == 403
    assert request(server, "POST", "/mqtt/save", body, cookie=cookies["bob"])[0] == 403
    assert request(server, "POST", "/mqtt/save", body, cookie=cookies["guest"])[0] == 403
    assert request(server, "POST", "/mqtt/save", body)[0] in (302, 401, 403)
    assert not app.MQTT.enabled()
    for user in ("bob", "guest"):  # the MQTT row is in the admin's General tab only
        assert t("en", "mqtt.title") not in request(server, "GET", "/settings?tab=general", cookie=cookies[user])[2]


def test_mqtt_row_in_status(server, cookies, mqtt_settings):
    """Settings -> Status: no row while MQTT is off, "connected to host:port", or the last error in amber."""
    status = lambda: request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])[2]  # noqa: E731
    assert t("en", "st.mqtt") + "</div>" not in status()
    cfg, _ = app.mqtt.validate({"enabled": "yes", "host": "broker.lan"}, app.MQTT.cfg)
    app.MQTT.save(cfg)
    assert t("en", "st.mqtt_connecting", host="broker.lan", port=1883) in status()
    app.MQTT.state = "connected"
    assert (f'<div class="k">{t("en", "st.mqtt")}</div><div class="v ">'
            f'{t("en", "st.mqtt_ok", host="broker.lan", port=1883)}</div>') in status()
    app.MQTT.state, app.MQTT.error = "error", Msg("mqtt.err.credentials")
    body = status()
    assert f'<div class="v warn">{t("en", "st.mqtt_none", host="broker.lan", port=1883)}</div>' in body
    assert t("en", "mqtt.err.credentials") in body
    ru = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"] + "; zorcam_lang=ru")[2]
    assert t("ru", "mqtt.err.credentials") in ru


def test_motion_goes_to_mqtt_for_every_enabled_camera(server, mqtt_settings, monkeypatch):
    """The detector's signal reaches MQTT whatever the camera's push switch says; a switched-off camera and an unknown one
    are not published; while MQTT is off nothing is remembered."""
    def signal(cam_id):
        return request(server, "POST", "/internal/motion", json.dumps({"camera": cam_id, "name": "x"}),
                       headers=motion_headers())[0]
    assert signal("front") == 200 and not mqtt_settings.episodes.active
    cfg, _ = app.mqtt.validate({"enabled": "yes", "host": "broker.lan"}, mqtt_settings.cfg)
    mqtt_settings.save(cfg)
    real = app.camera
    monkeypatch.setattr(app, "camera", lambda cam_id: dict(real(cam_id), notify_motion=False) if real(cam_id) else None)
    assert signal("front") == 200 and signal("nope") == 200
    assert mqtt_settings.episodes.active == {"front"} and mqtt_settings.wake.is_set()
    monkeypatch.setattr(app, "camera", lambda cam_id: dict(real(cam_id), enabled=False) if real(cam_id) else None)
    assert signal("yard") == 200
    assert mqtt_settings.episodes.active == {"front"}


def yandex_client_id():
    return app.yandex.CLIENT_ID


def test_status_shows_the_edition(server, cookies):
    """Settings → Status names the edition: the free Zorcam NVR here (no EDITION file next to the program)."""
    status, _, body = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])
    assert status == 200 and t("en", "st.edition") in body and t("en", "edition.community") in body


def test_device_info_reads_the_model_and_the_nominal_ram(tmp_path):
    """The model comes from the device tree (a trailing NUL dropped), the RAM is MemTotal rounded up to the nominal
    size when that is within a quarter (part of it goes to the GPU and the kernel); nothing readable gives None."""
    model, meminfo = tmp_path / "model", tmp_path / "meminfo"
    model.write_bytes(b"Raspberry Pi 3 Model B Rev 1.2\0")
    meminfo.write_text("MemTotal:         927824 kB\nMemFree:  1000 kB\n")
    assert app.device_info((str(model),), str(meminfo)) == ("Raspberry Pi 3 Model B Rev 1.2", 1024)
    meminfo.write_text("MemTotal:        12288000 kB\n")  # 12 GB: not near a power of two — as it is
    assert app.device_info((str(tmp_path / "none"), str(model)), str(meminfo))[1] == 12000
    assert app.device_info((str(tmp_path / "none"),), str(tmp_path / "none")) == (None, None)
    assert app.fmt_ram("ru", 1024) == t("ru", "st.ram_gb", n="1")
    assert app.fmt_ram("en", 512) == "512 MB" and app.fmt_ram("en", 12000) == "11.7 GB"


def test_status_shows_the_device(server, cookies, monkeypatch):
    """Settings -> Status: a Device row with the model and the RAM."""
    monkeypatch.setattr(app, "device_info", lambda: ("Test Board 3", 1024))
    body = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])[2]
    assert f'<div class="k">{t("en", "st.device")}</div><div class="v ">Test Board 3\nRAM 1 GB</div>' in body


def test_status_advises_about_the_network_only_when_there_is_something_to_fix(server, cookies, monkeypatch):
    """Settings -> Status: no Network row on a cable with the radios off; on Wi-Fi - the value and the advice in amber;
    on a cable with a radio on - the other advice. Not a check: the summary and the problem count stay as they were."""
    monkeypatch.setattr(app.HEALTH, "checks", {"disk": (True, app.Msg("health.running"))})
    monkeypatch.setattr(app.HEALTH, "problems", {})
    monkeypatch.setattr(app.HEALTH, "checked", time.time())
    monkeypatch.setattr(app.HEALTH, "events", [])
    summary = f'<div class="k">{t("en", "st.summary")}</div><div class="v ok">{t("en", "st.all_ok")}</div>'
    label = f'<div class="k">{t("en", "st.network")}</div>'

    def page(state, cookie=cookies["admin"]):
        monkeypatch.setattr(zorcam_config, "network_state", lambda *a, **k: state)
        return request(server, "GET", "/settings?tab=status", cookie=cookie)[2]

    body = page({"via": "ethernet", "wifi": False, "bluetooth": False})
    assert label not in body and t("en", "st.network_why_radio") not in body and summary in body
    assert label not in page({"via": None, "wifi": False, "bluetooth": False})  # no route, nothing to turn off

    def row(body, value, advice, lang="en"):  # the label with the advice under it (amber), the value (amber) on the right
        sub = html.escape(t(lang, advice))
        return (f'<div class="row"><div class="k">{t(lang, "st.network")}<span class="sub warn">{sub}</span></div>'
                f'<div class="v warn">{t(lang, value)}</div></div>') in body

    wifi = page({"via": "wifi", "wifi": True, "bluetooth": False})
    assert row(wifi, "st.network_wifi", "st.network_why_wifi") and summary in wifi
    for state, value in (({"via": "ethernet", "wifi": True, "bluetooth": False}, "st.network_radio_wifi"),
                         ({"via": "ethernet", "wifi": False, "bluetooth": True}, "st.network_radio_bt"),
                         ({"via": "ethernet", "wifi": True, "bluetooth": True}, "st.network_radio_both"),
                         ({"via": None, "wifi": False, "bluetooth": True}, "st.network_radio_bt")):
        body = page(state)
        assert row(body, value, "st.network_why_radio") and summary in body, state
        assert "Connect the device with an Ethernet cable" not in body
    # the row follows the Device row and comes before the rest
    monkeypatch.setattr(app, "device_info", lambda: ("Test Board 3", 1024))
    body = page({"via": "wifi", "wifi": True, "bluetooth": True})
    assert body.index(t("en", "st.device")) < body.index(t("en", "st.network")) < body.index(t("en", "st.timezone"))
    ru = page({"via": "wifi", "wifi": True, "bluetooth": True}, cookie=cookies["admin"] + "; zorcam_lang=ru")
    assert row(ru, "st.network_wifi", "st.network_why_wifi", "ru")
    # only admin has the Status tab
    assert request(server, "GET", "/settings?tab=status", cookie=cookies["bob"])[2].count(t("en", "st.network")) == 0


def test_notifications_tab_switches_work_at_once(server, cookies):
    """Settings -> Notifications (viewer and up): the general switch and one for each camera of the user, no Save
    button: every switch goes to the server at once. A liveviewer has no such tab;
    push goes only to those who did not switch the camera or everything off."""
    import zorcam_users
    guest_page = request(server, "GET", "/settings?tab=notify", cookie=cookies["guest"])[2]
    assert "data-notify" not in guest_page and t("en", "settab.notify") not in guest_page
    page = request(server, "GET", "/settings?tab=notify", cookie=cookies["bob"])[2]
    assert t("en", "settab.notify") in page and 'data-notify="all" checked' in page
    cams = [c["id"] for c in app.user_cameras("bob", "viewer")]
    assert cams and all(f'data-notify="{c}"' in page for c in cams)
    assert 'type="submit"' not in page  # no Save button
    csrf = re.search(r'<section data-csrf="([0-9a-f]+)"', page).group(1)

    def switch(who, camera, on, token=csrf):
        return request(server, "POST", "/settings/notify", json.dumps({"csrf": token, "camera": camera, "on": on}),
                       cookie=cookies[who], headers={"Content-Type": "application/json"})[0]

    assert switch("bob", cams[0], False) == 200
    assert not zorcam_users.wants_push("bob", cams[0]) and all(zorcam_users.wants_push("bob", c) for c in cams[1:])
    assert f'data-notify="{cams[0]}" checked' not in request(server, "GET", "/settings?tab=notify", cookie=cookies["bob"])[2]
    assert app.push_users(cams[0]) & {"bob"} == set()
    assert switch("bob", cams[0], True) == 200 and zorcam_users.wants_push("bob", cams[0])
    assert switch("bob", "all", False) == 200
    assert all(not zorcam_users.wants_push("bob", c) for c in cams) and "bob" not in app.push_users(cams[0])
    assert 'data-notify="all" checked' not in request(server, "GET", "/settings?tab=notify", cookie=cookies["bob"])[2]
    assert switch("bob", "all", True) == 200
    # the choice is the user's own: nothing changes for the others
    assert zorcam_users.wants_push("admin", cams[0])
    # refused: a wrong token, a camera that is not the user's, a non-boolean, a liveviewer
    assert switch("bob", cams[0], False, token="0" * 64) == 403
    assert switch("bob", "nope", False) == 400
    assert request(server, "POST", "/settings/notify", json.dumps({"csrf": csrf, "camera": "all", "on": "no"}),
                   cookie=cookies["bob"], headers={"Content-Type": "application/json"})[0] == 400
    assert switch("guest", "all", False) == 403
    assert request(server, "POST", "/settings/notify", "{}")[0] == 302  # not signed in
    assert zorcam_users.wants_push("bob", cams[0])


def test_notifications_tab_turns_on_the_camera_setting(server, cookies, restore_cameras):
    """A camera with Motion notifications off in its settings is shown off but usable; turning it on there turns on
    the camera setting (cameras.json) and the user's own choice; turning it off leaves the camera setting alone."""
    import zorcam_users
    cams = app.cameras()
    cams[0]["notify_motion"] = False
    zorcam_config.save_cameras(cams)
    cam = cams[0]["id"]
    page = request(server, "GET", "/settings?tab=notify", cookie=cookies["bob"])[2]
    assert f'data-notify="{cam}"' in page and f'data-notify="{cam}" checked' not in page
    assert not re.search(r'<input[^>]*data-notify[^>]*disabled', page)
    csrf = re.search(r'<section data-csrf="([0-9a-f]+)"', page).group(1)

    def switch(who, camera, on):
        return request(server, "POST", "/settings/notify", json.dumps({"csrf": csrf, "camera": camera, "on": on}),
                       cookie=cookies[who], headers={"Content-Type": "application/json"})[0]

    assert switch("bob", cam, False) == 200 and app.camera(cam)["notify_motion"] is False
    assert switch("guest", cam, True) == 403 and app.camera(cam)["notify_motion"] is False
    assert switch("bob", "nope", True) == 400 and app.camera(cam)["notify_motion"] is False
    assert switch("bob", cam, True) == 200
    assert app.camera(cam)["notify_motion"] is True and zorcam_users.wants_push("bob", cam)
    assert f'data-notify="{cam}" checked' in request(server, "GET", "/settings?tab=notify", cookie=cookies["bob"])[2]
    assert switch("bob", cam, False) == 200  # only the user's own choice
    assert app.camera(cam)["notify_motion"] is True and not zorcam_users.wants_push("bob", cam)
    assert switch("bob", cam, True) == 200


def test_styles_are_the_strict_light_console(server, cookies):
    """One light theme as in the cloud app: colours are CSS variables, no dark theme, no old iOS blue, no shadows,
    radii up to 2 px (circles only for dots and the spinner), no fixed black page background of the old design."""
    for path in ("/login", "/?tab=live", "/settings", "/config", f"/watch/front/{VIDEO}"):
        body = request(server, "GET", path, cookie=None if path == "/login" else cookies["admin"])[2]
        body += assets(server, body)  # the CSS and JS of the page
        assert "prefers-color-scheme" not in body and "--bg:#f3f3f1" in body and "--text:#1c1c1c" in body, path
        assert "--mono:ui-monospace" in body and "color-scheme:light;" in body, path
        assert "#0b0c0f" not in body and "var(--accent)" not in body and "var(--bg-elevated)" not in body, path
        assert '<meta name="theme-color" content="#f3f3f1">' in body, path
        assert 'status-bar-style" content="default"' in body, path
        assert not re.search(r"#007aff|#0a84ff|#34c759|#ff3b30|#ff9500"
                              r"|var\(--(blue|orange|label2?|sep|group|seg-on)\)", body), path
        assert "box-shadow" not in body, path
        assert not re.search(r"border-radius:\s*(?!0|1px|2px|50%)\d+", body), path
        assert "backdrop-filter" not in body, path
        assert "linear-gradient(rgba" not in body and "linear-gradient(transparent" not in body, path


def test_phone_header_has_the_clock_of_the_pi(server, cookies):
    """The phone header: ZORCAM, the clock by the zone of the Pi (the offset for JS), then the buttons."""
    for path in ("/?tab=live", "/settings"):
        body = request(server, "GET", path, cookie=cookies["admin"])[2]
        assert re.search(r'<span class="clock" id="clock" data-off="-?\d+" title="[^"]+">\d\d:\d\d:\d\d</span>', body), path
        assert "getElementById('clock')" in assets(server, body), path
    assert 'id="clock"' not in request(server, "GET", "/?tab=live&cam=front&embed=1", cookie=cookies["admin"])[2]


# ---------------------------------------------------------------- snapshots with the motion mark (<time>_m.jpg)

def test_motion_marked_snapshots_work_like_plain_ones(server, cookies, tmp_path, monkeypatch):
    """The detector names a snapshot taken after motion <time>_m.jpg: it is listed, captioned, opened, thumbnailed,
    counted, removed by age and by space and deleted with the others, exactly as a plain one."""
    from datetime import datetime, timedelta
    monkeypatch.setattr(app, "BASE_DIR", str(tmp_path))
    raw = tmp_path / "front"
    raw.mkdir()
    (tmp_path / "front_videos").mkdir()
    now = datetime.now()
    data = jpeg()
    plain, marked = [], []
    for i in range(6):
        name = (now - timedelta(seconds=5 * i)).strftime("%Y%m%d_%H%M%S_") + f"{i:03d}"
        (marked if i % 2 else plain).append(name + ("_m" if i % 2 else "") + ".jpg")
        (raw / (marked if i % 2 else plain)[-1]).write_bytes(data)
    names = sorted(plain + marked, reverse=True)
    assert app.list_files("front", ".jpg") == names
    for n in marked:
        assert app.parse_ts(n) == app.parse_ts(n.replace("_m", "")) and app.format_caption(n) != n
        assert app.card_caption(n).count("<span>") == 2
    body = request(server, "GET", "/?tab=all&cam=front", cookie=cookies["bob"])[2]
    assert 'class="badge"' not in body
    for n in marked:
        assert request(server, "GET", f"/img/front/{n}", cookie=cookies["bob"])[0] == 200
        assert request(server, "GET", f"/thumb/front/{n}", cookie=cookies["bob"])[0] == 200
    poster = request(server, "GET", "/cloud/v1/poster/front", cookie=cookies["bob"])  # the newest one is plain
    assert poster[0] == 200
    (raw / names[0]).unlink()
    (raw / (names[0][:-4] + "_m.jpg")).write_bytes(data)  # the newest is a marked one now
    assert request(server, "GET", "/cloud/v1/poster/front", cookie=cookies["bob"])[0] == 200
    # by age (the file's date, whatever its name) and by space: the oldest go first
    old = raw / "20200101_000000_000_m.jpg"
    old.write_bytes(data)
    os.utime(old, (0, 0))
    app.Health().cleanup()
    assert not old.exists() and len(list(raw.iterdir())) == len(names)
    removed, _ = app.free_disk(str(tmp_path), len(data) * 2)
    assert removed == 2 and len(list(raw.iterdir())) == len(names) - 2
    app.HEALTH.cleanup()
    assert app.RECORDINGS_STATS["front"][0] == len(names) - 2
    assert app.delete_recordings("front", "frames")[0] == len(names) - 2 and not list(raw.iterdir())


def test_connections_are_kept_alive(server, cookies):
    """HTTP/1.1: pages, redirects, JSON and images go over one connection one after another (the timelapse takes
    hundreds of frames); an answer to a POST and an error close it, so a request body left unread cannot be
    taken for the next request."""
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=15)
    cookie = {"Cookie": cookies["bob"]}

    def get(path, headers=None, method="GET", body=None):
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp, resp.read()
    sock = None
    for path, status, headers in (("/healthz", 200, {}), ("/login", 200, {}), ("/", 302, {}),
                                  (f"/thumb/front/{FRAME}", 200, cookie), (f"/img/front/{FRAME}", 200, cookie),
                                  ("/cloud/v1/cameras", 200, cookie), ("/?tab=all", 200, cookie),
                                  (f"/img/front/{FRAME}", 206, {**cookie, "Range": "bytes=0-9"}),
                                  (f"/img/front/{FRAME}", 416, {**cookie, "Range": "bytes=999999999-"})):
        resp, body = get(path, headers)
        assert resp.status == status and not resp.will_close, path
        assert resp.getheader("Content-Length") is not None, path
        sock = sock or conn.sock
        assert conn.sock is sock, path  # still the same connection
    assert len(body) == 0 and get("/", cookie)[0].status == 200
    resp, _ = get("/nope", cookie)  # an error answer closes the connection
    assert resp.status == 404 and resp.will_close
    resp, body = get("/login", method="POST", body=urlencode({"username": "bob", "password": "x" * 5000}),
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status == 401 and resp.will_close and b"Sign in" in body
    resp, _ = get("/healthz")  # a new connection after that one works
    assert resp.status == 200
    conn.close()


def test_timelapse_tab_is_the_player_only(server, cookies):
    """Timelapse: only the player and the calendar under it, no grid, no mode switch, no period pills of the grid;
    the camera tabs stay. Old links of the snapshot grid (mode=, horizon=, fpage=) open the same tab."""
    for user in ("admin", "bob"):
        body = request(server, "GET", "/?tab=all&cam=front", cookie=cookies[user])[2]
        panel = body[body.index('data-panel="all"'):body.index("<script")]
        assert 'class="tl" data-cam="front" data-api="/timelapse/front"' in panel and "tl-hint" not in panel, user
        assert "tl-canvas" in panel and 'class="tl-strip"' in panel and 'class="tl-cal"' in panel
        # the time of the frame is at the bottom of the picture, the motion mark at the top
        assert panel.index('class="tl-top"') < panel.index('class="tl-bottom"') < panel.index('class="tl-time"'), user
        assert 'class="pill active" href="/?tab=all&amp;cam=front"' in panel
        assert 'href="/?tab=all&amp;cam=yard"' in panel
        assert 'class="card"' not in panel and 'class="grid"' not in panel and 'class="pager"' not in panel
        assert "modes" not in panel and "mode=" not in panel and "horizon=" not in panel and "/view/" not in panel
        assert "data-period" not in panel  # no Hour / Today switch
        assert re.findall(r'data-go="(\w+)">([^<]+)<', panel) == [("all", "Timelapse"), ("videos", "Video")]
        assert re.findall(r'data-speed="([\d.]+)">([^<]+)<', panel) == [
            (n, "×" + n) for n in ("0.25", "0.5", "1", "2", "4")]  # real-time labels come from the frames, in JS
        assert 'class="pill active" data-speed="1"' in panel
    for query in ("mode=grid&horizon=60", "mode=timelapse", "horizon=5&fpage=3", "mode=%ff&horizon=x&fpage=y"):
        status, _, old = request(server, "GET", f"/?tab=all&cam=front&{query}", cookie=cookies["bob"])
        assert status == 200 and 'class="tl" data-cam="front"' in old and 'class="grid"' not in old.split("<script")[0].split(
            'data-panel="all"')[1], query
    ru = request(server, "GET", "/?tab=all&cam=front", cookie=cookies["admin"] + "; zorcam_lang=ru")[2]
    assert t("ru", "tab.records") in ru and "[[" not in ru
    assert request(server, "GET", f"/view/front/{FRAME}", cookie=cookies["admin"])[0] == 404  # no page of one snapshot


def test_timelapse_speeds_slow_down_without_skipping(server, cookies):
    """x0.25 and x0.5 show every frame at 15 * speed fps; x1, x2, x4 are 15 fps and skip 0, 1, 3 frames; the value is a float."""
    body = request(server, "GET", "/?tab=all&cam=front", cookie=cookies["bob"])[2]
    js = assets(server, body)
    assert "parseFloat(b.getAttribute('data-speed'))" in js and "parseInt(b.getAttribute('data-speed')" not in js
    assert "function stepOf() { return speed < 1 ? 1 : speed; }" in js
    assert "1000 / (FPS * Math.min(speed, 1)) - 3" in js


def test_records_tab_switches_video_and_timelapse(server, cookies):
    """One Records tab in the bar; Video / Timelapse is a switch in the panel (Timelapse in Russian too); the panel id
    (all), the URL and the camera counters stay."""
    assert t("en", "tab.all") == t("ru", "tab.all") == "Timelapse"
    for headers, lang in (({}, "en"), (RU, "ru")):
        body = request(server, "GET", "/?tab=all&cam=front", cookie=cookies["bob"], headers=headers)[2]
        bar = body[body.index('<nav class="tabbar">'):body.index("</nav>", body.index('<nav class="tabbar">'))]
        assert 'data-tab="records"' in bar and 'data-tab="all"' not in bar and 'data-tab="videos"' not in bar, lang
        assert bar.count('class="tab') == 4 and "Timelapse" not in bar, lang  # Live, Records, Settings (+ the active class)
        assert 'data-go="all">Timelapse</button>' in body and "Snapshots" not in bar, lang
        assert "\u0421\u043d\u0438\u043c\u043a\u0438" not in bar, lang  # the old Russian name
    for user in ("bob", "admin"):
        assert 'href="/?tab=all&amp;cam=yard"' in request(server, "GET", "/?tab=all&cam=front", cookie=cookies[user])[2]


def test_timelapse_speeds_are_real_time_multipliers(server, cookies):
    """The buttons keep the factors 0.25 ... 4 (data-speed) and get real-time labels in JS: the median gap between the
    frames * 15 fps * factor; the default (every snapshot) is chosen again on every load of a period or a day."""
    for headers, lang in (({}, "en"), (RU, "ru")):
        body = request(server, "GET", "/?tab=all&cam=front", cookie=cookies["bob"], headers=headers)[2]
        assert json.dumps(t(lang, "tl.realtime"), ensure_ascii=False) in body and "[[" not in body, lang
        assert "[[" not in assets(server, body), lang
        assert "{n}" not in t(lang, "tl.realtime")
    js = assets(server, body)
    for piece in ("function snapshotInterval()", "var every = snapshotInterval() * FPS;",
                  "Math.max(1, Math.round(every * f))", "x >= 1000 ? String(Math.round(x / 100) / 10) + 'k'",
                  "b.title = n + ", "function resetSpeed()", "resetSpeed(); labelSpeeds();",
                  "range.max = Math.round(total); range.disabled = false;\n        labelSpeeds();",
                  "o.getAttribute('data-speed') === '1'"):
        assert piece in js, piece
    assert "function stepOf() { return speed < 1 ? 1 : speed; }" in js  # playback itself is unchanged


def test_timelapse_strip_shows_gaps(server, cookies):
    """A pause in the recording (the camera was off) takes its real length on the strip and the slider: a gap .g;
    the hours inside it are marked where they are in time; a tap or a slide into it lands on its nearer edge."""
    js = assets(server, request(server, "GET", "/?tab=all&cam=front", cookie=cookies["bob"])[2])
    for piece in ("var GAP_INTERVALS = 3, GAP_MIN = 60;", "every && gap > limit ? gap / every : 1",
                  "span('g', pAt(i - 1), pAt(i))", "function frameAt(p)", "j = frameAt(x / r.width * total)",
                  "range.value = Math.round(pos[j])", ".tl-strip .g {"):
        assert piece in js, piece


def test_timelapse_strip_shows_gaps_at_the_edges_of_the_day(server, cookies):
    """The camera off from midnight to the first frame (only if an earlier day has frames) and from the last frame to the
    end of the day / to now is a gap too; the reply gives the Pi's now for the end of today."""
    js = assets(server, request(server, "GET", "/?tab=all&cam=front", cookie=cookies["bob"])[2])
    for piece in ("function pAt(k)", "function tAt(k)", "it.d < dstr && it.frames > 0", "Math.min(t0 + 86399, nowT)",
                  "nowT = data.now || 0;", "if (lead && p < pos[0]) return 0;", "if (tail && p > pos[frames.length - 1])",
                  "gapTo = frames.length; gp = pos[cur]; stepGap(ts);", "showGap(gapTo, total); pause();"):
        assert piece in js, piece
    reply = json.loads(request(server, "GET", "/timelapse/front?period=today", cookie=cookies["bob"])[2])
    assert isinstance(reply["now"], int) and abs(reply["now"] - calendar.timegm(time.localtime())) < 60


def test_timelapse_at_home_for_viewers_and_their_cameras(server, cookies):
    """/timelapse is served at home by the snapshot rules: a liveviewer is refused, a viewer sees only the cameras
    switched on for them; the reply is gzip-compressed only when the client asks."""
    url = "/timelapse/front?period=hour"
    assert request(server, "GET", url)[0] == 302  # not signed in
    assert request(server, "GET", url, cookie=cookies["guest"])[0] == 403
    for user in ("bob", "admin"):
        status, headers, body = request(server, "GET", url, cookie=cookies[user])
        assert status == 200 and headers["Content-Type"] == "application/json" and "Content-Encoding" not in headers
        assert json.loads(body)["camera"] == "front"
    assert request(server, "GET", "/timelapse/nope?period=hour", cookie=cookies["bob"])[0] == 404
    for period in ("day", "week", "month", ""):  # the periods that are gone are unknown periods
        assert request(server, "GET", f"/timelapse/front?period={period}", cookie=cookies["bob"])[0] == 400, period
    assert request(server, "GET", "/timelapse/front?period=today", cookie=cookies["bob"])[0] == 200


def test_timelapse_reply_lists_ready_videos_of_the_window(tmp_path, monkeypatch):
    """/timelapse gives `videos` (saved clips only: an .mp4 and no failure mark) of the same window as the frames, oldest first,
    and the clip length: the player links a motion mark to /watch."""
    from datetime import date, datetime
    vids = tmp_path / "front_videos"
    vids.mkdir()
    (tmp_path / "front").mkdir()
    for name, ext in (("20260101_120000_000", ".mp4"), ("20260101_090000_000", ".mp4"), ("20251231_235900_000", ".mp4"),
                      ("20260101_100000_000", ".failed"), ("20260101_110000_000", "")):
        (vids / (name + ".jpg")).write_bytes(b"x")
        if ext:
            (vids / (name + ext)).write_bytes(b"v")
    monkeypatch.setattr(app, "BASE_DIR", str(tmp_path))
    now = datetime(2026, 1, 1, 12, 30)
    assert app.timelapse_videos("front", "today", now=now) == ["20260101_090000_000", "20260101_120000_000"]
    assert app.timelapse_videos("front", "hour", now=now) == ["20260101_120000_000"]
    assert app.timelapse_videos("front", None, day=date(2025, 12, 31)) == ["20251231_235900_000"]
    app._timelapse_cache.clear()
    reply = json.loads(app.timelapse_reply("front", None, day=date(2026, 1, 1)))
    assert reply["videos"] == ["20260101_090000_000", "20260101_120000_000"] and isinstance(reply["clip"], int)
    app._timelapse_cache.clear()


# ---------------------------------------------------------------- the Pi's time zone

def test_time_zone_of_the_pi(tmp_path, monkeypatch):
    """The zone name from the /etc/localtime link (timedatectl changes it), else /etc/timezone; the offset and the
    abbreviation of the running process (the names of the recordings follow it); a zone set after the start is
    pending until a restart; the result is kept for a minute."""
    link, plain = tmp_path / "localtime", tmp_path / "timezone"
    monkeypatch.setattr(app, "TIMEZONE_LINK", str(link))
    monkeypatch.setattr(app, "TIMEZONE_FILE", str(plain))
    assert app.zone_name() is None
    plain.write_text("Europe/Moscow\n")
    assert app.zone_name() == "Europe/Moscow"
    link.symlink_to("/usr/share/zoneinfo/Asia/Tokyo")
    assert app.zone_name() == "Asia/Tokyo"  # the link wins over a stale /etc/timezone

    def local(gmtoff, zone):
        return lambda *a: time.struct_time((2026, 10, 11, 12, 0, 0, 6, 284, 0), {"tm_gmtoff": gmtoff, "tm_zone": zone})

    def tz(gmtoff, zone, start):
        monkeypatch.setattr(app.time, "localtime", local(gmtoff, zone))
        monkeypatch.setattr(app, "TZ_AT_START", start)
        app._tz_cache[:] = [0.0, None]
        return app.pi_timezone()
    assert tz(10800, "MSK", "Asia/Tokyo") == {"name": "Asia/Tokyo", "abbr": "MSK", "offset": "UTC+03:00",
                                               "label": "MSK", "pending": None}
    assert tz(10800, "+03", "Asia/Tokyo")["label"] == "UTC+03:00"  # a zone without letters: the offset
    assert tz(-16200, "-0430", None)["offset"] == "UTC-04:30"
    assert tz(0, "UTC", "Etc/UTC")["offset"] == "UTC+00:00" and app.pi_timezone()["label"] == "UTC"
    assert tz(10800, "MSK", "Europe/Moscow")["pending"] == "Asia/Tokyo"  # timedatectl after the start
    link.unlink()
    assert app.pi_timezone()["pending"] == "Asia/Tokyo"  # from the cache, within a minute
    app._tz_cache[0] -= app.TZ_CACHE_SECONDS
    assert app.pi_timezone()["pending"] is None  # /etc/timezone says Europe/Moscow again
    app._tz_cache[:] = [0.0, None]


def test_time_zone_in_the_status(server, cookies, monkeypatch):
    """Settings → Status: the Time zone row with the zone and its offset; a zone changed after the start is red."""
    zone = {"name": "Europe/Moscow", "abbr": "MSK", "offset": "UTC+03:00", "label": "MSK", "pending": None}
    monkeypatch.setattr(app, "pi_timezone", lambda: zone)
    body = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])[2]
    assert (f'<div class="row"><div class="k">{t("en", "st.timezone")}</div>'
            '<div class="v ">Europe/Moscow (UTC+03:00)</div></div>') in body
    assert "timedatectl" not in body and 'class="foot"' not in body  # no note with commands under the group
    ru = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"] + "; zorcam_lang=ru")[2]
    assert t("ru", "st.timezone") in ru
    zone["pending"] = "Asia/Tokyo"
    body = request(server, "GET", "/settings?tab=status", cookie=cookies["admin"])[2]
    assert ('<span class="sub bad">' + html.escape(t("en", "st.tz_pending", zone="Asia/Tokyo")) + "</span></div>"
            '<div class="v bad">Europe/Moscow (UTC+03:00)</div>') in body


def test_wide_screen_layout(server, cookies):
    """From 1024 px the tab bar is a sidebar (the same links and the same role-based items), the pages have their wide
    CSS; the phone styles stay as they are (the wide ones are only inside the media query)."""
    for user in ROLES_ORDER:
        body = request(server, "GET", "/?tab=live", cookie=cookies[user])[2]
        css = assets(server, body)
        assert "@media (min-width: 1024px)" in css, user
        bar = body[body.index('<nav class="tabbar">'):body.index("</nav>", body.index('<nav class="tabbar">'))]
        assert '<div class="brand"><span>Zorcam.nvr</span>' in bar and f'<span class="nm">{user}<small>' in bar, user
        assert ('data-tab="records"' in bar) == (user != "guest") and 'href="/settings"' in bar, user
        assert "flex-direction:column" in css[css.index("@media (min-width: 1024px)"):]
    for path, cookie in (("/settings", cookies["admin"]), ("/login", None), (f"/watch/front/{VIDEO}", cookies["admin"])):
        page = request(server, "GET", path, cookie=cookie)[2]
        assert "@media (min-width: 1024px)" in assets(server, page), path


def test_wide_settings_are_master_detail(server, cookies):
    """Settings: the tab list (.subtabs) and the tab content (.detail) are two blocks for the wide two-column layout;
    the pages without a back link (setup wizard) get body.plain."""
    body = request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    assert body.index('<nav class="subtabs">') < body.index('<div class="detail">') \
        and "grid-template-columns:240px" in assets(server, body)
    assert '<body class="tabbed">' in body
    assert '<body class="plain">' in app.settings_page("en", "Wizard")  # no Back link, no tab bar
    assert "<body>" in app.settings_page("en", "Nested", back="/settings")


def test_wide_grids_have_day_headers_and_toolbar(server, cookies):
    """Videos: a toolbar with the camera tabs, a sticky header at the start of each day in the grid."""
    body = request(server, "GET", "/?tab=videos", cookie=cookies["bob"])[2]
    videos = body[body.index('data-panel="videos"'):body.index('data-panel="all"')]
    assert '<div class="toolbar"><div class="camtabs-wrap"><div class="horizon-pills camtabs">' in videos
    assert re.findall(r'<h3 class="dayhead">([\d.]+)</h3>', videos) == ["26.09.2026"]
    css = assets(server, body)
    assert ".dayhead {" in css and "position:sticky" in css
    frames = request(server, "GET", "/?tab=all", cookie=cookies["bob"])[2]
    assert '<div class="toolbar">' in frames


def test_timelapse_keyboard_shortcuts(server, cookies):
    """The timelapse player has the keyboard (Space, arrows, Shift, Home/End) and a hint about it in both languages."""
    body = request(server, "GET", "/?tab=all&cam=front", cookie=cookies["bob"])[2]
    js = assets(server, body)
    js = js[js.index("document.addEventListener('keydown'"):]
    for key in ("' '", "'ArrowLeft'", "'ArrowRight'", "e.shiftKey ? 10 : 1", "'Home'", "'End'", "isContentEditable"):
        assert key in js[:1800], key
    assert '<p class="tl-keys">' + t("en", "tl.keys") in body
    ru = request(server, "GET", "/?tab=all&cam=front", cookie=cookies["bob"] + "; zorcam_lang=ru")[2]
    assert t("ru", "tl.keys") in ru and t("ru", "tl.keys") != t("en", "tl.keys")


@pytest.fixture
def ptz_cam(server, ptz_camera, restore_cameras):
    """A camera "turner" with PTZ on a fake ONVIF camera (admin / secret); bob (viewer) sees it, guest (liveviewer)
    sees it too, "yard" is closed to bob."""
    xaddr, seen = ptz_camera
    zorcam_config.save_cameras(CAMS + [{"id": "turner", "name": "Turner", "rtsp_url": "rtsp://127.0.0.1:1/t",
                                         "rtsp_user": "admin", "onvif_url": xaddr}])
    zorcam_config.set_camera_password("turner", "secret")
    zorcam_users.set_cameras("bob", {"front", "turner"})
    zorcam_users.set_cameras("guest", {"front", "turner"})
    app.ptz_forget("turner")
    app.PTZ_LAST.clear()
    yield seen
    zorcam_users.set_cameras("bob", {"front", "yard"})
    zorcam_users.set_cameras("guest", {"front", "yard"})
    app.ptz_forget("turner")


def ptz_post(server, cookie, cam, body):
    csrf = re.search(r'data-cam="turner" data-csrf="([0-9a-f]+)"', request(server, "GET", "/?tab=live", cookie=cookie)[2])
    return request(server, "POST", f"/ptz/{cam}", json.dumps(dict({"csrf": csrf.group(1) if csrf else "x"}, **body)),
                   cookie=cookie, headers={"Content-Type": "application/json"})


def test_ptz_buttons_only_for_viewers(server, cookies, ptz_cam):
    page = request(server, "GET", "/?tab=live", cookie=cookies["bob"])[2]
    # only "turner" has an ONVIF address: "front" gets no control block at all; hidden until /ptz says yes
    assert page.count('class="ptz" role="group"') == 1 and page.count('data-dir="left"') == 1
    assert 'class="ptz" role="group" aria-label="' + t("en", "ptz.group") + '" data-cam="turner"' in page
    assert t("en", "ptz.left") in page and t("en", "js.ptz_failed") in page
    assert page.count('class="ptz-toggle" aria-expanded="false">') == 1 and page.count('class="ptz-pad" hidden') == 1  # folded
    # Live of one camera: the buttons are open at once (the toggle still folds them)
    one = live_panel(request(server, "GET", "/?tab=live&cam=turner", cookie=cookies["bob"])[2])
    assert 'class="ptz-toggle" aria-expanded="true">' in one and 'class="ptz-pad">' in one and "ptz-pad\" hidden" not in one
    assert 'class="ptz"' not in live_panel(request(server, "GET", "/?tab=live&cam=front", cookie=cookies["bob"])[2])
    assert 'data-dir="left"' not in request(server, "GET", "/?tab=live", cookie=cookies["guest"])[2]  # liveviewer
    assert 'class="ptz"' not in request(server, "GET", "/?tab=live&cam=turner&embed=1", cookie=cookies["bob"])[2]
    assert json.loads(request(server, "GET", "/ptz/turner", cookie=cookies["bob"])[2]) == {"ptz": True, "pan": True, "zoom": True}
    assert json.loads(request(server, "GET", "/ptz/front", cookie=cookies["bob"])[2]) == \
        {"ptz": False, "reason": "no_onvif_url"}
    assert request(server, "GET", "/ptz/turner", cookie=cookies["guest"])[0] == 403
    assert request(server, "GET", "/ptz/yard", cookie=cookies["bob"])[0] == 404  # not enabled for bob
    assert request(server, "GET", "/ptz/nope", cookie=cookies["admin"])[0] == 404


def test_ptz_commands(server, cookies, ptz_cam):
    seen = ptz_cam
    assert ptz_post(server, cookies["bob"], "turner", {"action": "move", "dir": "left"})[0] == 200
    assert 'x="-0.50" y="0.00"' in seen[-1][1] and "PT2S" in seen[-1][1]  # a camera-side Timeout
    assert ptz_post(server, cookies["bob"], "turner", {"action": "move", "dir": "up"})[0] == 429  # too fast
    assert ptz_post(server, cookies["bob"], "turner", {"action": "stop"})[0] == 200
    assert "<tptz:Stop>" in seen[-1][1]
    app.PTZ_LAST.clear()
    assert ptz_post(server, cookies["admin"], "turner", {"action": "move", "dir": "in"})[0] == 200
    assert '<tt:Zoom x="0.50"/>' in seen[-1][1]
    n = len(seen)
    assert ptz_post(server, cookies["admin"], "turner", {"action": "move", "dir": "sideways"})[0] == 400
    assert ptz_post(server, cookies["admin"], "turner", {"action": "jump"})[0] == 400
    assert ptz_post(server, cookies["guest"], "turner", {"action": "stop"})[0] == 403  # liveviewer
    assert ptz_post(server, cookies["bob"], "yard", {"action": "stop"})[0] == 404  # closed to bob
    assert ptz_post(server, cookies["bob"], "nope", {"action": "stop"})[0] == 404
    assert request(server, "POST", "/ptz/turner", json.dumps({"csrf": "bad", "action": "stop"}), cookie=cookies["bob"])[0] == 403
    assert request(server, "POST", "/ptz/turner", json.dumps({"action": "stop"}), cookie=cookies["bob"])[0] == 403
    assert request(server, "POST", "/ptz/turner", json.dumps({"action": "stop"}))[0] == 302  # signed out
    assert len(seen) == n  # nothing of this reached the camera


def test_ptz_only_what_the_camera_can_do(server, cookies, ptz_cam, monkeypatch):
    """A camera with the zoom only: /ptz says so (the page then hides the arrows), an arrow command never reaches it."""
    seen = ptz_cam
    monkeypatch.setitem(PTZ_SPACES, "spaces", ("ContinuousZoomVelocitySpace",))
    app.ptz_forget("turner")
    assert json.loads(request(server, "GET", "/ptz/turner", cookie=cookies["bob"])[2]) == \
        {"ptz": True, "pan": False, "zoom": True}
    n = len(seen)
    status, _, body = ptz_post(server, cookies["bob"], "turner", {"action": "move", "dir": "left"})
    assert status == 409 and json.loads(body)["error"] == "unsupported" and len(seen) == n
    assert ptz_post(server, cookies["bob"], "turner", {"action": "move", "dir": "in"})[0] == 200
    assert '<tt:Zoom x="0.50"/>' in seen[-1][1]
    assert "classList.toggle('zoom-only'" in assets(server, request(server, "GET", "/?tab=live", cookie=cookies["bob"])[2])
    app.ptz_forget("turner")


def test_ptz_camera_without_ptz_and_with_a_wrong_password(server, cookies, ptz_cam):
    page = request(server, "GET", "/?tab=live", cookie=cookies["admin"])[2]
    csrf = re.search(r'data-cam="turner" data-csrf="([0-9a-f]+)"', page).group(1)
    status, _, body = request(server, "POST", "/ptz/front", json.dumps({"csrf": csrf, "action": "stop"}),
                              cookie=cookies["admin"])
    assert status == 409 and json.loads(body)["error"] == "no_onvif_url"
    zorcam_config.set_camera_password("turner", "wrong")
    app.ptz_forget("turner")
    # no buttons, no error page
    assert json.loads(request(server, "GET", "/ptz/turner", cookie=cookies["bob"])[2]) == {"ptz": False}


def test_ptz_without_onvif_url_never_reaches_the_camera(server, cookies, ptz_cam):
    seen = ptz_cam
    n = len(seen)
    # the page has no control block for such a camera, so the CSRF token is taken while it still has the address
    csrf = re.search(r'data-cam="turner" data-csrf="([0-9a-f]+)"',
                     request(server, "GET", "/?tab=live", cookie=cookies["bob"])[2]).group(1)
    cams = zorcam_config.load_cameras()
    zorcam_config.save_cameras([dict(c, onvif_url="") if c["id"] == "turner" else c for c in cams])
    app.ptz_forget("turner")
    assert json.loads(request(server, "GET", "/ptz/turner", cookie=cookies["bob"])[2]) == \
        {"ptz": False, "reason": "no_onvif_url"}
    assert 'data-cam="turner" data-csrf' not in request(server, "GET", "/?tab=live", cookie=cookies["bob"])[2]
    status = request(server, "POST", "/ptz/turner", json.dumps({"csrf": csrf, "action": "move", "dir": "left"}),
                     cookie=cookies["bob"], headers={"Content-Type": "application/json"})[0]
    assert status == 409 and len(seen) == n


# ---------------------------------------------------------------- markup in files: static/, templates/, the escaping helper

def test_css_and_js_are_public_typed_and_cached_for_good(server):
    """/static/<file>.css|js: no sign-in (the login page and the wizard need them), the right type, a year in the cache
    ("immutable": the address carries the hash of the content), the bytes of the file; anything else is 404."""
    assert {"app.css", "login.css", "watch.css", "settings.css", "main.css", "app.js", "watch.js", "settings.js",
            "main.js", "timelapse.js", "hls.min.js"} <= set(app.ASSETS)
    for name, (body, _ctype, version) in app.ASSETS.items():
        status, headers, text = request(server, "GET", f"/static/{name}?v={version}")  # no cookie
        assert status == 200, name
        assert headers["Content-Type"] == ("text/css; charset=utf-8" if name.endswith(".css")
                                           else "application/javascript; charset=utf-8"), name
        assert headers["Cache-Control"] == "public, max-age=31536000, immutable", name
        assert text.encode() == body and int(headers["Content-Length"]) == len(body), name
        if name != app.HLS_JS_FILE:  # (a minified third-party file has "[[" in its arrays)
            assert "[[" not in text and "{{" not in text, name  # no translation marks or doubled braces left in a plain file
    for path in ("/static/nope.css", "/static/", "/static/../app.py", "/static/%2e%2e/app.py", "/static/app.css/x",
                 "/static/icon-192.png"):  # the icons stay at their own addresses
        assert request(server, "GET", path)[0] == 404, path


def test_records_open_on_the_timelapse(server, cookies):
    """Records opens on Timelapse (the owner's decision of 13.10.2026): /?tab=records and the tab bar link lead there;
    the speed buttons have a speedometer in front of them."""
    body = request(server, "GET", "/?tab=records", cookie=cookies["admin"])[2]
    assert re.search(r'data-panel="all"(?![^>]*hidden)', body) and re.search(r'data-panel="videos"[^>]*hidden', body)
    body = request(server, "GET", "/settings", cookie=cookies["admin"])[2]
    assert 'href="/?tab=all"' in body
    body = request(server, "GET", "/?tab=all", cookie=cookies["admin"])[2]
    assert re.search(r'class="seg tl-speed"[^>]*><span class="tl-speed-icon"', body)


def test_app_icons_are_linked_by_the_hash_of_their_content(server):
    """Safari keeps a Home Screen icon by its address: a new icon must come at a new one, /icon-192.png?v=<hash>."""
    import hashlib
    page = request(server, "GET", "/login")[2]
    manifest = json.loads(request(server, "GET", "/manifest.webmanifest")[2])
    links = {"/icon-192.png": manifest["icons"][0]["src"], "/icon-512.png": manifest["icons"][1]["src"]}
    for path, url in links.items():
        with open(os.path.join(app.STATIC_DIR, app.STATIC_FILES[path]), "rb") as f:
            assert url == f"{path}?v={hashlib.sha256(f.read()).hexdigest()[:12]}", path
        status, headers, _ = request(server, "GET", url)
        assert status == 200 and headers["Content-Type"] == "image/png", url
    assert 'rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials"' in page


def test_home_screen_icon_is_inside_the_page(server):
    """Behind a router proxy that asks for its own login iPhone fetched the icon without it and drew a letter:
    the icon is a data: URL, nothing to fetch."""
    import base64
    page = request(server, "GET", "/login")[2]
    href = re.search(r'rel="apple-touch-icon" sizes="180x180" href="([^"]+)"', page).group(1)
    with open(os.path.join(app.STATIC_DIR, "apple-touch-icon.png"), "rb") as f:
        assert href == "data:image/png;base64," + base64.b64encode(f.read()).decode()


def test_pages_link_files_by_the_hash_of_their_content(server, cookies):
    """The address of every CSS and JS file in a page ends in ?v=<hash of the file>, so that a browser keeps the file
    for good and takes a new one after an update; what a page links exists."""
    import hashlib
    pages = {"login": request(server, "GET", "/login")[2],
             "main": request(server, "GET", "/?tab=live", cookie=cookies["admin"])[2],
             "settings": request(server, "GET", "/settings", cookie=cookies["admin"])[2],
             "watch": request(server, "GET", f"/watch/front/{VIDEO}", cookie=cookies["admin"])[2]}
    for name, body in pages.items():
        links = re.findall(r'(?:href|src)="/static/([\w.]+)\?v=(\w+)"', body)
        assert links and "<style" not in body, name
        for file, version in links:
            data = app.ASSETS[file][0]
            assert version == hashlib.sha256(data).hexdigest()[:12] == app.ASSETS[file][2], (name, file)
    assert [f for f, _ in re.findall(r'(?:href|src)="/static/([\w.]+)\?v=(\w+)"', pages["main"])] == [
        "app.css", "main.css", "app.js", "timelapse.js", "main.js"]
    # no JavaScript in the page itself except the texts and the data for the files: window.T and window.PAGE
    for name, body in pages.items():
        inline = re.findall(r"<script>(.*?)</script>", body, re.S)
        assert all(code.startswith("window.T = ") for code in inline), name


@pytest.mark.parametrize("lang,headers", [("en", {}), ("ru", RU)])
def test_window_t_has_the_texts_of_the_scripts_in_the_page_language(server, cookies, lang, headers):
    """The JS gets its texts from window.T (the keys it names as T['key']), in the language of the page."""
    for path, files in (("/?tab=live", app.PAGE_SCRIPTS), ("/settings", app.SETTINGS_SCRIPTS)):
        body = request(server, "GET", path, cookie=cookies["admin"], headers=headers)[2]
        texts = json.loads(re.search(r"window\.T = (.*);", body).group(1))
        wanted = {k for f in files for k in re.findall(r"""\bT\[['"]([\w.]+)['"]\]""", app.ASSETS[f][0].decode())}
        assert wanted and set(texts) == wanted, path
        assert all(texts[k] == t(lang, k) and "[[" not in texts[k] for k in wanted), path
    assert t("ru", "js.connecting") != t("en", "js.connecting")
    for key in re.findall(r"""\bT\[['"]([\w.]+)['"]\]""", "".join(b.decode() for b, _, _ in app.ASSETS.values())):
        assert key in app.i18n.STRINGS, key  # a text the script asks for exists, in both languages (test_i18n)


def test_templates_are_plain_files_with_dollar_names(server, cookies):
    """The page frames are files: $name for the values (no doubled braces), [[key]] for the texts; every page renders
    in both languages without a mark or a $name left in it."""
    for name, text in app.TEMPLATES.items():
        assert "{{" not in text and "}}" not in text and "[[js:" not in text, name
    for headers in ({}, RU):
        for path in ("/login", "/", "/settings", "/config", f"/watch/front/{VIDEO}", "/?tab=live&cam=front&embed=1"):
            body = request(server, "GET", path, cookie=None if path == "/login" else cookies["admin"], headers=headers)[2]
            assert "[[" not in body and not re.search(r"\$[a-z_]+|&lt;(div|span|section|a|button|input|h2)\b", body), path


EVIL = '<b>x</b> "q" \'a\' &amp;'


def test_camera_name_is_escaped_everywhere(server, cookies, restore_cameras):
    """A camera named with markup is text on every page that shows the name (Live, Videos, Timelapse, the embedded
    player, a clip, Settings, the camera form), in the page and in its JSON, never markup."""
    cams = copy.deepcopy(CAMS)
    cams[0]["name"] = EVIL
    zorcam_config.save_cameras(cams)
    escaped = html.escape(EVIL)
    for path in ("/?tab=live", "/?tab=videos&cam=front", "/?tab=all&cam=front", "/?tab=live&cam=front&embed=1",
                 f"/watch/front/{VIDEO}", "/settings?tab=cameras", "/settings?tab=notify", "/settings?tab=users",
                 "/camera/front"):
        for user in ("admin", "bob"):
            if user == "bob" and path.startswith(("/settings?tab=c", "/settings?tab=u", "/camera")):
                continue
            body = request(server, "GET", path, cookie=cookies[user])[2]
            assert "<b>x</b>" not in body and "&lt;b&gt;x" in body, (path, user)
            assert escaped in body or path.startswith("/camera/"), (path, user)  # the heading has it as "Edit: <name>"
    # the name is also an attribute value: the quote cannot end it
    body = request(server, "GET", "/settings?tab=cameras", cookie=cookies["admin"])[2]
    assert f'aria-label="{html.escape(t("en", "cam.label.enabled") + ": " + EVIL)}"' in body


def test_users_and_files_are_escaped_in_pages(server, cookies):
    """The name of a user (tab bar, Users tab) and the file names of a clip are text in the pages."""
    zorcam_users.set_user('e<i>v"il', "viewer", "x" * 8)
    try:
        body = request(server, "GET", "/settings?tab=users", cookie=cookies["admin"])[2]
        assert "<i>v" not in body and html.escape('e<i>v"il') in body
    finally:
        zorcam_users.delete_user('e<i>v"il')


def test_records_switch_puts_timelapse_first(server, cookies):
    body = request(server, "GET", "/?tab=records", cookie=cookies["bob"])[2]
    seg = body[body.index('class="seg recseg"'):]
    assert seg.index('data-go="all"') < seg.index('data-go="videos"')
    assert 'data-panel="all"' in body and 'data-panel="videos"' in body


# ---------------------------------------------------------------- sign out on the other devices

def sessions_post(port, cookie, path, fields=None, headers=None, csrf=True):
    data = dict(fields or {})
    if csrf:
        data["csrf"] = csrf_of_session(cookie)
    return request(port, "POST", path, urlencode(data), cookie=cookie, headers=headers)


def test_sign_out_on_other_devices_keeps_this_one(server, cookies):
    """Every role: the row is on the Users tab; the POST ends the other sessions of this user only."""
    for user in ROLES_ORDER:
        page = request(server, "GET", "/settings?tab=users", cookie=cookies[user])[2]
        assert 'action="/sessions/others"' in page and t("en", "users.signout_others") in page, user
        assert page.index('href="/logout"') < page.index('action="/sessions/others"'), user
        mine = [login(server, user), login(server, user)]  # two more devices of the same user
        other = login(server, "guest" if user != "guest" else "bob")
        here = login(server, user)
        status, headers, _ = sessions_post(server, here, "/sessions/others")
        assert status == 303 and headers["Location"] == "/settings?tab=users&signed_out=1", user
        assert request(server, "GET", "/settings", cookie=here)[0] == 200, user  # this one stays
        assert [request(server, "GET", "/settings", cookie=c)[0] for c in mine] == [302, 302], user
        assert request(server, "GET", "/settings", cookie=other)[0] == 200  # another user is not touched
        assert request(server, "GET", "/settings", cookie=cookies[user])[0] == 302  # the fixture's own session was one of them
        cookies[user] = login(server, user)  # the shared fixture signs in again
        page = request(server, "GET", headers["Location"], cookie=here)[2]
        assert t("en", "users.signout_others_done") in page, user


def test_sign_out_on_other_devices_checks_the_token_and_the_sign_in(server, cookies):
    bob = cookies["bob"]
    other = login(server, "bob")
    assert request(server, "POST", "/sessions/others", "csrf=x")[0] == 302  # not signed in
    for fields in ({"csrf": "x"}, {"csrf": "\u0436" * 4}, {"csrf": csrf_of_session(cookies["admin"])}):
        assert request(server, "POST", "/sessions/others", urlencode(fields), cookie=bob)[0] == 403, fields
    assert request(server, "POST", "/sessions/others", "", cookie=bob)[0] == 400
    assert request(server, "POST", "/sessions/others", urlencode({"csrf": csrf_of_session(bob)}), cookie=bob,
                   headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert request(server, "GET", "/settings", cookie=other)[0] == 200  # nothing ended
    request(server, "GET", "/logout", cookie=other)


def test_admin_ends_all_sessions_of_a_user(server, cookies):
    PASSWORDS["dina"] = "dina-pass"
    zorcam_users.set_user("dina", "viewer", PASSWORDS["dina"])
    try:
        page = request(server, "GET", "/settings?tab=users", cookie=cookies["admin"])[2]
        assert page.count('action="/users/sessions"') == len(zorcam_users.load()) - 1  # all but admin (self)
        assert ('name="user" value="dina"' in page and t("en", "users.end_sessions") in page)
        section = page[page.index('<h2>dina</h2>'):]
        assert section.index('action="/users/sessions"') < section.index('action="/users/delete"')
        # a plain action row, not red (red is for deletion only): outside .rec, no danger class
        assert f">{t('en', 'users.end_sessions')}</button>" in section and "danger" not in section
        assert section.index('class="actions"><form') < section.index('action="/users/sessions"') \
            < section.index('class="rec"') < section.index('action="/users/delete"')
        admin_section = page[page.index(t("en", "setup.user_title.admin")):page.index('<h2>bob</h2>')]
        assert "/users/sessions" not in admin_section
        d1, d2 = login(server, "dina"), login(server, "dina")
        app._cloud_sessions["dina"] = d2  # the cloud session of the user is forgotten too
        # only admin; the token; not itself; a known user
        for who in ("bob", "guest"):
            assert sessions_post(server, cookies[who], "/users/sessions", {"user": "dina"})[0] == 403, who
        assert sessions_post(server, cookies["admin"], "/users/sessions", {"user": "dina"}, csrf=False)[0] == 403
        assert request(server, "POST", "/users/sessions", urlencode({"csrf": "\u0436", "user": "dina"}),
                       cookie=cookies["admin"])[0] == 403
        assert request(server, "POST", "/users/sessions", urlencode({"user": "dina"}), cookie=None)[0] == 302
        for bad in ("admin", "nobody", ""):
            assert sessions_post(server, cookies["admin"], "/users/sessions", {"user": bad})[0] == 400, bad
        assert request(server, "GET", "/settings", cookie=d1)[0] == 200
        status, headers, _ = sessions_post(server, cookies["admin"], "/users/sessions", {"user": "dina"})
        assert status == 303 and headers["Location"] == "/settings?tab=users&ended=dina"
        assert [request(server, "GET", "/settings", cookie=c)[0] for c in (d1, d2)] == [302, 302]
        assert "dina" not in app._cloud_sessions
        assert request(server, "GET", "/settings", cookie=cookies["admin"])[0] == 200  # the admin's own is alive
        assert t("en", "users.sessions_ended", name="dina") in request(server, "GET", headers["Location"],
                                                                      cookie=cookies["admin"])[2]
        assert t("en", "users.sessions_ended", name="nobody") not in request(
            server, "GET", "/settings?tab=users&ended=nobody", cookie=cookies["admin"])[2]
        # the user signs in again with the password
        assert login(server, "dina")
    finally:
        zorcam_users.delete_user("dina")
        del PASSWORDS["dina"]
        app._cloud_sessions.pop("dina", None)


def test_ending_sessions_keeps_the_shared_cloud_session_it_was_pressed_in():
    """end_sessions(user, keep=token): the cloud session of the user (one for all his cloud clients) stays only if it is
    the kept one; any other call forgets it, the next request through the cloud makes a new one."""
    zorcam_users.set_user("erin", "viewer", "erin-pass")
    try:
        home, cloud = app.create_session("erin"), app.create_session("erin")
        app._cloud_sessions["erin"] = cloud
        assert app.end_sessions("erin", keep=cloud) == 1  # the home one
        assert app.check_session(cloud)[0] == "erin" and app.check_session(home)[0] is None
        assert app._cloud_sessions["erin"] == cloud
        home = app.create_session("erin")
        assert app.end_sessions("erin", keep=home) == 1  # pressed at home: the cloud one goes
        assert app.check_session(home)[0] == "erin" and app.check_session(cloud)[0] is None
        assert "erin" not in app._cloud_sessions
        assert app.end_sessions("erin") == 1 and app.check_session(home)[0] is None
    finally:
        zorcam_users.delete_user("erin")
        app._cloud_sessions.pop("erin", None)


# ---------------------------------------------------------------- sign-in limits

@pytest.fixture
def fresh_limits(monkeypatch):
    monkeypatch.setattr(app, "FAILED_ATTEMPTS", {})
    monkeypatch.setattr(app, "USER_FAILS", {})
    yield


def sign_in(port, name, password, headers=None):
    return request(port, "POST", "/login", urlencode({"username": name, "password": password}), headers=headers)


def expire_lock(name):
    app.USER_FAILS[app.login_key(name)][1] = time.time() - 1


def test_a_name_is_closed_for_longer_after_every_failure(server, fresh_limits):
    """5 failures in a row close the name for 1 minute, then 2, 4, 8 and at most 15; the answer is the same page with 429;
    the right password does not open it meanwhile; the address is not blamed (others sign in)."""
    for _ in range(app.USER_LOCK_AFTER - 1):
        assert sign_in(server, "bob", "wrong")[0] == 401
    assert app.user_locked_out("bob") == 0
    status, _, body = sign_in(server, "bob", "wrong")
    assert status == 401 and 55 < app.user_locked_out("bob") <= 61  # the fifth: closed for a minute
    for password in ("wrong", PASSWORDS["bob"]):
        status, _, body = sign_in(server, "bob", password)
        assert status == 429 and t("en", "login.locked") in body
    assert app.FAILED_ATTEMPTS["127.0.0.1"] and len(app.FAILED_ATTEMPTS["127.0.0.1"]) == 5  # refused tries are not counted
    assert sign_in(server, "guest", PASSWORDS["guest"])[0] == 302  # another name from the same address
    pauses = []
    for _ in range(6):
        expire_lock("bob")
        assert sign_in(server, "bob", "wrong")[0] == 401  # not 429: the pause is over
        pauses.append(app.user_locked_out("bob"))
    assert [round(p, -1) for p in pauses] == [120, 240, 480, 900, 900, 900]
    expire_lock("bob")
    assert sign_in(server, "bob", PASSWORDS["bob"])[0] == 302  # the right password after the pause
    assert app.user_locked_out("bob") == 0 and app.login_key("bob") not in app.USER_FAILS  # the count is reset
    for _ in range(app.USER_LOCK_AFTER - 1):
        sign_in(server, "bob", "wrong")
    assert sign_in(server, "bob", PASSWORDS["bob"])[0] == 302  # four in a row do not close it, and a success resets
    assert app.login_key("bob") not in app.USER_FAILS


def test_the_journal_names_only_a_real_user_in_the_pause_line(server, fresh_limits, monkeypatch):
    """Somebody may type the password into the name field: an unknown name is never written to the journal."""
    logged = []
    monkeypatch.setattr(app, "log", logged.append)
    for name in ("BOB", "my-secret-pass"):
        for _ in range(app.USER_LOCK_AFTER):
            sign_in(server, name, "x")
    lines = [m for m in logged if "is closed for" in m]
    assert len(lines) == 2
    assert lines[0].startswith('login: sign-in as "bob" is closed for 60 s')  # as saved, not as typed
    assert lines[1].startswith("login: sign-in as an unknown name is closed for 60 s")
    assert not any("my-secret-pass" in m for m in logged)


def test_the_pause_does_not_tell_whether_the_name_exists(server, fresh_limits):
    """A name nobody has is counted and closed exactly like a real one, in any case of letters."""
    for name in ("Nobody", "nobody", " NOBODY ", "noBody", "nobody"):
        assert sign_in(server, name, "x")[0] == 401
    ghost = sign_in(server, "NoBody", "x")
    assert ghost[0] == 429 and app.user_locked_out("nobody")
    for _ in range(5):
        sign_in(server, "BOB", "wrong")
    real = sign_in(server, "bob", "wrong")
    assert real[0] == 429 and real[2] == ghost[2]  # the very same page
    assert len(app.USER_FAILS) == 2


def test_an_address_is_limited_more_mildly_over_many_names(server, fresh_limits):
    """The address limit stays (against guessing over many names), milder: MAX_ATTEMPTS failures in ATTEMPT_WINDOW."""
    assert app.MAX_ATTEMPTS == 30 and app.ATTEMPT_WINDOW == 300
    for i in range(app.MAX_ATTEMPTS):
        assert sign_in(server, f"user{i}", "x")[0] == 401
    status, _, body = sign_in(server, "another", "x")
    assert status == 429 and t("en", "login.locked") in body
    assert sign_in(server, "guest", PASSWORDS["guest"])[0] == 429  # the address, whatever the name
    app.FAILED_ATTEMPTS.clear()
    assert sign_in(server, "guest", PASSWORDS["guest"])[0] == 302


def test_name_failures_are_few_and_forgotten(monkeypatch):
    monkeypatch.setattr(app, "USER_FAILS", {})
    monkeypatch.setattr(app, "USER_FAILS_MAX", 50)
    app.register_user_failed("victim")
    for _ in range(app.USER_LOCK_AFTER):
        app.register_user_failed("victim")
    assert app.user_locked_out("victim")
    for i in range(200):  # a guesser invents names
        app.register_user_failed(f"n{i}" + "x" * 100)
    assert len(app.USER_FAILS) <= 50
    assert all(len(k) <= 64 for k in app.USER_FAILS)
    # idle long enough after the pause: forgotten, counting starts over
    app.USER_FAILS.clear()
    for _ in range(app.USER_LOCK_AFTER):
        app.register_user_failed("late")
    entry = app.USER_FAILS["late"]
    entry[1] = entry[2] = time.time() - app.USER_FAILS_IDLE - 1
    assert app.register_user_failed("late") == 0 and app.USER_FAILS["late"][0] == 1


def test_trusted_proxies_give_the_client_address():
    """A connection from an address of [gallery] trusted_proxies: the client is the last X-Forwarded-For entry (the one the
    proxy appended); from anyone else the header means nothing. Through the tunnel it is X-Zorcam-Client-IP."""
    mine = {"127.0.0.1", "::1"}

    def ip_of(peer, forwarded=None, tunnel=False, client=None, proxies=mine):
        headers = {}
        if forwarded is not None:
            headers["X-Forwarded-For"] = forwarded
        if client is not None:
            headers["X-Zorcam-Client-IP"] = client
        handler = types.SimpleNamespace(client_address=(peer, 1), headers=headers, on_tunnel=tunnel)
        handler.tunnel_client_ip = lambda: app.Handler.tunnel_client_ip(handler)
        saved = app.TRUSTED_PROXIES
        app.TRUSTED_PROXIES = proxies
        try:
            return app.Handler.client_ip(handler)
        finally:
            app.TRUSTED_PROXIES = saved
    assert ip_of("127.0.0.1", "203.0.113.7") == "203.0.113.7"
    assert ip_of("127.0.0.1", "10.9.9.9, 203.0.113.7") == "203.0.113.7"  # the first ones can be written by the client
    assert ip_of("127.0.0.1", " 2001:DB8::1 ") == "2001:db8::1"
    assert ip_of("127.0.0.1") == "127.0.0.1" and ip_of("127.0.0.1", "") == "127.0.0.1"
    assert ip_of("127.0.0.1", "not-an-ip") == "127.0.0.1" and ip_of("127.0.0.1", "1.2.3.4:80") == "127.0.0.1"
    assert ip_of("192.168.1.9", "203.0.113.7") == "192.168.1.9"  # not a proxy of ours
    assert ip_of("127.0.0.1", "203.0.113.7", proxies=set()) == "127.0.0.1"  # the default: nobody is trusted
    assert ip_of("127.0.0.1", "198.51.100.1", tunnel=True, client="203.0.113.9") == "203.0.113.9"
    assert ip_of("127.0.0.1", "198.51.100.1", tunnel=True) == "127.0.0.1"  # the tunnel does not take X-Forwarded-For
    assert app.parse_proxies("127.0.0.1, ::1 ,bad, 10.0.0.1,") == {"127.0.0.1", "::1", "10.0.0.1"}
    assert app.parse_proxies("") == set() == app.parse_proxies(None)


def test_sign_in_counts_the_client_behind_a_trusted_proxy(server, fresh_limits, monkeypatch):
    """With 127.0.0.1 trusted, failures are counted per real client (X-Forwarded-For) and not for the proxy at once."""
    monkeypatch.setattr(app, "TRUSTED_PROXIES", {"127.0.0.1"})
    monkeypatch.setattr(app, "MAX_ATTEMPTS", 3)
    for i in range(3):
        assert sign_in(server, f"x{i}", "x", {"X-Forwarded-For": "203.0.113.1"})[0] == 401
    assert sign_in(server, "guest", PASSWORDS["guest"], {"X-Forwarded-For": "203.0.113.1"})[0] == 429
    assert sign_in(server, "guest", PASSWORDS["guest"], {"X-Forwarded-For": "198.51.100.2, 203.0.113.9"})[0] == 302
    assert sorted(app.FAILED_ATTEMPTS) == ["203.0.113.1"]
    monkeypatch.setattr(app, "TRUSTED_PROXIES", set())  # not trusted: the header is ignored, the address is the proxy's
    app.FAILED_ATTEMPTS.clear()
    for i in range(3):
        sign_in(server, f"y{i}", "x", {"X-Forwarded-For": f"203.0.113.{i}"})
    assert list(app.FAILED_ATTEMPTS) == ["127.0.0.1"]


# ---------------------------------------------------------------- the password set by the installer

@pytest.fixture
def installer_password(server):
    """admin has the password the installer set (zorcam_users.py set admin admin --initial): the mark is on."""
    users = zorcam_users.load()
    users["admin"]["initial"] = True
    zorcam_users.save(users)
    yield
    zorcam_users.set_user("admin", "admin", PASSWORDS["admin"])  # the password of the other tests; clears the mark too


def post_password(port, cookie, path, **fields):
    csrf = csrf_of_session(cookie)
    return request(port, "POST", path, urlencode({"csrf": csrf, **fields}), cookie=cookie)


def test_installer_password_leads_admin_to_the_change_password_page(server, cookies, installer_password):
    """The main page of admin (after signing in with the form: the same address) is the "Change the password" page while
    the installer's password stands; nobody else is asked, no other page is closed, the cloud's embedded Live stays."""
    for path in ("/", "/?tab=live", "/?tab=records", "/?tab=videos&cam=front", "/?tab=live&cam=front"):
        status, headers, _ = request(server, "GET", path, cookie=cookies["admin"])
        assert status == 303 and headers["Location"] == "/setup/password", path
    status, headers, _ = request(server, "GET", app.LIVE_URL, cookie=login(server, "admin"))  # where the form sends admin
    assert status == 303 and headers["Location"] == "/setup/password"
    assert request(server, "GET", "/?tab=live&cam=front&embed=1", cookie=cookies["admin"])[0] == 200
    for path in ("/settings", "/config", "/setup/cameras", "/setup/password"):
        assert request(server, "GET", path, cookie=cookies["admin"])[0] == 200, path
    for user in ("bob", "guest"):
        assert request(server, "GET", "/", cookie=cookies[user])[0] == 200, user
    page = request(server, "GET", "/setup/password", cookie=cookies["admin"])[2]
    assert t("en", "pw.title") in page and t("en", "pw.intro") in page and 'class="back"' not in page  # a page like the wizard's
    assert csrf_of(page, "/setup/password") == csrf_of(page, "/setup/password/skip") == csrf_of_session(cookies["admin"])
    assert 'name="password"' in page and 'name="repeat"' in page and '<div class="v">admin</div>' in page
    assert f'<button type="submit">{t("en", "cfgpage.save")}</button>' in page and 'class="skip"' in page
    assert f'<button type="submit">{t("en", "pw.skip")}</button>' in page
    ru = request(server, "GET", "/setup/password", cookie=cookies["admin"], headers=RU)[2]
    assert t("ru", "pw.title") in ru and t("ru", "pw.skip") in ru and t("ru", "cfgpage.save") in ru


def test_the_mark_is_off_without_the_installer(server, cookies):
    assert not zorcam_users.is_initial("admin")
    assert request(server, "GET", "/", cookie=cookies["admin"])[0] == 200  # the page is still there, nothing leads to it
    assert request(server, "GET", "/setup/password", cookie=cookies["admin"])[0] == 200


def test_installer_password_is_replaced_with_save(server, cookies, installer_password):
    here, other = cookies["admin"], login(server, "admin")
    bad = [("newsecret", "other", "setup.mismatch"), ("short", "short", "setup.short"), ("", "", "setup.short")]
    for p1, p2, msg in bad:
        status, _, body = post_password(server, here, "/setup/password", password=p1, repeat=p2)
        assert status == 400 and t("en", msg) in body and "newsecret" not in body and 'name="csrf"' in body
        assert zorcam_users.is_initial("admin") and zorcam_users.verify("admin", PASSWORDS["admin"]) == "admin"
    status, headers, _ = post_password(server, here, "/setup/password", password="newsecret", repeat="newsecret")
    assert status == 303 and headers["Location"] == "/"  # the cameras are there already
    assert not zorcam_users.is_initial("admin")
    assert zorcam_users.verify("admin", "newsecret") == "admin" and zorcam_users.verify("admin", PASSWORDS["admin"]) is None
    assert request(server, "GET", "/", cookie=here)[0] == 200  # this session stays, and the main page opens
    assert request(server, "GET", "/settings", cookie=other)[0] == 302  # the others are ended, as after any new password


def test_installer_password_can_be_kept_with_skip(server, cookies, installer_password):
    status, headers, _ = post_password(server, cookies["admin"], "/setup/password/skip")
    assert status == 303 and headers["Location"] == "/"
    assert not zorcam_users.is_initial("admin") and zorcam_users.verify("admin", PASSWORDS["admin"]) == "admin"
    assert request(server, "GET", "/", cookie=cookies["admin"])[0] == 200


def test_after_the_password_a_device_without_cameras_goes_to_add_one(server, cookies, installer_password, tmp_path, monkeypatch):
    """No camera yet (a new device): both buttons lead to step 3 of the wizard, which works for a signed-in admin of a
    device that is set up already; with cameras they lead to the main page."""
    monkeypatch.setattr(zorcam_config, "CAMERAS_FILE", str(tmp_path / "cameras.json"))
    for path, fields in (("/setup/password/skip", {}), ("/setup/password", {"password": "newsecret", "repeat": "newsecret"})):
        users = zorcam_users.load()
        users["admin"]["initial"] = True
        zorcam_users.save(users)
        status, headers, _ = post_password(server, cookies["admin"], path, **fields)
        assert status == 303 and headers["Location"] == "/setup/cameras", path
        zorcam_users.set_user("admin", "admin", PASSWORDS["admin"])
        page = request(server, "GET", headers["Location"], cookie=cookies["admin"])[2]
        assert t("en", "setup.ready") in page and 'href="/camera/new?setup=1"' in page, path
        assert request(server, "GET", "/camera/new?setup=1", cookie=cookies["admin"])[0] == 200


def test_installer_password_page_checks_the_role_the_token_and_the_sign_in(server, cookies, installer_password):
    for user in ("bob", "guest"):
        for path in ("/setup/password", "/setup/password/skip"):
            status = post_password(server, cookies[user], path, password="newsecret", repeat="newsecret")[0]
            assert status == 403, (user, path)
    for path in ("/setup/password", "/setup/password/skip"):
        assert request(server, "POST", path, "csrf=x&password=newsecret&repeat=newsecret")[0] == 302  # not signed in
        for csrf in ("x", chr(0x436) * 4, csrf_of_session(cookies["bob"])):  # not ASCII: 403, not 500
            body = urlencode({"csrf": csrf, "password": "newsecret", "repeat": "newsecret"})
            assert request(server, "POST", path, body, cookie=cookies["admin"])[0] == 403, (path, csrf)
        assert request(server, "POST", path, "", cookie=cookies["admin"])[0] == 400
        assert request(server, "POST", path, urlencode({"csrf": csrf_of_session(cookies["admin"])}), cookie=cookies["admin"],
                       headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert request(server, "GET", "/setup/password")[0] == 302  # not signed in
    assert zorcam_users.is_initial("admin") and zorcam_users.verify("admin", PASSWORDS["admin"]) == "admin"


def test_any_other_new_password_clears_the_mark(server, cookies, installer_password):
    """Settings -> Users: the password of admin typed there is a chosen one, no longer the installer's."""
    status, _, _ = users_form(server, cookies["admin"], user="admin", password="another1", repeat="another1")
    assert status == 303 and not zorcam_users.is_initial("admin")
