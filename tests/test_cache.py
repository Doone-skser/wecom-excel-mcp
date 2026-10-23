import asyncio
from pathlib import Path

import pytest

from wecom_excel_mcp import server
from wecom_excel_mcp.client import (
    CookieProvider,
    DocInfo,
    SheetCache,
    Tab,
    WecomClient,
    WecomError,
)
from wecom_excel_mcp.decoder import SheetData

FIX = Path(__file__).parent / "fixtures"
PAD = "e3_ExampleSheet0000000000000001"
URL = f"https://doc.weixin.qq.com/sheet/{PAD}?tab=BB08J2"


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch):
    """The fake client makes no requests; keep httpx from setting up a proxy from the environment."""
    for k in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy"):
        monkeypatch.delenv(k, raising=False)


def sheet(n: int) -> SheetData:
    return SheetData(cells={(r, 0): r for r in range(n)})


def test_cache_hit_needs_same_version():
    c = SheetCache(100)
    c.put("p", "t", 1, sheet(3), {"max_row": 3})
    assert c.get("p", "t", 1)[1] == {"max_row": 3}
    assert c.get("p", "t", 2) is None  # doc edited since
    assert c.get("p", "t", None) is None


def test_cache_without_version_stores_nothing():
    c = SheetCache(100)
    c.put("p", "t", None, sheet(3), {})
    assert c.get("p", "t", None) is None and c._cells == 0


def test_cache_evicts_least_recently_used():
    c = SheetCache(25)  # each entry: 10 cells + 1
    c.put("p", "a", 1, sheet(10), {})
    c.put("p", "b", 1, sheet(10), {})
    c.get("p", "a", 1)
    c.put("p", "c", 1, sheet(10), {})
    assert c.get("p", "b", 1) is None
    assert c.get("p", "a", 1) and c.get("p", "c", 1)
    assert c._cells == 22


def test_cache_new_version_replaces_old_and_oversized_is_skipped():
    c = SheetCache(25)
    c.put("p", "a", 1, sheet(10), {})
    c.put("p", "a", 2, sheet(5), {})
    assert c.get("p", "a", 1) is None and c.get("p", "a", 2) and c._cells == 6
    c.put("p", "a", 3, sheet(30), {})
    assert c.get("p", "a", 3) is None and c._cells == 0


class FakeClient(WecomClient):
    """Serves the plain_strings fixture as tab BB08J2 (and an empty tab T2), recording requests.

    `rev` is the revision content comes with; `info_rev` the one the tab list reports (default: the same)."""

    def __init__(self, rev: int | None = 1):
        super().__init__(CookieProvider(cookie="k=v"))
        self.rev = rev
        self.info_rev: int | None = None
        self.tabs = [Tab("BB08J2", "工作表1", "grid", False)]
        self.fail: set[str] = set()
        self.chunk_rows: int | None = None  # serve at most this many rows per request
        self.grid = (1000, 26)
        self.fetches: list[tuple[str, int, int]] = []

    async def _fetch_doc_info(self, pad_id):
        rev = self.rev if self.info_rev is None else self.info_rev
        return DocInfo(pad_id, "doc", "sheet", self.tabs, 0, rev)

    async def fetch_sheet(self, pad_id, tab_id, start_row=0, end_row=-1):
        self.fetches.append((tab_id, start_row, end_row))
        await asyncio.sleep(0.01)
        if tab_id in self.fail:
            raise WecomError("boom")
        last = self.grid[0] - 1 if end_row < 0 else end_row
        if self.chunk_rows:
            last = min(last, start_row + self.chunk_rows - 1)
        meta = {"max_row": self.grid[0], "max_col": self.grid[1], "end_row_index": last, "rev": self.rev}
        if self.chunk_rows and start_row > 0:
            self.rev += 1  # edited while the chunks were fetched
        return (FIX / "plain_strings.b64").read_text(), meta

    def whole_fetches(self):
        return [f for f in self.fetches if f[1:] == (0, -1)]


@pytest.fixture
async def fake():
    server._sizes.clear()
    client = FakeClient()
    server._client = client
    yield client
    server._client = None
    await client.aclose()


async def test_whole_tab_concurrent_calls_share_one_fetch(fake):
    info = await fake.doc_info(PAD)
    (a, _), (b, _) = await asyncio.gather(fake.whole_tab(info, "BB08J2"), fake.whole_tab(info, "BB08J2"))
    assert a is b and len(fake.fetches) == 1
    assert (await fake.whole_tab(info, "BB08J2"))[0] is a and len(fake.fetches) == 1
    assert not fake._inflight


async def test_whole_tab_shared_failure_reaches_every_caller(fake):
    fake.fail.add("BB08J2")
    info = await fake.doc_info(PAD)
    res = await asyncio.gather(fake.whole_tab(info, "BB08J2"), fake.whole_tab(info, "BB08J2"), return_exceptions=True)
    assert all(isinstance(r, WecomError) for r in res) and len(fake.fetches) == 1
    assert not fake._inflight


async def test_whole_tab_one_caller_cancelled_others_still_served(fake):
    info = await fake.doc_info(PAD)
    a = asyncio.ensure_future(fake.whole_tab(info, "BB08J2"))
    b = asyncio.ensure_future(fake.whole_tab(info, "BB08J2"))
    await asyncio.sleep(0)
    a.cancel()
    data, _ = await b
    assert data.cells and len(fake.fetches) == 1


