"""MCP server exposing read-only access to WeCom (企业微信) online sheets."""

from __future__ import annotations

import argparse
import asyncio
import functools
import inspect
import logging
import os
import re
import sys
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Literal

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .client import CookieProvider, DocInfo, Tab, WecomClient, WecomError, parse_doc_ref, request_cookie
from .decoder import DecodeError
from .formatting import images_in, render_html
from .images import Tile, tile_image
from .table import Range, build_grid, col_letter, parse_range, render
from .usage import UsageLog
from .web import client_ip

_client: WecomClient | None = None
# Whether the server's own cookies (WECOM_COOKIE / WECOM_COOKIE_API) may be used; off for a shared HTTP
# server, which then reads every doc with the cookie its caller sends in COOKIE_HEADER.
_use_env_cookies = True
COOKIE_HEADER = "x-wecom-cookie"
USER_HEADER = "x-wecom-user"  # the account name the local client reports (URL-encoded), for usage statistics
CLIENT_ID_HEADER = "x-wecom-client-id"  # the local client's random install id, for usage statistics
_CLIENT_ID = re.compile(r"[0-9a-fA-F-]{8,64}")
_usage: UsageLog | None = None  # usage statistics (HTTP with --usage-db)
_MAX_IMAGE_BYTES = 4_500_000  # stay under the ~5 MB per-image limit of model APIs
_IMAGE_FETCH_CONCURRENCY = 4
_SHEET_FETCH_CONCURRENCY = 6
# list_sheets size lines by (pad_id, tab_id, rev): kept apart from the tab cache, which a big doc
# can overflow, so listing a doc again needs no fetch.
_sizes: dict[tuple[str, str, int], str] = {}
_MAX_SIZES = 10_000
# list_sheets reads tabs in full, smallest first, until their grids (rows x cols) add up to this; the rest
# report their grid size only.
_LIST_SCAN_CELLS = 2_000_000


@asynccontextmanager
async def lifespan(_server):
    global _client
    _client = WecomClient(CookieProvider(use_env=_use_env_cookies))
    try:
        yield None
    finally:
        await _client.aclose()
        _client = None


mcp = MCPServer(
    name="wecom-excel",
    instructions=(
        "Read WeCom (企业微信/doc.weixin.qq.com) online spreadsheets. "
        "Pass a doc URL or padId (e3_...). Call list_sheets first for tab names and the range holding data, "
        "then read_sheet with an A1 range for large sheets. Row numbers are 1-based like Excel. "
        "read_sheet's `content` picks the detail: \"text\" (values, default), \"text_images\" (values plus "
        "the pictures in cells) or \"full\" (HTML with merges/colours/fonts, plus pictures). get_images returns "
        "only pictures."
    ),
    lifespan=lifespan,
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
Format = Literal["csv", "markdown", "json", "records"]
Content = Literal["text", "text_images", "full"]


def _client_or_fail() -> WecomClient:
    if _client is None:
        raise ToolError("server not initialised")
    return _client


def _use_caller_cookie(ctx: Context | None) -> None:
    """Read this tool call with the cookie its caller sent, if any (HTTP transport only).

    The cookie lives in a context variable for the length of the call and is never stored or logged."""
    headers = (ctx.headers if ctx is not None else None) or {}
    cookie = headers.get(COOKIE_HEADER)
    if cookie is None and not _use_env_cookies:
        raise ToolError(f"no cookie: send your doc.weixin.qq.com cookie in the {COOKIE_HEADER} header")
    request_cookie.set(cookie.strip() if cookie else None)


# Hard caps on what one call may ask for, whatever the caller sends: each picture is downloaded and tiled in
# memory, and each row rendered.
LIMITS = {"max_rows": 10_000, "max_images": 50, "max_results": 1000}
# Tool calls per caller IP on HTTP: RATE a second, bursts of BURST (nginx in front may limit more).
RATE, BURST = 10.0, 40.0
_buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last refill)
_MAX_BUCKETS = 10_000


def _request(ctx):
    return getattr(getattr(ctx, "request_context", None), "request", None) if ctx is not None else None


