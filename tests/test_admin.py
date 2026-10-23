"""The /admin login: password hash, session cookie, throttling."""

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from wecom_excel_mcp import admin as admin_mod
from wecom_excel_mcp.admin import Admin, check_password, hash_password
from wecom_excel_mcp.usage import UsageLog


class FakeMCP:
    def __init__(self):
        self.routes = []

    def custom_route(self, path, methods, include_in_schema=True):
        def deco(fn):
            from starlette.routing import Route

            self.routes.append(Route(path, fn, methods=methods))
            return fn

        return deco


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for k in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(admin_mod, "_ITERATIONS", 1000)
    pw = tmp_path / "admin-password"
    pw.write_text("boss:" + hash_password("s3cret", 1000))
    mcp = FakeMCP()
    admin_mod.add_routes(mcp, Admin(UsageLog(tmp_path / "usage.db"), pw, tmp_path))
    client = TestClient(Starlette(routes=mcp.routes), base_url="https://testserver", follow_redirects=False)
    return client, pw


def test_hash():
    h = hash_password("pw", 1000)
    assert check_password("pw", h) and not check_password("PW", h) and not check_password("pw", "garbage")


def test_login_required(setup):
    client, _ = setup
    assert client.get("/admin").headers["location"] == "/admin/login"
    assert client.get("/admin/api/stats").status_code == 401
    assert "账号" in client.get("/admin/login").text


def test_login_and_logout(setup):
    client, _ = setup
    bad = client.post("/admin/login", data={"username": "boss", "password": "nope"})
    assert bad.status_code == 401 and "账号或密码不对" in bad.text
    assert client.post("/admin/login", data={"username": "other", "password": "s3cret"}).status_code == 401
    assert client.post("/admin/login", data={"password": "s3cret"}).status_code == 401
    ok = client.post("/admin/login", data={"username": "boss", "password": "s3cret"})
    assert ok.status_code == 303 and ok.headers["location"] == "/admin"
    cookie = ok.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Path=/admin" in cookie and "SameSite=strict" in cookie and "Secure" in cookie
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/api/stats?days=7").json()["days"] == 7
    assert client.get("/admin/login").headers["location"] == "/admin"
    client.post("/admin/logout")
    assert client.get("/admin/api/stats").status_code == 401


def test_new_password_ends_sessions(setup):
    client, pw = setup
    client.post("/admin/login", data={"username": "boss", "password": "s3cret"})
    assert client.get("/admin/api/stats").status_code == 200
    pw.write_text("boss:" + hash_password("other", 1000))
    assert client.get("/admin/api/stats").status_code == 401


def test_forged_cookie(setup):
    client, _ = setup
    client.cookies.set("wecom_admin", "9999999999.deadbeef", domain="testserver", path="/admin")
    assert client.get("/admin/api/stats").status_code == 401


def test_too_many_failures(setup):
    client, _ = setup
    for _ in range(admin_mod.MAX_FAILURES):
        assert client.post("/admin/login", data={"username": "boss", "password": "x"}).status_code == 401
    blocked = client.post("/admin/login", data={"username": "boss", "password": "s3cret"})
    assert blocked.status_code == 429 and "set-cookie" not in blocked.headers


def test_many_ips_failing_together_stop_all_logins(setup, monkeypatch):
    client, _ = setup
    monkeypatch.setattr(admin_mod, "MAX_FAILURES_PER_MINUTE", 3)
    for i in range(3):
        client.post("/admin/login", data={"username": "boss", "password": "x"}, headers={"x-real-ip": f"10.0.0.{i}"})
    blocked = client.post("/admin/login", data={"username": "boss", "password": "s3cret"}, headers={"x-real-ip": "10.9.9.9"})
    assert blocked.status_code == 429


def test_failure_records_drop_the_oldest_not_all(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_mod, "MAX_IPS", 3)
    a = Admin(UsageLog(tmp_path / "u.db"), tmp_path / "pw", tmp_path)
    for ip in ["a", "b", "c", "d"]:
        a._fail(ip)
    assert list(a.failures) == ["b", "c", "d"]


def test_passwd_needs_a_long_password(tmp_path, monkeypatch, capsys):
    import io
    import sys

    from wecom_excel_mcp import server

    pw = tmp_path / "admin-password"
    monkeypatch.setattr(sys, "stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit):
        server.passwd(["--file", str(pw)])
    assert not pw.exists()
    monkeypatch.setattr(sys, "stdin", io.StringIO("long enough pw\n"))
    server.passwd(["--file", str(pw), "--user", "boss"])
    assert pw.read_text().startswith("boss:pbkdf2_sha256$")


def test_label(setup):
    client, _ = setup
    assert client.post("/admin/api/label", json={"key": "k", "label": "x"}).status_code == 401
    client.post("/admin/login", data={"username": "boss", "password": "s3cret"})
    assert client.post("/admin/api/label", data={"key": "k", "label": "x"}).status_code == 415  # a form post
    assert client.post("/admin/api/label", json={"label": "x"}).status_code == 400
    assert client.post("/admin/api/label", json={"key": "uuid-a", "label": "小王"}).json()["ok"]


def test_file_without_username_is_not_set(setup):
    client, pw = setup
    pw.write_text(hash_password("s3cret", 1000))  # the old one-field format
    assert client.get("/admin/login").status_code == 503


def test_no_password_set(setup):
    client, pw = setup
    pw.unlink()
    assert client.get("/admin/login").status_code == 503
    assert client.post("/admin/login", data={"password": ""}).status_code == 503
