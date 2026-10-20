"""Внешний IPv4 устройства по STUN (RFC 5389): один Binding Request по UDP, без библиотек.

Нужен для WebRTC из интернета: go2rtc сам внешний адрес не находит (ice_servers пуст), поэтому галерея узнаёт его
здесь в фоне и дописывает кандидатом в ответ на каждый offer. Запросов в Live это не касается: берётся последний результат."""
import ipaddress
import os
import re
import socket
import struct
import threading
import time

import zorcam_cloud

MAGIC = 0x2112A442
BINDING_REQUEST, BINDING_SUCCESS = 0x0001, 0x0101
MAPPED_ADDRESS, XOR_MAPPED_ADDRESS = 0x0001, 0x0020
INTERVAL = 300       # как часто спрашивать
RETRY_NONE = 30      # никто не ответил (например, сеть ещё не поднялась после загрузки)
RETRY_FALLBACK = 60  # ответил не первый сервер: у роутера бывает VPN для части адресов, и запасной видит его выход
TIMEOUT = 2.0        # на один сервер
FALLBACK = [("stun.l.google.com", 19302), ("stun.cloudflare.com", 3478)]
# STUN (coturn) облака того же стенда: если облако не привязано, адрес всё равно известен; у prod — имя в zorcam.com
# (тот же coturn): устройство без облака общается только с международным доменом (решение владельца 20.10.2026)
ENV_SERVERS = {"prod": ("stun.zorcam.com", 3478), "stage": ("turn.stage.zorcam.ru", 3488),
               "test": ("turn.test.zorcam.ru", 3498)}
CGNAT = ipaddress.ip_network("100.64.0.0/10")
TURN_RE = re.compile(r"(turns?):([a-z0-9.-]+)(?::(\d{1,5}))?")

_lock = threading.Lock()
_state = {"ip": None, "at": None, "server": None, "checked": None, "why": None, "logged": None}
_cloud_hosts = []  # [(хост, порт)] из TURN-адресов облака
_environment = "prod"
_custom = []  # [(хост, порт)] из [live] stun_servers: если задан, заменяет весь список


def request_packet():
    """(пакет, transaction id): Binding Request без атрибутов."""
    tid = os.urandom(12)
    return struct.pack("!HHI", BINDING_REQUEST, 0, MAGIC) + tid, tid


def parse_response(data, tid):
    """Внешний IPv4 из ответа STUN или None: чужой transaction id, не IPv4 и мусор отбрасываются."""
    if len(data) < 20:
        return None
    mtype, length, magic = struct.unpack("!HHI", data[:8])
    if mtype != BINDING_SUCCESS or magic != MAGIC or data[8:20] != tid or length > len(data) - 20:
        return None
    pos, end, mapped = 20, 20 + length, None
    while pos + 4 <= end:
        atype, alen = struct.unpack("!HH", data[pos:pos + 4])
        body = data[pos + 4:pos + 4 + alen]
        if len(body) < alen:
            return None
        if atype in (MAPPED_ADDRESS, XOR_MAPPED_ADDRESS) and alen >= 8 and body[1] == 1:  # семейство 1 — IPv4
            raw = body[4:8]
            if atype == XOR_MAPPED_ADDRESS:
                raw = bytes(a ^ b for a, b in zip(raw, struct.pack("!I", MAGIC), strict=True))
                return str(ipaddress.IPv4Address(raw))  # XOR-адрес главнее MAPPED
            mapped = str(ipaddress.IPv4Address(raw))
        pos += 4 + (alen + 3) // 4 * 4  # атрибуты выровнены по 4 байта
    return mapped


def query(host, port, timeout=TIMEOUT):
    """Внешний IPv4 по ответу одного сервера; исключения — наверх (разбирает probe)."""
    packet, tid = request_packet()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        sock.sendto(packet, (host, port))
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("no answer")
            sock.settimeout(left)
            data, _addr = sock.recvfrom(1024)
            ip = parse_response(data, tid)
            if ip:  # чужой или битый пакет — ждём дальше до конца таймаута
                return ip


