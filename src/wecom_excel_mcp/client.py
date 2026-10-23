"""WeCom doc HTTP client: cookie handling, doc metadata and sheet payload fetch."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
from http.cookiejar import CookieJar
from urllib.parse import parse_qs, urlparse

import httpx

from .decoder import SheetData, decode_related_sheet

BASE = "https://doc.weixin.qq.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
RETCODE_NOT_LOGGED_IN = 30004
IMAGE_CDN_SUFFIXES = (".qpic.cn",)
_RETCODE_HINTS = {
    RETCODE_NOT_LOGGED_IN: "not logged in: cookie is missing or expired",
    200000: "doc not found",
    200063: "doc not found or this account has no access to it",
}
# Fields returned by the cookie API that are bookkeeping, not cookies.
_COOKIE_API_EXTRA_KEYS = ("owner", "date")
_PAD_ID_RE = re.compile(r"\b([a-z]\d_[A-Za-z0-9_-]{20,})")
_FORCED_REFRESH_DEDUP = 5.0  # seconds
# Seconds. Every tool call needs the tab list, and its revision is what invalidates cached tabs, so this
# is also how long an edit to the doc can go unseen: short, but enough for a burst of calls.
DOC_INFO_TTL = 5.0
_MAX_DOC_INFOS = 1000  # past this, expired entries are dropped
# Rough upper bound on decoded tabs kept in memory, in cells (~350 bytes each with their styles).
SHEET_CACHE_CELLS = int(os.environ.get("WECOM_SHEET_CACHE_CELLS", "500000"))


# Cookie string sent by the caller of the current request (HTTP transport); takes the place of the
# server's own cookies. Set per tool call, and copied into the tasks that call starts.
request_cookie: ContextVar[str | None] = ContextVar("request_cookie", default=None)


class WecomError(Exception):
    pass


class _NoStoreJar(CookieJar):
    """Keeps nothing from responses: the client is shared by callers with different cookies."""

    def extract_cookies(self, response, request) -> None:
        pass


def _int_or_none(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_doc_ref(ref: str) -> tuple[str, str | None]:
    """Accept a padId or a doc URL; return (pad_id, tab_id_from_url_or_None)."""
    ref = ref.strip()
    tab = None
    if ref.startswith("http"):
        u = urlparse(ref)
        tab = (parse_qs(u.query).get("tab") or [None])[0]
        m = _PAD_ID_RE.search(u.path)
    else:
        m = _PAD_ID_RE.fullmatch(ref)
    if not m:
        raise WecomError(f"cannot find a doc id (like e3_xxx) in {ref!r}")
    return m.group(1), tab


def check_image_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or not (u.hostname or "").endswith(IMAGE_CDN_SUFFIXES):
        raise WecomError(f"not a WeCom image CDN URL: {url}")


def _parse_cookie_string(s: str) -> dict[str, str]:
    out = {}
    for part in s.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class CookieProvider:
    """Cookie source: the caller's cookie (`request_cookie`) when there is one, else, if allowed, env:

    WECOM_COOKIE      raw "k=v; k2=v2" cookie string copied from a logged-in browser
    WECOM_COOKIE_API  URL returning {"code": 0, "data": {cookie dict}}
    WECOM_COOKIE_TTL  seconds to cache API cookies (default 300)

    With `use_env=False` only callers' cookies are used, so a shared server never lends out its own.
    """

    def __init__(
        self, cookie: str | None = None, api: str | None = None, ttl: float | None = None, use_env: bool = True
    ):
        self.static = cookie if cookie is not None else (os.environ.get("WECOM_COOKIE") if use_env else None)
        self.api = api if api is not None else (os.environ.get("WECOM_COOKIE_API") if use_env else None)
        self.ttl = ttl if ttl is not None else float(os.environ.get("WECOM_COOKIE_TTL", "300"))
        self._cached: dict[str, str] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()
        if use_env and not self.static and not self.api:
            raise WecomError("set WECOM_COOKIE or WECOM_COOKIE_API")

    async def get(self, http: httpx.AsyncClient, force: bool = False) -> dict[str, str]:
        if (caller := request_cookie.get()) is not None:
            return _parse_cookie_string(caller)
        if self.static:
            return _parse_cookie_string(self.static)
        if not self.api:
            raise WecomError("no cookie: send your doc.weixin.qq.com cookie in the X-Wecom-Cookie header")
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            # Concurrent requests that all hit "not logged in" share one forced refresh.
            stale = force and age > _FORCED_REFRESH_DEDUP
            if stale or self._cached is None or age > self.ttl:
                r = await http.get(self.api)
                r.raise_for_status()
                body = r.json()
                if body.get("code") != 0 or not isinstance(body.get("data"), dict):
                    raise WecomError(f"cookie API failed: code={body.get('code')} msg={body.get('msg')}")
                data = {k: str(v) for k, v in body["data"].items() if k not in _COOKIE_API_EXTRA_KEYS}
                self._cached, self._fetched_at = data, time.monotonic()
            return dict(self._cached)

    @property
    def refreshable(self) -> bool:
        return request_cookie.get() is None and not self.static and bool(self.api)

    @staticmethod
    def identity() -> str:
        """Who the current request reads as: a digest of the caller's cookie, or "" for the server's own.
        Keys per-caller caches without keeping the cookie itself."""
        caller = request_cookie.get()
        return "" if caller is None else hashlib.sha256(caller.encode()).hexdigest()


class SheetCache:
    """Whole decoded tabs, least recently used first out once they hold more than `max_cells` cells.

    An entry is filed under the doc revision its content came with (`rev`, doc-wide, raised by every edit)
    and is a hit only for that revision."""

    def __init__(self, max_cells: int = SHEET_CACHE_CELLS):
        self.max_cells = max_cells
        self._entries: OrderedDict[tuple[str, str], tuple[int, SheetData, dict, int]] = OrderedDict()
        self._cells = 0

    @staticmethod
    def _size(data: SheetData) -> int:
        return max(len(data.cells), len(data.styles)) + len(data.images) + 1

    def get(self, pad_id: str, tab_id: str, version: int | None) -> tuple[SheetData, dict] | None:
        hit = self._entries.get((pad_id, tab_id))
        if hit is None or version is None or hit[0] != version:
            return None
        self._entries.move_to_end((pad_id, tab_id))
        return hit[1], hit[2]

    def put(self, pad_id: str, tab_id: str, version: int | None, data: SheetData, meta: dict) -> None:
        if version is None:  # no way to tell when it goes stale
            return
        self._drop((pad_id, tab_id))
        size = self._size(data)
        if size > self.max_cells:
            return
        self._entries[(pad_id, tab_id)] = (version, data, meta, size)
        self._cells += size
        while self._cells > self.max_cells:
            self._drop(next(iter(self._entries)))

    def _drop(self, key: tuple[str, str]) -> None:
        old = self._entries.pop(key, None)
        if old is not None:
            self._cells -= old[3]


@dataclass
class Tab:
    id: str
    name: str
    type: str
    hidden: bool


@dataclass
class DocInfo:
    pad_id: str
    title: str
    pad_type: str
    tabs: list[Tab]
    last_modify_time: int | None
    rev: int | None = None  # doc-wide revision, the same counter get/sheet returns with the content


class WecomClient:
    def __init__(self, cookies: CookieProvider, timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None):
        self.cookies = cookies
        self.http = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": UA, "Referer": f"{BASE}/"},
            follow_redirects=True,
            cookies=_NoStoreJar(),
        )
        # By (caller identity, pad_id): the tab list is also the access check, since it is fetched with the
        # caller's cookie. Decoded tabs (`sheet_cache`) are shared, and only reached after that check.
        self._doc_info_cache: dict[tuple[str, str], tuple[float, DocInfo]] = {}
        self.sheet_cache = SheetCache()
        self._inflight: dict[tuple[str, str, int | None], asyncio.Future] = {}

    async def aclose(self) -> None:
        tasks = list(self._inflight.values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.http.aclose()

    async def _get_json(self, path: str, params: dict) -> dict:
        """GET with cookies, retrying once with fresh cookies on a login failure."""
        for attempt in (0, 1):
            try:
                ck = await self.cookies.get(self.http, force=attempt == 1)
                cookie = "; ".join(f"{k}={v}" for k, v in ck.items())
                r = await self.http.get(BASE + path, params=params, headers={"Cookie": cookie})
                r.raise_for_status()
                body = r.json()
            except (httpx.HTTPError, ValueError) as e:
                raise WecomError(f"request to {path} failed: {type(e).__name__}: {e}") from e
            retcode = body.get("retcode") or (body.get("clientVars") or {}).get("retcode")
            if retcode == RETCODE_NOT_LOGGED_IN and attempt == 0 and self.cookies.refreshable:
                continue
            if retcode:
                msg = _RETCODE_HINTS.get(retcode) or body.get("errmsg") or "unknown error"
                raise WecomError(f"WeCom API error retcode={retcode}: {msg}")
            return body
        raise AssertionError("unreachable")

    def peek_doc_info(self, pad_id: str) -> DocInfo | None:
        """The cached doc info if still fresh, without a request."""
        hit = self._doc_info_cache.get((self.cookies.identity(), pad_id))
        if hit and time.monotonic() - hit[0] < DOC_INFO_TTL:
            return hit[1]
        return None

    async def doc_info(self, pad_id: str) -> DocInfo:
        """Doc title and tabs, cached for DOC_INFO_TTL seconds."""
        hit = self.peek_doc_info(pad_id)
        if hit:
            return hit
        info = await self._fetch_doc_info(pad_id)
        now = time.monotonic()
        if len(self._doc_info_cache) >= _MAX_DOC_INFOS:
            self._doc_info_cache = {k: v for k, v in self._doc_info_cache.items() if now - v[0] < DOC_INFO_TTL}
        self._doc_info_cache[(self.cookies.identity(), pad_id)] = (now, info)
        return info

    async def _fetch_doc_info(self, pad_id: str) -> DocInfo:
        body = await self._get_json(
            "/dop-api/opendoc",
            {"id": pad_id, "outformat": 1, "normal": 1, "startrow": 0, "endrow": 0},
        )
        cv = body.get("clientVars") or {}
        collab = cv.get("collab_client_vars") or {}
        pad_type = cv.get("padType") or body.get("padType") or ""
        tabs = []
        for group in collab.get("header") or []:
            for t in group.get("d") or []:
                tabs.append(Tab(t.get("id") or "", t.get("name") or "", t.get("type") or "", bool(t.get("hidden"))))
        title = (body.get("bodyData") or {}).get("initialTitle") or cv.get("initialTitle") or ""
        return DocInfo(pad_id, title, pad_type, tabs, cv.get("lastModifyTime"), _int_or_none(collab.get("rev")))

    async def fetch_sheet(self, pad_id: str, tab_id: str, start_row: int = 0, end_row: int = -1) -> tuple[str, dict]:
        """Raw payload and metadata (max_row, max_col, end_row_index, ...) of rows [start_row, end_row]
        (inclusive, 0-based; -1 = to the end) of one tab."""
        body = await self._get_json(
            "/dop-api/get/sheet",
            {"padId": pad_id, "subId": tab_id, "startrow": start_row, "endrow": end_row, "outformat": 1, "nowb": 1},
        )
        try:
            text = body["data"]["initialAttributedText"]["text"][0]
            payload = text["related_sheet"]
        except (KeyError, IndexError, TypeError) as e:
            raise WecomError(f"unexpected get/sheet response shape (missing {e}); is this a normal sheet?") from e
        meta = {k: v for k, v in text.items() if k not in ("related_sheet", "workbook")}
        meta["rev"] = _int_or_none(body["data"].get("rev"))
        return payload, meta

    async def sheet(
        self, pad_id: str, tab_id: str, start_row: int = 0, end_row: int = -1, sheet_names: dict[str, str] | None = None
    ) -> tuple[SheetData, dict]:
        """Fetch and decode rows [start_row, end_row] of one tab.

        `sheet_names` (tab id -> name) names other tabs referenced by formulas."""
        payload, meta = await self.fetch_sheet(pad_id, tab_id, start_row, end_row)
        data = await asyncio.to_thread(decode_related_sheet, payload, tab_id, sheet_names)
        return data, meta

    async def fetch_image(self, url: str) -> tuple[bytes, str]:
        """Download a picture from WeCom's CDN (public URL; no cookies are sent).

        Picture URLs come from sheet content anyone with edit access controls, and this server may run
        inside a private network, so only https URLs on WeCom's image CDN are fetched, without redirects.
        """
        check_image_url(url)
        r = await self.http.get(url, follow_redirects=False)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "").split(";")[0] or "image/png"

    async def fetch_sheet_range(
        self, pad_id: str, tab_id: str, start_row: int = 0, end_row: int = -1
    ) -> tuple[list[str], dict]:
        """Like `fetch_sheet`, but keeps fetching if the server returns fewer rows than asked (chunked docs)."""
        payload, meta = await self.fetch_sheet(pad_id, tab_id, start_row, end_row)
        payloads = [payload]
        max_row = int(meta.get("max_row") or 0)
        target = max_row - 1 if end_row < 0 else min(end_row, max_row - 1)
        got = int(meta.get("end_row_index", target))
        while got < target:
            more, more_meta = await self.fetch_sheet(pad_id, tab_id, got + 1, end_row)
            payloads.append(more)
            if more_meta.get("rev") != meta.get("rev"):
                meta["rev"] = None  # edited between chunks: no single revision describes the whole

            new_got = int(more_meta.get("end_row_index", target))
            if new_got <= got:
                break
            got = new_got
        return payloads, meta

    @staticmethod
    async def decode(payloads: list[str], tab_id: str, sheet_names: dict[str, str] | None = None) -> SheetData:
        """Decode the chunks from `fetch_sheet_range` into one SheetData."""

        def run() -> SheetData:
            data = decode_related_sheet(payloads[0], tab_id, sheet_names)
            for p in payloads[1:]:
                data.update(decode_related_sheet(p, tab_id, sheet_names))
            return data

        return await asyncio.to_thread(run)

    async def sheet_range(
        self, pad_id: str, tab_id: str, start_row: int = 0, end_row: int = -1, sheet_names: dict[str, str] | None = None
    ) -> tuple[SheetData, dict]:
        """Fetch and decode rows [start_row, end_row] of one tab, following chunks."""
        payloads, meta = await self.fetch_sheet_range(pad_id, tab_id, start_row, end_row)
        return await self.decode(payloads, tab_id, sheet_names), meta

    def cache_tab(self, info: DocInfo, tab_id: str, data: SheetData, meta: dict) -> None:
        """Keep a whole tab, filed under the revision its content came with."""
        if info.rev is None:  # lookups go by the tab list's revision: without one, nothing would ever hit
            return
        self.sheet_cache.put(info.pad_id, tab_id, meta.get("rev"), data, meta)

    async def whole_tab(self, info: DocInfo, tab_id: str) -> tuple[SheetData, dict]:
        """All of one tab, decoded: from the cache when the doc is still at a revision read before, else
        fetched and cached. Concurrent calls for the same tab share one fetch."""
        hit = self.sheet_cache.get(info.pad_id, tab_id, info.rev)
        if hit:
            return hit
        key = (info.pad_id, tab_id, info.rev)
        task = self._inflight.get(key)
        if task is None:

            async def load() -> tuple[SheetData, dict]:
                payloads, meta = await self.fetch_sheet_range(info.pad_id, tab_id)
                data = await self.decode(payloads, tab_id, {t.id: t.name for t in info.tabs})
                self.cache_tab(info, tab_id, data, meta)
                return data, meta

            task = asyncio.ensure_future(load())
            self._inflight[key] = task

            def done(t: asyncio.Future) -> None:
                self._inflight.pop(key, None)
                if not t.cancelled():
                    t.exception()  # mark as retrieved even if every caller has given up

            task.add_done_callback(done)
        return await asyncio.shield(task)  # one caller giving up must not cancel the others
