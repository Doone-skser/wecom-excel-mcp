#!/usr/bin/env python3
"""Local MCP server for reading WeCom (企业微信) online sheets through https://wecom.example.com/mcp.

    python3 wecom-mcp.py            stdio MCP server (what `claude mcp add` runs)
    python3 wecom-mcp.py login      log in from a terminal (--qr to skip the desktop app)

The sheet tools are forwarded to the shared server with this machine's cookie in the X-Wecom-Cookie header.
The `login` tool gets that cookie, from the WeCom desktop app when it is logged in on this machine
(localhost.work.weixin.qq.com:50010) or by scanning a QR code, and keeps it in ~/.config/wecom-excel/cookie
(mode 600). The cookie is sent nowhere else and never shown. Standard library only.
"""

from __future__ import annotations

import http.server
import json
import os
import random
import re
import secrets
import select
import shutil
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.cookiejar import CookieJar
from pathlib import Path

VERSION = "1.4.1"
REMOTE = os.environ.get("WECOM_MCP_URL", "https://wecom.example.com/mcp")
COOKIE_FILE = Path(os.environ.get("WECOM_COOKIE_FILE", Path.home() / ".config" / "wecom-excel" / "cookie"))
QR_FILE = COOKIE_FILE.with_name("login-qr.txt")
LOGIN_FILE = COOKIE_FILE.with_name("login.json")  # how the cookie was obtained: {"method", "who"}
CLIENT_ID_FILE = COOKIE_FILE.with_name("client-id")  # a random id for this install, for the usage statistics
UPDATE_URL = os.environ.get("WECOM_MCP_UPDATE_URL", REMOTE.rsplit("/", 1)[0] + "/wecom-mcp.py")  # the QR code as text while a login without a screen waits
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
APPID = "MjAwMDAx"  # WeCom docs
REDIRECT = "https://doc.weixin.qq.com/home/recent"
LOGIN = "https://login.work.weixin.qq.com"
DESKTOP = "https://localhost.work.weixin.qq.com:50010"
QR_TIMEOUT = 150  # seconds
EXPIRED_HINTS = ("retcode=30004", "no cookie")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------- login


class LoginError(Exception):
    pass


