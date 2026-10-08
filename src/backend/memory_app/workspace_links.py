"""Validated public text-link acquisition."""

from __future__ import annotations

import ipaddress
import http.client
import html
import zlib
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlsplit
from fastapi import HTTPException
from .workspace_contracts import _MAX_FILE, _MAX_TEXT, _text


def _fetch_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise HTTPException(400, "invalid_url") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or port not in (None, 80, 443)):
        raise HTTPException(400, "invalid_url")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port or (443 if parsed.scheme == "https" else 80))
        if not addresses or any(not ipaddress.ip_address(info[4][0]).is_global for info in addresses):
            raise HTTPException(400, "url_address_blocked")
        # Pin the vetted address for the actual connection; a second DNS lookup
        # at connect time would leave a rebinding window after the address check.
        address = addresses[0][4][0]
        port = port or (443 if parsed.scheme == "https" else 80)
        connection = (http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection)(
            parsed.hostname, port, timeout=12)
        connection._create_connection = lambda _address, timeout=None, source_address=None: socket.create_connection(
            (address, port), timeout=timeout, source_address=source_address)
        try:
            connection.request("GET", (parsed.path or "/") + (("?" + parsed.query) if parsed.query else ""),
                               headers={"User-Agent": "ChriptmasWorkspace/1", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status in range(300, 400):
                raise HTTPException(422, "link_redirect_disallowed")
            if response.status >= 400:
                raise HTTPException(422, "link_fetch_failed")
            if "text/html" not in response.getheader("content-type", "") and "text/plain" not in response.getheader("content-type", ""):
                raise HTTPException(415, "unsupported_link_type")
            data = response.read(_MAX_FILE + 1)
            encoding = response.getheader("content-encoding", "").lower().strip()
        finally:
            connection.close()
        if len(data) > _MAX_FILE:
            raise HTTPException(413, "link_too_large")
        if encoding == "gzip":
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            data = decoder.decompress(data, _MAX_FILE + 1)
            if not decoder.eof or len(data) > _MAX_FILE:
                raise HTTPException(413, "link_too_large")
        elif encoding not in {"", "identity"}:
            raise HTTPException(415, "unsupported_link_encoding")
        if len(data) > _MAX_FILE:
            raise HTTPException(413, "link_too_large")
        text = data.decode("utf-8", errors="replace")
        if parsed.hostname == "mp.weixin.qq.com":
            parser = _WechatArticleText()
            parser.feed(text)
            text = re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()
            if not text:
                # A verification/removed article page is never source material.
                raise HTTPException(422, "link_fetch_failed")
        elif "<html" in text[:1000].lower():
            article = re.search(r"<(?:article|main)\b[^>]*>(.*?)</(?:article|main)>", text, re.I | re.S)
            parser = _HTMLText()
            parser.feed(article.group(1) if article else text)
            text = re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts)))
        return _text(text[:_MAX_TEXT], "source_text")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "link_fetch_failed") from None


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "header", "footer", "aside", "form"}:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "header", "footer", "aside", "form"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


class _WechatArticleText(_HTMLText):
    """Restrict the existing script/style filter to the real article subtree."""

    _VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "param", "source", "track", "wbr"})

    def __init__(self):
        super().__init__()
        self._depth = 0
        self._found = False

    def handle_starttag(self, tag, attrs):
        if self._depth:
            if tag not in self._VOID:
                self._depth += 1
            super().handle_starttag(tag, attrs)
        elif not self._found and ("id", "js_content") in attrs and tag not in self._VOID:
            self._found = True
            self._depth = 1
            super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self._depth:
            super().handle_endtag(tag)
            if tag not in self._VOID:
                self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            super().handle_data(data)
