# ruff: noqa: F811
# (the fixtures imported from test_gallery are arguments of the tests)
"""Links without login (issue #175): app/gallery/shares.py and the gallery pages /s/<token> and /camera/<id>/shares.
A link opens the Live of one camera for anybody who has it, and nothing else of the gallery."""
import http.server
import json
import os
import re
import threading
import time
from urllib.parse import urlencode

import pytest

import app
import shares
import zorcam_config
from test_brand import BRAND_JSON, THEME, full_brand, make_brand, png, sha  # noqa: F401  (make_brand: fixture)
from test_gallery import CAMS, RU, cookies, csrf_of, request, restore_cameras, server, tunnel  # noqa: F401  (fixtures)
from zorcam_i18n import t

TOKEN_RE = re.compile(r"/s/([A-Za-z0-9_-]{43})")
UNAVAILABLE = t("en", "live.embed_unavailable")


@pytest.fixture
def clock():
    return [1_800_000_000.0]


@pytest.fixture
def store(tmp_path, monkeypatch, clock):
    """Empty links of the gallery in a temporary file, with a clock the test winds."""
    path = tmp_path / "shares.json"
    lines = []
    monkeypatch.setattr(app, "SHARES", shares.Shares(str(path), log=lines.append, max_viewers=lambda: app.SHARE_VIEWERS_MAX,
                                                      clock=lambda: clock[0]))
    app.SHARES.lines = lines
    return app.SHARES


def stored(store):
    """The links file as text."""
    with open(store.path) as f:
        return f.read()


def make_share(server, cookies, camera="front", label="Neighbour", expires="never"):
    """Create a link as admin; the token from the page that shows it."""
    page = request(server, "GET", f"/camera/{camera}/shares", cookie=cookies["admin"])[2]
    form = {"csrf": csrf_of(page, "/camera/shares/create"), "id": camera, "label": label, "expires": expires}
    status, _, body = request(server, "POST", "/camera/shares/create", urlencode(form), cookie=cookies["admin"])
    assert status == 200
    return TOKEN_RE.search(body).group(1)


@pytest.fixture
def hls(tmp_path, monkeypatch):
    """The HLS of a camera without ffmpeg: what the gallery asked for is in .asked."""
    asked = []

    class Stream:
        def __init__(self, cam_id):
            self.dir = str(tmp_path / cam_id)
            os.makedirs(self.dir, exist_ok=True)
            with open(os.path.join(self.dir, "seg_00001.ts"), "wb") as f:
                f.write(b"ts-of-" + cam_id.encode())

        def touch(self):
            pass

        def wait_playlist(self, timeout=12):
            return "#EXTM3U\n#EXTINF:1,\nseg_00001.ts\n"

    monkeypatch.setattr(app, "live_stream", lambda cam_id: (asked.append(cam_id), Stream(cam_id))[1])
    return asked


@pytest.fixture
def go2rtc(monkeypatch):
    """A go2rtc that answers every offer; .asked has the stream names it was asked for."""
    asked = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            asked.append(self.path.split("src=")[1])
            body = b"v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
            self.send_response(201)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    fake = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    monkeypatch.setattr(app, "GO2RTC_WEBRTC_URL", f"http://127.0.0.1:{fake.server_address[1]}/api/webrtc?src=")
    yield asked
    fake.shutdown()
    fake.server_close()


# ---------------------------------------------------------------- the store

def test_the_token_is_not_stored_only_its_hash(store, clock):
    item, token = store.create("front", "  Neighbour\n", 7)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token) and "hash" not in item
    assert item["label"] == "Neighbour" and item["expires"] == clock[0] + 7 * 86400 and item["last_opened"] is None
    text = stored(store)
    assert token not in text and shares.token_hash(token) in text
    assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"
    assert store.find(token)["id"] == item["id"]
    assert store.find(token[:-1] + ("a" if token[-1] != "a" else "b")) is None
    assert store.find("") is None and store.find("short") is None and store.find(None) is None
    # a new process reads the same links
    again = shares.Shares(store.path, clock=lambda: clock[0])
    assert again.find(token)["camera"] == "front"


def test_expiry_and_listing(store, clock):
    forever, t_forever = store.create("front", "a")
    day, t_day = store.create("front", "b", 1)
    other, _ = store.create("yard", "c")
    assert forever["expires"] is None
    assert [i["label"] for i in store.listing("front")] == ["a", "b"] and all("hash" not in i for i in store.listing("front"))
    clock[0] += 86400
    assert store.find(t_day) is None and store.find(t_forever) is not None
    assert [i["expired"] for i in store.listing("front")] == [False, True]  # the admin still sees it, to delete
    store.create("front", "d")  # a new link drops the expired ones
    assert [i["label"] for i in store.listing("front")] == ["a", "d"] and other["id"] in {i["id"] for i in store.listing("yard")}


