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


def test_is_public_http_url_rejects_malformed():
    assert not is_public_http_url("not a url")
    assert not is_public_http_url("http://")
    assert not is_public_http_url("example.com")  # 缺 scheme


def test_is_public_http_url_dns_failure(monkeypatch):
    _fake_dns(monkeypatch, {})
    assert not is_public_http_url("https://nohost.invalid")


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
