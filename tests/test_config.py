import json
import glob
import os
import pathlib
import re

import pytest

import zorcam_config
import zorcam_i18n as i18n
from conftest import REPO


@pytest.fixture
def conf():
    """Clean zorcam.conf and secrets.env in the temporary HOME for every test."""
    paths = [zorcam_config.CONFIG_FILE, zorcam_config.SECRETS_FILE, zorcam_config.CAMERAS_FILE]
    for p in paths:
        if os.path.exists(p):
            os.remove(p)
    yield [pathlib.Path(p) for p in paths]
    for p in paths:
        if os.path.exists(p):
            os.remove(p)


def test_example_file_is_up_to_date():
    """config/zorcam.conf.example is generated from PARAMS; regenerate it after changing PARAMS:
    python3 app/common/zorcam_config.py --example > config/zorcam.conf.example"""
    with open(os.path.join(REPO, "config", "zorcam.conf.example"), encoding="utf-8") as f:
        assert f.read() == zorcam_config.example()


def test_params_are_consistent():
    keys = [(s, k) for s, k, *_ in zorcam_config.PARAMS]
    assert len(keys) == len(set(keys)), "duplicate parameter in PARAMS"
    for s, k, default, typ, desc, who in zorcam_config.PARAMS:
        assert desc and who
        assert s in zorcam_config.SECTIONS
        assert (s, k) in i18n.PARAM_RU, f"no Russian description for [{s}] {k}"
        if typ is not list:
            assert isinstance(default, typ), (s, k)
    for key, choices in zorcam_config.CHOICES.items():
        assert key in keys
        default = next(d for s, k, d, *_ in zorcam_config.PARAMS if (s, k) == key)
        assert default in choices


def test_missing_file_gives_defaults(conf):
    cfg = zorcam_config.load()
    assert not cfg.exists and not cfg.errors
    assert cfg.get("gallery", "videos_per_page") == 30
    assert cfg.get("gallery", "environment") == "prod"


def test_values_errors_and_duplicate_sections(conf):
    path, *_ = conf
    path.write_text("[gallery]\nvideos_per_page = 60\nvideo_limit = 5\n"
                    "[capture]\nstall_timeout = abc\n"
                    "[gallery]\nsession_days = 50\nmax_grid = 120\nhorizons = 5, 10\nunknown = 1\n", encoding="utf-8")
    cfg = zorcam_config.load()
    assert cfg.get("gallery", "videos_per_page") == 60
    assert cfg.get("gallery", "session_days") == 50  # a repeated [gallery] does not break the file
    assert ("gallery", "video_limit") not in cfg.values  # an obsolete parameter is skipped silently
    assert any("stall_timeout" in str(e) for e in cfg.errors)
    assert any("unknown" in str(e) for e in cfg.errors)
    assert cfg.overridden == {("gallery", "videos_per_page"), ("gallery", "session_days")}
    assert not any("max_grid" in str(e) for e in cfg.errors)  # removed: every snapshot is shown
    assert not any("horizons" in str(e) for e in cfg.errors)  # removed with the snapshot grid


@pytest.mark.parametrize("section,key,raw,expected", [
    ("gallery", "videos_per_page", "10", 10),
    ("gallery", "videos_per_page", "45", None),
    ("gallery", "videos_per_page", "x", None),
    ("capture", "stall_timeout", "-1", None),
    ("language", "language", "ru", "ru"),
    ("language", "language", "de", None),
    ("gallery", "environment", "prod", "prod"),
    ("gallery", "environment", "stage", "stage"),
    ("gallery", "environment", "test", "test"),
    ("gallery", "environment", "dev", None),
    ("gallery", "environment", "TEST", None),
])
def test_validate(section, key, raw, expected):
    value, err = zorcam_config.validate(section, key, raw)
    assert value == expected
    assert (err is None) == (expected is not None)
    if err is not None:
        assert err.text("en") and err.text("ru") and err.text("en") != err.text("ru")


def test_save_roundtrip(conf):
    values = dict(zorcam_config.load().values)
    values[("gallery", "videos_per_page")] = 10
    zorcam_config.save_config(values)
    cfg = zorcam_config.load()
    assert not cfg.errors
    assert cfg.values == values


def test_rtsp_url_encodes_password(conf):
    _path, secrets_path, _cams = conf
    cam = {"id": "front-door", "rtsp_url": "rtsp://192.168.1.50:554/live", "rtsp_user": "cam"}
    zorcam_config.set_camera_password("front-door", "p@ss:w/rd")
    assert oct(secrets_path.stat().st_mode & 0o777) == "0o600"
    assert "ZORCAM_CAMERA_PASSWORD_FRONT_DOOR=p@ss:w/rd" in secrets_path.read_text(encoding="utf-8")
    assert zorcam_config.rtsp_url(cam) == "rtsp://cam:p%40ss%3Aw%2Frd@192.168.1.50:554/live"
    assert zorcam_config.rtsp_url({**cam, "rtsp_user": ""}) == "rtsp://192.168.1.50:554/live"
    # a login without a password: no authentication
    assert zorcam_config.rtsp_url({**cam, "id": "yard"}) == "rtsp://192.168.1.50:554/live"
    assert zorcam_config.rtsp_url(cam, "") == "rtsp://192.168.1.50:554/live"


def test_write_secret_keeps_other_lines(conf):
    _, secrets_path, _ = conf
    secrets_path.write_text("# comment\nOTHER=1\nZORCAM_CAMERA_PASSWORD_A=old\n", encoding="utf-8")
    zorcam_config.write_secret("ZORCAM_CAMERA_PASSWORD_A", "new")
    assert secrets_path.read_text(encoding="utf-8") == "# comment\nOTHER=1\nZORCAM_CAMERA_PASSWORD_A=new\n"


def test_unchanged_files_are_not_rewritten(conf):
    """Every write of zorcam.conf, cameras.json or secrets.env restarts go2rtc and the detector (Live drops for
    a few seconds): saving what is already there writes nothing (a new file would be a new inode)."""
    path, secrets_path, cams_path = conf
    zorcam_config.save_config(dict(zorcam_config.load().values))
    zorcam_config.save_cameras([CAMERA])
    zorcam_config.set_camera_password("front-door", "secret")
    files = (path, secrets_path, cams_path)
    before = [(p.stat().st_ino, p.stat().st_mtime_ns) for p in files]
    zorcam_config.save_config(dict(zorcam_config.load().values))
    zorcam_config.save_cameras(zorcam_config.load_cameras())
    zorcam_config.set_camera_password("front-door", "secret")
    zorcam_config.set_camera_password("yard", "")  # no password and none to remove
    assert [(p.stat().st_ino, p.stat().st_mtime_ns) for p in files] == before
    secrets_path.chmod(0o644)
    zorcam_config.set_camera_password("front-door", "secret")
    assert oct(secrets_path.stat().st_mode & 0o777) == "0o600" and secrets_path.stat().st_ino == before[1][0]
    zorcam_config.set_camera_password("front-door", "changed")
    assert secrets_path.stat().st_ino != before[1][0] and "=changed" in secrets_path.read_text(encoding="utf-8")