def _allow(ip: str) -> bool:
    now = time.monotonic()
    tokens, last = _buckets.pop(ip, (BURST, now))
    tokens = min(BURST, tokens + (now - last) * RATE)
    ok = tokens >= 1
    _buckets[ip] = (tokens - 1 if ok else tokens, now)  # re-inserted: the dict stays in least-recent order
    while len(_buckets) > _MAX_BUCKETS:
        del _buckets[next(iter(_buckets))]
    return ok


def _guarded(tool):
    """Every tool call: clamp the LIMITS arguments, rate-limit HTTP callers by IP, and record the call in the
    usage log when there is one (who, which doc, outcome, duration). Calls that bring no cookie are not
    recorded, so nobody can fill the log without a WeCom login."""

    sig = inspect.signature(tool)

    @functools.wraps(tool)
    async def wrapper(*args, **kwargs):
        bound = sig.bind_partial(*args, **kwargs)
        for name, cap in LIMITS.items():
            if isinstance(bound.arguments.get(name), int):
                bound.arguments[name] = max(0, min(bound.arguments[name], cap))
        ctx = bound.arguments.get("ctx")
        if (request := _request(ctx)) is not None and not _allow(client_ip(request)):
            raise ToolError("too many requests: wait a few seconds and try again")
        headers = (ctx.headers if ctx is not None else None) or {}
        if _usage is None or (request is not None and COOKIE_HEADER not in headers):
            return await tool(*bound.args, **bound.kwargs)
        start, error = time.monotonic(), None
        try:
            return await tool(*bound.args, **bound.kwargs)
        except Exception as e:
            error = str(e) or type(e).__name__
            raise
        finally:
            try:
                _record(tool.__name__, bound.arguments, error, start)
            except Exception:  # statistics never break a call
                logging.getLogger(__name__).exception("recording usage failed")

    return wrapper


def _record(name: str, kwargs: dict, error: str | None, start: float) -> None:
    ctx = kwargs.get("ctx")
    headers = (ctx.headers if ctx is not None else None) or {}
    pad_id = title = None
    if doc := kwargs.get("doc"):
        try:
            pad_id, _ = parse_doc_ref(doc)
        except Exception:
            pass
        if pad_id and _client is not None and (info := _client.peek_doc_info(pad_id)):
            title = info.title
    _usage.record(
        tool=name, ok=error is None, error=error, ms=int((time.monotonic() - start) * 1000),
        user=urllib.parse.unquote(headers.get(USER_HEADER, ""))[:100] or None,
        ip=client_ip(request) if (request := _request(ctx)) else None,
        client=(headers.get("user-agent") or "")[:100] or None,
        pad_id=pad_id, title=title,
        client_id=cid if _CLIENT_ID.fullmatch(cid := headers.get(CLIENT_ID_HEADER, "")) else None,
    )


def _pick_tab(info: DocInfo, sheet: str | None, url_tab: str | None) -> Tab:
    if not info.tabs:
        raise ToolError(f"doc {info.pad_id} has no sheet tabs (padType={info.pad_type!r}); only normal sheets are supported")
    want = sheet or url_tab
    if not want:
        return next((t for t in info.tabs if not t.hidden), info.tabs[0])
    for t in info.tabs:
        if want in (t.id, t.name):
            return t
    names = ", ".join(f"{t.name} ({t.id})" for t in info.tabs)
    raise ToolError(f"sheet {want!r} not found; available: {names}")


