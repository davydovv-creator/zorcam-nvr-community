"""The slots of the core for a build made on top of it (for example Zorcam Box): the optional folder /opt/zorcam-nvr/brand
(app/gallery/brand.py) and what the gallery takes from it, the serial number from device.json. Without the folder and the
file the pages are what they always were."""
import base64
import hashlib
import io
import json
import re

import pytest
from PIL import Image

import app
import brand
import zorcam_config
from conftest import SRC
from zorcam_i18n import t
from test_gallery import RU, VIDEO, cookies, fresh_install, request, server  # noqa: F401  (fixtures)

RU_NAME = "\u0410\u043a\u043c\u0435.view"  # Acme.view in Russian: tests are English files
BRAND_JSON = {"name": {"en": "Acme.view", "ru": RU_NAME}, "app_name": "Acme View", "theme_color": "#123456",
              "version": "0.1.0"}
THEME = ":root { --bg:#123456; --text:#eee; }\n"
LOGO = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/></svg>'
# pages of every kind: the sign-in page, the main page and its tabs, Settings, a form, a clip, a page of the wizard, an error
PAGES = ["/login", "/?tab=live", "/?tab=records", "/?tab=live&cam=front&embed=1", "/settings", "/settings?tab=status",
         "/config", "/camera/new", "/camera/front/zones", "/setup/cameras", "/setup/password", "/yandex", "/mqtt",
         f"/watch/front/{VIDEO}"]


def png(color):
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, "PNG")
    return buf.getvalue()


def sha(data):
    return hashlib.sha256(data).hexdigest()[:12]


@pytest.fixture
def make_brand(tmp_path):
    """A folder like the one a build installs, taken by the gallery the way it takes it at start; the plain gallery is
    back afterwards. make({"theme.css": text or bytes, ...}) -> the folder; the journal lines it wrote are in make.log."""
    def make(files):
        folder = tmp_path / "brand"
        folder.mkdir(exist_ok=True)
        for name, body in files.items():
            (folder / name).write_bytes(body if isinstance(body, bytes) else body.encode())
        make.log = []
        app.apply_brand(brand.Brand(str(folder), log=make.log.append))
        return folder
    make.log = []
    yield make
    app.apply_brand(None)


def full_brand(make_brand, **override):
    return make_brand({"brand.json": json.dumps(BRAND_JSON), "theme.css": THEME, "logo.svg": LOGO,
                       "icon-192.png": png((11, 12, 15)), "icon-512.png": png((12, 13, 16)),
                       "apple-touch-icon.png": png((13, 14, 17)), **override})


def get(port, cookie, path, headers=None):
    return request(port, "GET", path, cookie=None if path == "/login" else cookie, headers=headers)[2]


def stylesheets(body):
    return re.findall(r'<link rel="stylesheet" href="([^"]+)">', body)


# ---------------------------------------------------------------- without the folder: the core as it is

def test_no_folder_nothing_changes(server, cookies, monkeypatch, tmp_path):  # noqa: F811
    assert app.BRAND.assets == {} and app.BRAND.icons == {} and app.TEMPLATE_VARS["theme_css"] == ""
    for path in PAGES:
        body = get(server, cookies["admin"], path)
        assert not re.search(r"brandlogo|loginlogo|/static/brand/", body), path
        assert 2 <= len(stylesheets(body)) <= 3, path  # app.css, the page's own (and zones.css): nothing after them
    main = get(server, cookies["admin"], "/?tab=live")
    assert '<span class="appname"><span>Zorcam.nvr</span><button' in main and "<title>Zorcam " + app.VERSION in main
    assert '<meta name="theme-color" content="#f3f3f1">' in main
    assert '<meta name="apple-mobile-web-app-title" content="Zorcam">' in main
    assert '<div class="login">\n  <h1>Zorcam</h1>' in get(server, None, "/login")
    manifest = json.loads(request(server, "GET", "/manifest.webmanifest")[2])
    assert (manifest["name"], manifest["short_name"], manifest["theme_color"], manifest["background_color"]) == (
        "Zorcam", "Zorcam", "#f3f3f1", "#f3f3f1")
    for path in ("/static/brand/theme.css", "/static/brand/logo.svg", "/static/brand/brand.json"):
        assert request(server, "GET", path)[0] == 404, path
    monkeypatch.setattr(zorcam_config, "DEVICE_FILE", str(tmp_path / "none.json"))  # no device.json: no serial number
    status = get(server, cookies["admin"], "/settings?tab=status")
    assert f'<div class="v ">{t("en", "edition.community")}</div>' in status and t("en", "st.serial") not in status


