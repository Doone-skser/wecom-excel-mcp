"""The setup page, llms.txt and the local client script, served by the HTTP server itself.

The files in site/ say https://wecom.example.com; each response has it replaced by this server's own address
(--public-url, else the scheme and Host of the request), so a server reached as http://1.2.3.4:8765 hands out
a client that talks to http://1.2.3.4:8765/mcp.
"""

from __future__ import annotations

import ipaddress
import re
from importlib import resources

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

PLACEHOLDER = "https://wecom.example.com"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/llms.txt": ("llms.txt", "text/plain; charset=utf-8"),
    "/wecom-mcp.py": ("wecom-mcp.py", "text/plain; charset=utf-8"),
}
_HOST = re.compile(r"[A-Za-z0-9.\-]+(:\d{1,5})?|\[[0-9A-Fa-f:.]+\](:\d{1,5})?")


def _from_local_proxy(request: Request) -> bool:
    try:
        return bool(request.client) and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """The caller's address. X-Real-IP is believed only from a proxy on this machine (nginx), since anyone
    talking to the port directly could send any value in it."""
    forwarded = request.headers.get("x-real-ip") if _from_local_proxy(request) else None
    return forwarded or (request.client.host if request.client else "?")


def is_https(request: Request) -> bool:
    if _from_local_proxy(request) and "x-forwarded-proto" in request.headers:
        return request.headers["x-forwarded-proto"] == "https"
    return request.url.scheme == "https"


def base_url(request: Request, public_url: str = "") -> str:
    if public_url:
        return public_url.rstrip("/")
    host = request.headers.get("host", "")
    if not _HOST.fullmatch(host):
        host = "%s:%d" % tuple(request.scope["server"]) if request.scope.get("server") else "localhost"
    return f"{'https' if is_https(request) else 'http'}://{host}"


def add_routes(mcp, public_url: str = "") -> None:
    files = resources.files(__package__).joinpath("site")
    texts = {name: files.joinpath(name).read_text(encoding="utf-8") for name, _ in FILES.values()}

    async def serve(request: Request) -> Response:
        name, media = FILES[request.url.path]
        body = texts[name].replace(PLACEHOLDER, base_url(request, public_url))
        return PlainTextResponse(body, media_type=media, headers={"Cache-Control": "no-cache"})

    for path in FILES:
        mcp.custom_route(path, methods=["GET"], include_in_schema=False)(serve)