async def test_whole_tab_refetches_after_an_edit(fake):
    info = await fake.doc_info(PAD)
    await fake.whole_tab(info, "BB08J2")
    fake.rev = 2
    await fake.whole_tab(DocInfo(info.pad_id, info.title, info.pad_type, info.tabs, 0, 2), "BB08J2")
    assert len(fake.fetches) == 2


async def test_content_is_filed_under_its_own_revision(fake):
    fake.rev, fake.info_rev = 2, 1  # tab list read before an edit, content after it
    info = await fake.doc_info(PAD)
    await fake.whole_tab(info, "BB08J2")
    assert fake.sheet_cache.get(PAD, "BB08J2", 1) is None
    assert fake.sheet_cache.get(PAD, "BB08J2", 2)


async def test_chunks_from_different_revisions_are_not_cached(fake):
    fake.chunk_rows = 400
    info = await fake.doc_info(PAD)
    _, meta = await fake.whole_tab(info, "BB08J2")
    assert len(fake.fetches) == 3 and meta["rev"] is None
    assert not fake.sheet_cache._entries


async def test_list_sheets_reports_used_range_and_grid(fake):
    out = await server.list_sheets(PAD)
    # values end at column G; column M holds only pictures
    assert "工作表1 id=BB08J2 type=grid used=A1:M90 (90 rows x 13 cols) grid=1000x26" in out
    assert len(fake.whole_fetches()) == 1


async def test_list_sheets_again_needs_no_fetch_even_when_the_tab_cache_is_full(fake):
    fake.sheet_cache.max_cells = 0  # nothing fits
    first = await server.list_sheets(PAD)
    n = len(fake.fetches)
    assert await server.list_sheets(PAD) == first
    assert len(fake.fetches) == n


async def test_list_sheets_scans_smallest_tabs_within_budget(fake, monkeypatch):
    fake.tabs = [Tab("BB08J2", "工作表1", "grid", False), Tab("T2", "空表", "grid", False)]
    monkeypatch.setattr(server, "_LIST_SCAN_CELLS", 1000 * 26)  # room for one tab
    out = await server.list_sheets(PAD)
    assert "id=BB08J2 type=grid used=A1:M90" in out
    assert "id=T2 type=grid grid=1000x26 (not scanned for its used range: too big; read it with a range)" in out
    assert len(fake.whole_fetches()) == 1


async def test_list_sheets_reports_a_failing_tab_and_lists_the_rest(fake):
    fake.tabs = [Tab("BB08J2", "工作表1", "grid", False), Tab("T2", "空表", "grid", False)]
    fake.fail.add("T2")
    out = await server.list_sheets(PAD)
    assert "id=BB08J2 type=grid used=A1:M90" in out
    assert "id=T2 type=grid (size unavailable: boom)" in out


async def test_range_read_is_served_from_the_cached_tab(fake):
    await server.read_sheet(PAD)  # whole tab: fetched and cached
    out = await server.read_sheet(PAD, range="A1:B2")
    assert fake.fetches == [("BB08J2", 0, -1)]
    assert out[0].splitlines()[1:] == ["row,A,B", "1,key,简体", "2,Sample text 1,示例文本 2"]
    full = await server.read_sheet(PAD, range="A1:B2", content="full")
    pics = await server.get_images(PAD, range="M1:M90", list_only=True)
    assert "<table" in full[0] and "picture(s)" in pics[0] and len(fake.fetches) == 1


async def test_range_read_without_cache_fetches_only_the_range(fake):
    await server.read_sheet(PAD, range="3:4")
    await server.read_sheet(PAD, range="3:4")
    assert fake.fetches == [("BB08J2", 2, 3)] * 2


async def test_url_tab_prefetch_is_used_and_cached(fake):
    await server.read_sheet(URL)  # tab fetched alongside the tab list
    fake._doc_info_cache.clear()  # tab list expired: prefetch again, cancelled once the cache answers
    await server.read_sheet(URL)
    await server.read_sheet(URL, range="A1:B2")  # tab list fresh now: no prefetch at all
    assert fake.fetches == [("BB08J2", 0, -1)]


async def test_unknown_revision_disables_caching():
    client = FakeClient(rev=None)
    server._client = client
    try:
        await server.read_sheet(PAD)
        await server.read_sheet(PAD)
        assert len(client.fetches) == 2
    finally:
        server._client = None
        await client.aclose()


async def test_old_prefetch_is_filed_under_its_own_revision(fake):
    fake.rev, fake.info_rev = 1, 2  # prefetch answered before an edit, tab list after it
    await server.read_sheet(URL)
    assert fake.sheet_cache.get(PAD, "BB08J2", 2) is None
    fake.rev = 2
    await server.read_sheet(URL)  # tab list fresh: no prefetch; rev 1 copy must not answer
    assert fake.whole_fetches() == [("BB08J2", 0, -1)] * 2


async def test_list_sheets_uses_a_cached_tab_beyond_the_budget(fake, monkeypatch):
    await server.read_sheet(PAD)
    monkeypatch.setattr(server, "_LIST_SCAN_CELLS", 0)
    out = await server.list_sheets(PAD)
    assert "used=A1:M90" in out and len(fake.whole_fetches()) == 1
