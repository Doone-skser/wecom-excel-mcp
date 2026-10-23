"""Usage statistics: what a tool call records, and what the admin page gets back."""

import urllib.parse

import pytest

from wecom_excel_mcp import server
from wecom_excel_mcp.usage import UsageLog

from test_caller_cookies import PAD, FakeCtx, opendoc_client

NOW = 1791950400.0  # 2026-10-14 12:00 China time
DAY = 86400


@pytest.fixture
def log(tmp_path):
    return UsageLog(tmp_path / "usage.db")


def test_stats(log):
    log.record(ts=NOW - 60, tool="read_sheet", ok=True, ms=100, user="示例公司 / 甲", client="wecom-mcp.py/1.4.0", pad_id="e3_a", title="表A", client_id="uuid-a", ip="9.9.9.9")
    log.record(ts=NOW - 120, tool="read_sheet", ok=False, ms=300, error="retcode=30004", user="示例公司 / 甲", client="wecom-mcp.py/1.3.0", pad_id="e3_a", client_id="uuid-a", ip="8.8.8.8")
    log.record(ts=NOW - 2 * DAY, tool="list_sheets", ok=True, ms=50, ip="1.2.3.4", pad_id="e3_b", title="表B")
    log.record(ts=NOW - 10 * DAY, tool="list_sheets", ok=True, ms=50, user="示例公司 / 乙")  # previous 7-day period

    s = log.stats(7, now=NOW)
    assert s["today"] == "2026-10-14"
    assert [d["day"] for d in s["daily"]] == [f"2026-10-{d:02d}" for d in range(8, 15)]
    assert s["daily"][-1] == {"day": "2026-10-14", "calls": 2, "errors": 1, "users": 1}
    assert s["daily"][-3]["calls"] == 1 and s["daily"][0]["calls"] == 0
    t = s["totals"]
    assert (t["calls"], t["errors"], t["users"], t["docs"], t["today"]) == (3, 1, 2, 2, 2)
    assert s["previous"]["calls"] == 1
    users = {u["key"]: u for u in s["users"]}
    a = users["uuid-a"]  # counted by install id; name, IP and client are the latest ones
    assert (a["calls"], a["name"], a["ip"], a["client"]) == (2, "示例公司 / 甲", "9.9.9.9", "wecom-mcp.py/1.4.0")
    assert "ip:1.2.3.4" in users  # a client before 1.2.0 reports no account and no id
    assert s["docs"][0] == {"pad_id": "e3_a", "title": "表A", "calls": 2, "users": 1, "last_ts": NOW - 60}
    assert s["errors"] == [{"error": "retcode=30004", "calls": 1, "last_ts": NOW - 120}]
    assert [r["ts"] for r in s["recent"]] == sorted((r["ts"] for r in s["recent"]), reverse=True)
    assert {r["tool"]: r["calls"] for r in s["tools"]} == {"read_sheet": 2, "list_sheets": 1}


def test_labels(log):
    log.record(ts=NOW, tool="read_sheet", ok=True, ms=1, client_id="uuid-a", user="示例公司 / 甲")
    log.record(ts=NOW, tool="read_sheet", ok=True, ms=1, user="示例公司 / 乙")  # 1.2.0/1.3.0: by account name
    log.set_label("uuid-a", "  产品  小王 ")
    log.set_label("user:示例公司 / 乙", "运营")
    s = log.stats(1, now=NOW)
    assert {u["key"]: u["label"] for u in s["users"]} == {"uuid-a": "产品 小王", "user:示例公司 / 乙": "运营"}
    assert {r["label"] for r in s["recent"]} == {"产品 小王", "运营"}
    log.set_label("uuid-a", "")
    assert {u["key"]: u["label"] for u in log.stats(1, now=NOW)["users"]}["uuid-a"] is None


def test_log_from_before_client_ids(tmp_path):
    import sqlite3

    path = tmp_path / "usage.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE calls (ts REAL NOT NULL, tool TEXT NOT NULL, ok INTEGER NOT NULL, error TEXT,"
               " ms INTEGER NOT NULL, user TEXT, ip TEXT, client TEXT, pad_id TEXT, title TEXT)")
    db.execute("INSERT INTO calls (ts, tool, ok, ms, user) VALUES (?, 'list_sheets', 1, 5, '甲')", (NOW,))
    db.commit()
    db.close()
    log = UsageLog(path)
    log.record(ts=NOW, tool="read_sheet", ok=True, ms=1, client_id="uuid-a")
    assert {u["key"] for u in log.stats(1, now=NOW)["users"]} == {"user:甲", "uuid-a"}


def test_old_rows_are_dropped(tmp_path):
    path = tmp_path / "usage.db"
    UsageLog(path).record(ts=1.0, tool="list_sheets", ok=True, ms=1)
    assert UsageLog(path).stats(180)["recent"] == []


@pytest.fixture
def recording(log, monkeypatch):
    seen: list = []
    monkeypatch.setattr(server, "_client", opendoc_client(seen, retcode_for={"u=old": 30004}))
    monkeypatch.setattr(server, "_usage", log)
    monkeypatch.setattr(server, "_use_env_cookies", False)
    return log


def headers(cookie, user=None, cid=None):
    h = {"x-wecom-cookie": cookie, "user-agent": "wecom-mcp.py/1.4.0", "x-real-ip": "10.0.0.1"}
    if cid:
        h["x-wecom-client-id"] = cid
    if user:
        h["x-wecom-user"] = urllib.parse.quote(user)
    return h


async def test_a_call_is_recorded_without_the_cookie(recording):
    cid = "0b0d2c1e-6f1a-4c55-9d7e-3f2a9b8c7d6e"
    await server.list_sheets(f"https://doc.weixin.qq.com/sheet/{PAD}?tab=x", ctx=FakeCtx(headers("u=alice", "示例公司 / 甲", cid)))
    with pytest.raises(Exception):
        await server.list_sheets(PAD, ctx=FakeCtx(headers("u=old", cid="not an id; drop table")))
    rows = recording.stats(1)["recent"]
    assert [(r["tool"], r["ok"], r["key"], r["name"], r["ip"], r["pad_id"], r["client"]) for r in reversed(rows)] == [
        ("list_sheets", 1, cid, "示例公司 / 甲", "10.0.0.1", PAD, "wecom-mcp.py/1.4.0"),
        ("list_sheets", 0, "ip:10.0.0.1", None, "10.0.0.1", PAD, "wecom-mcp.py/1.4.0"),  # a malformed id is dropped
    ]
    assert "not logged in" in rows[0]["error"]
    dump = "\n".join(recording._db.iterdump())
    assert "u=alice" not in dump and "u=old" not in dump


async def test_ip_header_is_believed_only_from_a_local_proxy(recording):
    spoofed = headers("u=alice")  # says x-real-ip 10.0.0.1
    await server.list_sheets(PAD, ctx=FakeCtx(spoofed, peer="203.0.113.5"))  # straight to the port
    await server.list_sheets(PAD, ctx=FakeCtx(spoofed))  # through nginx on 127.0.0.1
    assert [r["ip"] for r in reversed(recording.stats(1)["recent"])] == ["203.0.113.5", "10.0.0.1"]


async def test_no_usage_log_records_nothing(monkeypatch):
    monkeypatch.setattr(server, "_usage", None)
    monkeypatch.setattr(server, "_client", opendoc_client([]))
    await server.list_sheets(PAD, ctx=FakeCtx(headers("u=alice")))