# ---------------------------------------------------------------- the folder

def test_every_page_frame_has_the_slot_for_theme_css():
    """A new template with a <head> must end it with $theme_css (the build's CSS is the last link of every page)."""
    frames = {name: text for name, text in app.TEMPLATES.items() if "<head>" in text}
    assert set(frames) >= {"page", "login", "settings", "watch"}
    for name, text in frames.items():
        assert re.search(r"\$theme_css</head>", text), name
        assert text.rindex('<link rel="stylesheet"') < text.index("$theme_css"), name


@pytest.mark.parametrize("lang,headers", [("en", {}), ("ru", RU)])
def test_theme_css_is_the_last_link_of_every_page(server, cookies, make_brand, lang, headers):  # noqa: F811
    full_brand(make_brand)
    theme = f"/static/brand/theme.css?v={sha(THEME.encode())}"
    for path in PAGES + ["/?tab=videos", "/?tab=all", "/settings?tab=notify", "/camera/front"]:
        body = get(server, cookies["admin"], path, headers)
        links = stylesheets(body)
        assert links[-1] == theme and links.count(theme) == 1 and len(links) >= 2, path
        assert body.index(f'href="{theme}"') < body.index("</head>"), path
    # the page of a user without rights and the 403 page too
    status, _, body = request(server, "GET", "/config", cookie=cookies["bob"], headers=headers)
    assert status == 403 and stylesheets(body)[-1] == theme


def test_theme_css_is_the_last_link_of_the_wizard(server, fresh_install, make_brand):  # noqa: F811
    full_brand(make_brand)
    theme = f"/static/brand/theme.css?v={sha(THEME.encode())}"
    for path in ("/setup", "/setup/users"):
        status, _, body = request(server, "GET", path)
        assert status == 200 and stylesheets(body)[-1] == theme, path
        assert f'<img class="loginlogo" src="/static/brand/logo.svg?v={sha(LOGO.encode())}" alt=""><h1' in body, path


def test_brand_files_are_public_typed_and_cached_for_good(server, make_brand):  # noqa: F811
    full_brand(make_brand)
    for name, body, ctype in (("theme.css", THEME, "text/css; charset=utf-8"), ("logo.svg", LOGO, "image/svg+xml")):
        status, headers, text = request(server, "GET", f"/static/brand/{name}?v={sha(body.encode())}")  # no cookie
        assert status == 200 and text == body and headers["Content-Type"] == ctype, name
        assert headers["Cache-Control"] == "public, max-age=31536000, immutable" and int(headers["Content-Length"]) == len(body)
    for path in ("/static/brand/", "/static/brand/nope.css", "/static/brand/brand.json", "/static/brand/icon-192.png",
                 "/static/brand/../app.css", "/static/brand/%2e%2e/app.css", "/static/brand/theme.css/x"):
        assert request(server, "GET", path)[0] == 404, path
    assert request(server, "GET", "/static/app.css")[0] == 200  # the core's files stay where they were


