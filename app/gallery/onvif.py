"""ONVIF без сторонних пакетов: поиск камер в домашней сети (WS-Discovery) и их RTSP-адреса (сведения об устройстве,
профили, адреса потоков; WS-Security UsernameToken с дайджестом пароля)."""
import base64
import hashlib
import ipaddress
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

MULTICAST = ("239.255.255.250", 3702)
SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS = {
    "s": SOAP,
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
}

PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<s:Header><a:Action s:mustUnderstand="1">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>
<a:MessageID>uuid:{id}</a:MessageID><a:ReplyTo><a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>
<a:To s:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To></s:Header>
<s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></s:Body></s:Envelope>"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Без перенаправлений: камера не должна отправить запрос галереи на адрес вне домашней сети."""

    def redirect_request(self, *args, **kwargs):
        return None


# без прокси из окружения: камеры — в домашней сети
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class OnvifError(Exception):
    """key — onvif.err.<key> в zorcam_i18n (текст для интерфейса), detail — подробность (в журнал и в текст)."""

    def __init__(self, key, detail=""):
        super().__init__(f"{key}: {detail}" if detail else key)
        self.key, self.detail = key, detail


def local_host(host):
    """Только адреса домашней сети: галерея не должна стать способом обращаться к любым узлам."""
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        return False
    return a.is_private or a.is_link_local


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(root, name):
    return next((el for el in root.iter() if _local(el.tag) == name), None)


def _text(root, name):
    el = _find(root, name)
    return (el.text or "").strip() if el is not None else ""


