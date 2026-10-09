"""全文抓取的 SSRF 校验与真实连接必须用同一次 DNS 解析结果(防 DNS 重绑定)。

全部离线: 本地 127.0.0.1 HTTP/HTTPS server + 打桩 getaddrinfo; 连接 seam `_connect_pinned` 记录目标 IP
后转连本地 "公网替身" server, 不出网。修复前的同一测试会连到本地 "内网" server 拿到 SECRET。
"""
from __future__ import annotations

import datetime as dt
import http.server
import socket
import ssl
import threading

import pytest
import requests

from personal_intel_loop import fulltext

PUBLIC_A = "93.184.216.34"
PUBLIC_B = "93.184.216.35"


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    seen_hosts: list = []

    def do_GET(self):  # noqa: N802
        host = (self.headers.get("Host") or "").split(":")[0]
        type(self).seen_hosts.append(host)
        status, body, location = type(self).routes.get(host, type(self).routes.get("*", (404, "nf", None)))
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        data = body.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _serve(routes, tls_context=None):
    handler = type("H", (_Handler,), {"routes": routes, "seen_hosts": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler


@pytest.fixture
def servers():
    started = []

    def start(routes, tls_context=None):
        server, handler = _serve(routes, tls_context)
        started.append(server)
        return server, handler

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def _rebinding_dns(monkeypatch, first_answers):
    """每个主机名第一次解析给公网 IP, 之后一律给 127.0.0.1(典型 DNS 重绑定)。"""
    calls: dict[str, int] = {}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls[host] = calls.get(host, 0) + 1
        ip = first_answers[host] if calls[host] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return calls


def _route_pinned_to(monkeypatch, public_port):
    """连接 seam: 记录被要求连的 (ip, port), 实际转连本地 '公网替身' server。"""
    targets = []

    def fake_connect(ip, port, timeout, source_address, socket_options):
        targets.append((ip, port))
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # 不用 create_connection: 它会撞上 DNS 桩
        sock.settimeout(5)
        sock.connect(("127.0.0.1", public_port))
        return sock

    monkeypatch.setattr(fulltext, "_connect_pinned", fake_connect, raising=False)
    return targets


def test_n4_rebinding_connects_to_validated_ip_not_localhost(monkeypatch, servers):
    secret, secret_handler = servers({"*": (200, "SECRET-LOCAL", None)})
    public, public_handler = servers({"*": (200, "PUBLIC-PAGE", None)})
    port = secret.server_address[1]
    calls = _rebinding_dns(monkeypatch, {"evil.example.com": PUBLIC_A})
    targets = _route_pinned_to(monkeypatch, public.server_address[1])

    try:
        html = fulltext._default_http_get(f"http://evil.example.com:{port}/news")
    except ValueError:  # 修复前: 请求已发到本机, 事后校验才拒掉响应(盲 SSRF)
        html = None

    assert secret_handler.seen_hosts == [], "请求不得打到本机(哪怕响应事后被丢弃)"
    assert html == "PUBLIC-PAGE", "连接必须落在校验过的 IP 上, 不能被第二次解析带到本机"
    assert targets == [(PUBLIC_A, port)]
    assert calls == {"evil.example.com": 1}, "每跳只允许解析一次"
    assert public_handler.seen_hosts == ["evil.example.com"], "Host 头仍是原域名"


def test_n4_each_redirect_hop_is_pinned_independently(monkeypatch, servers):
    secret, secret_handler = servers({"*": (200, "SECRET-LOCAL", None)})
    port = secret.server_address[1]
    public, _ = servers({
        "a.example.com": (302, "", f"http://b.example.com:{port}/final"),
        "b.example.com": (200, "PUBLIC-B", None),
    })
    calls = _rebinding_dns(monkeypatch, {"a.example.com": PUBLIC_A, "b.example.com": PUBLIC_B})
    targets = _route_pinned_to(monkeypatch, public.server_address[1])

    try:
        html = fulltext._default_http_get(f"http://a.example.com:{port}/s")
    except ValueError:
        html = None

    assert secret_handler.seen_hosts == [], "任何一跳都不得打到本机"
    assert html == "PUBLIC-B"
    assert targets == [(PUBLIC_A, port), (PUBLIC_B, port)]
    assert calls == {"a.example.com": 1, "b.example.com": 1}


def test_n4_proxy_env_is_ignored(monkeypatch, servers):
    """全文抓取不走代理(走代理则 DNS 由代理解析, 固定 IP 失效); 环境里设了代理也直连。"""
    public, _ = servers({"*": (200, "PUBLIC-PAGE", None)})
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    _rebinding_dns(monkeypatch, {"news.example.com": PUBLIC_A})
    targets = _route_pinned_to(monkeypatch, public.server_address[1])

    assert fulltext._default_http_get("http://news.example.com/x") == "PUBLIC-PAGE"
    assert targets == [(PUBLIC_A, 80)]


@pytest.mark.parametrize("host", ["日本語.jp", "News.Example.COM", "news.example.com."])
def test_n4_idn_case_and_trailing_dot_hosts_are_not_rejected(monkeypatch, servers, host):
    """防御性 host 比对不得误伤 IDN(requests 转 xn--)/大小写/尾点主机名。"""
    public, _ = servers({"*": (200, "PUBLIC-PAGE", None)})
    targets = _route_pinned_to(monkeypatch, public.server_address[1])
    resp = fulltext._pinned_get(f"http://{host}/x", addresses=[PUBLIC_A], timeout=5, allow_redirects=False)
    assert resp.text == "PUBLIC-PAGE"
    assert targets == [(PUBLIC_A, 80)]


def test_n4_first_resolution_private_is_still_blocked(monkeypatch):
    _rebinding_dns(monkeypatch, {"evil.example.com": "127.0.0.1"})
    with pytest.raises(ValueError):
        fulltext._default_http_get("http://evil.example.com/x")


# ---- HTTPS: 连固定 IP, 但 SNI/证书主机名仍按原域名校验 ------------------------------

def _self_signed(tmp_path, dns_name):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(dns_name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / f"{dns_name}.crt"
    key_path = tmp_path / f"{dns_name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return str(cert_path), ctx


def _no_dns(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("pinned connection must not resolve DNS again")
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def test_n4_https_pinned_ip_still_verifies_cert_against_domain(monkeypatch, servers, tmp_path):
    ca, ctx = _self_signed(tmp_path, "news.example.com")
    server, handler = servers({"*": (200, "TLS-OK", None)}, tls_context=ctx)
    _no_dns(monkeypatch)

    resp = fulltext._pinned_get(
        f"https://news.example.com:{server.server_address[1]}/a",
        addresses=["127.0.0.1"], verify=ca, timeout=5, allow_redirects=False)

    assert resp.status_code == 200 and resp.text == "TLS-OK"
    assert handler.seen_hosts == ["news.example.com"]


def test_n4_https_pinned_ip_rejects_cert_for_other_domain(monkeypatch, servers, tmp_path):
    ca, ctx = _self_signed(tmp_path, "other.example.com")
    server, _ = servers({"*": (200, "SHOULD-NOT-READ", None)}, tls_context=ctx)
    _no_dns(monkeypatch)

    with pytest.raises(requests.exceptions.SSLError):
        fulltext._pinned_get(
            f"https://news.example.com:{server.server_address[1]}/a",
            addresses=["127.0.0.1"], verify=ca, timeout=5, allow_redirects=False)
