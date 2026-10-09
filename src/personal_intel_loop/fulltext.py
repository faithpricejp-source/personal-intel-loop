"""全文抓取: requests(15s, 浏览器 UA) + trafilatura, 两者都可注入; 每条抓完立即提交, 可续跑。

默认 http_get 带 SSRF 防护: 只允许 http/https, 解析目标主机并阻断私网/环回/链路本地地址,
重定向逐跳同样校验。每跳只解析一次, TCP 连接只连这次校验过的 IP
(连接层固定, URL 与 Host/SNI/证书主机名仍是原域名), 堵住"校验时公网、连接时 127.0.0.1"的
DNS 重绑定窗口; 全文抓取不走代理(见 _pinned_get)。
"""
from __future__ import annotations

import ipaddress
import socket
import sqlite3
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urljoin, urlsplit

FULLTEXT_MAX_CHARS = 20000
DEFAULT_TIMEOUT_SECONDS = 15
# 重定向逐跳跟随的上限: 新闻站短链套短链常见 ≥2 跳
MAX_REDIRECT_HOPS = 5
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

def _is_blocked_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """非全局(私有/环回/链路本地/CGNAT/文档/保留/组播)地址: 全文抓取只面向公网新闻页。

    用标准库 ipaddress 的 IANA 特殊用途地址表(is_global 等)判定, 不手写网段清单。
    """
    return (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
    )


def _assert_public_http_url(url: str) -> list[str]:
    """校验并返回该 URL 主机解析出的全部地址(去重保序); 任一地址落在非公网段即整条拒。"""
    parts = urlsplit(str(url or ""))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"unsupported fulltext url: {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    addresses: list[str] = []
    for info in socket.getaddrinfo(parts.hostname, port):
        address = ipaddress.ip_address(info[4][0])
        if _is_blocked_address(address):
            raise ValueError(f"blocked fulltext url (non-public address): {url!r}")
        if str(address) not in addresses:
            addresses.append(str(address))
    if not addresses:
        raise ValueError(f"fulltext url resolved to no address: {url!r}")
    return addresses


def _connect_pinned(ip: str, port: int, timeout, source_address, socket_options) -> socket.socket:
    """按 IP 字面量直接建 TCP 连接。刻意不走 urllib3 create_connection: 它对 IP 字面量也会再调一次
    getaddrinfo, 那是又一个可被劫持的解析点。测试在此打桩记录目标 IP。"""
    from urllib3.util.timeout import _DEFAULT_TIMEOUT

    sock = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM)
    try:
        for option in socket_options or ():
            sock.setsockopt(*option)
        if timeout is not _DEFAULT_TIMEOUT:
            sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        sock.connect((ip, port))
        return sock
    except BaseException:
        sock.close()
        raise


def _pinned_connection_classes():
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

    class _PinnedMixin:
        def __init__(self, *args, pinned_addresses=(), **kwargs):
            self._pinned_addresses = tuple(pinned_addresses)
            super().__init__(*args, **kwargs)

        def _new_conn(self):  # 覆盖 urllib3: 不按 self.host 解析, 只连已校验的 IP
            if not self._pinned_addresses:
                raise NewConnectionError(self, "no pinned address (refusing to resolve again)")
            last_error: OSError | None = None
            for ip in self._pinned_addresses:
                try:
                    return _connect_pinned(ip, self.port, self.timeout, self.source_address, self.socket_options)
                except OSError as e:  # 含 TimeoutError: 换下一个已校验 IP
                    last_error = e
            if isinstance(last_error, TimeoutError):
                raise ConnectTimeoutError(self, f"Connection to {self.host} timed out (pinned {self._pinned_addresses})")
            raise NewConnectionError(self, f"Failed to connect to pinned {self._pinned_addresses}: {last_error}")

    class PinnedHTTPConnection(_PinnedMixin, HTTPConnection):
        pass

    # HTTPS: self.host 仍是域名, urllib3 以它做 SNI 与证书主机名校验(server_hostname 未覆写)
    class PinnedHTTPSConnection(_PinnedMixin, HTTPSConnection):
        pass

    return PinnedHTTPConnection, PinnedHTTPSConnection