@pytest.mark.parametrize("lang,headers,name", [("en", {}, "Acme.view"), ("ru", RU, RU_NAME)])
def test_name_logo_title_colour_and_manifest(server, cookies, make_brand, lang, headers, name, monkeypatch):  # noqa: F811
    full_brand(make_brand)
    logo = f'<img class="brandlogo" src="/static/brand/logo.svg?v={sha(LOGO.encode())}" alt="">'
    main = get(server, cookies["admin"], "/?tab=live", headers)
    assert f'<span class="appname">{logo}<span>{name}</span><button' in main  # the header of a phone
    assert f'<div class="brand">{logo}<span>{name}</span><button' in main  # the sidebar of a laptop
    assert f"<title>Acme View {app.VERSION}</title>" in main
    assert '<meta name="apple-mobile-web-app-title" content="Acme View">' in main
    assert '<meta name="theme-color" content="#123456">' in main
    login = get(server, None, "/login", headers)
    mark = f'<img class="loginlogo" src="/static/brand/logo.svg?v={sha(LOGO.encode())}" alt="">'
    assert f'<div class="login">\n  {mark}<h1>Zorcam</h1>' in login
    assert "<title>Acme View " in login and '<meta name="theme-color" content="#123456">' in login
    manifest = json.loads(request(server, "GET", "/manifest.webmanifest")[2])
    assert (manifest["name"], manifest["short_name"], manifest["theme_color"], manifest["background_color"]) == (
        "Acme View", "Acme View", "#123456", "#123456")
    # the mark of Settings (the header of its main page) too
    assert f'<span class="appname">{logo}<span>{name}</span>' in get(server, cookies["admin"], "/settings", headers)
    # a page without a header (a wizard step, the change-password page): the mark above the title
    assert '<div class="wrap">\n  <img class="loginlogo"' in get(server, cookies["admin"], "/setup/password", headers)
    # through the cloud the name stays Zorcam.cloud (the mark stays: it is the build's)
    app.REQUEST.cloud = True
    try:
        cloud = app.brand_html(lang, "appname")
    finally:
        app.REQUEST.cloud = False
    assert t(lang, "brand.cloud") in cloud and name not in cloud and logo in cloud


def test_the_stand_keeps_its_suffix_in_the_app_name(make_brand, monkeypatch):
    """test and stage: «Acme View test» under the icon and in the title, the manifest keeps the plain name (as for the core)."""
    monkeypatch.setattr(app, "ENVIRONMENT", "test")
    full_brand(make_brand)
    assert app.APP_NAME == "Acme View test" and 'content="Acme View test"' in app.APP_HEAD
    assert json.loads(app.MANIFEST)["name"] == "Acme View"
    app.apply_brand(None)
    assert app.APP_NAME == "Zorcam test"


def test_the_edition_row_has_the_version_of_the_build(server, cookies, make_brand, monkeypatch):  # noqa: F811
    monkeypatch.setattr(app.zorcam_config, "edition", lambda: "box")
    full_brand(make_brand)
    body = get(server, cookies["admin"], "/settings?tab=status")
    assert f'<div class="k">{t("en", "st.edition")}</div><div class="v ">Zorcam Box 0.1.0</div>' in body
    assert f'<div class="k">{t("en", "st.version")}</div><div class="v ">{app.VERSION}</div>' in body  # the core's version
    make_brand({"brand.json": json.dumps({"app_name": "Acme View"})})  # no version: the edition alone
    assert f'<div class="k">{t("en", "st.edition")}</div><div class="v ">Zorcam Box</div>' in get(
        server, cookies["admin"], "/settings?tab=status")


