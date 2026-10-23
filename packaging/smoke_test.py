"""Start a built executable with `serve` and check every page answers. Standard library only.

    python packaging/smoke_test.py dist/wecom-excel-mcp-linux-x86_64
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 18765
BASE = f"http://127.0.0.1:{PORT}"


def get(path: str, data: bytes | None = None, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path, data, headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> None:
    exe = os.path.abspath(sys.argv[1])
    data = tempfile.mkdtemp()
    log = open(os.path.join(data, "server.log"), "w+b")
    # output to a file, not a pipe: a onefile executable runs as two processes, and the inner one would keep a
    # pipe open after the outer one is gone
    proc = subprocess.Popen([exe, "serve", "--port", str(PORT), "--data-dir", data], stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if get("/admin/login")[0] == 200:
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise SystemExit("server did not start")
        assert "llms.txt" in get("/")[1]
        assert f"{BASE}/mcp" in get("/llms.txt")[1]
        assert f'"{BASE}/mcp"' in get("/wecom-mcp.py")[1]
        assert os.path.exists(os.path.join(data, "admin-password"))
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "X-Wecom-Cookie": "x=1"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}}}
        status, body = get("/mcp", json.dumps(init).encode(), headers)
        assert status == 200 and '"wecom-excel"' in body, body
        status, body = get("/mcp", json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode(), headers)
        assert all(t in body for t in ("list_sheets", "read_sheet", "search_sheet", "get_images")), body
        print("smoke test passed")
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.seek(0)
        print(log.read().decode(errors="replace")[-3000:])
    version = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout
    print(version)
    assert "decoder: rust" in version, "the Rust decoder is not inside the executable"


if __name__ == "__main__":
    main()