async def _load(doc: str, sheet: str | None, start_row: int, end_row: int):
    """Doc info, tab and decoded rows [start_row, end_row] (0-based; -1 = to the end).

    A whole tab is read through the client's cache, and a range is served from a cached copy of its tab when
    there is one; otherwise only the range is fetched (and not cached)."""
    whole = start_row == 0 and end_row < 0
    try:
        pad_id, url_tab = parse_doc_ref(doc)
        client = _client_or_fail()
        info = client.peek_doc_info(pad_id)
        # Without a fresh tab list, start on the URL's tab while the list loads: saves a round trip.
        guess = url_tab if sheet is None and info is None else None
        early = asyncio.create_task(client.fetch_sheet_range(pad_id, guess, start_row, end_row)) if guess else None
        try:
            info = info or await client.doc_info(pad_id)
            tab = _pick_tab(info, sheet, url_tab)
            hit = client.sheet_cache.get(pad_id, tab.id, info.rev)
            if hit:
                data, meta = hit
            elif early and tab.id == guess:
                payloads, meta = await early
                data = await client.decode(payloads, tab.id, {t.id: t.name for t in info.tabs})
                if whole:
                    client.cache_tab(info, tab.id, data, meta)
            elif whole:
                data, meta = await client.whole_tab(info, tab.id)
            else:
                payloads, meta = await client.fetch_sheet_range(pad_id, tab.id, start_row, end_row)
                data = await client.decode(payloads, tab.id, {t.id: t.name for t in info.tabs})
        finally:
            if early and not early.done():
                early.cancel()
            elif early and not early.cancelled():
                early.exception()  # unused guess: mark a failure as retrieved
    except (WecomError, DecodeError) as e:
        raise ToolError(str(e)) from e
    return info, tab, data, meta


def _area(grid: tuple[int, int] | str) -> int:
    return grid[0] * grid[1] if isinstance(grid, tuple) else 0


def _used_range(data) -> str:
    """A1 range from A1 to the last row and column holding a value or a picture, or "empty"."""
    keys = [*data.cells, *data.images]
    if not keys:
        return "empty"
    rows, cols = max(r for r, _ in keys) + 1, max(c for _, c in keys) + 1
    return f"A1:{col_letter(cols - 1)}{rows} ({rows} rows x {cols} cols)"


def _notes(data) -> str:
    if not data.unknown_types:
        return ""
    kinds = ", ".join(f"{k}×{n}" for k, n in data.unknown_types.items())
    return f"\n(note: this tab has cells of undecoded value types {kinds}; they read as empty)"


