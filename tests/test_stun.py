"""app/gallery/stun.py: the external address by STUN (no network: the query function is replaced)."""
import struct

import pytest

import stun


def response(tid, attrs, mtype=stun.BINDING_SUCCESS):
    body = b"".join(struct.pack("!HH", t, len(v)) + v + b"\0" * (-len(v) % 4) for t, v in attrs)
    return struct.pack("!HHI", mtype, len(body), stun.MAGIC) + tid + body


def xor_addr(ip):
    raw = bytes(int(x) for x in ip.split("."))
    return b"\0\x01" + struct.pack("!H", 8555 ^ (stun.MAGIC >> 16)) + bytes(
        a ^ b for a, b in zip(raw, struct.pack("!I", stun.MAGIC), strict=True))


def plain_addr(ip):
    return b"\0\x01" + struct.pack("!H", 8555) + bytes(int(x) for x in ip.split("."))


TID = bytes(range(12))


def test_xor_mapped_address():
    assert stun.parse_response(response(TID, [(stun.XOR_MAPPED_ADDRESS, xor_addr("203.0.113.7"))]), TID) == "203.0.113.7"


def test_mapped_address_and_the_xor_one_wins():
    assert stun.parse_response(response(TID, [(stun.MAPPED_ADDRESS, plain_addr("198.51.100.4"))]), TID) == "198.51.100.4"
    both = [(stun.MAPPED_ADDRESS, plain_addr("198.51.100.4")), (stun.XOR_MAPPED_ADDRESS, xor_addr("203.0.113.7"))]
    assert stun.parse_response(response(TID, both), TID) == "203.0.113.7"
    other = [(0x8022, b"zorcam"), (stun.XOR_MAPPED_ADDRESS, xor_addr("203.0.113.7"))]  # SOFTWARE first, padded
    assert stun.parse_response(response(TID, other), TID) == "203.0.113.7"


def test_foreign_or_broken_answers_are_ignored():
    good = response(TID, [(stun.XOR_MAPPED_ADDRESS, xor_addr("203.0.113.7"))])
    assert stun.parse_response(good, bytes(12)) is None  # someone else's transaction id
    assert stun.parse_response(response(TID, [(stun.XOR_MAPPED_ADDRESS, xor_addr("203.0.113.7"))], mtype=0x0111), TID) is None
    assert stun.parse_response(good[:15], TID) is None and stun.parse_response(b"", TID) is None
    assert stun.parse_response(b"x" * 64, TID) is None
    assert stun.parse_response(good[:-3], TID) is None  # length beyond the packet
    ipv6 = b"\0\x02" + b"\0" * 18
    assert stun.parse_response(response(TID, [(stun.XOR_MAPPED_ADDRESS, ipv6)]), TID) is None
    assert stun.parse_response(response(TID, []), TID) is None


def test_request_packet():
    packet, tid = stun.request_packet()
    assert len(packet) == 20 and len(tid) == 12 and packet[8:] == tid
    assert struct.unpack("!HHI", packet[:8]) == (1, 0, stun.MAGIC)


@pytest.mark.parametrize("ip,ok", [
    ("203.0.113.7", False),   # documentation range is not global either
    ("93.184.216.34", True), ("192.168.1.5", False), ("10.1.2.3", False), ("172.16.0.1", False), ("127.0.0.1", False),
    ("169.254.1.1", False), ("100.64.0.1", False), ("100.127.255.254", False), ("100.128.0.1", True),
    ("::1", False), ("2001:4860::1", False), ("junk", False)])
def test_only_public_addresses_are_usable(ip, ok):
    assert stun.usable(ip) is ok


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.setattr(stun, "_state", {"ip": None, "at": None, "server": None, "checked": None, "why": None,
                                          "logged": None})
    monkeypatch.setattr(stun, "_cloud_hosts", [])
    monkeypatch.setattr(stun, "_custom", [])
    monkeypatch.setattr(stun, "_environment", "prod")


def ice(*urls):
    return [{"urls": list(urls), "username": "u", "credential": "c"}]


PROD = ("stun.zorcam.com", 3478)
GOOGLE, CLOUDFLARE = ("stun.l.google.com", 19302), ("stun.cloudflare.com", 3478)


def test_servers_without_and_with_the_cloud(clean):
    assert stun.servers() == [PROD, GOOGLE, CLOUDFLARE]
    stun.note_cloud_ice(ice("turn:turn.example.org:3480?transport=udp", "turn:turn.example.org:3480?transport=tcp",
                            "turns:turn.example.org:5349?transport=tcp"))
    assert stun.servers() == [("turn.example.org", 3480), ("turn.example.org", 3478), PROD, GOOGLE, CLOUDFLARE]
    stun.note_cloud_ice([])  # nothing new: the known ones stay
    assert stun.servers()[0] == ("turn.example.org", 3480)
    stun.note_cloud_ice(ice("turn:other.example.org"))
    assert stun.servers()[0] == ("other.example.org", 3478)


def test_servers_by_environment(clean, monkeypatch):
    for env, server in (("prod", PROD), ("stage", ("turn.stage.zorcam.ru", 3488)), ("test", ("turn.test.zorcam.ru", 3498))):
        monkeypatch.setattr(stun, "_environment", env)
        assert stun.servers() == [server, GOOGLE, CLOUDFLARE]
    monkeypatch.setattr(stun, "_environment", "other")  # unknown stand: only the public ones
    assert stun.servers() == [GOOGLE, CLOUDFLARE]


