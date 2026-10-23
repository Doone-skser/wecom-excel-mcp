"""The setup page and client script, served with this server's own address in them."""

from starlette.applications import Starlette
from starlette.testclient import TestClient

from wecom_excel_mcp import web

from test_admin import FakeMCP


def client(public_url="", peer="203.0.113.5"):
    mcp = FakeMCP()
    web.add_routes(mcp, public_url)
    return TestClient(Starlette(routes=mcp.routes), base_url="http://1.2.3.4:8765", client=(peer, 50000))


def test_address_comes_from_the_request():
    c = client()
    assert 'REMOTE = os.environ.get("WECOM_MCP_URL", "http://1.2.3.4:8765/mcp")' in c.get("/wecom-mcp.py").text
    assert "http://1.2.3.4:8765/llms.txt" in c.get("/").text
    assert "example.com" not in c.get("/llms.txt").text


def test_forwarded_proto_only_from_a_local_proxy():
    h = {"x-forwarded-proto": "https", "host": "wecom.test"}
    assert "http://wecom.test/mcp" in client().get("/llms.txt", headers=h).text
    assert "https://wecom.test/mcp" in client(peer="127.0.0.1").get("/llms.txt", headers=h).text


def test_public_url_wins_and_bad_host_is_ignored():
    assert "https://wecom.test/mcp" in client("https://wecom.test/").get("/llms.txt").text
    assert "<script>" not in client().get("/llms.txt", headers={"host": "x<script>"}).text