def _pinned_get(url: str, *, addresses: list[str], **kwargs):
    """单跳 GET: 新建 Session + 固定 IP 的 adapter, 用完即关(每跳独立 pin, 不跨跳复用连接)。

    代理: 全文抓取一律直连。走代理时 DNS 由代理解析, 固定 IP 无意义。开发环境核实无代理:
    进程环境无 *_PROXY、launchd plist 无代理变量、`scutil --proxy` 无 HTTP(S)Proxy;
    故 trust_env=False 不改变现状, 只是把"不走代理"钉死(也不读 .netrc / REQUESTS_CA_BUNDLE)。
    若将来要为全文抓取上代理, 需另做: 代理侧过滤内网, 或改用 requests-hardened 之类的代理感知方案。
    """
    import requests
    from requests.adapters import HTTPAdapter

    if not hasattr(HTTPAdapter, "get_connection_with_tls_context"):  # requests<2.32 不调此钩子 → 会静默不 pin
        raise RuntimeError("requests>=2.32 required for pinned fulltext fetch")
    http_cls, https_cls = _pinned_connection_classes()
    # 与 pool.host 同一编码口径比较(requests 把 IDN 主机转成 xn--, urlsplit 不转)
    hostname = urlsplit(requests.Request("GET", url).prepare().url).hostname

    class _PinnedAdapter(HTTPAdapter):
        def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
            if any((proxies or {}).values()):
                raise ValueError("fulltext fetch refuses proxies (IP pinning would be bypassed)")
            pool = super().get_connection_with_tls_context(request, verify, proxies=None, cert=cert)
            if pool.host != hostname:
                raise ValueError(f"pinned host mismatch: {pool.host!r} != {hostname!r}")
            pool.ConnectionCls = https_cls if pool.scheme == "https" else http_cls
            pool.conn_kw["pinned_addresses"] = tuple(addresses)
            return pool

    with requests.Session() as session:
        session.trust_env = False
        adapter = _PinnedAdapter()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session.get(url, **kwargs)


def _default_http_get(url: str) -> str:
    addresses = _assert_public_http_url(url)
    resp = _pinned_get(
        url,
        addresses=addresses,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
        allow_redirects=False,
    )
    # 10-05 审计 F22: 逐跳跟随到非 3xx 为止, 每跳都做 SSRF 校验; 原先只跟一跳,
    # 第二跳仍是 3xx 时 raise_for_status 不抛, 跳转页 HTML 被当正文返回
    for _hop in range(MAX_REDIRECT_HOPS):
        if not (resp.is_redirect or resp.is_permanent_redirect):
            break
        location = resp.headers.get("Location", "")
        # Location 允许是相对地址(路径相对/协议相对, RFC 7231, Django/nginx 都会发),
        # 先按当前响应 URL 补全再校验, 否则相对跳转全部被当非法 URL 拒掉
        location = urljoin(resp.url, location)
        # 每跳重新解析一次并把这次的结果固定给连接层
        addresses = _assert_public_http_url(location)
        resp = _pinned_get(
            location,
            addresses=addresses,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=False,
        )
    else:
        raise ValueError(f"too many redirects (>{MAX_REDIRECT_HOPS}): {url!r}")
    resp.raise_for_status()
    return resp.text


def _default_extract(html: str) -> str | None:
    import trafilatura

    return trafilatura.extract(html)


_IMAGE_META_RE = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:og:image(?::url)?|twitter:image(?::src)?)["'][^>]*content=["']([^"']+)["']"""
    r"""|<meta[^>]+content=["']([^"']+)["'][^>]*(?:property|name)=["'](?:og:image(?::url)?|twitter:image(?::src)?)["']""",
    re.IGNORECASE,
)
# 抓全文时顺带取的主图：url -> image_url（fetch_fulltext 写，ensure_fulltext 取走）；保持 fetcher 两元组接口不变
_LAST_IMAGE: dict[str, str] = {}


def extract_main_image(html: str, page_url: str = "") -> str | None:
    """页面主图：og:image / twitter:image；相对地址按页面地址补全；只收 http(s)。"""
    from urllib.parse import urljoin

    m = _IMAGE_META_RE.search(html or "")
    if not m:
        return None
    src = (m.group(1) or m.group(2) or "").strip().replace("&amp;", "&")
    src = urljoin(page_url, src) if page_url else src
    return src if src.startswith(("http://", "https://")) else None