@mcp.tool(annotations=READ_ONLY)
@_guarded
async def list_sheets(doc: str, ctx: Context = None) -> str:
    """List the tabs of a WeCom online spreadsheet with the range that holds data.

    `used` runs from A1 to the last row and column with a value or picture; `grid` is the tab's allocated
    size, often far larger. Finding `used` reads tabs in full, so the first call on a big doc takes a few
    seconds (very big docs get `grid` only for their largest tabs); the tabs stay cached, which makes
    reading them afterwards fast.

    Args:
        doc: Doc URL (https://doc.weixin.qq.com/sheet/e3_...) or padId.
    """
    _use_caller_cookie(ctx)
    try:
        pad_id, _ = parse_doc_ref(doc)
        client = _client_or_fail()
        info = await client.doc_info(pad_id)
    except WecomError as e:
        raise ToolError(str(e)) from e
    sem = asyncio.Semaphore(_SHEET_FETCH_CONCURRENCY)
    grids = [t for t in info.tabs if t.type == "grid"]
    sizes = {t.id: _sizes[k] for t in grids if (k := (pad_id, t.id, info.rev)) in _sizes}

    async def grid(t) -> tuple[int, int] | str:
        async with sem:
            try:
                _, meta = await client.fetch_sheet(pad_id, t.id, 0, 0)  # metadata only, no decoding
            except WecomError as e:
                return f" (size unavailable: {e})"
        return int(meta.get("max_row") or 0), int(meta.get("max_col") or 0)

    async def used(t, rows: int, cols: int) -> str:
        async with sem:
            try:
                data, meta = await client.whole_tab(info, t.id)
            except (WecomError, DecodeError) as e:
                return f" grid={rows}x{cols} (used range unavailable: {e})"
        line = f" used={_used_range(data)} grid={rows}x{cols}"
        if info.rev is not None and meta.get("rev") is not None:  # filed under the revision the content came with
            if len(_sizes) >= _MAX_SIZES:
                _sizes.clear()
            _sizes[(pad_id, t.id, meta["rev"])] = line
        return line

    todo = [t for t in grids if t.id not in sizes]
    budget, scans = _LIST_SCAN_CELLS, []
    for t, g in sorted(zip(todo, await asyncio.gather(*(grid(t) for t in todo))), key=lambda x: _area(x[1])):
        if isinstance(g, str):
            sizes[t.id] = g
        elif client.sheet_cache.get(pad_id, t.id, info.rev):
            scans.append((t, *g))  # already decoded: costs nothing
        elif _area(g) <= budget:
            budget -= _area(g)
            scans.append((t, *g))
        else:
            sizes[t.id] = f" grid={g[0]}x{g[1]} (not scanned for its used range: too big; read it with a range)"
    scans.reverse()  # start the biggest first: they take longest
    for (t, *_), line in zip(scans, await asyncio.gather(*(used(*x) for x in scans))):
        sizes[t.id] = line

    lines = [f"title: {info.title}", f"padId: {info.pad_id}", f"type: {info.pad_type}", "sheets:"]
    for t in info.tabs:
        hidden = " hidden" if t.hidden else ""
        lines.append(f"- {t.name} id={t.id} type={t.type}{hidden}{sizes.get(t.id, '')}")
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
@_guarded
async def read_sheet(
    doc: str,
    sheet: str | None = None,
    range: str | None = None,
    content: Content = "text",
    format: Format = "csv",
    header: bool = True,
    max_rows: int = 500,
    max_images: int = 5,
    tile: bool = True,
    show_formulas: bool = False,
    ctx: Context = None,
) -> list[str | Image]:
    """Read a WeCom online spreadsheet tab at the level of detail you need.

    Args:
        doc: Doc URL (https://doc.weixin.qq.com/sheet/e3_...) or padId. A `tab=` in the URL selects the sheet.
        sheet: Tab name or id; defaults to the URL's tab, else the first visible tab.
        range: A1 range like "A1:F100", "A:C", "2:50" or "B3"; omitted = whole sheet.
        content: How much to return:
            "text" (default): cell values only; cells holding a picture read "[image]".
            "text_images": the same text plus the pictures in the returned rows, each labelled with its cell.
            "full": an HTML table with all formatting (merged cells, bold, font colour/size, fill,
                alignment, borders, column widths, row heights) plus the pictures. `format` and
                `header` do not apply.
        format: For text/text_images: csv (compact, default), markdown, json (rows with values), or
            records (list of objects keyed by the first row — use with header=true).
        header: For records: treat the first returned row as column names.
        max_rows: Truncate after this many rows (default 500, at most 10000).
        max_images: For text_images/full: return at most this many pictures (default 5, at most 50); the rest are
            listed by cell (fetch them with get_images and a narrower range).
        tile: Split pictures too large to be seen at full resolution into overlapping tiles (default true).
        show_formulas: Show formula text (e.g. "=SUM(E2:E22)") instead of the computed value.
    """
    _use_caller_cookie(ctx)
    try:
        rng = parse_range(range)
    except ValueError as e:
        raise ToolError(str(e)) from e
    if content == "full":
        info, tab, data, _ = await _load(doc, sheet, 0, -1)  # whole tab: merges may start above the range
        body, span, truncated = render_html(data, rng, max_rows, show_formulas)
        head = f"# {info.title} / {tab.name}"
        if span is None:
            return [head + " — no data in range"]
        head += f" — rows {span[0] + 1}-{span[1] + 1}"
        if truncated:
            head += f" (truncated; continue with range=\"{span[1] + 2}:{span[1] + 1 + max_rows}\" or raise max_rows)"
        shown = Range(span[0], span[1], rng.c0, rng.c1)
        return [head + "\n" + body + _notes(data), *await _pictures(data, shown, max_images, tile)]

    end_row = -1 if rng.r1 is None else rng.r1
    info, tab, data, meta = await _load(doc, sheet, rng.r0, end_row)
    cells = {**data.cells, **data.formulas} if show_formulas else dict(data.cells)
    for k in data.images:  # pictures have no value of their own; mark them so they are not mistaken for blanks
        cells[k] = f"{str(cells.get(k, '')).strip()} [image]".lstrip()
    rows, cols, grid = build_grid(cells, rng, max_rows)
    total = len(rows)
    truncated = total > max_rows
    if truncated:
        rows = rows[:max_rows]
    head = f"# {info.title} / {tab.name}"
    if rows:
        head += f" — {col_letter(cols[0])}{rows[0] + 1}:{col_letter(cols[-1])}{rows[-1] + 1}, {len(rows)} non-empty rows"
    else:
        head += " — no data in range"
    if truncated:
        nxt = rows[-1] + 2
        head += f" (truncated from {total} rows; continue with range=\"{nxt}:{nxt + max_rows - 1}\" or raise max_rows)"
    out: list[str | Image] = [head + "\n" + render(rows, cols, grid, format, header) + _notes(data)]
    if content == "text_images" and rows:
        out += await _pictures(data, Range(rng.r0, rows[-1], rng.c0, rng.c1), max_images, tile)
    return out


