"""ONVIF: the reply of a WS-Discovery probe and reading the stream addresses from a fake camera (a local HTTP server
that checks the WS-Security password digest)."""
import base64
import hashlib
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "gallery"))
import onvif  # noqa: E402

PROBE_MATCH = """<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><s:Body><d:ProbeMatches><d:ProbeMatch>
<d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/Hall%20cam onvif://www.onvif.org/hardware/IPC_T21</d:Scopes>
<d:XAddrs>http://[fe80::1]/onvif/device_service http://192.168.1.50:8000/onvif/device_service</d:XAddrs>
</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"""


def test_probe_match():
    dev = onvif.parse_probe_match(PROBE_MATCH.encode(), "192.168.1.50")
    assert dev == {"host": "192.168.1.50", "xaddr": "http://192.168.1.50:8000/onvif/device_service",
                   "name": "Hall cam", "hardware": "IPC T21"}
    assert onvif.parse_probe_match(b"<junk", "192.168.1.50") is None
    assert not onvif.local_host("8.8.8.8") and onvif.local_host("192.168.1.50")


def reply(body):
    return ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
            f'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><s:Body>{body}</s:Body></s:Envelope>')


PROFILE = ('<trt:Profiles token="{tok}"><tt:VideoEncoderConfiguration><tt:Resolution><tt:Width>{w}</tt:Width>'
           '<tt:Height>{h}</tt:Height></tt:Resolution></tt:VideoEncoderConfiguration></trt:Profiles>')


@pytest.fixture
def fake_camera():
    """A camera with the login admin / secret: two profiles; the main stream URI carries credentials and 0.0.0.0."""
    calls = []

    class Camera(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"])).decode()
            action = re.search(r"<s:Body><(?:tds|trt):(\w+)", data).group(1)
            calls.append(action)
            if action == "GetSystemDateAndTime":
                body = ('<tds:GetSystemDateAndTimeResponse><tt:UTCDateTime><tt:Date><tt:Year>2026</tt:Year><tt:Month>1'
                        '</tt:Month><tt:Day>2</tt:Day></tt:Date><tt:Time><tt:Hour>3</tt:Hour><tt:Minute>4</tt:Minute>'
                        '<tt:Second>5</tt:Second></tt:Time></tt:UTCDateTime></tds:GetSystemDateAndTimeResponse>')
                return self.answer(200, reply(body))
            nonce = base64.b64decode(re.search(r"<Nonce[^>]*>([^<]+)<", data).group(1))
            created = re.search(r"<Created[^>]*>([^<]+)<", data).group(1)
            digest = re.search(r"<Password[^>]*>([^<]+)<", data).group(1)
            good = base64.b64encode(hashlib.sha1(nonce + created.encode() + b"secret").digest()).decode()
            if "<Username>admin</Username>" not in data or digest != good or not created.startswith("2026-01-02T03:0"):
                return self.answer(400, reply('<s:Fault><s:Reason><s:Text>Sender not Authorized</s:Text></s:Reason>'
                                              '</s:Fault>'))
            port = self.server.server_address[1]
            body = {
                "GetDeviceInformation": "<tds:GetDeviceInformationResponse><tds:Manufacturer>Acme</tds:Manufacturer>"
                                        "<tds:Model>C2</tds:Model></tds:GetDeviceInformationResponse>",
                "GetCapabilities": f"<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>"
                                   f"http://127.0.0.1:{port}/onvif/media</tt:XAddr></tt:Media></tds:Capabilities>"
                                   "</tds:GetCapabilitiesResponse>",
                # the camera's order: main, its copy, sub, a tiny third one (not for the detector)
                "GetProfiles": "<trt:GetProfilesResponse>" + PROFILE.format(tok="main", w=1920, h=1080)
                               + PROFILE.format(tok="main2", w=1920, h=1080) + PROFILE.format(tok="sub", w=640, h=360)
                               + PROFILE.format(tok="third", w=320, h=180) + "</trt:GetProfilesResponse>",
            }.get(action)
            if action == "GetStreamUri":
                uri = {"main": "rtsp://admin:secret@0.0.0.0:554/stream1", "sub": "rtsp://127.0.0.1:554/stream2?ch=1",
                       "third": "rtsp://127.0.0.1:554/stream3"}[re.search(r"<trt:ProfileToken>(\w+)<", data).group(1)]
                body = f"<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{uri}</tt:Uri></trt:MediaUri></trt:GetStreamUriResponse>"
            self.answer(200, reply(body))

        def answer(self, code, text):
            data = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/soap+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Camera)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/onvif/device_service", calls
    srv.shutdown()


def test_streams_from_the_camera(fake_camera):
    xaddr, calls = fake_camera
    found = onvif.camera_streams(xaddr, "admin", "secret")
    # the camera's clock (2026-01-02 03:04:05) signs the token; no password in the addresses; 0.0.0.0 -> its address;
    # the additional address is the stream after the main one, not the smallest
    assert found == {"manufacturer": "Acme", "model": "C2", "main": "rtsp://127.0.0.1:554/stream1",
                     "sub": "rtsp://127.0.0.1:554/stream2?ch=1", "main_size": (1920, 1080), "sub_size": (640, 360),
                     "auth": "ws"}
    assert calls[:2] == ["GetSystemDateAndTime", "GetDeviceInformation"]