class Session:
    def __init__(self) -> None:
        self.jar = CookieJar()
        self.web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        # The desktop app listens on 127.0.0.1: never through a proxy. Its certificate is not checked: it is a
        # local process, and nothing secret is sent to it.
        insecure = ssl.create_default_context()
        insecure.check_hostname, insecure.verify_mode = False, ssl.CERT_NONE
        self.local = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=insecure)
        )
        self.headers = {"User-Agent": UA, "Referer": f"{LOGIN}/", "Origin": LOGIN}

    def request(self, url, data=None, form=False, local=False, timeout=15):
        h = dict(self.headers)
        body = None
        if data is not None:
            if form:
                body = urllib.parse.urlencode(data).encode()
                h["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                body = json.dumps(data).encode()
                h["Content-Type"] = "application/json"
        with (self.local if local else self.web).open(urllib.request.Request(url, body, h), timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")

    def api(self, path, payload, **kw) -> dict:
        q = urllib.parse.urlencode({"lang": "zh_CN", "ajax": 1, "f": "json", "random": random.randint(0, 999999)})
        body = json.loads(self.request(f"{LOGIN}/wwlogin/monoApi/partner/login/{path}?{q}", payload, **kw))
        return body.get("data") or {}

    def doc_cookies(self, auth_code: str) -> dict[str, str]:
        """Trade a login auth_code for the doc.weixin.qq.com session cookies."""
        self.request(
            "https://doc.weixin.qq.com/disklogin/login?sid=&wedoc_xsrf=1",
            {"product": "2", "from": "28", "code": auth_code, "f": "json"},
            form=True,
        )
        got = {c.name: c.value for c in self.jar if c.domain.endswith("doc.weixin.qq.com") and c.name.startswith("wedoc_")}
        if "wedoc_sid" not in got:
            raise LoginError("企业微信没有返回文档登录 cookie")
        return got


def _who(d: dict) -> str:
    return " / ".join(x for x in (d.get("corp_name"), d.get("user_name")) if x) or "未知账号"


def desktop_start(s: Session) -> tuple[str, str]:
    """Ask the desktop app to log in; returns (web_key, who). Raises LoginError if it is not available."""
    q = urllib.parse.urlencode({"appid": APPID, "redirect_uri": REDIRECT, "redirect_type": "callback",
                                "state": "", "panel_size": "middle", "version": "1.6.0"})
    page = s.request(f"{LOGIN}/wwlogin/partner/login?{q}")

    def field(name):
        m = re.search(rf'"{name}":"(.*?)"', page)
        if not m:
            raise LoginError(f"登录页缺少 {name}")
        return m.group(1).replace("\\u002F", "/")

    web_key, client_key = field("web_key"), field("client_key")
    s.headers["Authorization"] = field("sessionSignature")
    try:
        state = json.loads(s.request(f"{DESKTOP}/checkLoginState", {"redirect_uri": REDIRECT, "scene": 1},
                                     local=True, timeout=3)).get("data") or {}
    except (OSError, ValueError) as e:
        raise LoginError("本机没有运行企业微信") from e
    if state.get("login_state") != 1 or not state.get("quick_login_enabled"):
        raise LoginError("本机企业微信未登录")
    s.request(f"{DESKTOP}/confirmLogin", {"scene": 1, "web_key": client_key}, local=True, timeout=5)
    return web_key, _who(s.api("getDocWebKeyStatus", {"webKey": web_key, "lastStatus": "QRCODE_SCAN_NEVER"}))


def desktop_detect() -> str | None:
    """Who the desktop app on this machine is logged in as, or None."""
    try:
        return desktop_start(Session())[1]
    except (LoginError, OSError, ValueError):
        return None


def login_desktop(expect: str | None = None) -> tuple[dict[str, str], str]:
    """Log in through the desktop app; with `expect`, only if it is logged in as that account."""
    s = Session()
    web_key, who = desktop_start(s)
    if expect is not None and who != expect:
        raise LoginError(f"本机企业微信现在登录的是 {who}，不是 {expect}")
    code = s.api("confirmQuickLoginByKey", {"qrcode_key": web_key}).get("auth_code")
    if not code:
        raise LoginError("本机企业微信没有确认登录")
    return s.doc_cookies(code), who


QR_PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>企业微信扫码登录</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --fg:#16181d; --muted:#6b7280; --ok:#07c160; --bad:#e5484d; }
@media (prefers-color-scheme: dark) { :root { --bg:#0b0c0f; --card:#14161b; --fg:#eceef2; --muted:#9aa1ad; } }
body { margin:0; height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg);
  font:14px/1.5 -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; }
main { text-align:center; padding:24px; }
h1 { margin:0 0 16px; font-size:17px; font-weight:600; }
#qr { width:220px; height:220px; padding:12px; margin:0 auto; background:#fff; border-radius:14px;
  box-shadow:0 1px 3px rgba(0,0,0,.08); display:grid; place-items:center; transition:opacity .2s; }
#qr.dim { opacity:.15; }
#mark { display:none; font-size:64px; color:var(--ok); line-height:1; }
#msg { margin-top:16px; min-height:21px; color:var(--muted); }
.ok #mark { display:block; } .ok #qr { display:none; } .ok #msg { color:var(--fg); font-size:15px; }
.bad #msg { color:var(--bad); }
</style>
<main id="m"><h1>企业微信扫码登录</h1><div id="qr"></div><div id="mark">&#10003;</div><div id="msg">正在获取二维码…</div></main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>
try {  // an app window reuses its last size; make it small and centred
  window.resizeTo(400, 500);
  window.moveTo((screen.availWidth - 400) / 2 + (screen.availLeft || 0), (screen.availHeight - 500) / 2 + (screen.availTop || 0));
} catch (e) {}
var shown = "", box = document.getElementById("qr"), msg = document.getElementById("msg"), m = document.getElementById("m");
function end(cls, text) { m.className = cls; msg.textContent = text; setTimeout(function () { window.close(); }, cls == "ok" ? 1200 : 4000); }
function poll() {
  fetch("state", {cache: "no-store"}).then(function (r) { return r.json(); }).then(function (s) {
    if (s.qr && s.qr != shown) { shown = s.qr; box.innerHTML = ""; new QRCode(box, {text: s.qr, width: 220, height: 220}); }
    box.className = s.step == "scanned" ? "dim" : "";
    if (s.step == "done") return end("ok", "登录成功" + (s.who ? "：" + s.who : "") + "，窗口即将关闭");
    if (s.step == "fail") return end("bad", s.error || "登录失败");
    msg.textContent = s.step == "scanned" ? "已扫码，请在手机上确认登录" : "用企业微信扫一扫";
    setTimeout(poll, 800);
  }).catch(function () { end("bad", "登录已结束，可以关闭此窗口"); });
}
poll();
</script>"""


class QrWindow:
    """A local page (127.0.0.1, random port and path) that shows the QR code and the login progress, and closes
    itself when the login ends. The QR code is drawn in the browser: the login key goes to no QR service."""

    def __init__(self) -> None:
        self.state = {"qr": "", "step": "wait", "who": "", "error": ""}
        self.last_poll = 0.0
        token = secrets.token_urlsafe(16)
        window = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == f"/{token}/":
                    body, ctype = QR_PAGE.encode(), "text/html; charset=utf-8"
                elif self.path == f"/{token}/state":
                    window.last_poll = time.monotonic()
                    body, ctype = json.dumps(window.state).encode(), "application/json"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/{token}/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def open(self) -> None:
        open_window(self.url)

    def closed(self) -> bool:
        """The page stopped polling: the user closed the window."""
        return self.last_poll > 0 and time.monotonic() - self.last_poll > 20

    def finish(self, **state) -> None:
        """Show the outcome, and stop serving once the page has had time to read it."""
        self.state.update(state)
        threading.Timer(5, self.httpd.shutdown).start()


def _chromium() -> list[str]:
    """Installed Chrome / Edge / Chromium executables, which can open a page as a small app window."""
    if sys.platform == "darwin":
        apps = ["Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser"]
        found = [f"{root}/{a}.app/Contents/MacOS/{a}" for a in apps for root in ("/Applications", str(Path.home() / "Applications"))]
    elif sys.platform == "win32":
        roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
        found = [os.path.join(root, r) for r in rel for root in roots if root]
    else:
        names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"]
        found = [p for n in names if (p := shutil.which(n))]
    return [p for p in found if os.path.isfile(p)]


def open_window(url: str) -> None:
    for exe in _chromium():
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=380,480"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=sys.platform != "win32")
            return
        except OSError:
            continue
    webbrowser.open(url)


def has_display() -> bool:
    """Whether a window opened here is in front of the user (not a VPS, not an SSH session)."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def login_qr(say=log, window: bool = True, on_qr=None) -> tuple[dict[str, str], str]:
    """Log in by QR code. `window` opens the QR window; `on_qr(url)` is called with each code shown
    (the first one and any that replace an expired one)."""
    s = Session()

    def new_key() -> str:
        key = s.api("getWebQrCodeKey", {"appid": APPID, "redirect_uri": REDIRECT, "redirect_type": "callback",
                                       "state": None, "panel_size": "middle", "version": "1.6.0",
                                       "refresh_key": True}).get("web_key")
        if not key:
            raise LoginError("获取二维码失败")
        url = f"https://open.work.weixin.qq.com/wwopen/login/h5?key={key}&bar_style_type=2"
        win.state.update(qr=url, step="wait")
        if on_qr:
            on_qr(url)
        return key

    win = QrWindow()
    try:
        key = new_key()
        if window:
            win.open()
            say("已弹出二维码窗口，请用企业微信扫码并确认")
        status, who, deadline = "QRCODE_SCAN_NEVER", "", time.monotonic() + QR_TIMEOUT
        while time.monotonic() < deadline:
            if win.closed():
                raise LoginError("二维码窗口已关闭")
            d = s.api("getDocWebKeyStatus", {"webKey": key, "lastStatus": status}, timeout=30)
            who = _who(d) if d.get("user_name") else who
            if d.get("auth_code"):
                cookies = s.doc_cookies(d["auth_code"])
                win.finish(step="done", who=who)
                return cookies, who or "扫码账号"
            status = d.get("status") or status
            if "CANCEL" in status or "EXPIRE" in status:  # a fresh code, until the deadline
                key, status = new_key(), "QRCODE_SCAN_NEVER"
            elif status != "QRCODE_SCAN_NEVER":
                win.state["step"] = "scanned"
            time.sleep(1)
        raise LoginError(f"{QR_TIMEOUT} 秒内没有完成扫码")
    except Exception as e:
        if win.state["step"] != "done":
            win.finish(step="fail", error=f"登录失败：{e}，请回到对话重试")
        raise


# ---------------------------------------------------------------------------------------------- QR code
# A small QR encoder (byte mode, error correction level L) for showing the code in a terminal.

_QR_ECC = [0, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28,
           30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30]  # ECC codewords per block, by version
_QR_BLOCKS = [0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16,
              17, 18, 19, 19, 20, 21, 22, 24, 25]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_remainder(data: list[int], degree: int) -> list[int]:
    div, root = [0] * (degree - 1) + [1], 1
    for _ in range(degree):
        for j in range(degree):
            div[j] = _gf_mul(div[j], root)
            if j + 1 < degree:
                div[j] ^= div[j + 1]
        root = _gf_mul(root, 2)
    rem = [0] * degree
    for b in data:
        factor = b ^ rem.pop(0)
        rem.append(0)
        for i, c in enumerate(div):
            rem[i] ^= _gf_mul(c, factor)
    return rem


def qr_matrix(text: str) -> list[list[bool]]:
    data = text.encode()
    for ver in range(1, 41):
        raw = (16 * ver + 128) * ver + 64
        if ver >= 2:
            n = ver // 7 + 2
            raw -= (25 * n - 10) * n - 55 + (36 if ver >= 7 else 0)
        total, nblocks, ecclen = raw // 8, _QR_BLOCKS[ver], _QR_ECC[ver]
        cap = (total - ecclen * nblocks) * 8
        if 4 + (8 if ver < 10 else 16) + 8 * len(data) <= cap:
            break
    else:
        raise ValueError("text too long for a QR code")
    bits = [int(c) for c in f"0100{len(data):0{8 if ver < 10 else 16}b}" + "".join(f"{b:08b}" for b in data)]
    bits += [0] * min(4, cap - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    words += [0xEC, 0x11] * ((cap // 8 - len(words)) // 2 + 1)
    words = words[:cap // 8]

    short, shortlen, blocks, k = nblocks - total % nblocks, total // nblocks, [], 0
    for i in range(nblocks):
        dat = words[k:k + shortlen - ecclen + (0 if i < short else 1)]
        k += len(dat)
        blocks.append(dat + ([0] if i < short else []) + _rs_remainder(dat, ecclen))
    stream = [b[i] for i in range(len(blocks[0])) for j, b in enumerate(blocks) if i != shortlen - ecclen or j >= short]

    size = ver * 4 + 17
    m = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def put(x, y, dark):
        m[y][x], fn[y][x] = dark, True

    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 <= cx + dx < size and 0 <= cy + dy < size:
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
    if ver > 1:
        n = ver // 7 + 2
        step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
        pos = [6] + sorted(size - 7 - i * step for i in range(n - 1))
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) not in ((0, 0), (0, n - 1), (n - 1, 0)):
                    for dy in range(-2, 3):
                        for dx in range(-2, 3):
                            put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vbits = ver << 12 | rem
        for i in range(18):
            a, b, bit = size - 11 + i % 3, i // 3, (vbits >> i) & 1 == 1
            put(a, b, bit)
            put(b, a, bit)

    def format_bits(mask):
        d = 1 << 3 | mask  # level L
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        f = (d << 10 | rem) ^ 0x5412
        bit = lambda i: (f >> i) & 1 == 1
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6)); put(8, 8, bit(7)); put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, size - 15 + i, bit(i))
        put(8, size - 8, True)

    format_bits(0)  # reserve the format areas before placing data
    i, right = 0, size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if (right + 1) & 2 == 0 else vert
                if not fn[y][x] and i < len(stream) * 8:
                    m[y][x] = (stream[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2

    masks = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]

    def masked(k):
        return [[m[y][x] ^ (not fn[y][x] and masks[k](x, y)) for x in range(size)] for y in range(size)]

    def penalty(g):  # runs, 2x2 blocks and balance; enough to avoid a bad mask
        p = 0
        for line in g + [list(c) for c in zip(*g)]:
            run = 1
            for a, b in zip(line, line[1:]):
                run = run + 1 if a == b else 1
                p += 3 if run == 5 else 1 if run > 5 else 0
        p += 3 * sum(g[y][x] == g[y][x + 1] == g[y + 1][x] == g[y + 1][x + 1]
                     for y in range(size - 1) for x in range(size - 1))
        dark = sum(map(sum, g))
        return p + abs(dark * 20 - size * size * 10) // (size * size) * 10

    best = min(range(8), key=lambda k: penalty(masked(k)))
    m[:] = masked(best)
    format_bits(best)
    return m


def qr_text(text: str, ansi: bool = False) -> str:
    """The QR code drawn with half blocks, two rows per line. Without `ansi` it assumes a dark terminal
    (light text on a dark background) and draws the light modules; with `ansi` it sets its own colours."""
    m = qr_matrix(text)
    q = 2
    size = len(m) + 2 * q
    dark = lambda x, y: q <= x < size - q and q <= y < size - q and m[y - q][x - q]
    lines = []
    for y in range(0, size, 2):
        row = ""
        for x in range(size):
            top, bottom = dark(x, y), y + 1 < size and dark(x, y + 1)
            if y + 1 >= size:
                bottom = not ansi  # past the last row: blank
            if ansi:  # black on bright white: draw the dark modules
                row += {(True, True): "█", (True, False): "▀", (False, True): "▄", (False, False): " "}[(top, bottom)]
            else:  # draw the light modules
                row += {(False, False): "█", (False, True): "▀", (True, False): "▄", (True, True): " "}[(top, bottom)]
        lines.append(f"\x1b[30;107m{row}\x1b[0m" if ansi else row)
    return "\n".join(lines)


def save_cookie(cookies: dict[str, str], method: str, who: str) -> None:
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(COOKIE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("; ".join(f"{k}={v}" for k, v in cookies.items()))
    os.chmod(COOKIE_FILE, 0o600)
    LOGIN_FILE.write_text(json.dumps({"method": method, "who": who}, ensure_ascii=False), encoding="utf-8")


relogin_lock = threading.Lock()


def relogin_quietly(expired: str | None) -> bool:
    """After the cookie `expired`, get a new one without asking: only through the desktop app, and only when the
    last login was through it as the same account. True when there is a cookie other than `expired` to retry with
    (also when a concurrent call already renewed it)."""
    with relogin_lock:
        if read_cookie() != expired:
            return True
        try:
            info = json.loads(LOGIN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if info.get("method") != "desktop":
            return False
        try:
            cookies, who = login_desktop(expect=info.get("who"))
        except (LoginError, OSError, ValueError) as e:
            log(f"自动重新登录失败：{e}")
            return False
        save_cookie(cookies, "desktop", who)
        log(f"cookie 已失效，已通过本机企业微信自动重新登录：{who}")
        return True


def read_cookie() -> str | None:
    try:
        return COOKIE_FILE.read_text().strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------------------------------------- remote


def client_id() -> str:
    """This install's random id (made on first use), so the statistics can tell installs apart."""
    try:
        cid = CLIENT_ID_FILE.read_text(encoding="utf-8").strip()
        if cid:
            return cid
    except OSError:
        pass
    cid = str(uuid.uuid4())
    try:
        CLIENT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        CLIENT_ID_FILE.write_text(cid + "\n", encoding="utf-8")
    except OSError:
        return ""
    return cid


def remote(method: str, params: dict | None = None, timeout: float = 300) -> dict:
    """One JSON-RPC request to the shared server (stateless streamable HTTP); returns its `result`."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "User-Agent": f"wecom-mcp.py/{VERSION}"}
    if cookie := read_cookie():
        headers["X-Wecom-Cookie"] = cookie
    if cid := client_id():
        headers["X-Wecom-Client-Id"] = cid
    try:  # the account name, for the server's usage statistics
        if who := json.loads(LOGIN_FILE.read_text(encoding="utf-8")).get("who"):
            headers["X-Wecom-User"] = urllib.parse.quote(who)
    except (OSError, ValueError):
        pass
    req = urllib.request.Request(REMOTE, body, headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("请求太频繁，被服务器限流了，请稍等几秒再试") from e
        raise RuntimeError(f"{REMOTE} 返回 HTTP {e.code}") from e
    for chunk in [text] if text.lstrip().startswith("{") else re.findall(r"^data: (.*)$", text, re.M):
        msg = json.loads(chunk)
        if msg.get("id") == 1:
            if "error" in msg:
                raise RuntimeError(msg["error"].get("message") or str(msg["error"]))
            return msg["result"]
    raise RuntimeError(f"{REMOTE} 没有返回结果")


# ---------------------------------------------------------------------------------------------- MCP

LOGIN_TOOL = {
    "name": "login",
    "description": (
        "登录企业微信文档，cookie 只保存在本机。读表工具报“未登录”或“cookie 已失效”时调用。\n"
        "不带 method 调用：如果本机企业微信桌面端已登录，会返回账号信息并要求你询问用户——"
        "用这个账号直接登录（再以 method=\"desktop\" 调用），还是扫码登录（method=\"qr\"）；"
        "本机没有登录企业微信时直接开始扫码。\n"
        "method=\"qr\" 会弹出二维码窗口，等待用户用企业微信扫码确认（最长约 2.5 分钟），完成后窗口自动关闭；"
        "没有图形界面的机器（VPS、SSH）上则立即返回文字二维码，扫码后再调用一次 login 确认结果。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"method": {"type": "string", "enum": ["desktop", "qr"], "description": "登录方式"}},
    },
    "annotations": {"title": "登录企业微信", "readOnlyHint": False, "openWorldHint": True},
}
INSTRUCTIONS = (
    "Read WeCom (企业微信/doc.weixin.qq.com) online spreadsheets. Pass a doc URL or padId (e3_...). "
    "Call list_sheets first for tab names and the range holding data, then read_sheet with an A1 range. "
    "If a tool says the user is not logged in or the cookie expired, call `login` (with no method first) "
    "and follow what it says, then retry."
)


def text_result(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


class TerminalQrLogin:
    """QR login without a screen (a VPS, an SSH session): the tool call returns the QR code as text at once,
    and the login keeps waiting for the scan in the background, saving the cookie when it is done."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.qr = ""
        self.outcome: dict | None = None  # the final tool result, once the login ended

    def start(self) -> dict:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return self.waiting()
            self.qr, self.outcome = "", None
            ready = threading.Event()

            def on_qr(url):
                self.qr = url
                QR_FILE.parent.mkdir(parents=True, exist_ok=True)
                QR_FILE.write_text(qr_text(url) + "\n", encoding="utf-8")
                ready.set()

            def run():
                try:
                    cookies, who = login_qr(say=lambda _: None, window=False, on_qr=on_qr)
                    save_cookie(cookies, "qr", who)
                    self.outcome = text_result(f"登录成功：{who}。cookie 已保存在本机 {COOKIE_FILE}，现在可以读表了。")
                except Exception as e:
                    self.outcome = text_result(f"登录失败：{e}。可以再调用 login 重新获取二维码。", error=True)
                finally:
                    QR_FILE.unlink(missing_ok=True)
                    ready.set()

            self.thread = threading.Thread(target=run, daemon=True)
            self.thread.start()
        ready.wait(30)
        return self.waiting() if self.qr else (self.outcome or text_result("获取二维码超时", error=True))

    def waiting(self) -> dict:
        return text_result(
            "这台机器没有图形界面，二维码以文字形式给出（需要深色背景的终端）。"
            "请告诉用户：按 ctrl+o 展开这条工具输出，用企业微信扫码并确认；"
            f"也可以在另一个终端运行 `cat {QR_FILE}` 查看。不要自己重画二维码。"
            "用户扫完后再调用一次 login（不带参数）确认结果。\n\n"
            + qr_text(self.qr)
        )

    def active(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def check(self) -> dict | None:
        """The state of a login started earlier, or None if there is none."""
        if self.thread is None:
            return None
        if self.thread.is_alive():
            return self.waiting()
        outcome, self.thread = self.outcome, None
        return outcome


terminal_login = TerminalQrLogin()


def do_login(method: str | None) -> dict:
    if method != "desktop" and (pending := terminal_login.check()):
        return pending
    try:
        if method is None:
            who = desktop_detect()
            if who:
                return text_result(
                    f"检测到本机企业微信已登录：{who}。\n"
                    "请询问用户选择：1) 用这个账号直接登录；2) 扫码登录（换一个账号）。"
                    "然后用 method=\"desktop\" 或 method=\"qr\" 再调用 login。"
                )
            method = "qr"
        if method == "qr" and not has_display():
            return terminal_login.start()
        cookies, who = login_desktop() if method == "desktop" else login_qr()
    except (LoginError, OSError, ValueError) as e:
        hint = "可以改用 method=\"qr\" 扫码登录。" if method == "desktop" else ""
        return text_result(f"登录失败：{e}。{hint}", error=True)
    save_cookie(cookies, method, who)
    return text_result(f"登录成功：{who}。cookie 已保存在本机 {COOKIE_FILE}，现在可以读表了。")


def call_tool(name: str, args: dict) -> dict:
    if name == "login":
        return do_login(args.get("method"))
    cookie = read_cookie()
    if not cookie:
        return text_result("还没有登录企业微信。请先调用 login 工具。", error=True)
    result = remote("tools/call", {"name": name, "arguments": args})
    if expired(result) and relogin_quietly(cookie):
        result = remote("tools/call", {"name": name, "arguments": args})
    if expired(result):
        result["content"].append({"type": "text", "text": "cookie 已失效：请调用 login 工具重新登录，然后重试。"})
    global update_note
    if update_note and not result.get("isError"):
        result["content"].append({"type": "text", "text": update_note})
        update_note = ""
    return result


def expired(result: dict) -> bool:
    text = " ".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    return bool(result.get("isError")) and any(h in text for h in EXPIRED_HINTS)


# ---------------------------------------------------------------------------------------------- self-update

update_note = ""  # told to the user once, with the next result


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


UPDATE_EVERY = 3600  # seconds: the first request after this long checks for a new version
HOT_RESTART = os.name == "posix"  # exec() keeps the process (and the client's pipes) only on POSIX


def _trusted_channel(url: str) -> bool:
    """HTTPS, or this machine: where nobody on the network can swap the file for their own code."""
    u = urllib.parse.urlparse(url)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"))


def self_update() -> str | None:
    """Replace this file with a newer published version; returns that version, or None.
    Only over HTTPS (see _trusted_channel): the new file is run as is. It is checked to compile first."""
    global update_note
    me = Path(sys.argv[0]).resolve()
    if os.environ.get("WECOM_MCP_NO_UPDATE") or not me.is_file() or me.suffix != ".py":
        return None
    if not _trusted_channel(UPDATE_URL):
        return None
    try:
        req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": f"wecom-mcp.py/{VERSION}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            new = r.read()
        m = re.search(rb'^VERSION = "([^"]+)"', new, re.M)
        if not m or _version(m.group(1).decode()) <= _version(VERSION):
            return
        compile(new, str(me), "exec")
        tmp = me.with_name(me.name + ".new")
        tmp.write_bytes(new)
        os.replace(tmp, me)
    except Exception as e:  # never in the way of reading sheets
        log(f"检查更新失败：{e}")
        return None
    version = m.group(1).decode()
    log(f"wecom-mcp.py 已下载新版：{VERSION} -> {version}")
    if not HOT_RESTART:
        update_note = f"（wecom-excel 本地插件已自动更新到 {version}，重启客户端后生效。）"
    return version


class Server:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tools: list[dict] | None = None
        self.busy = 0  # requests being handled
        self.pending = b""  # stdin read but not yet a whole line
        self.last_check = -UPDATE_EVERY
        self.checking = False
        self.updated_to: str | None = None

    def send(self, msg: dict) -> None:
        with self.lock:
            sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    def handle(self, req: dict) -> None:
        try:
            self._handle(req)
        finally:
            with self.lock:
                self.busy -= 1

    def _handle(self, req: dict) -> None:
        rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "wecom-excel", "version": VERSION},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                if self.tools is None:
                    self.tools = remote("tools/list", timeout=30).get("tools", [])
                result = {"tools": [*self.tools, LOGIN_TOOL]}
            elif method == "tools/call":
                result = call_tool(params.get("name", ""), params.get("arguments") or {})
            elif rid is None:  # notification
                return
            else:
                self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})
                return
        except Exception as e:  # report, keep serving
            log(traceback.format_exc())
            if method == "tools/call":
                result = text_result(f"调用失败：{e}", error=True)
            else:
                self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})
                return
        if rid is not None:
            try:
                self.send({"jsonrpc": "2.0", "id": rid, "result": result})
            except Exception as e:  # never leave the client waiting for a reply
                log(traceback.format_exc())
                self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})

    # ------------------------------------------------------------------ updates

    def check_for_update(self) -> None:
        """At most once an hour, on the first request after that: look for a new version in the background."""
        now = time.monotonic()
        if self.checking or self.updated_to or now - self.last_check < UPDATE_EVERY:
            return
        self.last_check, self.checking = now, True

        def run():
            try:
                self.updated_to = self_update()
            finally:
                self.checking = False

        threading.Thread(target=run, daemon=True).start()

    def restart_if_idle(self) -> None:
        """Run the updated file in place of this process, between requests: with nothing being handled, no
        partial input, and no QR login waiting for a scan. exec() keeps the pid and the stdin/stdout pipes,
        so the client stays connected and does not notice."""
        if not (HOT_RESTART and self.updated_to):
            return
        with self.lock:
            if self.busy or self.pending or terminal_login.active():
                return
            sys.stdout.flush()
            os.environ["WECOM_MCP_UPDATED_FROM"] = VERSION
            log(f"wecom-mcp.py 重启为 {self.updated_to}")
            os.execv(sys.executable, [sys.executable, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]])

    # ------------------------------------------------------------------ input

    def serve(self) -> None:
        fd = sys.stdin.fileno()
        while True:
            self.restart_if_idle()
            # Wait in select(), so that a restart happens between reads and never drops input already read.
            if HOT_RESTART and not select.select([fd], [], [], 1.0)[0]:
                continue
            data = os.read(fd, 65536)
            if not data:
                return
            *lines, self.pending = (self.pending + data).split(b"\n")
            for line in lines:
                if not line.strip():
                    continue
                try:
                    req = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                self.check_for_update()
                with self.lock:
                    self.busy += 1
                # Each request on its own thread: a QR login waiting for a scan must not hold up other calls.
                threading.Thread(target=self.handle, args=(req,), daemon=True).start()