def test_errors_survive_the_capture_status_file(conf):
    """zorcam-nvr-capture writes its config errors as JSON; the gallery shows them translated, like its own."""
    path, *_ = conf
    path.write_text("[capture]\nstall_timeout = abc\n", encoding="utf-8")
    err = zorcam_config.load().errors[0]
    back = i18n.Msg.from_dict(json.loads(json.dumps(err.to_dict())))
    assert back.text("ru") == err.text("ru") and back.text("en") == err.text("en")


def test_an_old_external_ip_is_no_error(conf):
    """[live] public_ip of older versions: ignored, no error (the address comes by itself)."""
    path, *_ = conf
    path.write_text("[live]\npublic_ip = 203.0.113.10\n", encoding="utf-8")
    cfg = zorcam_config.load()
    assert not cfg.errors and ("live", "public_ip") not in cfg.values


def test_go2rtc_finds_its_public_address_itself():
    with open(os.path.join(REPO, "app", "go2rtc", "zorcam-nvr-go2rtc.service"), encoding="utf-8") as f:
        unit = f.read()
    assert "go2rtc-env" not in unit and "EnvironmentFile" not in unit
    assert "secrets.env" not in unit  # go2rtc needs no secrets: the camera URL comes from --rtsp-source
    with open(os.path.join(REPO, "app", "go2rtc", "go2rtc.yaml"), encoding="utf-8") as f:
        text = f.read()
    assert "\n  candidates:" not in text  # the gallery adds the STUN address itself (stun.py)
    assert "\n  ice_servers: []" in text  # no STUN gathering: it held every Live start while a STUN server was silent
    with open(os.path.join(REPO, "app", "go2rtc", "zorcam-nvr-go2rtc-config.path"), encoding="utf-8") as f:
        text = f.read()
    for name in ("zorcam.conf", "cameras.json", "secrets.env"):
        assert f"PathChanged=/var/lib/zorcam-nvr/config/{name}" in text


CAMERA = {"id": "front-door", "name": "Front door", "rtsp_url": "rtsp://192.168.1.50:554/live/0/MAIN",
          "rtsp_user": "admin"}


@pytest.mark.parametrize("raw, ok", [("camera", True), ("hallway-2", True), ("cam_1", True), ("", False),
                                     ("../x", False), ("a b", False), ("-x", False), ("cam_videos", False),
                                     ("x" * 33, False)])
def test_camera_id_format(raw, ok):
    cam, errors = zorcam_config.validate_camera({**CAMERA, "id": raw})
    assert ("id" not in errors) == ok and (cam.get("id") == raw if ok else "id" not in cam)


@pytest.mark.parametrize("key, raw", [("name", ""), ("name", "x" * 41), ("rtsp_url", "http://x/"),
                                      ("rtsp_url", "rtsp://admin:pass@10.0.0.2/live"), ("rtsp_url", "rtsp://"),
                                      ("clip_seconds", "1"), ("clip_seconds", "121"), ("clip_seconds", "2.5"),
                                      ("videos_days", "0"), ("frames_days", "366"), ("save_videos", "maybe"),
                                      ("frames_interval", "0"),
                                      ("frames_interval", "3601")])
def test_camera_fields_are_checked(key, raw):
    cam, errors = zorcam_config.validate_camera({**CAMERA, key: raw})
    assert list(errors) == [key]
    assert errors[key].text("en") != errors[key].text("ru")


def test_every_camera_field_has_a_form_label():
    for key, *_ in zorcam_config.CAMERA_FIELDS:
        assert f"cam.label.{key}" in i18n.STRINGS, key


def test_camera_recording_defaults_and_old_global_params():
    cam, errors = zorcam_config.validate_camera(CAMERA)
    assert not errors and (cam["save_videos"], cam["clip_seconds"], cam["videos_days"]) == (True, 10, 5)
    assert (cam["save_frames"], cam["frames_interval"], cam["frames_days"]) == (True, 5, 10)
    # the clips of older versions from keyframes (record_mode, video_height): ignored, no error, gone when saved
    old, errors = zorcam_config.validate_camera({**CAMERA, "record_mode": "keyframes", "video_height": "720"})
    assert not errors and "record_mode" not in old and "video_height" not in old
    assert zorcam_config.validate_camera({**CAMERA, "clip_seconds": "2", "frames_days": "365"})[0]["frames_days"] == 365


def test_the_old_global_recording_params_are_skipped(conf):
    path, *_ = conf
    path.write_text("[capture]\nclip_seconds = 10\nframe_interval = 5\n[storage]\nframes_minutes = 60\nvideos_days = 5\n",
                    encoding="utf-8")
    assert not zorcam_config.load().errors


def test_camera_ids_must_have_different_password_keys():
    """front-door and front_door would share ZORCAM_CAMERA_PASSWORD_FRONT_DOOR."""
    assert zorcam_config.validate_camera(CAMERA, ["front_door"])[1]["id"].key == "cfg.camera_id_taken"
    assert not zorcam_config.validate_camera(CAMERA, ["back"])[1]


def test_cameras_file_roundtrip_and_broken_entries(conf):
    *_, cams_path = conf
    cam, _ = zorcam_config.validate_camera(CAMERA)
    zorcam_config.save_cameras([cam, {**cam, "id": "yard", "name": "Yard"}])
    assert [c["id"] for c in zorcam_config.load_cameras()] == ["front-door", "yard"]
    data = json.loads(cams_path.read_text(encoding="utf-8"))
    data["cameras"] += [{"id": "../x", "name": "bad"}, {"id": "yard", "name": "twice"}, "junk"]
    cams_path.write_text(json.dumps(data), encoding="utf-8")
    assert [c["id"] for c in zorcam_config.load_cameras()] == ["front-door", "yard"]
    assert "password" not in cams_path.read_text(encoding="utf-8")