def test_wrong_password_and_foreign_addresses(fake_camera):
    xaddr, _ = fake_camera
    with pytest.raises(onvif.OnvifError) as e:
        onvif.camera_streams(xaddr, "admin", "wrong")
    assert e.value.key == "auth"
    with pytest.raises(onvif.OnvifError) as e:
        onvif.camera_streams("http://8.8.8.8/onvif/device_service", "admin", "secret")
    assert e.value.key == "address"
    with pytest.raises(onvif.OnvifError) as e:
        onvif.camera_streams("http://127.0.0.1:1/onvif/device_service", "admin", "secret", timeout=2)
    assert e.value.key == "network"


def test_no_redirects():
    """A camera must not send the gallery's request to an address outside the home network."""
    class Moved(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "http://8.8.8.8/onvif/device_service")
            self.send_header("Content-Length", "0")
            self.end_headers()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Moved)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(onvif.OnvifError) as e:
            onvif.soap(f"http://127.0.0.1:{srv.server_address[1]}/onvif/device_service", "<tds:GetDeviceInformation/>")
        assert e.value.key == "http" and e.value.detail == "302"
    finally:
        srv.shutdown()


def test_a_camera_that_wants_http_digest():
    """Some cameras ignore the WS-Security token and check HTTP Digest instead (401 with a challenge): the second way
    of signing in is tried and kept for the rest of the requests."""
    seen = []

    class Camera(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"])).decode()
            action = re.search(r"<s:Body><(?:tds|trt):(\w+)", data).group(1)
            auth = self.headers.get("Authorization", "")
            seen.append((action, auth.split(" ")[0]))
            if action == "GetSystemDateAndTime":
                return self.answer(200, reply("<tds:GetSystemDateAndTimeResponse/>"))
            if not auth.startswith("Digest ") or 'username="admin"' not in auth:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Digest realm="cam", nonce="abc", qop="auth"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = {"GetDeviceInformation": "<tds:GetDeviceInformationResponse><tds:Model>D1</tds:Model>"
                                            "</tds:GetDeviceInformationResponse>",
                    "GetProfiles": "<trt:GetProfilesResponse>" + PROFILE.format(tok="m", w=1280, h=720)
                                   + "</trt:GetProfilesResponse>",
                    "GetStreamUri": "<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://127.0.0.1/live</tt:Uri>"
                                    "</trt:MediaUri></trt:GetStreamUriResponse>"}.get(action, "<x/>")
            self.answer(200, reply(body))

        answer = BaseHTTPRequestHandler.send_response  # replaced below

    def answer(self, code, text):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    Camera.answer = answer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Camera)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        found = onvif.camera_streams(f"http://127.0.0.1:{srv.server_address[1]}/onvif/device_service", "admin", "pw")
        assert found["model"] == "D1" and found["main"] == "rtsp://127.0.0.1/live" and found["auth"] == "digest"
        assert ("GetDeviceInformation", "") in seen and ("GetProfiles", "Digest") in seen
    finally:
        srv.shutdown()


def test_authority_failure_is_a_refused_sign_in():
    root_fault = reply('<s:Fault><s:Reason><s:Text>Authority failure</s:Text></s:Reason></s:Fault>')
    assert any(w in "authority failure" for w in onvif.AUTH_WORDS) and "Authority failure" in root_fault


def test_subnet_hosts_only_home_networks_up_to_254():
    assert len(onvif.subnet_hosts("192.168.2.0/24")) == 254 and onvif.subnet_hosts("10.0.0.8/30") == ["10.0.0.9", "10.0.0.10"]
    assert onvif.subnet_hosts("192.168.2.77") == onvif.subnet_hosts("192.168.2.0") == onvif.subnet_hosts("192.168.2.0/24")
    assert onvif.subnet_hosts("192.168.2.77/32") == ["192.168.2.77"]  # one camera by its address
    assert onvif.subnet_hosts("192,168,2,0") == onvif.subnet_hosts("192.168.2.0")  # the iPhone keypad in Russian
    for bad in ("192.168.0.0/16", "8.8.8.0/24", "fe80::/120", "camera", ""):
        with pytest.raises(ValueError):
            onvif.subnet_hosts(bad)


def test_discover_asks_every_address_of_a_subnet():
    """Multicast does not cross the router: in another subnet each address gets the probe itself."""
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.2", 0))
    port = srv.getsockname()[1]

    def answer():
        data, addr = srv.recvfrom(65535)
        assert b"NetworkVideoTransmitter" in data
        srv.sendto(PROBE_MATCH.replace("http://192.168.1.50:8000", f"http://127.0.0.2:{port}").encode(), addr)
    threading.Thread(target=answer, daemon=True).start()
    try:
        found = onvif.discover(1.0, "127.0.0.0/30", port=port)
    finally:
        srv.close()
    assert [d["host"] for d in found] == ["127.0.0.2"] and found[0]["name"] == "Hall cam"