def usable(ip):
    """Подходит ли адрес как кандидат из интернета: только публичный (не частный, не CGNAT 100.64/10)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.version == 4 and addr.is_global and addr not in CGNAT


def note_cloud_ice(servers):
    """Запомнить STUN облака по TURN-адресам его ICE-серверов (cloud_ice). turn:host:port — тот же хост и порт;
    turns: идёт по TLS (обычно 5349) и STUN по UDP там не принимают — берём тот же хост с портом 3478."""
    found = []
    for srv in servers or []:
        for url in srv.get("urls", []):
            m = TURN_RE.fullmatch(url.split("?")[0])
            if not m:
                continue
            port = int(m.group(3)) if m.group(1) == "turn" and m.group(3) else 3478
            if 0 < port < 65536 and (m.group(2), port) not in found:
                found.append((m.group(2), port))
    if found:
        with _lock:
            _cloud_hosts[:] = found


def parse_servers(text):
    """[(хост, порт)] из строки «хост[:порт], …» (формат проверен в zorcam_config.PATTERNS); порт по умолчанию 3478."""
    out = []
    for item in text.split(","):
        item = item.strip().lower()
        if not item:
            continue
        host, _, port = item.partition(":")
        pair = (host, int(port) if port else 3478)
        if pair not in out:
            out.append(pair)
    return out


def configure(environment, custom=""):
    """Галерея при старте: стенд и [live] stun_servers."""
    global _environment
    _environment = environment
    _custom[:] = parse_servers(custom)


def cloud_json_server():
    """STUN из cloud.json (поле stun, «хост:порт»); читается при каждом опросе, чтобы пережить перезапуск галереи."""
    value = zorcam_cloud.load().get("stun")
    if isinstance(value, str) and zorcam_cloud.STUN_RE.fullmatch(value):
        host, _, port = value.partition(":")
        return [(host, int(port))]
    return []


def servers():
    """Серверы по очереди: свои из параметра (только они); иначе cloud.json и ICE облака, стенд, запасные."""
    if _custom:
        return list(_custom)
    with _lock:
        known = list(_cloud_hosts)
    out = []
    env = [ENV_SERVERS[_environment]] if _environment in ENV_SERVERS else []
    for s in cloud_json_server() + known + env + FALLBACK:
        if s not in out:
            out.append(s)
    return out


def probe(log=lambda m: None, ask=None):
    """Один опрос: по очереди до первого публичного адреса. Журнал — только при изменении результата."""
    ask = ask or query
    ip = server = why = None
    for host, port in servers():
        try:
            got = ask(host, port)
        except Exception:  # сеть, DNS, таймаут: пробуем следующий
            continue
        if usable(got):
            ip, server = got, f"{host}:{port}"
            break
        why = got  # ответ есть, но адрес не публичный
    now = time.time()
    key = ip or f"none:{why}"
    with _lock:
        prev, logged = _state["ip"], _state["logged"]
        _state.update(ip=ip, server=server, checked=now, why=why, logged=key)
        if ip:
            _state["at"] = now
    if key == logged:
        return ip  # ничего не изменилось: в журнал не пишем
    if ip and prev:
        log(f"stun: external address changed {prev} -> {ip} (via {server})")
    elif ip:
        log(f"stun: external address {ip} (via {server})")
    elif why:
        log(f"stun: external address not determined: {why} is not a public address (CGNAT or a private network), "
            "WebRTC from the internet will not work directly")
    elif prev:
        log(f"stun: external address lost: no STUN server answered (was {prev})")
    else:
        log("stun: external address not determined: no STUN server answered")
    return ip


def next_delay():
    """Пауза до следующего опроса по последнему результату."""
    with _lock:
        ip, server = _state["ip"], _state["server"]
    if not ip:
        return RETRY_NONE
    first = servers()[:1]
    if first and server != f"{first[0][0]}:{first[0][1]}":
        return RETRY_FALLBACK
    return INTERVAL


def loop(log=lambda m: None):
    """Фоновый поток: опрос сразу и потом раз в INTERVAL (чаще, пока нет ответа первого сервера); ошибки не роняют поток."""
    while True:
        try:
            probe(log)
            delay = next_delay()
        except Exception as e:
            log(f"stun: check failed: {e}")
            delay = INTERVAL
        time.sleep(delay)


def external():
    """(IP, сервер) или (None, None)."""
    with _lock:
        return _state["ip"], _state["server"]


def snapshot():
    """Копия состояния для «Статуса»."""
    with _lock:
        return dict(_state)