def test_labels_are_clean_and_short(store):
    item, _ = store.create("front", "x\x00y‮" + "z" * 200)
    assert len(item["label"]) <= shares.LABEL_MAX and all(ch.isprintable() for ch in item["label"])


def test_last_opened_is_written_once_an_hour(store, clock):
    item, _ = store.create("front", "a")
    writes = []
    real = store._save
    store._save = lambda: (writes.append(1), real())[1]
    assert store.opened(item["id"]) is True and len(writes) == 1
    clock[0] += 3599
    assert store.opened(item["id"]) is False and len(writes) == 1
    clock[0] += 2
    assert store.opened(item["id"]) is True and len(writes) == 2
    assert store.listing("front")[0]["last_opened"] == clock[0]
    assert store.opened("nope") is False


def test_delete_and_forget_camera(store):
    a, _ = store.create("front", "a")
    b, _ = store.create("front", "b")
    c, _ = store.create("yard", "c")
    assert store.delete(a["id"], "yard") is False  # of another camera
    assert store.delete(a["id"], "front") is True and store.delete(a["id"], "front") is False
    store.forget_camera("front")
    assert store.listing("front") == [] and [i["id"] for i in store.listing("yard")] == [c["id"]]
    assert b["id"] not in stored(store)


def test_a_broken_file_is_no_links(tmp_path):
    path = tmp_path / "shares.json"
    for text in ("", "not json", "[]", '{"shares": 5}', '{"shares": [1, {"id": "x"}, {"id": "y", "camera": "f", "hash": "zz", '
                 '"created": 1}]}'):
        path.write_text(text)
        assert shares.Shares(str(path)).items == [], text


def test_the_limit_of_links(store):
    for n in range(shares.MAX_SHARES):
        store.create("front", str(n))
    with pytest.raises(ValueError):
        store.create("front", "one more")


def test_viewers_are_counted_and_expire(store, clock):
    item, _ = store.create("front", "a")
    other, _ = store.create("front", "b")
    limit = [2]
    store.max_viewers = lambda: limit[0]
    assert store.admit(item["id"], "phone") and store.admit(item["id"], "tablet")
    assert not store.admit(other["id"], "laptop") and not store.admit(item["id"], "tv")  # all links together
    assert store.admit(item["id"], "phone") and len(store.viewers) == 2  # the one who watches stays; refusals leave no trace
    clock[0] += shares.VIEWER_TTL - 1
    assert store.admit(item["id"], "phone") and not store.admit(item["id"], "tv")
    clock[0] += 2  # the tablet was silent for a minute, the phone asked a moment ago
    assert store.admit(item["id"], "tv") and set(store.viewers) == {(item["id"], "phone"), (item["id"], "tv")}
    store.delete(item["id"], "front")
    assert store.viewers == {}


# ---------------------------------------------------------------- the admin pages

def test_the_camera_form_leads_to_the_links(server, cookies):
    form = request(server, "GET", "/camera/front", cookie=cookies["admin"])[2]
    assert f'<a class="row action" href="/camera/front/shares">{t("en", "share.link")}</a>' in form
    assert "/shares" not in request(server, "GET", "/camera/new", cookie=cookies["admin"])[2]
    assert request(server, "GET", "/camera/nope/shares", cookie=cookies["admin"])[0] == 404
    assert request(server, "GET", "/camera/front/shares")[0] == 302  # not signed in


