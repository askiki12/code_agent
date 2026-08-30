import socket

import pytest

from code_agent.web import WebContent, is_public_http_url


def _fake_dns(monkeypatch, mapping):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(8, "nodename nor servname provided, or not known")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
            for ip in mapping[host]
        ]

    monkeypatch.setattr("code_agent.web.socket.getaddrinfo", fake_getaddrinfo)


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    for key in (
        "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
        "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_web_content_dataclass():
    wc = WebContent(title="t", text="b", links=["https://a"])
    assert (wc.title, wc.text, wc.links) == ("t", "b", ["https://a"])


def test_is_public_http_url_hostname_public(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    assert is_public_http_url("https://example.com/path?q=1")


def test_is_public_http_url_public_ip():
    assert is_public_http_url("http://93.184.216.34/")


def test_is_public_http_url_non_http_schemes():
    assert not is_public_http_url("file:///etc/passwd")
    assert not is_public_http_url("ftp://example.com/a")


def test_is_public_http_url_rejects_localhost(monkeypatch):
    _fake_dns(monkeypatch, {"localhost": ["127.0.0.1"]})
    assert not is_public_http_url("http://localhost:8000")


def test_is_public_http_url_rejects_private_ip():
    for bad in [
        "http://127.0.0.1",
        "http://10.0.0.1",
        "http://192.168.1.1",
        "http://172.16.0.5",
        "http://169.254.1.1",
        "http://0.0.0.0",
    ]:
        assert not is_public_http_url(bad), bad


def test_is_public_http_url_rejects_private_ipv6():
    assert not is_public_http_url("http://[::1]")
    assert not is_public_http_url("http://[fc00::1]")
    assert not is_public_http_url("http://[fe80::1]")


def test_is_public_http_url_rejects_cgnat():
    assert not is_public_http_url("http://100.64.0.1")
    assert not is_public_http_url("http://100.100.1.1")


def test_is_public_http_url_rejects_malformed():
    assert not is_public_http_url("not a url")
    assert not is_public_http_url("http://")
    assert not is_public_http_url("example.com")  # 缺 scheme


def test_is_public_http_url_dns_failure(monkeypatch):
    _fake_dns(monkeypatch, {})
    assert not is_public_http_url("https://nohost.invalid")


def test_is_public_http_url_proxy_hostname_allowed(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")
    assert is_public_http_url("https://example.com/path?q=1")


def test_is_public_http_url_proxy_rejects_localhost(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")
    assert not is_public_http_url("http://localhost:8000")


def test_is_public_http_url_proxy_rejects_private_ip(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")
    assert not is_public_http_url("http://10.0.0.1")
    assert not is_public_http_url("http://192.168.1.1")


def test_is_public_http_url_proxy_rejects_non_http(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")
    assert not is_public_http_url("file:///etc/passwd")


def test_is_public_http_url_proxy_rejects_trailing_dot_forms(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")
    assert not is_public_http_url("http://127.0.0.1./")
    assert not is_public_http_url("http://localhost./")
    assert not is_public_http_url("http://10.0.0.1./")


def test_is_public_http_url_no_proxy_rejects_internal_suffixes():
    assert not is_public_http_url("http://example.local/")
    assert not is_public_http_url("http://sub.localhost/")


def test_is_public_http_url_proxy_trailing_dot_public_host_allowed(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7897")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:7897")
    assert is_public_http_url("https://example.com./")


from code_agent.web import extract_web_content


def test_extract_web_content_title_text_links():
    html = """<html><head><title>My Page</title></head><body>
      <h1>Hello</h1><p>Some <b>text</b> here.</p>
      <a href="/about">About</a>
      <a href="https://example.com/x">X</a>
      <a href="javascript:void(0)">bad</a>
    </body></html>"""
    wc = extract_web_content(html, "https://example.com/page")
    assert wc.title == "My Page"
    assert "Hello" in wc.text and "Some text here." in wc.text
    assert wc.links == ["https://example.com/about", "https://example.com/x"]


def test_extract_web_content_skips_script_style():
    html = """<html><head><title>T</title>
      <style>.a { color: red; }</style></head><body>
      <script>var x = 1; if (x) { alert('hi'); }</script>
      <p>visible</p><noscript>noscript body</noscript>
    </body></html>"""
    wc = extract_web_content(html, "https://example.com/")
    assert wc.text == "visible"
    assert "var x" not in wc.text and "noscript body" not in wc.text


def test_extract_web_content_collapses_whitespace():
    html = """<div>  line1

      <p>   spaced   text </p>
      <p>second</p></div>"""
    wc = extract_web_content(html, "https://example.com/")
    assert "line1" in wc.text
    assert "spaced text" in wc.text
    assert "second" in wc.text
    assert "\n" in wc.text


def test_extract_web_content_links_capped_at_max():
    html = "".join(
        f'<a href="/l{i}">L{i}</a>' for i in range(15)
    )
    wc = extract_web_content(html, "https://example.com/")
    assert len(wc.links) == 10


def test_extract_web_content_relative_links_resolved():
    html = '<a href="../up.html">up</a><a href="/root.html">root</a>'
    wc = extract_web_content(html, "https://example.com/a/b/c.html")
    assert wc.links == [
        "https://example.com/a/up.html",
        "https://example.com/root.html",
    ]


import requests

from code_agent.web import WebFetchError, fetch


class FakeResponse:
    def __init__(self, status_code=200, headers=None, body=b"", url="https://example.com/page", chunks=None):
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.url = url
        self._body = body
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size=8192):
        if self._chunks is not None:
            yield from self._chunks
            return
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, headers=None, timeout=None, allow_redirects=None, stream=None):
        self.requests.append(url)
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_fetch_success(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    html = "<html><head><title>Docs</title></head><body><p>Hello world</p><a href='/x'>x</a></body></html>"
    resp = FakeResponse(body=html.encode(), url="https://example.com/page")
    wc = fetch("https://example.com/page", session=FakeSession([resp]))
    assert wc.title == "Docs"
    assert "Hello world" in wc.text
    assert wc.links == ["https://example.com/x"]


def test_fetch_text_plain(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    resp = FakeResponse(headers={"Content-Type": "text/plain; charset=utf-8"}, body="hello world".encode())
    wc = fetch("https://example.com/f.txt", session=FakeSession([resp]))
    assert wc.text == "hello world"
    assert wc.title == "" and wc.links == []


def test_fetch_http_error(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    resp = FakeResponse(status_code=404, body=b"not found", url="https://example.com/nope")
    with pytest.raises(WebFetchError, match="404"):
        fetch("https://example.com/nope", session=FakeSession([resp]))


def test_fetch_timeout(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    with pytest.raises(WebFetchError, match="request failed"):
        fetch("https://example.com/", session=FakeSession([requests.Timeout("boom")]))


def test_fetch_rejects_private_target():
    with pytest.raises(WebFetchError, match="non-public"):
        fetch("http://10.0.0.1/", session=FakeSession([]))


def test_fetch_unsupported_content_type(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    resp = FakeResponse(headers={"Content-Type": "application/pdf"}, body=b"%PDF")
    with pytest.raises(WebFetchError, match="unsupported content type"):
        fetch("https://example.com/a.pdf", session=FakeSession([resp]))


def test_fetch_content_length_too_large(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    resp = FakeResponse(headers={"Content-Type": "text/html", "Content-Length": "3000000"}, body=b"x")
    with pytest.raises(WebFetchError, match="too large"):
        fetch("https://example.com/big", session=FakeSession([resp]))


def test_fetch_stream_truncates_over_limit(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    resp = FakeResponse(headers={"Content-Type": "text/plain"}, chunks=[b"x" * 60, b"y" * 60])
    wc = fetch("https://example.com/big", max_bytes=100, session=FakeSession([resp]))
    assert wc.text == "x" * 60 + "y" * 40


def test_fetch_redirect_revalidates_public(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    r1 = FakeResponse(status_code=302, headers={"Location": "https://example.com/real"}, body=b"")
    r2 = FakeResponse(body="<html><title>Real</title><body>ok</body></html>".encode(), url="https://example.com/real")
    sess = FakeSession([r1, r2])
    wc = fetch("https://example.com/start", session=sess)
    assert sess.requests == ["https://example.com/start", "https://example.com/real"]
    assert wc.title == "Real"


def test_fetch_redirect_to_private_blocked(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    r1 = FakeResponse(status_code=302, headers={"Location": "http://10.0.0.1/evil"}, body=b"")
    sess = FakeSession([r1])
    with pytest.raises(WebFetchError, match="non-public"):
        fetch("https://example.com/start", session=sess)


def test_fetch_redirect_cap(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    responses = [
        FakeResponse(status_code=302, headers={"Location": "https://example.com/x"})
        for _ in range(12)
    ]
    with pytest.raises(WebFetchError, match="too many redirects"):
        fetch("https://example.com/start", session=FakeSession(responses))


def test_fetch_redirect_without_location(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    resp = FakeResponse(status_code=302, headers={})
    with pytest.raises(WebFetchError, match="redirect without Location"):
        fetch("https://example.com/start", session=FakeSession([resp]))


def test_fetch_closes_response(monkeypatch):
    _fake_dns(monkeypatch, {"example.com": ["93.184.216.34"]})
    resp = FakeResponse(body="<html><title>T</title></html>".encode())
    fetch("https://example.com/", session=FakeSession([resp]))
    assert resp.closed


from code_agent.web import SearchResult, parse_search_results

_SAMPLE = """<html><head><title>DuckDuckGo</title></head><body>
<table border="0">
<tr><td valign="top">1.&nbsp;</td>
<td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python-requests.org%2Fen%2Fmaster%2Findex.html&amp;rut=abc" class='result-link'>Requests: HTTP for Humans — Requests 2.34.2 documentation</a></td></tr>
<tr><td>&nbsp;</td><td class='result-snippet'><b>Requests</b>: HTTP for Humans. <b>Requests</b> is an elegant and simple HTTP library.</td></tr>
<tr><td valign="top">2.&nbsp;</td>
<td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=javascript%3Aalert(1)&amp;rut=def" class='result-link'>Bad link</a></td></tr>
<tr><td>&nbsp;</td><td class='result-snippet'>should be filtered out</td></tr>
<tr><td valign="top">3.&nbsp;</td>
<td><a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpypi.org%2Fproject%2Frequests%2F&amp;rut=ghi" class='result-link'>requests · PyPI</a></td></tr>
<tr><td>&nbsp;</td><td class='result-snippet'>The Python Package Index page for requests.</td></tr>
</table>
</body></html>"""


def test_parse_search_results_basic():
    results = parse_search_results(_SAMPLE)
    assert [r.title for r in results] == [
        "Requests: HTTP for Humans — Requests 2.34.2 documentation",
        "requests · PyPI",
    ]
    assert results[0].url == "https://docs.python-requests.org/en/master/index.html"
    assert results[1].url == "https://pypi.org/project/requests/"
    assert results[0].snippet == (
        "Requests: HTTP for Humans. Requests is an elegant and simple HTTP library."
    )


def test_parse_search_results_snippet_attached_to_right_result():
    results = parse_search_results(_SAMPLE)
    assert results[0].snippet.startswith("Requests:")
    assert results[1].snippet == "The Python Package Index page for requests."


def test_parse_search_results_filters_non_http():
    urls = [r.url for r in parse_search_results(_SAMPLE)]
    assert "javascript:alert(1)" not in urls
    assert all(u.startswith(("http://", "https://")) for u in urls)


def test_parse_search_results_snippet_truncated():
    html = (
        '<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2F" '
        "class='result-link'>T</a>"
        f"<td class='result-snippet'>{'y' * 250}</td>"
    )
    results = parse_search_results(html)
    assert len(results) == 1
    assert len(results[0].snippet) == 203
    assert results[0].snippet.endswith("...")


def test_parse_search_results_missing_snippet_empty_string():
    html = (
        '<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2F" '
        "class='result-link'>T</a>"
    )
    results = parse_search_results(html)
    assert len(results) == 1
    assert results[0].snippet == ""


def test_parse_search_results_empty_html():
    assert parse_search_results("") == []
    assert parse_search_results("<html><body>no results here</body></html>") == []


from code_agent.web import WebFetchError, search


def _html_with(n):
    rows = []
    for i in range(1, n + 1):
        rows.append(
            f'<a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F{i}" '
            f"class='result-link'>R{i}</a>"
        )
    return "".join(rows)


def test_search_success(monkeypatch):
    _fake_dns(monkeypatch, {"lite.duckduckgo.com": ["93.184.216.34"]})
    resp = FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=_html_with(3).encode())
    sess = FakeSession([resp])
    results = search("python requests", session=sess)
    assert len(results) == 3
    assert results[0].url == "https://example.com/1"
    assert "lite.duckduckgo.com/lite/" in sess.requests[0]


def test_search_clamps_max_results(monkeypatch):
    _fake_dns(monkeypatch, {"lite.duckduckgo.com": ["93.184.216.34"]})
    resp = FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=_html_with(5).encode())
    r0 = search("q", max_results=0, session=FakeSession([resp]))
    assert len(r0) == 1
    resp = FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=_html_with(5).encode())
    r99 = search("q", max_results=99, session=FakeSession([resp]))
    assert len(r99) == 5


def test_search_upper_clamp_caps_at_ten(monkeypatch):
    _fake_dns(monkeypatch, {"lite.duckduckgo.com": ["93.184.216.34"]})
    resp = FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=_html_with(12).encode())
    results = search("q", max_results=99, session=FakeSession([resp]))
    assert len(results) == 10


def test_search_retries_then_succeeds(monkeypatch):
    _fake_dns(monkeypatch, {"lite.duckduckgo.com": ["93.184.216.34"]})
    sess = FakeSession([
        requests.Timeout("boom1"),
        requests.Timeout("boom2"),
        FakeResponse(headers={"Content-Type": "text/html; charset=utf-8"}, body=_html_with(2).encode()),
    ])
    results = search("q", session=sess)
    assert len(results) == 2


def test_search_persistent_failure(monkeypatch):
    _fake_dns(monkeypatch, {"lite.duckduckgo.com": ["93.184.216.34"]})
    sess = FakeSession([requests.Timeout("boom")] * 3)
    with pytest.raises(WebFetchError, match="after 3 attempts"):
        search("q", session=sess)


def test_search_empty_query():
    with pytest.raises(WebFetchError, match="query is required"):
        search("   ")