@mcp.tool(annotations=READ_ONLY)
@_guarded
async def search_sheet(
    doc: str, query: str, sheet: str | None = None, case_sensitive: bool = False, max_results: int = 100,
    ctx: Context = None,
) -> str:
    """Find cells containing text in a WeCom online spreadsheet tab; returns A1 addresses and values.

    Args:
        doc: Doc URL or padId.
        query: Substring to look for.
        sheet: Tab name or id; defaults to the URL's tab, else the first visible tab.
        case_sensitive: Match case exactly (default false).
        max_results: Stop after this many hits (default 100, at most 1000).
    """
    _use_caller_cookie(ctx)
    info, tab, data, _ = await _load(doc, sheet, 0, -1)
    needle = query if case_sensitive else query.lower()
    hits = []
    for (r, c), v in sorted(data.cells.items()):
        s = str(v)
        if needle in (s if case_sensitive else s.lower()):
            hits.append(f"{col_letter(c)}{r + 1}\t{s}")
            if len(hits) >= max_results:
                break
    head = f"# {info.title} / {tab.name} — {len(hits)} match(es) for {query!r}"
    if len(hits) >= max_results:
        head += " (limit reached)"
    return "\n".join([head, *hits])


def _picture_label(r: int, c: int, img) -> str:
    size = img.natural_size
    return f"{col_letter(c)}{r + 1}" + (f" ({size[0]}x{size[1]})" if size else "")


async def _pictures(data, rng: Range, max_images: int, tile: bool = True) -> list[str | Image]:
    """The original pictures in `rng` as labelled Image blocks, at most `max_images` of them;
    pictures too big for the model to see at full resolution are split into tiles when `tile`."""
    imgs = images_in(data, rng)
    if not imgs:
        return []
    shown, rest = imgs[: max(max_images, 0)], imgs[max(max_images, 0) :]
    client = _client_or_fail()
    sem = asyncio.Semaphore(_IMAGE_FETCH_CONCURRENCY)

    async def fetch(url):
        async with sem:
            try:
                return await client.fetch_image(url)
            except Exception as e:  # one broken picture should not fail the rest
                return e

    results = await asyncio.gather(*(fetch(img.original_url) for _, _, img in shown))
    out: list[str | Image] = [f"pictures: {len(imgs)} in range, showing {len(shown)}"]
    for (r, c, img), res in zip(shown, results):
        label = _picture_label(r, c, img)
        if isinstance(res, Exception):
            out.append(f"{label} — download failed: {res}")
            continue
        body, mime = res
        fmt = mime.removeprefix("image/")
        parts = await asyncio.to_thread(tile_image, body, fmt) if tile else [Tile(body, fmt, "")]
        for part in parts:
            text = f"{label} {part.label}".rstrip()
            if len(part.data) > _MAX_IMAGE_BYTES:
                out.append(f"{text} — {len(part.data) // 1024} KB, too large to return; {img.original_url}")
                continue
            out.append(text)
            out.append(Image(data=part.data, format=part.format))
    if rest:
        out.append("not shown: " + ", ".join(f"{col_letter(c)}{r + 1}" for r, c, _ in rest)
                   + " (raise max_images, or use get_images with a narrower range)")
    return out


