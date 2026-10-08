"""Public article extraction uses the real fetcher and its existing safety gates."""
import gzip
from pathlib import Path
import socket

import pytest
from fastapi import HTTPException

from backend.memory_app import workspace_links
from tests.memory_app.test_workspace import client


URL = "https://mp.weixin.qq.com/s/-T3ZaMP1VNVxQuGLby7Rfg"
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/wechat_public_excerpt.html"


def transport(monkeypatch, body, *, status=200, encoding="identity", addresses=None):
    calls = []
    if addresses is None:
        addresses = ["93.184.216.34"]
    def resolve(host, port):
        calls.append(("dns", host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in addresses]
    monkeypatch.setattr(workspace_links.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(workspace_links.socket, "create_connection",
                        lambda address, **kwargs: calls.append(("connect", address)))
    class Response:
        def __init__(self):
            self.status = status
        def getheader(self, name, default=""):
            return {"content-type": "text/html; charset=UTF-8", "content-encoding": encoding}.get(name, default)
        def read(self, amount):
            return body[:amount]
    class Connection:
        def __init__(self, host, port, timeout):
            calls.append(("connection", host, port, timeout))
        def request(self, method, path, headers):
            calls.append(("request", method, path, headers))
            self._create_connection(("not-a-second-dns-lookup", 443), timeout=12)
        def getresponse(self):
            return Response()
        def close(self):
            calls.append(("close",))
    monkeypatch.setattr(workspace_links.http.client, "HTTPSConnection", Connection)
    return calls


@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_public_fragment_extracts_only_body_without_script_style_or_chrome(monkeypatch, encoding):
    data = FIXTURE.read_bytes()
    transport(monkeypatch, gzip.compress(data) if encoding == "gzip" else data, encoding=encoding)
    text = workspace_links._fetch_url(URL)
    assert text == "“ 小程序新增数据周期更新能力与实时日志功能；「 小程序服务商助手」上线； 云开发新增实时数据推送能力。 ”"
    assert "noise" not in text and "操作按钮" not in text and "阅读数量" not in text


def test_nested_body_and_void_elements_do_not_end_article_early(monkeypatch):
    data = b'<html><body><div id="js_content"><section><p>one <b>two</b><br>three<img src="a"></p><p>four</p></section><p>five</p></div><div>outside</div></body></html>'
    transport(monkeypatch, data)
    assert workspace_links._fetch_url(URL) == "one two three four five"


@pytest.mark.parametrize("body", [
    '<html><body><h1>环境异常</h1><p>完成验证后继续访问</p></body></html>',
    '<html><body><div id="js_content"><script>script</script><style>style</style></div><p>outside</p></body></html>',
    '<html><body><div data-id="js_content">wrong attribute</div></body></html>',
])
def test_unavailable_or_empty_article_never_falls_back_to_verification_page(monkeypatch, body):
    transport(monkeypatch, body.encode("utf-8"))
    with pytest.raises(HTTPException) as error:
        workspace_links._fetch_url(URL)
    assert error.value.status_code == 422 and error.value.detail == "link_fetch_failed"


@pytest.mark.parametrize("host", ["example.com", "mp.weixin.qq.com.attacker.test"])
def test_other_hosts_keep_existing_article_preference(monkeypatch, host):
    transport(monkeypatch, b'<html><div id="js_content">outside</div><article>generic article</article></html>')
    assert workspace_links._fetch_url("https://" + host + "/article") == "generic article"


def test_safe_address_is_pinned_and_request_headers_are_preserved(monkeypatch):
    calls = transport(monkeypatch, b'<html><div id="js_content">public article</div></html>')
    assert workspace_links._fetch_url(URL) == "public article"
    assert [call for call in calls if call[0] == "dns"] == [("dns", "mp.weixin.qq.com", 443)]
    assert ("connect", ("93.184.216.34", 443)) in calls
    request, = [call for call in calls if call[0] == "request"]
    assert request[3] == {"User-Agent": "ChriptmasWorkspace/1", "Accept-Encoding": "identity"}


@pytest.mark.parametrize("status,addresses,code", [
    (302, ["93.184.216.34"], "link_redirect_disallowed"),
    (200, ["127.0.0.1"], "url_address_blocked"),
    (200, ["93.184.216.34", "10.0.0.1"], "url_address_blocked"),
])
def test_network_rejection_does_not_create_workspace_item(tmp_path, monkeypatch, status, addresses, code):
    http, records = client(tmp_path)
    calls = transport(monkeypatch, FIXTURE.read_bytes(), status=status, addresses=addresses)
    response = http.post("/api/workspace/v1/items/link", json={"url": URL})
    assert response.json()["detail"] == code
    assert response.status_code == (422 if status == 302 else 400)
    assert records.list("workspace_items") == ()
    assert len([call for call in calls if call[0] == "request"]) == (1 if status == 302 else 0)


def test_real_intake_stages_extracted_body_in_temporary_records(tmp_path, monkeypatch):
    http, records = client(tmp_path)
    transport(monkeypatch, FIXTURE.read_bytes())
    response = http.post("/api/workspace/v1/items/link", json={"url": URL, "project_id": "alpha"})
    assert response.status_code == 200
    item = response.json()
    assert item["status"] == "staged" and item["project_id"] == "alpha"
    assert item["url"] == URL
    assert item["source_text"].startswith("“ 小程序新增")
    assert "操作按钮" not in item["source_text"]
    assert records.read("workspace_items", item["id"]).payload["source_text"] == item["source_text"]