def test_icons_of_the_folder_replace_the_ones_of_the_core(server, cookies, make_brand):  # noqa: F811
    folder = full_brand(make_brand)
    for path, name in (("/icon-192.png", "icon-192.png"), ("/favicon.ico", "icon-192.png"), ("/icon-512.png", "icon-512.png"),
                       ("/apple-touch-icon.png", "apple-touch-icon.png"),
                       ("/apple-touch-icon-precomposed.png", "apple-touch-icon.png"),
                       ("/apple-touch-icon-180x180.png", "apple-touch-icon.png")):
        size = len((folder / name).read_bytes())
        status, headers, _ = request(server, "GET", path)
        assert status == 200 and headers["Content-Type"] == "image/png" and int(headers["Content-Length"]) == size, path
        head = request(server, "HEAD", path)
        assert head[0] == 200 and int(head[1]["Content-Length"]) == size, path
        assert app.ICON_FILES[path] == str(folder / name), path
    small, touch = (folder / "icon-192.png").read_bytes(), (folder / "apple-touch-icon.png").read_bytes()
    big = (folder / "icon-512.png").read_bytes()
    page = get(server, cookies["admin"], "/?tab=live")
    assert 'rel="apple-touch-icon" sizes="180x180" href="data:image/png;base64,' + base64.b64encode(touch).decode() in page
    assert f'<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png?v={sha(small)}">' in page
    manifest = json.loads(request(server, "GET", "/manifest.webmanifest")[2])
    assert [i["src"] for i in manifest["icons"]] == [f"/icon-192.png?v={sha(small)}", f"/icon-512.png?v={sha(big)}"]
    assert f'<img src="/apple-touch-icon.png?v={sha(touch)}" alt="">' in page  # the hint "add to the Home Screen"


def test_an_icon_that_is_not_a_png_is_skipped(server, make_brand):  # noqa: F811
    full_brand(make_brand, **{"icon-512.png": "not a picture"})
    assert any("icon-512.png is not a PNG" in line for line in make_brand.log)
    core = app.os.path.getsize(app.os.path.join(app.STATIC_DIR, "icon-512.png"))
    assert int(request(server, "GET", "/icon-512.png")[1]["Content-Length"]) == core  # the core's icon stays
    assert app.ICON_FILES["/icon-512.png"] == app.os.path.join(app.STATIC_DIR, "icon-512.png")
    assert request(server, "GET", "/icon-192.png")[0] == 200


def test_a_broken_folder_does_not_break_the_gallery(server, cookies, make_brand):  # noqa: F811
    """Not JSON, JSON that is not an object, fields of the wrong kind, a directory in place of a file: skipped one by
    one, with a line in the journal, and the pages stay what they were."""
    folder = make_brand({"brand.json": "{not json", "theme.css": THEME})
    assert any("brand.json" in line for line in make_brand.log) and app.BRAND.app_name == ""
    assert stylesheets(get(server, cookies["admin"], "/?tab=live"))[-1].startswith("/static/brand/theme.css")  # the rest works
    for body in ('["a"]', "5", "null", '{"name": 5, "app_name": "", "theme_color": "red", "version": ["1"]}',
                 '{"name": {"en": 5, "ru": ""}, "theme_color": "#12345", "app_name": "' + "x" * 200 + '"}'):
        make_brand({"brand.json": body})
        assert (app.BRAND.name, app.BRAND.app_name, app.BRAND.theme_color, app.BRAND.version) == ({}, "", "", ""), body
        main = get(server, cookies["admin"], "/?tab=live")
        assert "<span>Zorcam.nvr</span>" in main and '<meta name="theme-color" content="#f3f3f1">' in main, body
    (folder / "theme.css").unlink()
    (folder / "theme.css").mkdir()  # unreadable as a file
    (folder / "logo.svg").mkdir()
    app.apply_brand(brand.Brand(str(folder), log=make_brand.log.append))
    assert app.BRAND.assets == {} and any("cannot read" in line for line in make_brand.log)
    assert "brandlogo" not in get(server, cookies["admin"], "/?tab=live")
    assert brand.Brand(str(folder / "missing")).assets == {} and brand.Brand(None).icons == {}  # no folder at all


def test_one_name_for_every_language_and_the_fallback_to_english(tmp_path):
    def name_of(value, lang):
        (tmp_path / "brand.json").write_text(json.dumps({"name": value}), encoding="utf-8")
        return brand.Brand(str(tmp_path)).name_for(lang)

    assert name_of("Acme.view", "en") == name_of("Acme.view", "ru") == "Acme.view"  # one name for every language
    assert name_of({"en": "Acme.view"}, "ru") == "Acme.view"  # no Russian one: the English one
    assert name_of({"ru": RU_NAME}, "ru") == RU_NAME
    assert name_of({"ru": RU_NAME}, "en") == ""  # none: the core's name stays