@mcp.tool(annotations=READ_ONLY)
@_guarded
async def get_images(
    doc: str,
    sheet: str | None = None,
    range: str | None = None,
    max_images: int = 5,
    list_only: bool = False,
    tile: bool = True,
    ctx: Context = None,
) -> list[str | Image]:
    """Return only the pictures placed in cells of a tab (original files), each labelled with its cell.

    Args:
        doc: Doc URL or padId. A `tab=` in the URL selects the sheet.
        sheet: Tab name or id; defaults to the URL's tab, else the first visible tab.
        range: Only pictures in this A1 range, e.g. "J2:P10" or "L5".
        max_images: Return at most this many pictures (default 5, at most 50); the rest are listed by cell.
        list_only: Only list cells, sizes and original-file URLs (no login needed, e.g. for curl),
            without downloading the pictures.
        tile: Split pictures too large to be seen at full resolution (over ~1568 px or ~1.15 MP) into
            overlapping tiles so small text stays legible (default true).
    """
    _use_caller_cookie(ctx)
    try:
        rng = parse_range(range)
    except ValueError as e:
        raise ToolError(str(e)) from e
    end_row = -1 if rng.r1 is None else rng.r1
    info, tab, data, _ = await _load(doc, sheet, rng.r0, end_row)
    imgs = images_in(data, rng)
    head = f"# {info.title} / {tab.name} — {len(imgs)} picture(s)" + (f" in {range}" if range else "")
    if list_only or not imgs:
        return ["\n".join([head, *(f"{_picture_label(r, c, img)} {img.original_url}" for r, c, img in imgs)])]
    return [head, *await _pictures(data, rng, max_images, tile)]


def _data_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "wecom-excel")


def _write_admin(path: str, user: str, password: str) -> None:
    from .admin import hash_password

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{user}:{hash_password(password)}\n")
    os.replace(path + ".tmp", path)


