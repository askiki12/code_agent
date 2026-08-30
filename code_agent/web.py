"""Web retrieval: public-URL validation, HTML text extraction, and fetching."""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

MAX_LINKS = 10
DEFAULT_TIMEOUT = 20.0
MAX_BYTES = 2 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (compatible; code_agent/0.1)"


class WebFetchError(Exception):
    pass


@dataclass
class WebContent:
    title: str
    text: str
    links: list[str]


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return _is_private_ip(str(addr.ipv4_mapped))
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return True
    return False


def is_public_http_url(url: str) -> bool:
    """True if url is http(s) and every resolved IP is a public address."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            addrs = [info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_UNSPEC)]
        except OSError:
            return False
    else:
        addrs = [host]
    return all(not _is_private_ip(a) for a in addrs)


from html.parser import HTMLParser
from urllib.parse import urljoin


class _TextExtractor(HTMLParser):
    def __init__(self, base_url: str, max_links: int = MAX_LINKS) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._max_links = max_links
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.links: list[str] = []
        self._block_tags = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "pre", "ul", "ol", "blockquote"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag == "title" and not self._title_parts:
            self._in_title = True
        if tag in self._block_tags:
            self._text_parts.append("\n")
        for name, value in attrs:
            if name == "href" and value and len(self.links) < self._max_links:
                absolute = urljoin(self._base_url, value)
                if absolute.startswith(("http://", "https://")):
                    self.links.append(absolute)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        if tag in self._block_tags:
            self._text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        elif self._skip_depth == 0:
            self._text_parts.append(data)


def extract_web_content(html: str, base_url: str) -> WebContent:
    parser = _TextExtractor(base_url)
    parser.feed(html)
    title = " ".join("".join(parser._title_parts).split())
    lines = [" ".join(line.split()) for line in "".join(parser._text_parts).splitlines()]
    lines = [line for line in lines if line]
    return WebContent(title=title, text="\n".join(lines), links=parser.links)
