"""Usage statistics for the shared HTTP server: one SQLite row per tool call.

A row holds when, which tool, whether it worked, how long it took, who called (the random install id and the
account name the local client reports, the IP, the client's User-Agent) and which doc (padId and title). Never
the cookie, and never sheet content. Rows older than RETENTION_DAYS are dropped at start.

A caller is counted by its install id, else (clients before 1.4.0) its account name, else its IP. The admin can
label callers; labels are kept apart from the rows.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

RETENTION_DAYS = 180
TZ = "+8 hours"  # days are counted in China time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    ts REAL NOT NULL,
    tool TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT,
    ms INTEGER NOT NULL,
    user TEXT,
    ip TEXT,
    client TEXT,
    pad_id TEXT,
    title TEXT,
    client_id TEXT
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts);
CREATE TABLE IF NOT EXISTS labels (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    updated REAL NOT NULL
);
"""
# Who a row counts as: the install id, else the account name (before 1.4.0), else the IP (before 1.2.0).
_KEY = ("COALESCE(NULLIF(client_id, ''), CASE WHEN COALESCE(user, '') != '' THEN 'user:' || user"
        " ELSE 'ip:' || COALESCE(ip, '?') END)")
MAX_LABEL = 50
_DAY = f"date(ts, 'unixepoch', '{TZ}')"


class UsageLog:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            columns = {r["name"] for r in self._db.execute("PRAGMA table_info(calls)")}
            if "client_id" not in columns:  # a log made before 1.4.0
                self._db.execute("ALTER TABLE calls ADD COLUMN client_id TEXT")
            self._db.execute("DELETE FROM calls WHERE ts < ?", (time.time() - RETENTION_DAYS * 86400,))

    def record(self, *, tool: str, ok: bool, ms: int, error: str | None = None, user: str | None = None,
               ip: str | None = None, client: str | None = None, pad_id: str | None = None,
               title: str | None = None, client_id: str | None = None, ts: float | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO calls (ts, tool, ok, error, ms, user, ip, client, pad_id, title, client_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts if ts is not None else time.time(), tool, int(ok), (error or None) and error[:300], ms,
                 user, ip, client, pad_id, title, client_id),
            )

    def set_label(self, key: str, label: str) -> None:
        """Name a caller (by the `key` the stats report); an empty label removes it."""
        label = " ".join(label.split())[:MAX_LABEL]
        with self._lock:
            if label:
                self._db.execute("INSERT OR REPLACE INTO labels (key, label, updated) VALUES (?, ?, ?)",
                                 (key, label, time.time()))
            else:
                self._db.execute("DELETE FROM labels WHERE key = ?", (key,))

    def _all(self, sql: str, *args) -> list[dict]:
        return [dict(r) for r in self._db.execute(sql, args)]

    def stats(self, days: int = 30, now: float | None = None) -> dict:
        """Everything the admin page shows for the last `days` days (counted in China time, today included)."""
        days = max(1, min(days, RETENTION_DAYS))
        now = time.time() if now is None else now
        with self._lock:
            today = self._db.execute(f"SELECT date(?, 'unixepoch', '{TZ}')", (now,)).fetchone()[0]
            first = self._db.execute(f"SELECT date(?, '-{days - 1} days')", (today,)).fetchone()[0]
            prev = self._db.execute(f"SELECT date(?, '-{days} days')", (first,)).fetchone()[0]
            where = f"{_DAY} >= ?"

            def totals(lo: str, hi: str | None) -> dict:
                cond, args = (f"{_DAY} >= ? AND {_DAY} < ?", (lo, hi)) if hi else (where, (lo,))
                r = self._db.execute(
                    f"SELECT COUNT(*) AS calls, COALESCE(SUM(1 - ok), 0) AS errors, COUNT(DISTINCT {_KEY}) AS users,"
                    f" COUNT(DISTINCT pad_id) AS docs, CAST(COALESCE(AVG(ms), 0) AS INTEGER) AS avg_ms"
                    f" FROM calls WHERE {cond}", args).fetchone()
                return dict(r)

            cur = totals(first, None)
            mss = [r[0] for r in self._db.execute(f"SELECT ms FROM calls WHERE {where} ORDER BY ms", (first,))]
            cur["p95_ms"] = mss[min(len(mss) - 1, int(len(mss) * 0.95))] if mss else 0
            cur["today"] = self._db.execute(f"SELECT COUNT(*) FROM calls WHERE {_DAY} = ?", (today,)).fetchone()[0]

            by_day = {r["day"]: r for r in self._all(
                f"SELECT {_DAY} AS day, COUNT(*) AS calls, SUM(1 - ok) AS errors, COUNT(DISTINCT {_KEY}) AS users"
                f" FROM calls WHERE {where} GROUP BY day", first)}
            daily = []
            for i in range(days):
                day = self._db.execute(f"SELECT date(?, '+{i} days')", (first,)).fetchone()[0]
                daily.append(by_day.get(day) or {"day": day, "calls": 0, "errors": 0, "users": 0})

            labels = {r["key"]: r["label"] for r in self._all("SELECT key, label FROM labels")}
            latest = {r["key"]: r for r in self._all(  # each caller's latest name, IP and client: last row wins
                f"SELECT {_KEY} AS key, user AS name, ip, client, client_id FROM calls WHERE {where} ORDER BY ts",
                first)}
            result = {
                "days": days,
                "today": today,
                "totals": cur,
                "previous": totals(prev, first),
                "daily": daily,
                "hours": self._all(
                    f"SELECT CAST(strftime('%H', ts, 'unixepoch', '{TZ}') AS INTEGER) AS hour, COUNT(*) AS calls"
                    f" FROM calls WHERE {where} GROUP BY hour ORDER BY hour", first),
                "tools": self._all(
                    f"SELECT tool, COUNT(*) AS calls, SUM(1 - ok) AS errors, CAST(AVG(ms) AS INTEGER) AS avg_ms"
                    f" FROM calls WHERE {where} GROUP BY tool ORDER BY calls DESC", first),
                "users": self._all(
                    f"SELECT {_KEY} AS key, COUNT(*) AS calls, SUM(1 - ok) AS errors, COUNT(DISTINCT pad_id) AS docs,"
                    f" MAX(ts) AS last_ts FROM calls WHERE {where} GROUP BY key ORDER BY calls DESC LIMIT 100", first),
                "docs": self._all(
                    f"SELECT pad_id, MAX(title) AS title, COUNT(*) AS calls, COUNT(DISTINCT {_KEY}) AS users,"
                    f" MAX(ts) AS last_ts FROM calls WHERE {where} AND pad_id IS NOT NULL"
                    f" GROUP BY pad_id ORDER BY calls DESC LIMIT 20", first),
                "errors": self._all(
                    f"SELECT error, COUNT(*) AS calls, MAX(ts) AS last_ts FROM calls WHERE {where} AND ok = 0"
                    f" GROUP BY error ORDER BY calls DESC LIMIT 10", first),
                "recent": self._all(
                    f"SELECT ts, tool, ok, error, ms, {_KEY} AS key, user AS name, ip, client, client_id, pad_id, title"
                    f" FROM calls ORDER BY ts DESC LIMIT 50"),
            }
            for u in result["users"]:
                u.update({k: latest[u["key"]][k] for k in ("name", "ip", "client", "client_id")})
            for row in result["users"] + result["recent"]:
                row["label"] = labels.get(row["key"])
            return result
