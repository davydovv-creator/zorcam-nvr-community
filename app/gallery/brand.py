"""The look of a build made on top of the core: an optional folder next to the program (/opt/zorcam-nvr/brand, see
zorcam_config.BRAND_DIR) that the installer of that build fills. There is nothing build-specific in the core: without
the folder (the free Zorcam NVR) the gallery looks as it always did. Read once, when the gallery starts, like static/.

    brand.json   {"name": {"en": …, "ru": …}, "app_name": …, "theme_color": "#rrggbb", "version": …}, every field optional:
                 name        the name in the header and the sidebar instead of Zorcam.nvr (not when viewed through the cloud)
                 app_name    the name under the Home Screen icon, in the page title and in the manifest
                 theme_color the colour of the browser bar and of the manifest
                 version     the version of the build, shown next to the edition in Settings -> Status
    theme.css    linked last on every page: the colours and shapes of the build
    logo.svg     the mark before the name in the header and the sidebar and above the sign-in form
    icon-192.png, icon-512.png, apple-touch-icon.png   replace the icons of static/

A missing file is skipped; a broken one (not JSON, not a PNG) is skipped with a line in the journal.
"""
import hashlib
import json
import os
import re

FILES = {"theme.css": "text/css; charset=utf-8", "logo.svg": "image/svg+xml"}  # served at /static/brand/<name>
ICONS = ("icon-192.png", "icon-512.png", "apple-touch-icon.png")
PNG = b"\x89PNG\r\n\x1a\n"
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
MAX_TEXT = 64  # a name or a version is a few words


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() and len(value.strip()) <= MAX_TEXT else ""


class Brand:
    """What the folder holds: names, colour and version (brand.json), the files for /static/brand/ with the hash of their
    content (assets: name -> (bytes, Content-Type, version)) and the paths of the icons that replace the core's."""

    def __init__(self, directory=None, log=print):
        self.name, self.app_name, self.theme_color, self.version = {}, "", "", ""
        self.assets, self.icons = {}, {}
        if not directory or not os.path.isdir(directory):
            return
        self._read_json(os.path.join(directory, "brand.json"), log)
        for name, ctype in FILES.items():
            body = self._read(os.path.join(directory, name), log)
            if body is not None:
                self.assets[name] = (body, ctype, hashlib.sha256(body).hexdigest()[:12])
        for name in ICONS:
            path = os.path.join(directory, name)
            head = self._read(path, log, limit=len(PNG))
            if head is not None and head.startswith(PNG):
                self.icons[name] = path
            elif head is not None:
                log(f"brand: {name} is not a PNG file, the icon of the program stays")

    @staticmethod
    def _read(path, log, limit=None):
        try:
            with open(path, "rb") as f:
                return f.read(limit) if limit else f.read()
        except FileNotFoundError:
            return None
        except OSError as e:
            log(f"brand: cannot read {path}: {e}")
            return None

    def _read_json(self, path, log):
        raw = self._read(path, log)
        if raw is None:
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:  # includes UnicodeDecodeError
            data = None
        if not isinstance(data, dict):
            log(f"brand: {path} is not a JSON object, ignored")
            return
        name = data.get("name")
        if isinstance(name, dict):  # {"en": …, "ru": …}
            self.name = {lang: text for lang, v in name.items() if (text := _text(v))}
        elif _text(name):
            self.name = {"": _text(name)}  # one name for every language
        self.app_name = _text(data.get("app_name"))
        color = data.get("theme_color")
        self.theme_color = color.lower() if isinstance(color, str) and COLOR.fullmatch(color) else ""
        self.version = _text(data.get("version"))

    def name_for(self, lang):
        """The name for the language of the page ("" — none: the core's name stays)."""
        return self.name.get(lang) or self.name.get("") or self.name.get("en") or ""

    def url(self, name):
        """/static/brand/<name>?v=<hash of the content> ("" — the folder has no such file)."""
        asset = self.assets.get(name)
        return f"/static/brand/{name}?v={asset[2]}" if asset else ""