@pytest.fixture
def ptz_camera():
    """A camera with PTZ (login admin / secret): the PTZ service on its own address, two profiles, only the second has
    a PTZConfiguration. Records the bodies of ContinuousMove and Stop."""
    seen = []

    class Camera(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"])).decode()
            action = re.search(r"<s:Body><(?:tds|trt|tptz):(\w+)", data).group(1)
            port = self.server.server_address[1]
            if action == "GetSystemDateAndTime":
                return self.answer(200, reply("<tds:GetSystemDateAndTimeResponse/>"))
            nonce = base64.b64decode(re.search(r"<Nonce[^>]*>([^<]+)<", data).group(1))
            created = re.search(r"<Created[^>]*>([^<]+)<", data).group(1)
            digest = re.search(r"<Password[^>]*>([^<]+)<", data).group(1)
            good = base64.b64encode(hashlib.sha1(nonce + created.encode() + b"secret").digest()).decode()
            if "<Username>admin</Username>" not in data or digest != good:
                return self.answer(400, reply("<s:Fault><s:Reason><s:Text>Sender not Authorized</s:Text></s:Reason></s:Fault>"))
            if action in ("ContinuousMove", "Stop"):
                seen.append((self.path, data))
                return self.answer(200, reply(f"<tptz:{action}Response xmlns:tptz='http://www.onvif.org/ver20/ptz/wsdl'/>"))
            if action == "GetCapabilities":
                cat = re.search(r"<tds:Category>(\w+)<", data).group(1)
                path = {"Media": "media", "PTZ": "ptz"}[cat]
                body = (f"<tds:GetCapabilitiesResponse><tds:Capabilities><tt:{cat}><tt:XAddr>http://127.0.0.1:{port}"
                        f"/onvif/{path}</tt:XAddr></tt:{cat}></tds:Capabilities></tds:GetCapabilitiesResponse>")
            else:
                body = ("<trt:GetProfilesResponse>" + PROFILE.format(tok="fixed", w=640, h=360)
                        + '<trt:Profiles token="pt&amp;1"><tt:PTZConfiguration token="c"/></trt:Profiles>'
                        + "</trt:GetProfilesResponse>")
            self.answer(200, reply(body))

        def answer(self, code, text):
            data = text.encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Camera)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/onvif/device_service", seen
    srv.shutdown()


def test_ptz_move_and_stop(ptz_camera):
    xaddr, seen = ptz_camera
    cap = onvif.ptz_support(xaddr, "admin", "secret")
    assert cap["url"].endswith("/onvif/ptz") and cap["token"] == "pt&1"  # the profile that has a PTZConfiguration
    onvif.ptz_move(cap, "admin", "secret", pan=-0.5, tilt=0.5, seconds=2)
    onvif.ptz_move(cap, "admin", "secret", zoom=5)  # clamped to 1
    onvif.ptz_stop(cap, "admin", "secret")
    (path, move), (_, zoom), (_, stop) = seen
    assert path == "/onvif/ptz"
    assert ("<tptz:ProfileToken>pt&amp;1</tptz:ProfileToken><tptz:Velocity>"
            '<tt:PanTilt x="-0.50" y="0.50"/></tptz:Velocity><tptz:Timeout>PT2S</tptz:Timeout>') in move
    assert '<tt:Zoom x="1.00"/>' in zoom and "PanTilt" not in zoom
    assert "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>" in stop
    with pytest.raises(ValueError):
        onvif.ptz_move(cap, "admin", "secret")
    with pytest.raises(onvif.OnvifError) as e:
        onvif.ptz_support(xaddr, "admin", "wrong")
    assert e.value.key == "auth"


def test_a_camera_without_ptz(fake_camera):
    xaddr, _ = fake_camera
    with pytest.raises(onvif.OnvifError) as e:
        onvif.ptz_support(xaddr, "admin", "secret")
    assert e.value.key == "no_ptz"
    with pytest.raises(onvif.OnvifError) as e:
        onvif.ptz_support("http://8.8.8.8/onvif/device_service", "admin", "secret")
    assert e.value.key == "address"


def test_camera_url_stays_in_the_home_network():
    assert onvif.camera_url({"rtsp_url": "rtsp://192.168.1.5:554/live"}) == ""  # no fallback from the RTSP address
    assert onvif.camera_url({"rtsp_url": "rtsp://8.8.8.8/x"}) == ""
    assert onvif.camera_url({"rtsp_url": "rtsp://cam.local/x"}) == ""
    assert onvif.camera_url({"rtsp_url": "rtsp://8.8.8.8/x", "onvif_url": "http://10.0.0.2:8000/onvif/d"}) == \
        "http://10.0.0.2:8000/onvif/d"
    assert onvif.camera_url({"rtsp_url": "rtsp://192.168.1.5/x", "onvif_url": "http://1.2.3.4/onvif"}) == ""