_WSCN_ARTICLE_RE = re.compile(r"^https?://(?:www\.)?wallstreetcn\.com/articles/(\d+)")
WSCN_API = "https://api-one-wscn.awtmt.com/apiv1/content/articles/{}?extract=0"


def _html_fragment_to_text(fragment: str) -> str:
    import html as html_lib

    text = re.sub(r"(?i)<br\s*/?>|</(?:p|div|h[1-6]|li|blockquote)>", "\n", fragment or "")
    text = html_lib.unescape(re.sub(r"<[^>]+>", "", text))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _fetch_wallstreetcn(url: str, article_id: str, http_get: Callable[[str], str]) -> tuple[str | None, str]:
    """华尔街见闻文章页是 JS 渲染空壳(10-07 实测 4.5KB 无正文, 36/36 条 failed), 正文走其公开接口。"""
    import json

    try:
        data = (json.loads(http_get(WSCN_API.format(article_id))) or {}).get("data") or {}
        text = _html_fragment_to_text(data.get("content") or "")
        image = (data.get("image") or {}).get("uri") if isinstance(data.get("image"), dict) else None
    except Exception:
        return None, "failed"
    if not text:
        return None, "failed"
    if isinstance(image, str) and image.startswith(("http://", "https://")):
        _LAST_IMAGE[url] = image
    return text[:FULLTEXT_MAX_CHARS], "ok"


def fetch_fulltext(
    url: str,
    *,
    http_get: Callable[[str], str] | None = None,
    extract: Callable[[str], str | None] | None = None,
) -> tuple[str | None, str]:
    """抓单条正文, 返回 (正文或 None, status)。正文截到 20000 字; 任何失败都归为 failed 不抛。"""
    http_get = http_get or _default_http_get
    extract = extract or _default_extract
    wscn = _WSCN_ARTICLE_RE.match(url or "")
    if wscn:
        return _fetch_wallstreetcn(url, wscn.group(1), http_get)
    try:
        html = http_get(url)
        text = extract(html) if html else None
        image = extract_main_image(html, url) if isinstance(html, str) else None
        if image:
            _LAST_IMAGE[url] = image
    except Exception:
        return None, "failed"
    text = (text or "").strip()
    if not text:
        return None, "failed"
    return text[:FULLTEXT_MAX_CHARS], "ok"


FAILED_RETRY_AFTER = timedelta(hours=6)


def _retry_due(status: str | None, fetched_at: str | None) -> bool:
    """抓取失败的行 6 小时后可重试（10-05 审计 F23：原先失败一次即永久放弃，10-04 期 76 条里 23 条）。"""
    if status != "failed":
        return False
    try:
        when = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - when >= FAILED_RETRY_AFTER


def ensure_fulltext(
    conn: sqlite3.Connection,
    item_ids,
    *,
    fetcher: Callable[[str], tuple[str | None, str]] = fetch_fulltext,
    sleep_s: float = 1.0,
) -> int:
    """为一批 item 抓全文。已有 item_fulltext 记录的跳过(可续跑); 每条抓完立即写库提交;
    单条失败写 status='failed' 不抛异常。返回本轮新抓的条数。"""
    fetched = 0
    ids = list(item_ids)
    for index, item_id in enumerate(ids):
        existing = conn.execute("SELECT status, fetched_at FROM item_fulltext WHERE item_id=?", (item_id,)).fetchone()
        if existing is not None and not _retry_due(existing["status"], existing["fetched_at"]):
            continue
        row = conn.execute("SELECT url FROM items WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            continue
        try:
            text, status = fetcher(row["url"])
        except Exception:
            text, status = None, "failed"
        text = (text or "").strip()[:FULLTEXT_MAX_CHARS] or None
        image = _LAST_IMAGE.pop(row["url"], None)
        conn.execute(
            "INSERT OR REPLACE INTO item_fulltext (item_id, text, status, fetched_at, image_url) VALUES (?, ?, ?, ?, ?)",
            (item_id, text, status, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), image),
        )
        conn.commit()
        fetched += 1
        if sleep_s and index < len(ids) - 1:
            time.sleep(sleep_s)
    return fetched