def parse_probe_match(data, sender):
    """Ответ ProbeMatch: {"host", "xaddr", "name", "hardware"} или None."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    xaddrs = _text(root, "XAddrs").split()
    if not xaddrs:
        return None
    # адресов у камеры бывает несколько (IPv6, другая подсеть): первым — тот, с которого она ответила
    xaddr = next((x for x in xaddrs if urlparse(x).hostname == sender), xaddrs[0])
    host = urlparse(xaddr).hostname or sender
    scopes = _text(root, "Scopes").split()

    def scope(kind):
        for s in scopes:
            m = re.match(rf"onvif://www\.onvif\.org/{kind}/(.+)", s)
            if m:
                return urllib.request.unquote(m.group(1)).replace("_", " ")
        return ""
    return {"host": host, "xaddr": xaddr, "name": scope("name"), "hardware": scope("hardware")}


def subnet_hosts(subnet):
    """Адреса подсети для поиска: только домашняя сеть и не больше /24 (254 адреса); ValueError — нельзя.
    Без маски — вся /24 этого адреса: «192.168.2.0» и «192.168.2.31» — это 192.168.2.0/24."""
    subnet = subnet.strip().replace(",", ".")  # цифровая клавиатура iPhone с русской раскладкой ставит запятую
    net = ipaddress.ip_network(subnet if "/" in subnet else subnet + "/24", strict=False)
    if net.version != 4 or not net.is_private or net.prefixlen < 24:
        raise ValueError(subnet)
    return [str(h) for h in net.hosts()]


def discover(timeout=3.0, subnet="", port=MULTICAST[1]):
    """Камеры, ответившие на WS-Discovery, по возрастанию адреса: рассылка в сети Pi (multicast) и, если задана
    подсеть, тот же запрос каждому её адресу по отдельности — multicast роутер в другие подсети не пропускает."""
    found = {}
    targets = [(MULTICAST[0], port)] + [(h, port) for h in (subnet_hosts(subnet) if subnet else [])]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.settimeout(0.3)
        message = PROBE.format(id=uuid.uuid4()).encode()
        sent = 0
        for _ in range(2):  # UDP теряется: второй запрос чуть позже
            for target in targets:
                try:
                    sock.sendto(message, target)
                    sent += 1
                except OSError:  # нет сети или маршрута (для multicast или до этого адреса)
                    pass
            time.sleep(0.1)
        if not sent:
            return []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, (sender, _port) = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            dev = parse_probe_match(data, sender)
            if dev and local_host(dev["host"]):
                found.setdefault(dev["host"], dev)
    finally:
        sock.close()
    return sorted(found.values(), key=lambda d: ipaddress.ip_address(d["host"]))


def _security(user, password, offset):
    """WS-Security UsernameToken, PasswordDigest = Base64(SHA1(nonce + created + password)); created — по часам
    камеры (offset): токен, далёкий от её времени, камера отвергает."""
    nonce = os.urandom(16)
    created = datetime.fromtimestamp(time.time() + offset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    esc = (user.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return (
        '<Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken>'
        f"<Username>{esc}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0'
        f'#PasswordDigest">{digest}</Password>'
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0'
        f'#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>'
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"{created}</Created></UsernameToken></Security>")


# признаки отказа во входе в тексте SOAP Fault: «Sender not Authorized», «Authority failure», «NotAuthorized»…
AUTH_WORDS = ("author", "password", "unauthor", "credential", "not permitted")


def _opener(url, user, password, digest):
    if not digest:
        return _OPENER
    # HTTP Digest (Basic — если камера просит его): многие камеры проверяют вход так, а не подписью в запросе
    manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    manager.add_password(None, url, user, password)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect,
                                       urllib.request.HTTPDigestAuthHandler(manager),
                                       urllib.request.HTTPBasicAuthHandler(manager))


def soap(url, body, user="", password="", offset=0.0, timeout=5, digest=False):
    header = f"<s:Header>{_security(user, password, offset)}</s:Header>" if user else ""
    envelope = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{SOAP}" '
                f'xmlns:tds="{NS["tds"]}" xmlns:trt="{NS["trt"]}" xmlns:tt="{NS["tt"]}" '
                f'xmlns:tptz="{NS["tptz"]}">{header}'
                f"<s:Body>{body}</s:Body></s:Envelope>").encode()
    req = urllib.request.Request(url, data=envelope, headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        with _opener(url, user, password, digest).open(req, timeout=timeout) as resp:
            data = resp.read(1 << 20)
    except urllib.error.HTTPError as e:  # SOAP Fault приходит с 400/401/500
        if e.code == 401:
            raise OnvifError("auth", "HTTP 401") from None
        data = e.read(1 << 20)
        if not data:
            raise OnvifError("http", str(e.code)) from None
    except (OSError, ValueError) as e:
        raise OnvifError("network", str(getattr(e, "reason", e))) from None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise OnvifError("reply") from None
    fault = _find(root, "Fault")
    if fault is not None:
        reason = (_text(fault, "Text") or _text(fault, "Value") or "fault")[:200]
        if any(w in (reason + " " + _text(fault, "Subcode")).lower() for w in AUTH_WORDS):
            raise OnvifError("auth", reason)
        raise OnvifError("fault", reason[:200])
    return root


def clock_offset(xaddr):
    """Часы камеры минус наши, с (0 — неизвестно)."""
    try:
        root = soap(xaddr, "<tds:GetSystemDateAndTime/>")
    except OnvifError:
        return 0.0
    utc = _find(root, "UTCDateTime")
    if utc is None:
        return 0.0
    try:
        d, t = _find(utc, "Date"), _find(utc, "Time")
        stamp = datetime(int(_text(d, "Year")), int(_text(d, "Month")), int(_text(d, "Day")),
                         int(_text(t, "Hour")), int(_text(t, "Minute")), int(_text(t, "Second")), tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0.0
    return stamp.timestamp() - time.time()


def strip_credentials(uri, host):
    """RTSP-адрес для формы камеры: без логина и пароля (они в secrets.env); вместо 0.0.0.0 и localhost — адрес
    камеры."""
    p = urlparse(uri)
    if p.scheme not in ("rtsp", "rtsps") or not p.hostname:
        raise OnvifError("no_rtsp")  # сам адрес не в тексте: в нём бывает пароль
    name = p.hostname
    if name in ("0.0.0.0", "127.0.0.1", "localhost"):
        name = host
    netloc = f"[{name}]" if ":" in name else name
    if p.port:
        netloc += f":{p.port}"
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, ""))


def _sign_in(xaddr, user, password, timeout):
    """(сведения об устройстве, способ входа (смещение часов, HTTP Digest), смещение часов камеры)."""
    offset = clock_offset(xaddr)
    # способы входа по очереди, пока камера не перестанет отказывать: подпись в запросе по её часам; она же
    # и HTTP Digest; Digest и подпись по нашим часам (у камеры бывает неверный часовой пояс)
    ways = [(offset, False), (offset, True)] + ([(0.0, True)] if abs(offset) > 1 else [])
    info, refused = {"manufacturer": "", "model": ""}, None
    for way in ways:
        try:
            root = soap(xaddr, "<tds:GetDeviceInformation/>", user, password, way[0], timeout, way[1])
            info = {"manufacturer": _text(root, "Manufacturer"), "model": _text(root, "Model")}
        except OnvifError as e:
            if e.key == "auth":
                refused = e
                continue
        break
    else:
        raise refused
    return info, way, offset


def _service(call, xaddr, category):
    """Адрес службы камеры (Media, PTZ) из GetCapabilities; не назвала или не из домашней сети — адрес устройства."""
    try:
        root = call(xaddr, f'<tds:GetCapabilities><tds:Category>{category}</tds:Category></tds:GetCapabilities>')
        el = _find(root, category)
        found = _text(el, "XAddr") if el is not None else ""
        if found and local_host(urlparse(found).hostname or ""):
            return found
    except OnvifError as e:
        if e.key == "auth":
            raise
    return xaddr


def camera_streams(xaddr, user, password, timeout=5):
    """{"manufacturer", "model", "main", "sub", "main_size", "sub_size"} камеры: main — профиль с самой большой
    картинкой, sub — следующий за ним поток меньше него (для детектора; пусто, если такого нет)."""
    host = urlparse(xaddr).hostname or ""
    if not local_host(host):
        raise OnvifError("address")
    info, way, offset = _sign_in(xaddr, user, password, timeout)

    def call(url, body):
        return soap(url, body, user, password, way[0], timeout, way[1])
    media = _service(call, xaddr, "Media")
    root = call(media, "<trt:GetProfiles/>")
    profiles = []
    for prof in (el for el in root.iter() if _local(el.tag) == "Profiles"):
        token = prof.get("token")
        res = _find(prof, "Resolution")
        try:
            size = (int(_text(res, "Width")), int(_text(res, "Height"))) if res is not None else (0, 0)
        except ValueError:
            size = (0, 0)
        if token:
            profiles.append((size[0] * size[1], size, token))
    if not profiles:
        raise OnvifError("no_profiles")
    # основной — с самой большой картинкой; дополнительный — следующий по счёту поток камеры меньше основного
    # (третий у многих камер — совсем мелкий или MJPEG, детектору он не нужен); после основного нет — ближайший до него
    top = max(range(len(profiles)), key=lambda i: profiles[i][0])
    smaller = [i for i in range(len(profiles)) if profiles[i][0] < profiles[top][0]]
    following = [i for i in smaller if i > top] or [i for i in reversed(smaller) if i < top]
    profiles = [profiles[top]] + ([profiles[following[0]]] if following else [])

    def uri(token):
        body = ('<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport>'
                f'<tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup><trt:ProfileToken>{token}'
                '</trt:ProfileToken></trt:GetStreamUri>')
        return strip_credentials(_text(call(media, body), "Uri"), host)
    main = uri(profiles[0][2])
    sub, sub_size = "", None
    if len(profiles) > 1:
        try:
            sub, sub_size = uri(profiles[1][2]), profiles[1][1]
        except OnvifError:
            pass
    return dict(info, auth=("digest" if way[1] else "ws") + (", local clock" if way[0] == 0.0 and offset else ""),
                main=main, sub=sub if sub != main else "", main_size=profiles[0][1],
                sub_size=sub_size if sub and sub != main else None)


def camera_url(cam):
    """Адрес ONVIF камеры: поле onvif_url (запасного адреса из rtsp_url нет); пусто, если он не задан или не из
    домашней сети."""
    url = cam.get("onvif_url") or ""
    p = urlparse(url)
    return url if p.scheme in ("http", "https") and local_host(p.hostname or "") else ""


def ptz_support(xaddr, user, password, timeout=3):
    """Управление камерой: {"url", "token", "way"} — служба PTZ, профиль с PTZConfiguration и способ входа.
    OnvifError("no_ptz"), если камера поворотной не бывает."""
    if not local_host(urlparse(xaddr).hostname or ""):
        raise OnvifError("address")
    _info, way, _offset = _sign_in(xaddr, user, password, timeout)

    def call(url, body):
        return soap(url, body, user, password, way[0], timeout, way[1])
    root = call(_service(call, xaddr, "Media"), "<trt:GetProfiles/>")
    token = next((p.get("token") for p in root.iter() if _local(p.tag) == "Profiles" and p.get("token")
                  and _find(p, "PTZConfiguration") is not None), None)
    if not token:
        raise OnvifError("no_ptz")
    return {"url": _service(call, xaddr, "PTZ"), "token": token, "way": way}


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def ptz_move(cap, user, password, pan=0.0, tilt=0.0, zoom=0.0, seconds=2, timeout=3):
    """ContinuousMove со скоростью -1..1; Timeout — страховка: камера остановится сама, даже если Stop потерялся."""
    clamp = lambda v: max(-1.0, min(1.0, float(v)))  # noqa: E731
    pan, tilt, zoom = clamp(pan), clamp(tilt), clamp(zoom)
    velocity = (f'<tt:PanTilt x="{pan:.2f}" y="{tilt:.2f}"/>' if pan or tilt else "") + (
        f'<tt:Zoom x="{zoom:.2f}"/>' if zoom else "")
    if not velocity:
        raise ValueError("no movement")
    body = (f"<tptz:ContinuousMove><tptz:ProfileToken>{_escape(cap['token'])}</tptz:ProfileToken>"
            f"<tptz:Velocity>{velocity}</tptz:Velocity><tptz:Timeout>PT{int(seconds)}S</tptz:Timeout>"
            "</tptz:ContinuousMove>")
    soap(cap["url"], body, user, password, cap["way"][0], timeout, cap["way"][1])


def ptz_stop(cap, user, password, timeout=3):
    body = (f"<tptz:Stop><tptz:ProfileToken>{_escape(cap['token'])}</tptz:ProfileToken>"
            "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>")
    soap(cap["url"], body, user, password, cap["way"][0], timeout, cap["way"][1])
