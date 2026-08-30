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