def test_a_switched_off_camera_has_no_go2rtc_stream(conf, tmp_path):
    out = tmp_path / "streams.yaml"
    on, _ = zorcam_config.validate_camera(CAMERA)
    off, errors = zorcam_config.validate_camera({**CAMERA, "id": "yard", "enabled": "no"})
    assert not errors and on["enabled"] and not off["enabled"]
    zorcam_config.save_cameras([on, off])
    zorcam_config.go2rtc_streams(str(out))
    assert '"front-door"' in out.read_text() and '"yard"' not in out.read_text()
    assert [c["id"] for c in zorcam_config.load_cameras()] == ["front-door", "yard"]  # still a camera, with recordings


def test_go2rtc_streams(conf, tmp_path):
    out = tmp_path / "streams.yaml"
    zorcam_config.go2rtc_streams(str(out))
    assert "streams: {}" in out.read_text()
    cam, _ = zorcam_config.validate_camera(CAMERA)
    zorcam_config.save_cameras([cam])
    zorcam_config.set_camera_password("front-door", "secret")
    zorcam_config.go2rtc_streams(str(out))
    text = out.read_text()
    assert '"front-door": echo:python3 ' in text and "--rtsp-source front-door" in text
    assert "secret" not in text  # the password is built into the URL only when go2rtc connects
    assert ".sub" not in text  # no second stream set

    cam, errors = zorcam_config.validate_camera({**CAMERA, "rtsp_sub_url": "rtsp://192.168.1.60:554/live/0/SUB"})
    assert not errors
    zorcam_config.save_cameras([cam])
    zorcam_config.go2rtc_streams(str(out))
    text = out.read_text()
    assert '"front-door.sub": echo:python3 ' in text and "--rtsp-source front-door sub" in text
    assert zorcam_config.rtsp_url(cam, sub=True).endswith("@192.168.1.60:554/live/0/SUB") and "secret" in \
        zorcam_config.rtsp_url(cam, sub=True)
    assert zorcam_config.local_rtsp_url("front-door", sub=True) == "rtsp://127.0.0.1:8554/front-door.sub"
    _, errors = zorcam_config.validate_camera({**CAMERA, "rtsp_sub_url": "http://x/sub"})
    assert "rtsp_sub_url" in errors


@pytest.mark.parametrize("unit", ["capture/zorcam-nvr-capture.service", "gallery/zorcam-nvr-gallery.service",
                                  "go2rtc/zorcam-nvr-go2rtc.service"])
def test_units_limit_memory_and_cpu(unit):
    """A runaway service must be restarted by systemd, not freeze the Pi 3 (1 GB RAM) together with SSH."""
    with open(os.path.join(REPO, "app", unit), encoding="utf-8") as f:
        text = f.read()
    for key in ("MemoryHigh=", "MemoryMax=", "MemorySwapMax=0", "CPUQuota=", "CPUWeight="):
        assert key in text, (unit, key)


@pytest.mark.parametrize("unit,percent", [("capture/zorcam-nvr-capture.service", True),
                                          ("go2rtc/zorcam-nvr-go2rtc.service", True),
                                          ("gallery/zorcam-nvr-gallery.service", False),
                                          ("cloud/zorcam-nvr-cloud.service", False)])
def test_units_memory_limits_percent_or_megabytes(unit, percent):
    """capture and go2rtc scale with the memory of the device (percents); the rest have fixed limits (M)."""
    with open(os.path.join(REPO, "app", unit), encoding="utf-8") as f:
        text = f.read()
    for key in ("MemoryHigh", "MemoryMax"):
        value = re.search(rf"^{key}=(\S+)$", text, re.M).group(1)
        assert re.fullmatch(r"\d+%" if percent else r"\d+M", value), (unit, key, value)


@pytest.mark.parametrize("unit", ["capture/zorcam-nvr-capture.service", "gallery/zorcam-nvr-gallery.service",
                                  "go2rtc/zorcam-nvr-go2rtc.service"])