def test_hostile_texts_of_brand_json_are_escaped(server, cookies, make_brand):  # noqa: F811
    evil = '<b>x</b>"\'&'
    make_brand({"brand.json": json.dumps({"name": evil, "app_name": evil, "version": evil})})
    main = get(server, cookies["admin"], "/?tab=live")
    assert "<b>x</b>" not in main and "&lt;b&gt;x&lt;/b&gt;" in main
    assert json.loads(app.MANIFEST)["name"] == evil  # in JSON it is a string, not markup


# ---------------------------------------------------------------- where the folder is, the serial number

def test_the_folder_is_next_to_the_program_and_can_be_moved_by_the_variable(tmp_path):
    import os
    import subprocess
    import sys
    code = "import zorcam_config, os; print(os.path.normpath(zorcam_config.BRAND_DIR))"
    run = lambda env: subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,  # noqa: E731
                                     env={"PATH": os.environ["PATH"], "PYTHONPATH": SRC + "/common", **env}).stdout.strip()
    assert run({}) == os.path.normpath(os.path.join(SRC, "brand"))  # /opt/zorcam-nvr/brand next to gallery/ and capture/
    assert run({"ZORCAM_BRAND_DIR": str(tmp_path)}) == str(tmp_path)


def test_serial_number_of_the_device(server, cookies, tmp_path, monkeypatch):  # noqa: F811
    assert zorcam_config.DEVICE_FILE == app.os.path.join(zorcam_config.CONFIG_DIR, "device.json")  # next to users.json
    f = tmp_path / "device.json"
    monkeypatch.setattr(zorcam_config, "DEVICE_FILE", str(f))
    assert zorcam_config.device_serial() == ""  # no file
    row = f'<div class="k">{t("en", "st.serial")}</div><div class="v ">SN-0001</div>'
    f.write_text(json.dumps({"serial": " SN-0001 "}))
    assert zorcam_config.device_serial() == "SN-0001"
    body = get(server, cookies["admin"], "/settings?tab=status")
    assert row in body and body.index(t("en", "st.edition")) < body.index(row) < body.index(t("en", "st.device"))
    assert t("ru", "st.serial") in get(server, cookies["admin"], "/settings?tab=status", RU)
    too_long, newline = "Z" * 33, "A\\nB"
    for bad in ("", "{", "[]", "null", '{"serial": 5}', '{"serial": ""}',
                f'{{"serial": "{too_long}"}}', f'{{"serial": "{newline}"}}'):
        f.write_text(bad)
        assert zorcam_config.device_serial() == "", bad
        assert t("en", "st.serial") not in get(server, cookies["admin"], "/settings?tab=status"), bad


def test_install_leaves_the_folder_alone_and_installs_the_module():
    import os
    from conftest import REPO
    with open(os.path.join(REPO, "app", "install.sh"), encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert any(line.startswith('copy "$SRC/gallery/brand.py"') and "RESTART_GALLERY=1" in line for line in lines)
    for line in lines:  # the folder belongs to the installer of the build: not copied, not removed, not changed here
        if not line.lstrip().startswith("#"):
            assert "/brand" not in line.replace("gallery/brand.py", ""), line


def test_status_bar_follows_the_theme_colour(make_brand):
    """A dark theme colour gives the iPhone a dark status bar in the app mode; the core's light one stays default."""
    assert 'status-bar-style" content="default"' in app.APP_HEAD
    make_brand({"brand.json": json.dumps({"theme_color": "#123456"})})
    assert 'status-bar-style" content="black"' in app.APP_HEAD
    make_brand({"brand.json": json.dumps({"theme_color": "#eeeeee"})})
    assert 'status-bar-style" content="default"' in app.APP_HEAD