def test_create_and_delete_are_admin_only_with_csrf(server, cookies, store):
    admin = cookies["admin"]
    page = request(server, "GET", "/camera/front/shares", cookie=admin)[2]
    assert t("en", "share.none") in page and '<option value="never" selected>' in page
    assert re.findall(r'<option value="(\w+)"', page) == ["1", "7", "30", "never"] and "/s/" not in page
    csrf = csrf_of(page, "/camera/shares/create")
    form = {"csrf": csrf, "id": "front", "label": "Plumber", "expires": "7"}
    post = lambda body, who="admin", path="/camera/shares/create": request(  # noqa: E731
        server, "POST", path, urlencode(body), cookie=cookies[who])[0]
    for who in ("bob", "guest"):
        assert post(form, who) == 403
    assert request(server, "POST", "/camera/shares/create", urlencode(form))[0] in (302, 401, 403)
    assert post({**form, "csrf": "x"}) == 403 and post({**form, "csrf": csrf[::-1]}) == 403
    assert post({**form, "id": "nope"}) == 404
    assert post({**form, "expires": "365"}) == 400 and post({k: v for k, v in form.items() if k != "id"}) == 404
    assert store.listing("front") == []
    status, _, body = request(server, "POST", "/camera/shares/create", urlencode(form), cookie=admin)
    assert status == 200
    token = TOKEN_RE.search(body).group(1)
    base = f"http://127.0.0.1:{server}"
    assert f'data-copy="{base}/s/{token}"' in body and f'data-share="{base}/s/{token}"' in body
    assert t("en", "share.hint") in body and t("en", "share.new") in body and "/static/shares.js?v=" in body
    [item] = store.listing("front")
    assert item["label"] == "Plumber" and store.find(token)["id"] == item["id"]
    assert token not in "".join(store.lines)
    # the list shows the link without its token (only the hash is stored): label, expiry, "never opened", Delete
    listing = request(server, "GET", "/camera/front/shares", cookie=admin)[2]
    assert token not in listing and "Plumber" in listing and t("en", "share.never") in listing
    assert "Valid until " in listing
    assert f'name="share" value="{item["id"]}"' in listing and 'action="/camera/shares/delete"' in listing
    assert request(server, "GET", "/camera/yard/shares", cookie=admin)[2].count("Plumber") == 0
    # delete
    csrf = csrf_of(listing, "/camera/shares/delete")
    gone = {"csrf": csrf, "id": "front", "share": item["id"]}
    assert post(gone, "bob", "/camera/shares/delete") == 403 and post({**gone, "csrf": "x"}, path="/camera/shares/delete") == 403
    assert post({**gone, "id": "yard"}, path="/camera/shares/delete") == 404  # the link of another camera
    assert post({**gone, "share": "nope"}, path="/camera/shares/delete") == 404
    assert store.find(token) is not None
    status, headers, _ = request(server, "POST", "/camera/shares/delete", urlencode(gone), cookie=admin)
    assert status == 303 and headers["Location"] == "/camera/front/shares"
    assert store.find(token) is None and store.listing("front") == []
    assert request(server, "GET", f"/s/{token}")[0] == 404


def test_the_list_in_russian_and_with_the_limits(server, cookies, store, clock, monkeypatch):
    token = make_share(server, cookies, label="<b>Neighbour</b>", expires="1")
    page = request(server, "GET", "/camera/front/shares", cookie=cookies["admin"], headers=RU)[2]
    assert "&lt;b&gt;Neighbour&lt;/b&gt;" in page and "<b>Neighbour" not in page  # a label is text
    assert t("ru", "share.never") in page and t("ru", "share.create") in page and token not in page
    request(server, "GET", f"/s/{token}")  # opened
    page = request(server, "GET", "/camera/front/shares", cookie=cookies["admin"], headers=RU)[2]
    assert t("ru", "share.never") not in page and t("ru", "share.opened", at="") in page
    clock[0] += 2 * 86400
    page = request(server, "GET", "/camera/front/shares", cookie=cookies["admin"])[2]
    assert "Expired " in page
    monkeypatch.setattr(shares, "MAX_SHARES", 1)
    page = request(server, "GET", "/camera/front/shares", cookie=cookies["admin"])[2]
    store.create("front", "x")
    form = {"csrf": csrf_of(page, "/camera/shares/create"), "id": "front", "label": "y", "expires": "never"}
    status, _, body = request(server, "POST", "/camera/shares/create", urlencode(form), cookie=cookies["admin"])
    assert status == 400 and t("en", "share.too_many_links") in body


def test_the_link_is_local_even_when_paired(server, cookies, store, monkeypatch):
    monkeypatch.setattr(app, "yandex_cloud_url", lambda: "https://abc123.devices.zorcam.ru")
    token = make_share(server, cookies)
    page = request(server, "GET", "/camera/front/shares", cookie=cookies["admin"])[2]
    assert "/s/" not in page
    body = request(server, "POST", "/camera/shares/create", urlencode({
        "csrf": csrf_of(page, "/camera/shares/create"), "id": "front", "label": "", "expires": "never"}),
        cookie=cookies["admin"])[2]
    assert "devices.zorcam.ru" not in body
    assert f'data-copy="http://127.0.0.1:{server}/s/{TOKEN_RE.search(body).group(1)}"' in body
    assert token not in body and t("en", "share.no_label") in body
    # opened through a reverse proxy: the address the browser used
    monkeypatch.setattr(app, "yandex_cloud_url", lambda: "")
    page = request(server, "GET", "/camera/front/shares", cookie=cookies["admin"],
                   headers={"X-Forwarded-Host": "cam.example.org", "X-Forwarded-Proto": "https"})[2]
    body = request(server, "POST", "/camera/shares/create", urlencode({
        "csrf": csrf_of(page, "/camera/shares/create"), "id": "front", "label": "", "expires": "never"}), cookie=cookies["admin"],
        headers={"X-Forwarded-Host": "cam.example.org", "X-Forwarded-Proto": "https"})[2]
    assert 'data-copy="https://cam.example.org/s/' in body