def test_servers_from_cloud_json(clean, monkeypatch):
    """The cloud STUN saved in cloud.json comes first and survives a restart; ICE hosts follow, duplicates go once."""
    cloud = {"stun": "stun.cloud.example:3479"}
    monkeypatch.setattr(stun.zorcam_cloud, "load", lambda: cloud)
    assert stun.servers() == [("stun.cloud.example", 3479), PROD, GOOGLE, CLOUDFLARE]
    stun.note_cloud_ice(ice("turn:stun.cloud.example:3479", "turn:turn.example.org:3480"))
    assert stun.servers() == [("stun.cloud.example", 3479), ("turn.example.org", 3480), PROD, GOOGLE, CLOUDFLARE]
    cloud["stun"] = "stun.zorcam.com:3478"  # the same as the stand's: once
    assert stun.servers() == [PROD, ("stun.cloud.example", 3479), ("turn.example.org", 3480), GOOGLE, CLOUDFLARE]
    for bad in ("junk", "host:0", "host:70000", "host", 5, None, "Host:3478"):
        cloud["stun"] = bad
        assert stun.servers()[0] == ("stun.cloud.example", 3479), bad  # ignored


def test_parameter_replaces_the_list(clean):
    stun.note_cloud_ice(ice("turn:turn.example.org:3480"))
    stun.configure("test", "stun.my.example, 192.0.2.7:3490 ,stun.my.example:3478")
    assert stun.servers() == [("stun.my.example", 3478), ("192.0.2.7", 3490)]  # no cloud, stand or public ones
    stun.configure("test", "")
    assert stun.servers()[0] == ("turn.example.org", 3480) and ("turn.test.zorcam.ru", 3498) in stun.servers()


def test_probe_goes_through_the_servers_until_an_answer(clean):
    asked, lines = [], []

    def ask(host, port):
        asked.append(host)
        if host == "stun.cloudflare.com":
            return "93.184.216.34"
        raise TimeoutError

    stun.note_cloud_ice(ice("turn:turn.example.org:3478"))
    assert stun.probe(lines.append, ask) == "93.184.216.34"
    assert asked == ["turn.example.org", "stun.zorcam.com", "stun.l.google.com", "stun.cloudflare.com"]
    assert stun.external() == ("93.184.216.34", "stun.cloudflare.com:3478")
    stun.probe(lines.append, ask)
    assert len(lines) == 1 and "93.184.216.34" in lines[0]  # unchanged: not logged again


def test_probe_logs_only_changes(clean):
    lines, answers = [], iter(["93.184.216.34", "93.184.216.35", None, None, "93.184.216.35"])

    def ask(host, port):
        got = current[0]
        if got is None:
            raise TimeoutError
        return got

    current = [None]
    for ans in answers:
        current[0] = ans
        stun.probe(lines.append, ask)
    assert [m.split(" (")[0] for m in lines] == [
        "stun: external address 93.184.216.34", "stun: external address changed 93.184.216.34 -> 93.184.216.35",
        "stun: external address lost: no STUN server answered", "stun: external address 93.184.216.35"]
    assert "lost" in lines[2] and "93.184.216.35" in lines[3]


def test_probe_without_any_answer_and_with_cgnat(clean):
    lines = []

    def silent(host, port):
        raise OSError("down")
    assert stun.probe(lines.append, silent) is None and stun.probe(lines.append, silent) is None
    assert lines == ["stun: external address not determined: no STUN server answered"]
    assert stun.external() == (None, None) and stun.snapshot()["checked"]
    assert stun.probe(lines.append, lambda h, p: "100.64.1.2") is None
    assert "not a public address" in lines[-1] and "100.64.1.2" in lines[-1] and stun.snapshot()["why"] == "100.64.1.2"


def test_next_delay(clean):
    def only(name):
        def ask(host, port):
            if host == name:
                return "93.184.216.34"
            raise TimeoutError
        return ask

    stun.probe(ask=only("nobody"))
    assert stun.next_delay() == stun.RETRY_NONE  # no answer: soon again
    stun.probe(ask=only("stun.cloudflare.com"))
    assert stun.next_delay() == stun.RETRY_FALLBACK  # a fallback may see a router VPN exit: ask the first one soon
    stun.probe(ask=only("stun.zorcam.com"))
    assert stun.next_delay() == stun.INTERVAL


def test_query_with_a_local_server():
    import socket
    import threading
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))

    def serve():
        data, addr = srv.recvfrom(1024)
        tid = data[8:20]
        srv.sendto(response(bytes(12), [(stun.XOR_MAPPED_ADDRESS, xor_addr("1.2.3.4"))]), addr)  # foreign id first
        srv.sendto(response(tid, [(stun.XOR_MAPPED_ADDRESS, xor_addr("93.184.216.34"))]), addr)
    threading.Thread(target=serve, daemon=True).start()
    try:
        assert stun.query("127.0.0.1", srv.getsockname()[1], timeout=2) == "93.184.216.34"
    finally:
        srv.close()
