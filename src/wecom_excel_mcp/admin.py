"""The statistics page at /admin, behind a login form.

The credentials file holds one line, `username:<PBKDF2 hash>` (`wecom-excel-mcp --hash-password` makes the
hash). A login sets a signed session cookie (HttpOnly, SameSite=Strict, Path=/admin; Secure behind HTTPS) that
lasts SESSION_DAYS; its signature covers that line, so changing the username or password ends every session. An IP with
MAX_FAILURES wrong passwords within FAILURE_WINDOW is refused until the window passes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
import urllib.parse
from importlib import resources
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .usage import MAX_LABEL, UsageLog

COOKIE = "wecom_admin"
SESSION_DAYS = 7
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60
_ITERATIONS = 600_000


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except ValueError:
        return False


class Admin:
    def __init__(self, usage: UsageLog, password_file: str | Path, state_dir: str | Path) -> None:
        self.usage = usage
        self.password_file = Path(password_file)
        self.secret = self._secret(Path(state_dir) / "session.key")
        self.failures: dict[str, list[float]] = {}
        files = resources.files(__package__)
        self.dashboard = files.joinpath("admin.html").read_text(encoding="utf-8")
        self.login_page = files.joinpath("admin_login.html").read_text(encoding="utf-8")

    @staticmethod
    def _secret(path: Path) -> bytes:
        """The session signing key, made once and kept beside the usage log, so restarts keep sessions."""
        try:
            return path.read_bytes()
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
            return key

    def _stored(self) -> str:
        """The `username:hash` line, read on every use: replacing the file takes effect at once."""
        try:
            line = self.password_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        return line if ":" in line else ""

    def _check(self, username: str, password: str, stored: str) -> bool:
        user, _, digest = stored.partition(":")
        ok_password = check_password(password, digest)  # always run it: the same time for a wrong username
        return hmac.compare_digest(username.encode(), user.encode()) and ok_password

    # ------------------------------------------------------------------ sessions

    def _sign(self, expires: int, stored: str) -> str:
        return hmac.new(self.secret, f"{expires}|{stored}".encode(), hashlib.sha256).hexdigest()

    def _token(self) -> str:
        expires = int(time.time()) + SESSION_DAYS * 86400
        return f"{expires}.{self._sign(expires, self._stored())}"

    def authed(self, request: Request) -> bool:
        token = request.cookies.get(COOKIE, "")
        expires, _, sig = token.partition(".")
        stored = self._stored()
        if not (expires.isdigit() and stored) or int(expires) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(int(expires), stored))

    # ------------------------------------------------------------------ throttling

    def _blocked(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self.failures.get(ip, []) if now - t < FAILURE_WINDOW]
        if recent:
            self.failures[ip] = recent
        else:
            self.failures.pop(ip, None)
        return len(recent) >= MAX_FAILURES

    def _fail(self, ip: str) -> None:
        self.failures.setdefault(ip, []).append(time.time())
        if len(self.failures) > 10_000:  # bound the memory an attacker can make us use
            self.failures.clear()

    # ------------------------------------------------------------------ pages

    def _login(self, error: str = "", status: int = 200) -> HTMLResponse:
        page = self.login_page.replace("{{error}}", error).replace("{{error_hidden}}", "" if error else "hidden")
        return HTMLResponse(page, status_code=status, headers={"Cache-Control": "no-store"})

    async def page(self, request: Request) -> Response:
        if not self.authed(request):
            return RedirectResponse("/admin/login", status_code=303)
        return HTMLResponse(self.dashboard, headers={"Cache-Control": "no-store"})

    async def login_form(self, request: Request) -> Response:
        if not self._stored():
            return self._login("后台账号还没有设置", 503)
        if self.authed(request):
            return RedirectResponse("/admin", status_code=303)
        return self._login()

    async def login(self, request: Request) -> Response:
        ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
        stored = self._stored()
        if not stored:
            return self._login("后台账号还没有设置", 503)
        if self._blocked(ip):
            return self._login(f"登录失败次数太多，请 {FAILURE_WINDOW // 60} 分钟后再试", 429)
        form = urllib.parse.parse_qs((await request.body())[:4096].decode("utf-8", "replace"))
        username = (form.get("username") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        if not await asyncio.to_thread(self._check, username, password, stored):
            self._fail(ip)
            return self._login("账号或密码不对", 401)
        self.failures.pop(ip, None)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            COOKIE, self._token(), max_age=SESSION_DAYS * 86400, path="/admin", httponly=True, samesite="strict",
            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        )
        return response

    async def logout(self, _request: Request) -> Response:
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(COOKIE, path="/admin")
        return response

    async def stats(self, request: Request) -> Response:
        if not self.authed(request):
            return JSONResponse({"error": "login required"}, status_code=401)
        try:
            days = int(request.query_params.get("days", "30"))
        except ValueError:
            days = 30
        data = await asyncio.to_thread(self.usage.stats, days)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})


    async def set_label(self, request: Request) -> Response:
        """Name a caller. JSON only (and the session cookie is SameSite=Strict), so no other site can post it."""
        if not self.authed(request):
            return JSONResponse({"error": "login required"}, status_code=401)
        if not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"error": "JSON only"}, status_code=415)
        try:
            body = await request.json()
            key, label = str(body["key"])[:200], str(body.get("label") or "")
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "bad request"}, status_code=400)
        if not key:
            return JSONResponse({"error": "bad request"}, status_code=400)
        await asyncio.to_thread(self.usage.set_label, key, label)
        return JSONResponse({"ok": True, "max": MAX_LABEL})


def add_routes(mcp, admin: Admin) -> None:
    mcp.custom_route("/admin", methods=["GET"], include_in_schema=False)(admin.page)
    mcp.custom_route("/admin/login", methods=["GET"], include_in_schema=False)(admin.login_form)
    mcp.custom_route("/admin/login", methods=["POST"], include_in_schema=False)(admin.login)
    mcp.custom_route("/admin/logout", methods=["POST"], include_in_schema=False)(admin.logout)
    mcp.custom_route("/admin/api/stats", methods=["GET"], include_in_schema=False)(admin.stats)
    mcp.custom_route("/admin/api/label", methods=["POST"], include_in_schema=False)(admin.set_label)