def _local_address() -> str:
    """This machine's address on its outgoing interface (nothing is sent), for the startup message."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _run_http(host: str, port: int, *, caller_cookies_only: bool, usage_db: str = "", admin_password_file: str = "",
              allowed_hosts: list[str] | None = None, site: bool = False, public_url: str = "") -> None:
    global _use_env_cookies, _usage
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _use_env_cookies = not caller_cookies_only
    if usage_db:
        from .admin import Admin, add_routes

        _usage = UsageLog(usage_db)
        state = os.path.dirname(os.path.abspath(usage_db))
        add_routes(mcp, Admin(_usage, admin_password_file or os.path.join(state, "admin-password"), state))
    if site:
        from . import web

        web.add_routes(mcp, public_url)
    if allowed_hosts is None:  # a public server: any Host (DNS-rebinding checks protect servers on localhost)
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        security = TransportSecuritySettings(allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *allowed_hosts])
    # Stateless: every request stands alone (and carries its own cookie), so restarts drop no sessions.
    mcp.run("streamable-http", host=host, port=port, stateless_http=True, transport_security=security)


def serve(argv: list[str]) -> None:
    """`wecom-excel-mcp serve`: the shared server in one command, with its setup page and /admin."""
    p = argparse.ArgumentParser(prog="wecom-excel-mcp serve", description="run the shared server: setup page at /, "
                                "MCP at /mcp, usage statistics at /admin; each caller's own cookie, none stored")
    p.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"), help="default 0.0.0.0 (every interface)")
    p.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8765")))
    p.add_argument("--data-dir", default=os.environ.get("WECOM_DATA_DIR") or _data_dir(),
                   help=f"usage statistics and the /admin login (default {_data_dir()})")
    p.add_argument("--public-url", default=os.environ.get("WECOM_PUBLIC_URL", ""),
                   help="the address users reach, e.g. https://wecom.example.com (default: taken from each request)")
    p.add_argument("--no-stats", action="store_true", help="record nothing and serve no /admin")
    args = p.parse_args(argv)
    usage_db = "" if args.no_stats else os.path.join(args.data_dir, "usage.db")
    shown = args.public_url.rstrip("/") or f"http://{_local_address() if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"wecom-excel: 说明页 {shown}/   MCP {shown}/mcp", file=sys.stderr)
    if usage_db:
        pw_file = os.path.join(args.data_dir, "admin-password")
        if not os.path.exists(pw_file):
            import secrets

            password = secrets.token_urlsafe(12)
            _write_admin(pw_file, "admin", password)
            print(f"wecom-excel: 后台 {shown}/admin   账号 admin   密码 {password}\n"
                  f"             （只显示这一次；改密码：wecom-excel-mcp passwd）", file=sys.stderr)
        else:
            print(f"wecom-excel: 后台 {shown}/admin", file=sys.stderr)
    if not args.public_url.startswith("https://"):
        print("wecom-excel: 注意：没有 HTTPS 时 cookie 会明文经过网络，只在内网或可信网络里这样用", file=sys.stderr)
    _run_http(args.host, args.port, caller_cookies_only=True, usage_db=usage_db, site=True, public_url=args.public_url)


def passwd(argv: list[str]) -> None:
    """`wecom-excel-mcp passwd`: set the /admin account and password."""
    import getpass

    p = argparse.ArgumentParser(prog="wecom-excel-mcp passwd", description="set the /admin account and password")
    p.add_argument("--user", default="admin")
    p.add_argument("--data-dir", default=os.environ.get("WECOM_DATA_DIR") or _data_dir())
    p.add_argument("--file", default="", help="the credentials file (default: admin-password in --data-dir)")
    args = p.parse_args(argv)
    if sys.stdin.isatty():
        password = getpass.getpass("新密码: ")
        if getpass.getpass("再输一次: ") != password:
            sys.exit("两次输入不一样")
    else:
        password = sys.stdin.readline().rstrip("\n")
    from .admin import MIN_PASSWORD

    if len(password) < MIN_PASSWORD:
        sys.exit(f"密码至少要 {MIN_PASSWORD} 位")
    path = args.file or os.path.join(args.data_dir, "admin-password")
    _write_admin(path, args.user, password)
    print(f"已保存到 {path}（账号 {args.user}）。已登录的后台会话都已失效。")


def main() -> None:
    if sys.argv[1:2] == ["serve"]:
        return serve(sys.argv[2:])
    if sys.argv[1:2] == ["passwd"]:
        return passwd(sys.argv[2:])
    p = argparse.ArgumentParser(description="WeCom online-sheet MCP server (`serve` runs the shared server in one "
                                "command; `passwd` sets its /admin password)")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default=os.environ.get("MCP_TRANSPORT", "stdio"))
    p.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8765")))
    p.add_argument(
        "--caller-cookies-only",
        action="store_true",
        default=os.environ.get("WECOM_CALLER_COOKIES_ONLY") == "1",
        help=f"HTTP: read docs only with the cookie each caller sends in {COOKIE_HEADER}, never the server's own",
    )
    p.add_argument(
        "--allowed-hosts",
        default=os.environ.get("MCP_ALLOWED_HOSTS", ""),
        help="HTTP: extra Host header values to accept besides localhost (comma-separated, e.g. a proxy's domain)",
    )
    p.add_argument(
        "--usage-db",
        default=os.environ.get("WECOM_USAGE_DB", ""),
        help="HTTP: record each tool call (no cookies, no sheet content) in this SQLite file and serve the "
        "statistics page at /admin, behind a login form",
    )
    p.add_argument(
        "--admin-password-file",
        default=os.environ.get("WECOM_ADMIN_PASSWORD_FILE", ""),
        help="the /admin credentials, one line `username:<hash from --hash-password>`; "
        "default: admin-password beside --usage-db",
    )
    p.add_argument("--site", action="store_true", help="HTTP: also serve the setup page, llms.txt and wecom-mcp.py")
    p.add_argument("--hash-password", action="store_true", help="read a password and print its hash for /admin")
    p.add_argument("--version", action="store_true", help="print the version and which decoder is in use")
    args = p.parse_args()
    if args.version:
        from importlib.metadata import version

        from . import decoder

        native = decoder._native is not None and decoder.USE_NATIVE
        print(f"wecom-excel-mcp {version('wecom-excel-mcp')} (decoder: {'rust' if native else 'python'})")
        return
    if args.hash_password:
        import getpass

        from .admin import hash_password

        password = getpass.getpass("password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
        print(hash_password(password))
        return
    if args.transport == "stdio":
        logging.getLogger("httpx").setLevel(logging.WARNING)
        mcp.run("stdio")
        return
    _run_http(
        args.host, args.port, caller_cookies_only=args.caller_cookies_only, usage_db=args.usage_db,
        admin_password_file=args.admin_password_file, site=args.site,
        allowed_hosts=[h.strip() for h in args.allowed_hosts.split(",") if h.strip()],
    )


if __name__ == "__main__":
    main()
