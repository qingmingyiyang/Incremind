"""Narrow URL admission and redirect handling for workspace video sources."""

from __future__ import annotations

import http.client
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit


_DIRECT_HOSTS = {
    "bilibili": {"bilibili.com", "www.bilibili.com", "m.bilibili.com"},
    "xiaohongshu": {"xiaohongshu.com", "www.xiaohongshu.com"},
}
_SHORT_HOSTS = {"b23.tv": "bilibili", "xhslink.com": "xiaohongshu"}


def media_platform(url: str) -> str | None:
    """Classify only known video/share hosts, without fetching the URL."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError, UnicodeError):
        return None
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or port not in {None, 443} or parsed.fragment):
        return None
    if host in _SHORT_HOSTS:
        return _SHORT_HOSTS[host]
    for platform, hosts in _DIRECT_HOSTS.items():
        if host in hosts and (parsed.path.startswith("/video/") if platform == "bilibili"
                              else parsed.path.startswith(("/explore/", "/discovery/item/"))):
            return platform
    return None


def resolve_media_url(url: str) -> tuple[str, str]:
    """Resolve a known short host to a matching platform URL with pinned DNS.

    Direct platform URLs are returned unchanged because their query may be
    needed during one transient fetch. Callers expose only a canonical URL.
    """
    platform = media_platform(url)
    if platform is None:
        raise ValueError("unsupported_media_url")
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host not in _SHORT_HOSTS:
        return platform, url
    current = url
    for _ in range(3):
        location = _redirect_location(current)
        if not location:
            raise ValueError("media_short_link_unavailable")
        target = urljoin(current, location)
        target_platform = media_platform(target)
        if target_platform != platform:
            raise ValueError("media_short_link_target_invalid")
        target_host = (urlsplit(target).hostname or "").lower().rstrip(".")
        if target_host not in _SHORT_HOSTS:
            return platform, target
        current = target
    raise ValueError("media_short_link_redirect_limit")


def _redirect_location(url: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in _SHORT_HOSTS:
        raise ValueError("media_short_link_target_invalid")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError("media_short_link_address_blocked")
        address = addresses[0][4][0]
        connection = http.client.HTTPSConnection(host, 443, timeout=12)
        connection._create_connection = lambda _address, timeout=None, source_address=None: socket.create_connection(
            (address, 443), timeout=timeout, source_address=source_address)
        try:
            connection.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                               headers={"User-Agent": "ChriptmasWorkspace/1", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status not in {301, 302, 303, 307, 308}:
                raise ValueError("media_short_link_unavailable")
            return response.getheader("location")
        finally:
            connection.close()
    except ValueError:
        raise
    except Exception:
        raise ValueError("media_short_link_unavailable") from None
