"""A shared HTTP server reads each doc with the cookie its caller sends, and keeps none of them."""

import httpx
import pytest

from wecom_excel_mcp import server
from wecom_excel_mcp.client import CookieProvider, WecomClient, WecomError, request_cookie
from wecom_excel_mcp.server import _use_caller_cookie
from mcp.server.mcpserver.exceptions import ToolError

PAD = "e3_ExampleSheet0000000000000001"


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch):
    for k in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy"):
        monkeypatch.delenv(k, raising=False)


class FakeCtx:
    def __init__(self, headers):
        self.headers = headers


def opendoc_client(seen: list[str | None], retcode_for: dict[str, int] | None = None) -> WecomClient:
    """A client whose requests go to a mock WeCom answering opendoc, recording the Cookie header."""

    def handle(req: httpx.Request) -> httpx.Response:
        cookie = req.headers.get("cookie")
        seen.append(cookie)
        retcode = (retcode_for or {}).get(cookie or "", 0)
        body = {"clientVars": {"padType": "sheet", "collab_client_vars": {"rev": 7, "header": []}}}
        if retcode:
            body = {"retcode": retcode}
        return httpx.Response(200, json=body, headers={"Set-Cookie": "TOK=server-side; Path=/"})

    return WecomClient(CookieProvider(use_env=False), transport=httpx.MockTransport(handle))


async def test_caller_cookie_is_sent_and_nothing_is_kept():
    seen: list[str | None] = []
    client = opendoc_client(seen)
    request_cookie.set("a=1; b=2")
    await client.doc_info(PAD)
    assert seen == ["a=1; b=2"]
    assert not list(client.http.cookies.jar)  # Set-Cookie from WeCom is not stored for the next caller
    assert all("a=1" not in str(k) for k in client._doc_info_cache)  # cached under a digest only
    await client.aclose()


async def test_tab_list_is_cached_per_caller():
    seen: list[str | None] = []
    client = opendoc_client(seen, retcode_for={"u=bob": 200063})
    request_cookie.set("u=alice")
    await client.doc_info(PAD)
    await client.doc_info(PAD)
    assert seen == ["u=alice"]
    request_cookie.set("u=bob")  # alice's fresh tab list must not answer for bob, who has no access
    assert client.peek_doc_info(PAD) is None
    with pytest.raises(WecomError, match="no access"):
        await client.doc_info(PAD)
    assert seen == ["u=alice", "u=bob"]
    await client.aclose()


async def test_caller_cookie_is_not_retried_with_server_cookies():
    seen: list[str | None] = []
    client = opendoc_client(seen, retcode_for={"u=old": 30004})
    request_cookie.set("u=old")
    with pytest.raises(WecomError, match="not logged in"):
        await client.doc_info(PAD)
    assert seen == ["u=old"]
    await client.aclose()


async def test_no_cookie_without_env_is_an_error():
    request_cookie.set(None)
    async with httpx.AsyncClient() as http:
        with pytest.raises(WecomError, match="X-Wecom-Cookie"):
            await CookieProvider(use_env=False).get(http)


def test_header_selects_the_cookie(monkeypatch):
    monkeypatch.setattr(server, "_use_env_cookies", False)
    _use_caller_cookie(FakeCtx({"x-wecom-cookie": " k=v "}))
    assert request_cookie.get() == "k=v"
    with pytest.raises(ToolError, match="x-wecom-cookie"):
        _use_caller_cookie(FakeCtx({}))


def test_without_header_falls_back_to_server_cookies(monkeypatch):
    monkeypatch.setattr(server, "_use_env_cookies", True)
    request_cookie.set("stale")
    _use_caller_cookie(FakeCtx({}))
    assert request_cookie.get() is None
    _use_caller_cookie(None)  # called directly, outside a request
    assert request_cookie.get() is None