def ask(prompt: str) -> str:
    """A line from the terminal, even when stdin is the piped script (`curl ... | python3 - login`)."""
    try:
        with open("/dev/tty") as tty:
            print(prompt, end="", flush=True)
            return tty.readline().strip()
    except OSError:
        try:
            return input(prompt).strip()
        except EOFError:
            return ""


def cli_login(qr: bool) -> None:
    try:
        if not qr and (who := desktop_detect()):
            ans = ask(f"检测到本机企业微信已登录：{who}\n直接用这个账号登录？[Y/n，n 改为扫码] ").lower()
            qr = ans in ("n", "no")
        if qr:
            window = has_display()
            tty = sys.stdout.isatty()

            def show(url):
                print(qr_text(url, ansi=tty))
                print("用企业微信扫码并确认" + ("（也可以在弹出的窗口里扫）" if window else ""), flush=True)

            cookies, who = login_qr(say=lambda _: None, window=window, on_qr=show)
        else:
            cookies, who = login_desktop()
    except (LoginError, OSError, ValueError) as e:
        sys.exit(f"登录失败：{e}")
    save_cookie(cookies, "qr" if qr else "desktop", who)
    print(f"登录成功：{who}，cookie 已保存到 {COOKIE_FILE}")


def main() -> None:
    # MCP stdio is UTF-8; Windows defaults pipes to the ANSI code page (GBK), which garbles replies and
    # fails on characters it lacks, leaving the client waiting until it times out.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace", newline="\n" if stream is not sys.stdin else None)
    args = sys.argv[1:]
    if args and args[0] == "login":
        cli_login("--qr" in args)
    elif not args:
        global update_note
        if old := os.environ.pop("WECOM_MCP_UPDATED_FROM", ""):
            update_note = f"（wecom-excel 本地插件已自动从 {old} 更新到 {VERSION}。）"
        Server().serve()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