def test_units_keep_the_camera_passwords_out_of_the_environment(unit):
    """The code reads secrets.env itself; loaded into the environment, the passwords reached every child process."""
    with open(os.path.join(REPO, "app", unit), encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert not [line for line in lines if line.startswith("EnvironmentFile=") and "secrets.env" in line]


def _fake_camera(user, password, scheme="digest"):
    """A tiny RTSP server that answers DESCRIBE like a camera: 401 with a challenge, 200 with the right password."""
    import hashlib
    import socketserver
    import threading
    import base64

    realm, nonce = "cam", "abc123"
    md5 = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731

    class H(socketserver.StreamRequestHandler):
        def handle(self):
            while True:
                lines = []
                while True:
                    line = self.rfile.readline().decode()
                    if not line:
                        return
                    if line == "\r\n":
                        break
                    lines.append(line.strip())
                method, uri, _ = lines[0].split(" ")
                hdr = {k.lower(): v.strip() for k, _, v in (x.partition(":") for x in lines[1:])}
                auth, ok = hdr.get("authorization", ""), False
                if scheme == "digest" and auth.startswith("Digest"):
                    f = dict(__import__("re").findall(r'(\w+)="([^"]*)"', auth))
                    want = md5(f"{md5(f'{user}:{realm}:{password}')}:{nonce}:{md5(f'{method}:{uri}')}")
                    ok = f.get("response") == want
                elif scheme == "basic" and auth.startswith("Basic"):
                    ok = base64.b64decode(auth[6:]).decode() == f"{user}:{password}"
                cseq = hdr.get("cseq", "1")
                if ok:
                    sdp = "v=0\r\n"
                    self.wfile.write(f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\nContent-Length: {len(sdp)}\r\n\r\n{sdp}".encode())
                else:
                    ch = f'Digest realm="{realm}", nonce="{nonce}"' if scheme == "digest" else f'Basic realm="{realm}"'
                    self.wfile.write(f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\nWWW-Authenticate: {ch}\r\n\r\n".encode())

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("scheme", ["digest", "basic"])
def test_probe_camera_reports_the_camera_answer(scheme):
    srv = _fake_camera("cam", "p@ss:1", scheme)
    port = srv.server_address[1]
    try:
        assert zorcam_config.probe_camera(f"rtsp://cam:p%40ss%3A1@127.0.0.1:{port}/live/0/MAIN", 3) is None
        assert zorcam_config.probe_camera(f"rtsp://cam:wrong@127.0.0.1:{port}/live/0/MAIN", 3) == "401 Unauthorized"
        assert zorcam_config.probe_camera(f"rtsp://127.0.0.1:{port}/live", 3) == "401 Unauthorized"
    finally:
        srv.shutdown()
        srv.server_close()
    assert zorcam_config.probe_camera(f"rtsp://127.0.0.1:{port}/live", 3) == "Connection refused"


def _fake_service(reply):
    """A TCP service on the camera port that answers every connection with reply() (bytes, sent until it is done)."""
    import socketserver
    import threading

    class H(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(4096)
            try:
                for chunk in reply():
                    self.request.sendall(chunk)
            except OSError:
                pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.parametrize("reply, expected", [
    (lambda: iter(lambda: b"x" * 4096, None), "not an RTSP answer"),  # endless, no RTSP header: not read forever
    (lambda: [b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: abc\r\n\r\n"], None),  # a broken length: ignored
], ids=["endless", "broken length"])
def test_probe_camera_survives_odd_answers(reply, expected):
    import time
    srv = _fake_service(reply)
    try:
        start = time.monotonic()
        assert zorcam_config.probe_camera(f"rtsp://127.0.0.1:{srv.server_address[1]}/live", 3) == expected
        assert time.monotonic() - start < 3
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_detector_reads_the_camera_without_delay():
    """Measured over go2rtc: stream analysis on connect keeps ffmpeg ~1.4 s behind the camera for good, frame threads
    of the decoder add 4 frames more; the detector turns both off."""
    with open(os.path.join(REPO, "app", "capture", "daemon.py"), encoding="utf-8") as f:
        src = f.read()
    assert '"-probesize", "32", "-analyzeduration", "0", "-flags", "low_delay",' in src
    detector = src[src.index("def detector_cmd"):]
    assert detector.index('"-flags", "low_delay"') < detector.index('"-i", source')  # input options: before -i


def test_ffmpeg_never_gets_the_camera_password():
    """ffmpeg command lines are visible to everyone in ps: the detector and HLS read the camera through go2rtc."""
    for path in ("capture/daemon.py", "gallery/app.py"):
        with open(os.path.join(REPO, "app", path), encoding="utf-8") as f:
            src = f.read()
        assert '"-i", RTSP_URL' not in src and "local_rtsp_url" in src, path
    with open(os.path.join(REPO, "app", "go2rtc", "go2rtc.yaml"), encoding="utf-8") as f:
        assert 'listen: "127.0.0.1:8554"' in f.read()
    assert zorcam_config.local_rtsp_url("yard") == "rtsp://127.0.0.1:8554/yard"


def test_services_run_as_their_own_user():
    """The services run as the system user zorcam-nvr from /opt/zorcam-nvr (read-only for it), nothing in /home/admin."""
    for unit in ("capture/zorcam-nvr-capture.service", "gallery/zorcam-nvr-gallery.service", "go2rtc/zorcam-nvr-go2rtc.service"):
        with open(os.path.join(REPO, "app", unit), encoding="utf-8") as f:
            text = f.read()
        assert "\nUser=zorcam-nvr\nGroup=zorcam-nvr\n" in text and "NoNewPrivileges=yes" in text, unit
        assert "ProtectHome=yes" in text and "ProtectSystem=full" in text, unit
        assert "/home/" not in text and "/opt/zorcam-nvr/" in text, unit


def test_services_have_no_device_files():
    """PrivateDevices: a process of the service sees only /dev/null, /dev/urandom and the like (and /dev/shm), no disks.
    Not zorcam-nvr-storage: it is root and works with the disks."""
    for unit in ("capture/zorcam-nvr-capture.service", "gallery/zorcam-nvr-gallery.service",
                 "go2rtc/zorcam-nvr-go2rtc.service", "cloud/zorcam-nvr-cloud.service"):
        with open(os.path.join(REPO, "app", unit), encoding="utf-8") as f:
            assert "\nPrivateDevices=yes\n" in f.read(), unit
    for unit in glob.glob(os.path.join(REPO, "app", "storage", "*.service")):
        with open(unit, encoding="utf-8") as f:
            assert "PrivateDevices" not in f.read(), unit


def test_edition(tmp_path):
    """box only on the Zorcam Box image (a file next to the program, read-only for the services), community otherwise."""
    assert zorcam_config.edition(str(tmp_path / "missing")) == "community"
    f = tmp_path / "EDITION"
    f.write_text("box\n")
    assert zorcam_config.edition(str(f)) == "box"
    f.write_text("premium")  # anything else is the free edition
    assert zorcam_config.edition(str(f)) == "community"


def test_install_writes_the_edition_and_checks_it():
    with open(os.path.join(REPO, "app", "install.sh"), encoding="utf-8") as fh:
        text = fh.read()
    assert 'EDITION="${ZORCAM_EDITION:-$(cat "$PREFIX/EDITION"' in text  # an update without the variable keeps it
    assert "community|box) ;;" in text and 'copy "$EDITION_TMP" "$PREFIX/EDITION" 644' in text


def test_go2rtc_takes_the_sound_only_when_it_is_on(conf):
    """--rtsp-source (go2rtc echo:): video only, unless the camera's "Sound in Live" is on — then the main stream
    as it is, with its sound; the second stream (the detector's) is always video only. --camera-ids --enabled:
    only the switched-on cameras (install.sh checks those)."""
    import subprocess
    import sys
    script = zorcam_config.__file__

    def source(*args):
        return subprocess.run([sys.executable, script, "--rtsp-source", *args], capture_output=True, text=True,
                              check=True).stdout.strip()
    sub = "rtsp://192.168.1.60:554/live/0/SUB"
    cam, _ = zorcam_config.validate_camera({**CAMERA, "rtsp_sub_url": sub})
    assert cam["audio"] is False
    zorcam_config.save_cameras([cam])
    assert source("front-door").endswith("/live/0/MAIN#media=video")
    zorcam_config.save_cameras([{**cam, "audio": True}, {**cam, "id": "yard", "enabled": False}])
    assert source("front-door").endswith("/live/0/MAIN") and source("front-door", "sub").endswith("SUB#media=video")
    ids = subprocess.run([sys.executable, script, "--camera-ids", "--enabled"], capture_output=True, text=True,
                         check=True).stdout.split()
    assert ids == ["front-door"]


def test_snapshot_names_with_and_without_the_motion_mark():
    """<time>_<ms>.jpg, and <time>_<ms>_m.jpg for a snapshot after motion: one function writes and one reads them."""
    from datetime import datetime
    now = datetime(2026, 10, 11, 9, 5, 7, 42_999)
    assert zorcam_config.frame_name(now) == "20261011_090507_042"
    assert zorcam_config.frame_name(now, motion=True) == "20261011_090507_042_m"
    assert zorcam_config.parse_frame_name("20261011_090507_042.jpg") == (datetime(2026, 10, 11, 9, 5, 7, 42_000), False)
    assert zorcam_config.parse_frame_name("20261011_090507_042_m.jpg") == (datetime(2026, 10, 11, 9, 5, 7, 42_000), True)
    for bad in ("", "notes.jpg", "20261011_090507_042", "20261011_090507_042_x.jpg", "20261011_090507_042_m.jpg.tmp",
                "20261311_090507_042.jpg", "20261011_250507_042.jpg", "20261011_090507.jpg", "x20261011_090507_042.jpg"):
        assert zorcam_config.parse_frame_name(bad) is None, bad


def test_motion_zones_file(tmp_path):
    path = str(tmp_path / "motion_zones.json")
    assert zorcam_config.load_zones(path) == {}  # no file
    cells = "1" + "0" * 143
    zorcam_config.save_zone("front", "exclude", cells, path)
    zorcam_config.save_zone("yard", "include", "0" * 143 + "1", path)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert zorcam_config.load_zones(path) == {"front": {"mode": "exclude", "cells": cells},
                                              "yard": {"mode": "include", "cells": "0" * 143 + "1"}}
    mtime = os.stat(path).st_mtime_ns
    zorcam_config.save_zone("front", "exclude", cells, path)  # unchanged: not rewritten (the detector checks the mtime)
    zorcam_config.save_zone("nope", "exclude", None, path)
    assert os.stat(path).st_mtime_ns == mtime
    zorcam_config.save_zone("front", "exclude", "0" * 144, path)  # nothing painted: no zones
    assert list(zorcam_config.load_zones(path)) == ["yard"]
    with pytest.raises(ValueError):
        zorcam_config.save_zone("front", "exclude", "1" * 10, path)


def test_motion_zones_broken_entries_mean_no_zones(tmp_path):
    path = tmp_path / "motion_zones.json"
    good = {"mode": "exclude", "cells": "1" * 144}
    for text in ("not json", "[]", "null"):
        path.write_text(text)
        assert zorcam_config.load_zones(str(path)) == {}
    path.write_text(json.dumps({"a": good, "b": {"mode": "all", "cells": "1" * 144}, "c": {"mode": "exclude", "cells": "1" * 143},
                                "d": {"mode": "include", "cells": "x" * 144}, "e": "1" * 144, "f": {"mode": "include"}}))
    assert zorcam_config.load_zones(str(path)) == {"a": good}


def test_zone_cell_order_is_row_by_row():
    assert zorcam_config.valid_zone("exclude", "0" * 144) and not zorcam_config.valid_zone("exclude", "0" * 145)
    assert (zorcam_config.ZONE_COLS, zorcam_config.ZONE_ROWS) == (16, 9)


def test_internal_token_is_created_once_and_private(tmp_path):
    """The detector's secret for /internal/motion: created by the gallery (create=True), 600, never replaced while it is
    there; the detector only reads it (None when the gallery has not made it yet)."""
    path = str(tmp_path / "config" / "internal_token")
    assert zorcam_config.internal_token(path=path) is None and not os.path.exists(path)
    token = zorcam_config.internal_token(create=True, path=path)
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert zorcam_config.internal_token(path=path) == token
    assert zorcam_config.internal_token(create=True, path=path) == token  # the next start keeps it
    assert os.listdir(os.path.dirname(path)) == ["internal_token"]  # no temporary files left
    with open(path, "w") as f:  # an empty file is no secret
        f.write("\n")
    again = zorcam_config.internal_token(create=True, path=path)
    assert re.fullmatch(r"[0-9a-f]{64}", again) and again != token


def test_services_are_hardened():
    """Units of the capture, gallery, go2rtc and cloud services: no kernel settings, modules, logs or cgroups to change,
    no new SUID, realtime, namespaces or foreign system call architectures, only the address families they use, no
    capabilities (the gallery keeps the one for port 80), files private to the group (umask 027). Not
    zorcam-nvr-storage: it is root and formats and mounts disks."""
    common = ["UMask=0027", "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes",
              "ProtectControlGroups=yes", "RestrictSUIDSGID=yes", "LockPersonality=yes", "RestrictRealtime=yes",
              "RestrictNamespaces=yes", "SystemCallArchitectures=native",
              "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK"]
    for unit in ("capture/zorcam-nvr-capture.service", "gallery/zorcam-nvr-gallery.service",
                 "go2rtc/zorcam-nvr-go2rtc.service", "cloud/zorcam-nvr-cloud.service"):
        with open(os.path.join(REPO, "app", unit), encoding="utf-8") as f:
            lines = f.read().splitlines()
        for want in common:
            assert want in lines, (unit, want)
        caps = "CapabilityBoundingSet=CAP_NET_BIND_SERVICE" if "gallery" in unit else "CapabilityBoundingSet="
        assert caps in lines, unit
    for unit in glob.glob(os.path.join(REPO, "app", "storage", "*.service")):
        with open(unit, encoding="utf-8") as f:
            text = f.read()
        assert "RestrictAddressFamilies" not in text and "CapabilityBoundingSet" not in text, unit


def test_the_tunnel_has_its_own_port_in_the_gallery_and_frpc():
    """frpc connects to the gallery's tunnel port, never to port 80 (the home network and any reverse proxy)."""
    import zorcam_cloud
    assert zorcam_cloud.TUNNEL_PORT not in (80, 8555, 1984, 8554)
    conf = zorcam_cloud.frpc_config({"frps_addr": "c.example.org", "device_id": "dev1", "token": "t", "subdomain": "dev1"})
    assert f"localPort = {zorcam_cloud.TUNNEL_PORT}\n" in conf and 'localIP = "127.0.0.1"' in conf
    with open(os.path.join(REPO, "app", "install.sh"), encoding="utf-8") as f:
        install = f.read()
    # an update that moves the tunnel restarts the cloud service, which writes the new frpc settings and starts frpc
    assert re.search(r'copy "\$SRC/common/zorcam_cloud\.py"\s+"\$PREFIX/cloud/zorcam_cloud\.py"\s+644 && RESTART_CLOUD=1',
                     install)
    assert f"127\\.0\\.0\\.1:{zorcam_cloud.TUNNEL_PORT} " in install  # the check at the end of the installation


def test_stun_servers_parameter(conf):
    """[live] stun_servers: host[:port] separated by commas, empty by default (the built-in order)."""
    assert zorcam_config.load().get("live", "stun_servers") == ""
    for raw, ok in (("", True), ("stun.example.org", True), ("stun.example.org:19302", True), ("192.0.2.7:3478", True),
                    ("a.example, b.example:1", True), ("a.example:65535", True),
                    ("a.example:0", False), ("a.example:65536", False), ("a.example:", False), ("a.example,", False),
                    ("stun://a.example", False), ("a b", False), ("-a.example", False), ("a.example:12x", False)):
        assert (zorcam_config.validate("live", "stun_servers", raw)[1] is None) == ok, raw
    assert i18n.PARAM_RU[("live", "stun_servers")]
    assert "# stun_servers =\n" in zorcam_config.example()


def test_trusted_proxies_parameter(conf):
    """[gallery] trusted_proxies: IP addresses separated by commas, empty by default (nobody is trusted)."""
    assert zorcam_config.load().get("gallery", "trusted_proxies") == ""
    for raw, ok in (("", True), ("127.0.0.1", True), ("127.0.0.1, 10.0.0.2", True), ("::1,fe80::1", True),
                    ("nginx", False), ("127.0.0.1; evil", False), ("127.0.0.1,", False)):
        assert (zorcam_config.validate("gallery", "trusted_proxies", raw)[1] is None) == ok, raw
    assert i18n.PARAM_RU[("gallery", "trusted_proxies")]
    assert "# trusted_proxies =\n" in zorcam_config.example()  # no trailing space in the example


ROUTE_HEAD = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"


def route(iface, metric=100, dest="00000000", mask="00000000", flags="0003"):
    return f"{iface}\t{dest}\t0102A8C0\t{flags}\t0\t0\t{metric}\t{mask}\t0\t0\t0\n"


def fake_net(tmp_path, routes=None, ipv6=None, ifaces=None, rfkill=(), bluetooth=()):
    """A fake /proc and /sys in tmp_path. ifaces: {name: "wired" | "wifi" | "wext" | "virtual"} (a wired one has device/,
    a wifi one phy80211, a "wext" one wireless/); rfkill: [(type, soft, hard)]; bluetooth: names in /sys/class/bluetooth."""
    proc, sysfs = tmp_path / "proc", tmp_path / "sys"
    (proc / "net").mkdir(parents=True)
    if routes is not None:
        (proc / "net" / "route").write_text(ROUTE_HEAD + "".join(routes))
    if ipv6 is not None:
        (proc / "net" / "ipv6_route").write_text("".join(ipv6))
    for name, kind in (ifaces or {}).items():
        base = sysfs / "class" / "net" / name
        base.mkdir(parents=True)
        if kind == "wifi":
            (base / "phy80211").mkdir()
        elif kind == "wext":
            (base / "wireless").mkdir()
        if kind in ("wired", "wifi", "wext"):
            (base / "device").mkdir()
    for i, (kind, soft, hard) in enumerate(rfkill):
        base = sysfs / "class" / "rfkill" / f"rfkill{i}"
        base.mkdir(parents=True)
        (base / "type").write_text(kind + "\n")
        (base / "soft").write_text(f"{soft}\n")
        (base / "hard").write_text(f"{hard}\n")
    for name in bluetooth:
        (sysfs / "class" / "bluetooth" / name).mkdir(parents=True)
    return str(proc), str(sysfs)


def state(tmp_path, **kw):
    return zorcam_config.network_state(*fake_net(tmp_path, **kw))


def test_network_state_on_a_cable_with_the_radios_off(tmp_path):
    assert state(tmp_path, routes=[route("eth0")], ifaces={"eth0": "wired", "lo": "virtual"}) == \
        {"via": "ethernet", "wifi": False, "bluetooth": False}


def test_network_state_on_wifi(tmp_path):
    """The default route through a wireless interface (found by phy80211, or by wireless/ on older drivers)."""
    for kind in ("wifi", "wext"):
        assert state(tmp_path / kind, routes=[route("wlan0")], ifaces={"wlan0": kind}) == \
            {"via": "wifi", "wifi": True, "bluetooth": False}


def test_network_state_cable_with_the_radios_on(tmp_path):
    """A cable, but wlan0 has no route: Wi-Fi is on; hci0 - Bluetooth is on; hci0:256 (a connection) is not an adapter."""
    ifaces = {"eth0": "wired", "wlan0": "wifi"}
    assert state(tmp_path / "a", routes=[route("eth0")], ifaces=ifaces) == {"via": "ethernet", "wifi": True, "bluetooth": False}
    assert state(tmp_path / "b", routes=[route("eth0")], ifaces={"eth0": "wired"}, bluetooth=["hci0"]) == \
        {"via": "ethernet", "wifi": False, "bluetooth": True}
    assert state(tmp_path / "c", routes=[route("eth0")], ifaces={"eth0": "wired"}, bluetooth=["hci0:256"])["bluetooth"] is False


def test_network_state_radio_blocked_by_rfkill(tmp_path):
    """A soft or hard block turns the radio off; only the blocks of its own type count; of several adapters one that
    is not blocked is enough."""
    base = {"routes": [route("eth0")], "ifaces": {"eth0": "wired", "wlan0": "wifi"}, "bluetooth": ["hci0"]}
    on = state(tmp_path / "on", **base, rfkill=[("wlan", 0, 0), ("bluetooth", 0, 0)])
    assert (on["wifi"], on["bluetooth"]) == (True, True)
    soft = state(tmp_path / "soft", **base, rfkill=[("wlan", 1, 0), ("bluetooth", 1, 0)])
    assert (soft["wifi"], soft["bluetooth"]) == (False, False)
    hard = state(tmp_path / "hard", **base, rfkill=[("wlan", 0, 1), ("bluetooth", 0, 1)])
    assert (hard["wifi"], hard["bluetooth"]) == (False, False)
    mixed = state(tmp_path / "mixed", **base, rfkill=[("wlan", 1, 0), ("bluetooth", 0, 0)])  # Wi-Fi off, Bluetooth on
    assert (mixed["wifi"], mixed["bluetooth"]) == (False, True)
    two = state(tmp_path / "two", **{**base, "ifaces": {"eth0": "wired", "wlan0": "wifi", "wlan1": "wifi"}},
                rfkill=[("wlan", 1, 0), ("wlan", 0, 0)])
    assert two["wifi"] is True
    unreadable = state(tmp_path / "bad", **base, rfkill=[("wlan", "?", "?")])  # not "1": not blocked
    assert unreadable["wifi"] is True


def test_network_state_without_modules_and_without_a_route(tmp_path):
    """No radio modules (or off in the firmware: no interface at all) - both off; no default route - via None."""
    assert state(tmp_path / "a", routes=[route("eth0")], ifaces={"eth0": "wired"}) == \
        {"via": "ethernet", "wifi": False, "bluetooth": False}
    assert state(tmp_path / "b", routes=[], ifaces={"eth0": "wired", "wlan0": "wifi"}) == \
        {"via": None, "wifi": True, "bluetooth": False}
    not_default = route("eth0", dest="0001A8C0", mask="00FFFFFF")  # a route to a network, not the default
    assert state(tmp_path / "c", routes=[not_default], ifaces={"eth0": "wired"})["via"] is None


def test_network_state_virtual_default_interface_is_unknown(tmp_path):
    """A VPN, a tunnel or a bridge has no device: neither a cable nor Wi-Fi; an unknown interface name too."""
    ifaces = {"eth0": "wired", "wg0": "virtual", "tun0": "virtual", "br0": "virtual"}
    for name in ("wg0", "tun0", "br0", "ghost"):
        assert state(tmp_path / name, routes=[route(name, 0), route("eth0", 100)], ifaces=ifaces)["via"] is None
    for i, name in enumerate(("../net/eth0", "eth0/.", "..")):  # not an interface name: never a path to follow
        assert state(tmp_path / f"path{i}", routes=[route(name)], ifaces=ifaces)["via"] is None


def test_network_state_takes_the_route_with_the_lowest_metric(tmp_path):
    ifaces = {"eth0": "wired", "wlan0": "wifi"}
    assert state(tmp_path / "a", routes=[route("wlan0", 600), route("eth0", 100)], ifaces=ifaces)["via"] == "ethernet"
    assert state(tmp_path / "b", routes=[route("eth0", 600), route("wlan0", 100)], ifaces=ifaces)["via"] == "wifi"
    assert state(tmp_path / "c", routes=[route("eth0", 100), route("wlan0", 100)], ifaces=ifaces)["via"] == "ethernet"
    # a route that is down, or only half of the address space (a VPN that splits the default), is not the default
    down, half = route("wlan0", 1, flags="0002"), route("wlan0", 1, mask="80000000")
    assert state(tmp_path / "d", routes=[down, route("eth0", 100)], ifaces=ifaces)["via"] == "ethernet"
    assert state(tmp_path / "e", routes=[half, route("eth0", 100)], ifaces=ifaces)["via"] == "ethernet"


def test_network_state_ipv6_default_route_when_ipv4_has_none(tmp_path):
    def line(iface, metric, flags="00000003", dest="0" * 32, plen="00"):
        return f"{dest} {plen} {'0' * 32} 00 fe800000000000000a0027fffe000001 {metric:08x} 00000001 00000000 {flags} {iface}\n"
    ifaces = {"eth0": "wired", "wlan0": "wifi", "lo": "virtual"}
    v6 = [line("lo", 0xFFFFFFFF, flags="00200200"), line("wlan0", 1024), line("eth0", 100),
          line("eth0", 1, dest="2" + "0" * 31, plen="40")]  # the last one leads to a network, not everywhere
    assert state(tmp_path / "a", routes=[], ipv6=v6, ifaces=ifaces)["via"] == "ethernet"
    assert state(tmp_path / "b", routes=[], ipv6=[line("lo", 1, flags="00200203")], ifaces=ifaces)["via"] is None  # "unreachable"
    assert state(tmp_path / "c", routes=[route("wlan0", 600)], ipv6=v6, ifaces=ifaces)["via"] == "wifi"  # IPv4 first
    rejecting = [line("eth0", 1, flags="00000203"), line("wlan0", 1024)]  # a default that rejects is not a way out
    assert state(tmp_path / "e", routes=[], ipv6=rejecting, ifaces=ifaces)["via"] == "wifi"
    assert state(tmp_path / "d", routes=None, ipv6=v6[2:3], ifaces=ifaces)["via"] == "ethernet"  # no IPv4 file at all


def test_network_state_broken_files_are_not_determined(tmp_path):
    """Empty, short, binary or half-written files, missing folders: no exception, nothing to advise about."""
    nothing = {"via": None, "wifi": False, "bluetooth": False}
    assert zorcam_config.network_state(str(tmp_path / "no-proc"), str(tmp_path / "no-sys")) == nothing
    garbage = [ROUTE_HEAD, "\n", "eth0\n", "eth0\t00000000\n", "\x00\xff\xfe garbage \x00\n",
               "eth0\t00000000\tx\tzz\t0\t0\tq\t00000000\n", "eth0\t00000000\t0\t0003\t0\t0\t100\n"]
    for i, text in enumerate(garbage):
        proc, sysfs = fake_net(tmp_path / str(i), ifaces={"eth0": "wired"})
        (pathlib.Path(proc) / "net" / "route").write_bytes(text.encode("utf-8", "surrogatepass"))
        zeros = b"0" * 32
        v6 = b"\xff\xfe\n0000\n" + zeros + b" 00\n" + zeros + b" 00 x 0 y zz q 3 eth0\n"
        (pathlib.Path(proc) / "net" / "ipv6_route").write_bytes(v6)
        assert zorcam_config.network_state(proc, sysfs) == nothing, repr(text)
    proc, sysfs = fake_net(tmp_path / "files", ifaces={"eth0": "wired"}, rfkill=[("wlan", 0, 0)])
    (pathlib.Path(sysfs) / "class" / "bluetooth").write_text("not a folder")
    (pathlib.Path(sysfs) / "class" / "rfkill" / "rfkill0" / "soft").unlink()
    assert zorcam_config.network_state(proc, sysfs) == nothing


def test_network_state_of_this_machine_is_a_plain_dict():
    """The real /proc and /sys: whatever this machine is, a dict of the three keys and no exception."""
    result = zorcam_config.network_state()
    assert set(result) == {"via", "wifi", "bluetooth"} and result["via"] in ("ethernet", "wifi", None)
    assert isinstance(result["wifi"], bool) and isinstance(result["bluetooth"], bool)


# ---------------------------------------------------------------- --preset: values proposed by the installer of a build

def example_conf(tmp_path):
    """zorcam.conf as install.sh creates it: a copy of config/zorcam.conf.example."""
    path = tmp_path / "zorcam.conf"
    path.write_text(zorcam_config.example(), encoding="utf-8")
    return path


def preset_cli(path, *items):
    import subprocess
    import sys
    args = [a for item in items for a in ("--preset", item)]
    env = {**os.environ, "ZORCAM_CONFIG": str(path)}
    return subprocess.run([sys.executable, zorcam_config.__file__, *args], capture_output=True, text=True, env=env)


def test_preset_sets_what_the_owner_has_not_set_and_keeps_the_rest_of_the_file(tmp_path):
    path = example_conf(tmp_path)
    before = path.read_text(encoding="utf-8").splitlines()
    done = preset_cli(path, "language.language=ru", "gallery.environment=test")
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout.splitlines() == ["preset language.language = ru", "preset gallery.environment = test"]
    after = path.read_text(encoding="utf-8").splitlines()
    # only the two commented defaults became lines; every comment and description is where it was
    assert [(a, b) for a, b in zip(before, after, strict=True) if a != b] == [
        ("# language = auto", "language = ru"), ("# environment = prod", "environment = test")]
    assert len(before) == len(after)
    cfg = zorcam_config.load(str(path))
    assert cfg.errors == [] and cfg.get("language", "language") == "ru" and cfg.get("gallery", "environment") == "test"
    assert oct(path.stat().st_mode & 0o777) == "0o644"


def test_preset_does_not_replace_what_the_owner_set(tmp_path):
    path = tmp_path / "zorcam.conf"
    # environment = prod is the default, but it is a line of the owner
    owner = "# my notes\n[language]\nlanguage = en\n\n[gallery]\nenvironment = prod\nsession_days = 30\n"
    path.write_text(owner, encoding="utf-8")
    inode, mtime = path.stat().st_ino, path.stat().st_mtime_ns
    done = preset_cli(path, "language.language=ru", "gallery.environment=test", "gallery.session_days=7")
    assert done.returncode == 0
    assert done.stdout.splitlines() == ["kept language.language = en (set by the owner)",
                                        "kept gallery.environment = prod (set by the owner)",
                                        "kept gallery.session_days = 30 (set by the owner)"]
    # nothing changed: the file is not written at all (every write restarts go2rtc and the detector)
    assert path.read_text(encoding="utf-8") == owner
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (inode, mtime)


def test_preset_mixes_kept_and_set_and_keeps_the_owners_lines(tmp_path):
    path = tmp_path / "zorcam.conf"
    path.write_text("# my notes\n\n[gallery]\nsession_days = 30\n# something of mine\n\n[language]\nlanguage = en\n",
                    encoding="utf-8")
    report = zorcam_config.apply_presets(["language.language=ru", "gallery.environment=stage", "capture.stall_timeout=20"],
                                         str(path))
    assert report == ["kept language.language = en (set by the owner)", "preset gallery.environment = stage",
                      "preset capture.stall_timeout = 20"]
    assert path.read_text(encoding="utf-8") == (
        "# my notes\n\n[gallery]\nsession_days = 30\n# something of mine\nenvironment = stage\n\n[language]\nlanguage = en\n"
        "\n[capture]\nstall_timeout = 20\n")  # in its section, before the blank line; a section that was missing is added
    cfg = zorcam_config.load(str(path))
    assert cfg.errors == [] and cfg.get("gallery", "environment") == "stage" and cfg.get("capture", "stall_timeout") == 20
    assert cfg.get("language", "language") == "en" and cfg.get("gallery", "session_days") == 30


def test_preset_of_the_default_value_needs_no_line_and_a_missing_file_is_made(tmp_path):
    path = tmp_path / "config" / "zorcam.conf"
    assert zorcam_config.apply_presets(["language.language=auto"], str(path)) == ["preset language.language = auto"]
    assert not path.exists()  # nothing to write: the default is what it is
    assert zorcam_config.apply_presets(["language.language=ru"], str(path)) == ["preset language.language = ru"]
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Zorcam settings.") and "\nlanguage = ru\n" in text and "# language = auto" not in text
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert zorcam_config.apply_presets(["language.language=ru"], str(path)) == [
        "kept language.language = ru (set by the owner)"]  # the second run changes nothing


@pytest.mark.parametrize("items,message", [
    (["language.language=de"], "language.language = de: allowed: auto, en, ru"),
    (["gallery.environment=prod2"], "gallery.environment = prod2: allowed: prod, stage, test"),
    (["gallery.videos_per_page=7"], "gallery.videos_per_page = 7: allowed: 10, 30, 60"),
    (["gallery.trusted_proxies=not an ip"], "gallery.trusted_proxies = not an ip: expected IP addresses separated by commas"),
    (["capture.stall_timeout=soon"], "capture.stall_timeout = soon: expected a number"),
    (["nothing.here=1"], "'nothing.here=1': expected SECTION.KEY=VALUE"),
    (["language=ru"], "'language=ru': expected SECTION.KEY=VALUE"),
    (["language.language"], "'language.language': expected SECTION.KEY=VALUE"),
    (["language.language=ru", "language.language=en"], "language.language is given twice"),
    (["gallery.environment=test", "language.language=de"], "language.language = de"),  # a good one first: still nothing written
])
def test_preset_refuses_a_wrong_item_and_writes_nothing(tmp_path, items, message):
    path = example_conf(tmp_path)
    before, mtime = path.read_text(encoding="utf-8"), path.stat().st_mtime_ns
    done = preset_cli(path, *items)
    assert done.returncode == 1 and done.stdout == "" and message in done.stderr and done.stderr.startswith("error: ")
    assert path.read_text(encoding="utf-8") == before and path.stat().st_mtime_ns == mtime
    with pytest.raises(ValueError, match="expected SECTION.KEY=VALUE|allowed|given twice|expected"):
        zorcam_config.apply_presets(items, str(path))


def test_preset_needs_a_value_and_a_readable_file(tmp_path):
    path = example_conf(tmp_path)
    import subprocess
    import sys
    done = subprocess.run([sys.executable, zorcam_config.__file__, "--preset"], capture_output=True, text=True,
                          env={**os.environ, "ZORCAM_CONFIG": str(path)})
    assert done.returncode == 1 and "--preset needs SECTION.KEY=VALUE" in done.stderr
    path.write_bytes(b"\xff\xfe not text")
    with pytest.raises(ValueError, match="cannot read"):
        zorcam_config.apply_presets(["language.language=ru"], str(path))
    path.write_text("[language\nlanguage = en\n", encoding="utf-8")  # not INI
    with pytest.raises(ValueError, match="cannot read"):
        zorcam_config.apply_presets(["language.language=ru"], str(path))