def test_deleting_a_camera_deletes_its_links(server, cookies, store, restore_cameras, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(app, "BASE_DIR", str(tmp_path))
    token = make_share(server, cookies, camera="yard")
    keep = make_share(server, cookies, camera="front")
    page = request(server, "GET", "/camera/yard", cookie=cookies["admin"])[2]
    form = {"csrf": csrf_of(page, "/camera/delete"), "id": "yard", "recordings": ""}
    assert request(server, "POST", "/camera/delete", urlencode(form), cookie=cookies["admin"])[0] == 303
    assert store.find(token) is None and store.find(keep) is not None
    assert token not in stored(store) and shares.token_hash(token) not in stored(store)
    assert request(server, "GET", f"/s/{token}")[0] == 404


# ---------------------------------------------------------------- the public page

def test_the_page_of_a_link_is_one_player(server, cookies, store):
    token = make_share(server, cookies, camera="yard")
    for headers in ({}, RU):
        status, headers_, body = request(server, "GET", f"/s/{token}", headers=headers)  # no cookie: nobody signed in
        assert status == 200
        assert re.findall(r'data-cam="(\w+)"', body) == ["yard"] and "Front door" not in body
        assert '<body class="embed">' in body and '<h2 class="camera-name">Yard</h2>' in body
        for gone in ('class="tabbar"', 'class="topbar"', "livecols", 'id="bell"', "apphint", 'class="ptz"', 'href="/?',
                     'href="/settings'):
            assert gone not in body.split("</head>")[1].split("<script")[0], gone
        assert "<title>Yard </title>" in body and 'name="apple-mobile-web-app-title" content="Yard"' in body
        assert f'<link rel="manifest" href="/s/{token}/manifest.webmanifest">' in body
        assert '"liveBase": "/s/' + token + '"' in body and "webrtcFallbackMs" in body
        assert headers_["Cache-Control"] == "no-store" and headers_["X-Robots-Tag"] == "noindex"
        assert headers_["Referrer-Policy"] == "same-origin" and "Set-Cookie" not in headers_
    assert "/live/webrtc" in "".join(request(server, "GET", n)[2] for n in re.findall(r'"(/static/main\.js\?v=\w+)"', body))
    assert "liveBase" in request(server, "GET", re.search(r'"(/static/main\.js\?v=\w+)"', body).group(1))[2]
    # the signed-in page of the main app is still what it was
    assert "liveBase" not in request(server, "GET", "/?tab=live&cam=yard&embed=1", cookie=cookies["admin"])[2]



def test_the_controls_of_a_link_hide_a_second_after_the_last_touch(server, cookies, store):
    """The page of a link is the same Live player: its system controls go away a second after the last touch."""
    body = request(server, "GET", f"/s/{make_share(server, cookies)}")[2]
    scripts = re.findall(r'src="/static/(\w+)\.js\?v=\w+"', body)
    assert scripts.index("app") < scripts.index("main")
    js = "".join(request(server, "GET", n)[2] for n in re.findall(r'src="(/static/\w+\.js\?v=\w+)"', body))
    assert "var CONTROLS_HIDE_MS = 1000;" in js and "    autoHideControls(video);" in js

def test_the_manifest_of_a_link(server, cookies, store):
    token = make_share(server, cookies, camera="front")
    status, headers, body = request(server, "GET", f"/s/{token}/manifest.webmanifest")
    assert status == 200 and headers["Content-Type"] == "application/manifest+json"
    assert headers["Cache-Control"] == "no-store" and headers["X-Robots-Tag"] == "noindex"
    manifest = json.loads(body)
    assert manifest["name"] == manifest["short_name"] == "Front door"
    assert manifest["start_url"] == manifest["scope"] == f"/s/{token}" and manifest["display"] == "standalone"
    main = json.loads(request(server, "GET", "/manifest.webmanifest")[2])
    assert manifest["icons"] == main["icons"] and manifest["theme_color"] == main["theme_color"]
    assert main["start_url"] == "/?tab=live" and main["name"] == "Zorcam"  # the main one is as it was
    assert all(request(server, "GET", icon["src"])[0] == 200 for icon in manifest["icons"])


def test_a_link_follows_the_brand_folder(server, cookies, store, make_brand):
    token = make_share(server, cookies)
    full_brand(make_brand)
    page = request(server, "GET", f"/s/{token}")[2]
    theme = f"/static/brand/theme.css?v={sha(THEME.encode())}"
    links = re.findall(r'<link rel="stylesheet" href="([^"]+)">', page)
    assert links[-1] == theme and BRAND_JSON["theme_color"] in page
    assert 'name="apple-mobile-web-app-status-bar-style" content="black"' in page and "Acme View" not in page.split("</head>")[0]
    manifest = json.loads(request(server, "GET", f"/s/{token}/manifest.webmanifest")[2])
    assert manifest["name"] == "Front door" and manifest["theme_color"] == BRAND_JSON["theme_color"]
    assert manifest["icons"][0]["src"].startswith("/icon-192.png?v=" + sha(png((11, 12, 15))))


def test_live_requests_of_a_link_are_its_camera_only(server, cookies, store, hls, go2rtc, tmp_path):
    token = make_share(server, cookies, camera="front")
    base = f"/s/{token}"
    status, headers, body = request(server, "GET", base + "/stream.m3u8")
    assert status == 200 and "seg_00001.ts" in body and headers["Content-Type"] == "application/vnd.apple.mpegurl"
    assert headers["X-Robots-Tag"] == "noindex" and "no-store" in headers["Cache-Control"]
    status, headers, body = request(server, "GET", base + "/seg_00001.ts")
    assert status == 200 and body == "ts-of-front" and headers["X-Robots-Tag"] == "noindex"
    assert request(server, "GET", base + "/seg_1.ts")[0] == 404 and request(server, "GET", base + "/..")[0] == 404
    assert request(server, "GET", base + "/stream.m3u8?cam=yard")[0] == 200
    sdp = {"Content-Type": "application/sdp"}
    assert request(server, "POST", base + "/webrtc", "v=0", headers=sdp)[0] == 201
    assert request(server, "POST", base + "/webrtc?cam=yard", "v=0", headers=sdp)[0] == 201  # the link decides, not the address
    assert request(server, "POST", "/live/webrtc?cam=yard", "v=0", headers=sdp)[0] == 401  # the signed-in way stays closed
    assert request(server, "POST", base + "/webrtc", "", headers=sdp)[0] == 400
    assert request(server, "POST", base + "/webrtc-log", "front: ICE failed | UA")[0] == 204
    assert request(server, "GET", base + "/ping")[0] == 204
    assert set(hls) == {"front"} and go2rtc == ["front", "front"]
    # nothing else of the gallery opens with the link, in the address or as a header or a cookie
    others = ["/", "/?tab=live&cam=yard", "/?tab=live&embed=1&cam=front", "/live/front/stream.m3u8", "/live/yard/stream.m3u8",
              "/ptz/front", "/timelapse/front?period=hour", "/cloud/v1/cameras", "/cloud/v1/poster/front", "/camera-frame/front",
              "/thumb/front/x.jpg", f"/img/front/{'20260926_120000_000.jpg'}", "/settings", "/config", "/camera/front",
              "/camera/front/shares", "/push/key", "/internal/motion"]
    for path in others:
        for extra in ({}, {"X-Zorcam-Share": token, "Authorization": "Bearer " + token},
                      {"Cookie": f"zorcam_session={token}; zorcam_share={token}"}):
            status, headers_, _ = request(server, "GET", path, headers=extra)
            assert status in (302, 404, 405) and headers_.get("Location", "/login") == "/login", (path, status)
    for path in ("/ptz/front", "/camera/shares/create", "/camera/shares/delete", "/camera/delete", "/users/save",
                 "/live/webrtc-log"):
        status = request(server, "POST", path, "{}", headers={"Cookie": f"zorcam_session={token}"})[0]
        assert status in (302, 400, 401, 403, 404), path
    for path in (base + "/camera/save", base + "/x/y", base + "/../settings", "/s/", "/s", "/s//webrtc", base + "/%2e%2e"):
        status, _, text = request(server, "GET", path)
        assert status in (302, 404), (path, status)
    assert set(hls) == {"front"} and go2rtc == ["front", "front"]


def test_a_link_that_is_not_valid_is_one_answer(server, cookies, store, clock, hls, go2rtc):
    live = make_share(server, cookies, camera="front")
    gone = make_share(server, cookies, camera="front")
    old = make_share(server, cookies, camera="front", expires="1")
    yard = make_share(server, cookies, camera="yard")
    store.delete(store.find(gone)["id"], "front")
    clock[0] += 86400
    assert store.find(live) is not None
    unknown = "A" * 43
    bodies = set()
    for lang, headers in (("en", {}), ("ru", RU)):
        for token in (unknown, gone, old, "short", "x" * 300, "A" * 42 + "!"):
            for suffix in ("", "/manifest.webmanifest", "/stream.m3u8", "/ping", "/seg_00001.ts"):
                status, headers_, body = request(server, "GET", f"/s/{token}{suffix}", headers=headers)
                assert status == 404 and t(lang, "live.embed_unavailable") in body and "data-cam=" not in body, (token, suffix)
                assert headers_["X-Robots-Tag"] == "noindex" and headers_["Cache-Control"] == "no-store"
                bodies.add((lang, re.sub(r"\s+", " ", body)))
            status, _, body = request(server, "POST", f"/s/{token}/webrtc", "v=0",
                                      headers={"Content-Type": "application/sdp", **headers})
            assert status == 404 and t(lang, "live.embed_unavailable") in body
    assert len(bodies) == 2  # one text per language: nothing tells an unknown link from an expired one
    assert hls == [] and go2rtc == []
    # a camera switched off or deleted
    cams = [dict(c) for c in CAMS]
    try:
        zorcam_config.save_cameras([dict(cams[0], enabled=False), cams[1]])
        for suffix in ("", "/stream.m3u8", "/manifest.webmanifest"):
            status, _, body = request(server, "GET", f"/s/{live}{suffix}")
            assert status == 404 and UNAVAILABLE in body and "Front door" not in body
        assert request(server, "GET", f"/s/{yard}")[0] == 200
        zorcam_config.save_cameras([cams[1]])
        assert request(server, "GET", f"/s/{live}")[0] == 404
    finally:
        zorcam_config.save_cameras(CAMS)
    assert request(server, "GET", f"/s/{live}")[0] == 200
    assert hls == [] and go2rtc == []


def test_the_viewers_limit(server, cookies, store, clock, hls, go2rtc, monkeypatch):
    monkeypatch.setattr(app, "SHARE_VIEWERS_MAX", 2)
    one = make_share(server, cookies, camera="front")
    two = make_share(server, cookies, camera="yard")
    ua = lambda name: {"User-Agent": name}  # noqa: E731
    sdp = {"Content-Type": "application/sdp"}
    assert request(server, "GET", f"/s/{one}", headers=ua("phone"))[0] == 200
    assert request(server, "GET", f"/s/{two}", headers=ua("tablet"))[0] == 200
    status, headers, body = request(server, "GET", f"/s/{one}", headers=ua("laptop"))
    assert status == 429 and t("en", "share.too_many") in body and "data-cam=" not in body
    assert headers["X-Robots-Tag"] == "noindex"
    assert t("ru", "share.too_many") in request(server, "GET", f"/s/{two}", headers={**ua("tv"), **RU})[2]
    assert request(server, "POST", f"/s/{one}/webrtc", "v=0", headers={**sdp, **ua("laptop")})[0] == 429
    assert request(server, "GET", f"/s/{one}/stream.m3u8", headers=ua("laptop"))[0] == 429
    assert request(server, "GET", f"/s/{one}/ping", headers=ua("laptop"))[0] == 429
    assert request(server, "GET", f"/s/{one}/manifest.webmanifest", headers=ua("laptop"))[0] == 200  # nothing to watch there
    assert hls == [] and go2rtc == []
    # the viewers who are in stay in; the tablet's requests keep it in
    assert request(server, "GET", f"/s/{one}/stream.m3u8", headers=ua("phone"))[0] == 200
    assert request(server, "POST", f"/s/{two}/webrtc", "v=0", headers={**sdp, **ua("tablet")})[0] == 201
    # signed-in users are not counted
    assert request(server, "GET", "/?tab=live&cam=front&embed=1", cookie=cookies["bob"])[0] == 200
    assert request(server, "POST", "/live/webrtc?cam=front", "v=0", cookie=cookies["bob"], headers=sdp)[0] == 201
    # a minute without a request frees the place of the tablet; the phone has just asked
    clock[0] += shares.VIEWER_TTL - 5
    assert request(server, "GET", f"/s/{one}/ping", headers=ua("phone"))[0] == 204
    clock[0] += 10
    assert request(server, "GET", f"/s/{one}", headers=ua("laptop"))[0] == 200
    assert request(server, "GET", f"/s/{two}/ping", headers=ua("tablet"))[0] == 429  # its place went to the laptop
    # the same address and agent on another link is another viewer only when the place is free
    monkeypatch.setattr(app, "SHARE_VIEWERS_MAX", 3)
    assert request(server, "GET", f"/s/{two}/ping", headers=ua("tablet"))[0] == 204


def test_the_page_of_a_link_tells_the_viewer_the_script_pings(server, cookies, store):
    token = make_share(server, cookies)
    body = request(server, "GET", f"/s/{token}")[2]
    script = request(server, "GET", re.search(r'"(/static/main\.js\?v=\w+)"', body).group(1))[2]
    assert "BASE + '/ping'" in script and "T['share.too_many']" in script
    assert t("en", "share.too_many") in body and t("ru", "share.too_many") in request(server, "GET", f"/s/{token}", headers=RU)[2]


# ---------------------------------------------------------------- the cloud tunnel

def test_the_tunnel_takes_a_link_without_identity(server, tunnel, cookies, store, hls, go2rtc):
    token = make_share(server, cookies, camera="front")
    ice = [{"urls": ["turn:turn.example.org:3480"], "username": "u", "credential": "c"}]
    status, headers, body = request(tunnel, "GET", f"/s/{token}", headers={"X-Zorcam-Ice": json.dumps(ice)})
    assert status == 200 and 'data-cam="front"' in body and "turn.example.org:3480" in body
    home = request(server, "GET", f"/s/{token}", headers={"X-Zorcam-Ice": json.dumps(ice)})[2]
    assert "turn.example.org" not in home  # at home the header is nothing
    assert request(tunnel, "GET", f"/s/{token}/manifest.webmanifest")[0] == 200
    assert request(tunnel, "GET", f"/s/{token}/stream.m3u8")[0] == 200
    assert request(tunnel, "POST", f"/s/{token}/webrtc", "v=0", headers={"Content-Type": "application/sdp"})[0] == 201
    assert request(tunnel, "GET", "/s/" + "A" * 43)[0] == 404
    # nothing else changes for the tunnel: no identity, no gallery
    for path in ("/", "/?tab=live&cam=front&embed=1", "/live/front/stream.m3u8", "/settings", "/cloud/v1/cameras"):
        status, headers, _ = request(tunnel, "GET", path, headers={"X-Zorcam-Ice": json.dumps(ice)})
        assert status == 302 and headers["Location"] == "/login", path
    assert request(tunnel, "POST", "/live/webrtc?cam=front", "v=0", headers={"Content-Type": "application/sdp"})[0] == 401
    assert request(tunnel, "GET", "/camera/front/shares")[0] == 302


# ---------------------------------------------------------------- the journal

def test_the_token_is_never_in_the_journal(server, cookies, store, hls, go2rtc, capsys, monkeypatch):
    monkeypatch.setattr(app, "DEBUG", True)
    token = make_share(server, cookies, label="Neighbour")
    for path in ("", "/manifest.webmanifest", "/stream.m3u8", "/ping", "/seg_00001.ts", "/nothing"):
        request(server, "GET", f"/s/{token}{path}")
    request(server, "POST", f"/s/{token}/webrtc", "v=0", headers={"Content-Type": "application/sdp"})
    request(server, "POST", f"/s/{token}/webrtc-log", "front: failed")
    request(server, "POST", f"/s/{token}/webrtc", "v=0", headers={"Sec-Fetch-Site": "cross-site"})  # refused, and logged
    request(server, "GET", f"/s/{'B' * 43}")
    time.sleep(0.3)
    out = capsys.readouterr().out
    assert token not in out and "B" * 43 not in out
    assert "debug: GET /s/*** -> 200" in out and "debug: POST /s/***/webrtc -> 201" in out
    assert "post: /s/***/webrtc refused, cross-site" in out
    assert re.search(r'shares: link \w+ "Neighbour" to front opened from 127\.0\.0\.1', out)
    assert re.search(r'shares: link \w+ "Neighbour" to front created by admin', out)
    assert token not in stored(store) and token not in "".join(store.lines)
    assert app.mask_share("GET /s/abc/def HTTP/1.1 and /s/xyz?q=1") == "GET /s/***/def HTTP/1.1 and /s/***?q=1"


# ---------------------------------------------------------------- the Share button in Live

def share_buttons(body):
    return re.findall(r'<a class="live-share" href="([^"]+)" aria-label="([^"]+)" title="([^"]+)">', body)


def test_the_share_button_is_in_live_for_admin_only(server, cookies, store):
    token = make_share(server, cookies, camera="yard")
    for lang, headers in (("en", {}), ("ru", RU)):
        word = t(lang, "live.share")
        assert word != t("en" if lang == "ru" else "ru", "live.share")
        grid = request(server, "GET", "/?tab=live", cookie=cookies["admin"], headers=headers)[2]
        assert share_buttons(grid) == [("/camera/front/shares?from=live", word, word),
                                       ("/camera/yard/shares?from=live", word, word)]
        assert grid.count('class="cam has-share"') == 2
        single = request(server, "GET", "/?tab=live&cam=yard", cookie=cookies["admin"], headers=headers)[2]
        assert share_buttons(single) == [("/camera/yard/shares?from=live", word, word)]
    for who in ("bob", "guest"):  # no button in the markup at all
        for path in ("/?tab=live", "/?tab=live&cam=yard"):
            body = request(server, "GET", path, cookie=cookies[who])[2]
            assert "live-share" not in body.split("</head>")[1] and "has-share" not in body and "/shares" not in body, (who, path)
    for path in ("/?tab=live&cam=yard&embed=1", "/?tab=live&embed=1&cam=front"):
        body = request(server, "GET", path, cookie=cookies["admin"])[2]
        assert 'data-cam="' in body and "live-share" not in body.split("</head>")[1] and "/shares" not in body
    page = request(server, "GET", f"/s/{token}")[2]
    assert 'data-cam="yard"' in page and "live-share" not in page.split("</head>")[1] and "/shares" not in page
    assert "/camera/" not in page.split("</head>")[1]


def test_the_shares_page_goes_back_to_where_it_was_opened_from(server, cookies, store):
    admin = cookies["admin"]
    back = lambda body: re.search(r'<a class="back" href="([^"]+)"', body).group(1)  # noqa: E731
    page = request(server, "GET", "/camera/front/shares?from=live", cookie=admin)[2]
    assert back(page) == "/?tab=live&amp;cam=front" and page.count('name="from" value="live"') == 1
    assert back(request(server, "GET", "/camera/front/shares", cookie=admin)[2]) == "/camera/front"
    for odd in ("from=//evil.example", "from=http://evil.example/", "from=Live", "from=", "from=live&from=x"):
        body = request(server, "GET", "/camera/front/shares?" + odd, cookie=admin)[2]
        assert back(body) == "/camera/front" and 'name="from" value=""' in body and "evil" not in body, odd
    # the forms carry it through create and delete
    csrf = csrf_of(page, "/camera/shares/create")
    form = {"csrf": csrf, "id": "front", "label": "Plumber", "expires": "never", "from": "live"}
    status, _, body = request(server, "POST", "/camera/shares/create", urlencode(form), cookie=admin)
    assert status == 200 and back(body) == "/?tab=live&amp;cam=front" and 'name="share"' in body
    assert body.count('name="from" value="live"') == 2  # the create form and the delete button of the one link
    [item] = store.listing("front")
    status, headers, _ = request(server, "POST", "/camera/shares/delete", urlencode({
        "csrf": csrf, "id": "front", "share": item["id"], "from": "live"}), cookie=admin)
    assert status == 303 and headers["Location"] == "/camera/front/shares?from=live"
    token = make_share(server, cookies)
    [item] = store.listing("front")
    for odd in ("", "x", "//evil.example"):
        status, headers, _ = request(server, "POST", "/camera/shares/delete", urlencode({
            "csrf": csrf, "id": "front", "share": item["id"], "from": odd}), cookie=admin)
        assert status == 303 and headers["Location"] == "/camera/front/shares"
        token = make_share(server, cookies)
        [item] = store.listing("front")
    assert store.find(token) is not None


def test_the_error_report_of_a_viewer_is_counted_like_other_requests(server, cookies, store, monkeypatch, capsys):
    monkeypatch.setattr(app, "SHARE_VIEWERS_MAX", 1)
    token = make_share(server, cookies)
    assert request(server, "POST", f"/s/{token}/webrtc-log", "one", headers={"User-Agent": "phone"})[0] == 204
    assert request(server, "POST", f"/s/{token}/webrtc-log", "two", headers={"User-Agent": "tablet"})[0] == 429
    assert request(server, "POST", f"/s/{token}/webrtc-log", "again", headers={"User-Agent": "phone"})[0] == 204
    out = capsys.readouterr().out
    assert ": one" in out and ": again" in out and ": two" not in out


def test_the_link_via_the_cloud_uses_the_home_address(monkeypatch):
    """Page opened through the cloud tunnel: the copied link is still local, http://<device address in the home network>."""
    from types import SimpleNamespace
    monkeypatch.setattr(app, "lan_ip", lambda: "192.168.1.20")
    assert app.Handler.share_base(SimpleNamespace(on_tunnel=True)) == "http://192.168.1.20"
    monkeypatch.setattr(app, "lan_ip", lambda: "")
    assert app.Handler.share_base(SimpleNamespace(on_tunnel=True)) == ""


def test_lan_ip_is_an_address_or_empty():
    ip = app.lan_ip()
    assert ip == "" or (not ip.startswith("127.") and ip.count(".") == 3)
