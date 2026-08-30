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
    if addr in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return False


def _uses_proxy(url: str) -> bool:
    """True if requests would route this URL through a proxy (honors NO_PROXY/all_proxy).

    Scheme-specific: get_environ_proxies may include a non-empty ``no`` key (from
    NO_PROXY) even when no proxy is actually configured; requests only ever routes
    via the URL's scheme entry, so only that decides.
    """
    proxies = requests.utils.get_environ_proxies(url, no_proxy=None)
    return bool(proxies.get(urlparse(url).scheme))


def is_public_http_url(url: str) -> bool:
    """True if url is http(s) and safe to fetch.

    - Literal IP hosts are always checked directly (never proxied).
    - Hostnames are checked at name level: localhost/.local/.localhost are
      rejected in all modes.
    - With an HTTP(S) proxy configured, the connection goes through the proxy
      which resolves the real host, so DNS-IP checks are skipped (proxy-aware;
      e.g. fake-ip proxies synthesize reserved-range addresses).
    - Without a proxy, every resolved address must be public.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    host = host.rstrip(".").lower()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return not _is_private_ip(host)
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False
    if _uses_proxy(url):
        return True
    try:
        addrs = [info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_UNSPEC)]
    except OSError:
        return False
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


import re


def _guess_charset(content_type: str, body: bytes) -> str:
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    if m:
        return m.group(1)
    head = body[:1024].decode("latin-1", errors="replace")
    m = re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", head, re.I)
    if m:
        return m.group(1)
    return "utf-8"


def _read_limited(resp, max_bytes: int) -> bytes:
    content_length = resp.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                resp.close()
                raise WebFetchError(f"response too large: {content_length} bytes")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=8192):
        if total >= max_bytes:
            break
        room = max_bytes - total
        part = chunk[:room]
        chunks.append(part)
        total += len(part)
    return b"".join(chunks)


def _request_with_validation(session, url: str, timeout: float):
    for _ in range(10):
        if not is_public_http_url(url):
            raise WebFetchError("refusing non-public or unsupported URL")
        resp = session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise WebFetchError("redirect without Location header")
            url = urljoin(url, location)
            continue
        return resp
    raise WebFetchError("too many redirects")


def fetch(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BYTES,
    session=None,
) -> WebContent:
    """Fetch a public http(s) page and extract title/text/links. Raises WebFetchError."""
    close_session = session is None
    if session is None:
        session = requests.Session()
    try:
        try:
            resp = _request_with_validation(session, url, timeout)
            try:
                if resp.status_code != 200:
                    preview = _read_limited(resp, 4096).decode("utf-8", errors="replace")[:200]
                    raise WebFetchError(f"HTTP {resp.status_code}: {preview}")
                content_type = resp.headers.get("Content-Type", "").lower().split(";")[0].strip()
                if content_type not in ("text/html", "text/plain") and content_type:
                    raise WebFetchError(f"unsupported content type: {content_type}")
                body = _read_limited(resp, max_bytes)
                if content_type == "text/plain":
                    charset = _guess_charset(resp.headers.get("Content-Type", ""), body)
                    return WebContent(title="", text=body.decode(charset, errors="replace"), links=[])
                charset = _guess_charset(resp.headers.get("Content-Type", ""), body)
                html = body.decode(charset, errors="replace")
                return extract_web_content(html, resp.url)
            finally:
                resp.close()
        except WebFetchError:
            raise
        except requests.RequestException as e:
            raise WebFetchError(f"request failed: {e}") from e
        except OSError as e:
            raise WebFetchError(f"network error: {e}") from e
    finally:
        if close_session:
            session.close()


from urllib.parse import parse_qs

SEARCH_MAX_RESULTS = 8
SEARCH_BASE = "https://lite.duckduckgo.com/lite/"


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def _extract_uddg_url(href: str) -> str | None:
    try:
        parsed = urlparse(href)
    except ValueError:
        return None
    values = parse_qs(parsed.query).get("uddg")
    if not values:
        return None
    url = values[0]
    if url.startswith(("http://", "https://")):
        return url
    return None


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._in_link = False
        self._link_title: list[str] = []
        self._link_url: str | None = None
        self._in_snippet = False
        self._snippet_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "a" and attr.get("class") == "result-link":
            self._in_link = True
            self._link_title = []
            self._link_url = _extract_uddg_url(attr.get("href") or "")
        elif tag == "td" and attr.get("class") == "result-snippet":
            self._in_snippet = True
            self._snippet_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link:
            title = " ".join("".join(self._link_title).split())
            if self._link_url:
                self.results.append(SearchResult(title=title, url=self._link_url, snippet=""))
            self._in_link = False
        elif tag == "td" and self._in_snippet:
            snippet = " ".join("".join(self._snippet_parts).split())
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            if self.results and not self.results[-1].snippet:
                self.results[-1].snippet = snippet
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._link_title.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)


def parse_search_results(html: str) -> list[SearchResult]:
    parser = _SearchParser()
    parser.feed(html)
    return parser.results


import time
from urllib.parse import urlencode


def search(
    query: str,
    *,
    max_results: int = SEARCH_MAX_RESULTS,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BYTES,
    session=None,
) -> list[SearchResult]:
    """Search DuckDuckGo Lite and return parsed results. Raises WebFetchError."""
    if not query.strip():
        raise WebFetchError("query is required")
    max_results = max(1, min(10, max_results))
    close_session = session is None
    if session is None:
        session = requests.Session()
    url = f"{SEARCH_BASE}?{urlencode({'q': query})}"
    last_error: Exception | None = None
    try:
        for attempt in range(3):
            try:
                resp = _request_with_validation(session, url, timeout)
                try:
                    if resp.status_code != 200:
                        raise WebFetchError(f"HTTP {resp.status_code}")
                    content_type = resp.headers.get("Content-Type", "").lower().split(";")[0].strip()
                    if content_type not in ("text/html",) and content_type:
                        raise WebFetchError(f"unsupported content type: {content_type}")
                    body = _read_limited(resp, max_bytes)
                    charset = _guess_charset(resp.headers.get("Content-Type", ""), body)
                    html = body.decode(charset, errors="replace")
                    return parse_search_results(html)[:max_results]
                finally:
                    resp.close()
            except WebFetchError as e:
                last_error = e
            except requests.RequestException as e:
                last_error = WebFetchError(f"request failed: {e}")
            except OSError as e:
                last_error = WebFetchError(f"network error: {e}")
            if attempt < 2:
                time.sleep(1)
        raise WebFetchError(f"search failed after 3 attempts: {last_error}")
    finally:
        if close_session:
            session.close()
