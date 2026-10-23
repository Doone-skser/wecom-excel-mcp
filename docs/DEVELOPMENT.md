# Development notes

## Layout

| path | what |
|---|---|
| `src/wecom_excel_mcp/server.py` | MCP tools (`list_sheets`, `read_sheet`, `search_sheet`, `get_images`), CLI flags, usage recording |
| `src/wecom_excel_mcp/client.py` | WeCom HTTP client, cookie providers, tab cache |
| `src/wecom_excel_mcp/decoder.py` | pure-Python decoder of the `related_sheet` payload (format below) |
| `rust/src/lib.rs` | the same decoder in Rust (PyO3, built as `wecom_excel_mcp._native`) |
| `src/wecom_excel_mcp/formatting.py`, `table.py`, `images.py` | output formats, A1 ranges, picture download and tiling |
| `src/wecom_excel_mcp/usage.py`, `admin.py`, `admin*.html` | usage log (SQLite) and the `/admin` page |
| `src/wecom_excel_mcp/site/wecom-mcp.py` | the local client users install: login, cookie storage, forwarding, self-update (stdlib only) |
| `src/wecom_excel_mcp/site/index.html`, `llms.txt` | the setup page for people and AI assistants |
| `src/wecom_excel_mcp/web.py` | serves those three files with the server's own address; which proxy headers to believe |
| `deploy/install.sh` | Linux install (prebuilt executable, systemd, optional nginx + certificate), see [DEPLOY.md](../DEPLOY.md) |
| `packaging/` | PyInstaller build of the standalone executable and its smoke test |
| `.github/workflows/release.yml` | on a tag `v*`: executables for Linux / macOS / Windows, wheels, sdist, GitHub release |
| `scripts/anonymize_fixture.py` | turns a captured payload into a test fixture with made-up text |

The files in `site/` say `https://wecom.example.com`; `web.py` replaces it with the server's address (`--public-url`,
else the request's scheme and Host) on every response.

## Build and test

```bash
uv sync && uv run pytest            # builds the extension into src/ (editable)
uv sync --reinstall-package wecom-excel-mcp   # rebuild after changing rust/
cd rust && PYO3_PYTHON=$(cd .. && uv run python -c 'import sys; print(sys.executable)') cargo test --release
```

The Rust and Python decoders return identical results: the decoder tests run against both. If the extension is
not built the Python one is used; `WECOM_EXCEL_PURE_PYTHON=1` forces it. Rust decodes an 18,000-row,
100,000-cell tab in ~70 ms including building the Python objects (~0.9 s in pure Python).

Cross-compile a Linux wheel (abi3, any CPython ≥ 3.11) from another machine:

```bash
uvx --from 'maturin[zig]' maturin build --release --target x86_64-unknown-linux-gnu --zig \
  --compatibility manylinux2014 -o dist
```

New fixtures must not contain real sheet content: capture the base64 payload, then
`python scripts/anonymize_fixture.py captured.b64 tests/fixtures/<name>.b64` and look at the result.

## Standalone executable

```bash
uv pip install pyinstaller && packaging/build.sh && python packaging/smoke_test.py dist/wecom-excel-mcp
```

The Linux release build runs in a `manylinux_2_28` container (`packaging/build-linux.sh`) so it runs on glibc 2.28+.
`wecom-excel-mcp --version` says whether the Rust decoder is inside.

## Running the server other ways

```bash
# stdio, one fixed cookie (a browser session on doc.weixin.qq.com)
claude mcp add wecom-excel -e WECOM_COOKIE='k=v; k2=v2' -- uv --directory /path/to/repo run wecom-excel-mcp
# HTTP (no auth of its own: keep it on localhost or behind a proxy)
wecom-excel-mcp --transport streamable-http --host 127.0.0.1 --port 8765
```

Cookie sources for a server that is not `--caller-cookies-only`, in order: the caller's `X-Wecom-Cookie` header
(HTTP), `WECOM_COOKIE` (`k=v; k2=v2`), or `WECOM_COOKIE_API`, a URL returning `{"code": 0, "data": {<cookie
dict>}}` (cached `WECOM_COOKIE_TTL` seconds, default 300, refetched on `retcode 30004`).

Other settings: `WECOM_SHEET_CACHE_CELLS` (tab cache size, default 500000 cells, 0 disables), `WECOM_USAGE_DB`,
`WECOM_ADMIN_PASSWORD_FILE`, `MCP_ALLOWED_HOSTS`. Client: `WECOM_MCP_URL`, `WECOM_MCP_UPDATE_URL`,
`WECOM_MCP_NO_UPDATE=1`.

## Shared server, each caller's own cookie

With `--caller-cookies-only` (`WECOM_CALLER_COOKIES_ONLY=1`) the HTTP server reads docs only with the cookie each
caller sends in `X-Wecom-Cookie`, so everyone sees exactly the docs their own WeCom account can open. The cookie
lives only for one request: it is not stored, cached or logged (caches are keyed by a SHA-256 of it, and cookies
WeCom sets in responses are dropped). HTTP runs stateless. `--allowed-hosts` lists the Host names accepted
behind the proxy.

## The local client (`site/wecom-mcp.py`)

One file, standard library only. It adds a `login` tool and forwards the sheet tools with the cookie from
`~/.config/wecom-excel/cookie` (mode 600), plus `X-Wecom-User` (account name) and `X-Wecom-Client-Id` (a random
UUID kept in `~/.config/wecom-excel/client-id`) for the usage statistics.

- `login` with no arguments checks for a logged-in WeCom desktop app (its quick-login service on
  `localhost.work.weixin.qq.com:50010`) and returns the account so the assistant can ask whether to use it
  (`method="desktop"`) or scan a QR code (`method="qr"`: a small Chrome / Edge app window, or a browser tab, served
  from 127.0.0.1, which shows progress and closes itself).
- Without a screen (SSH, a VPS without `DISPLAY`) the QR code comes back as text at once and the scan is awaited in
  the background; a second `login` reports the result. The QR encoder is built in (byte mode, level L).
  `python3 wecom-mcp.py login` prints the QR code in a terminal.
- When a cookie expires and the last login went through the desktop app, it logs in again through the app (only as
  the same account) and retries the call.
- Self-update (only from an HTTPS server, or one on this machine: the new file is run as is): on the first
  request each hour (and at start) it compares `VERSION` with the published copy; if
  that is newer it replaces its file and, once idle, `exec`s it in the same process (on Windows the new version
  starts with the next client start). **Bump `VERSION` with every change to this file.**
- stdio is forced to UTF-8 (Windows defaults to the ANSI code page otherwise).

## Usage statistics

With `--usage-db <file>` each tool call is recorded in SQLite: time, tool, outcome, duration, install id, account
name, IP, User-Agent, padId and title, never the cookie or sheet content, kept 180 days. Callers are counted by
install id (else account name, else IP) and can be labelled on the page. `/admin` (daisyUI) is behind a login
form; credentials are one line `username:<PBKDF2 hash>` in `admin-password` beside the db (or
`--admin-password-file`), made with `wecom-excel-mcp --hash-password`. Replacing the file takes effect at once and
ends every session. Sessions are signed cookies (7 days); five failed logins from an IP lock it for 15 minutes.

## Tools in detail

`read_sheet` `content` levels:

| content | returns |
|---|---|
| `text` (default) | values as `csv` / `markdown` / `json` / `records`; picture cells read `[image]` |
| `text_images` | the same, plus the pictures in the returned rows labelled with their cell (`max_images`, default 5) |
| `full` | HTML with merged cells, bold / font colour / size / name, per-run formatting inside a cell, fill, alignment, borders, column widths, row heights, plus the pictures |

Pictures are the original uploads (`?imageView2/0`). Models downscale images over ~1568 px or ~1.15 MP, so large
pictures are split into overlapping tiles of at most 1092 px (`tile=false` for whole ones).
`get_images(list_only=true)` lists the original-file URLs, which need no login. Pictures are only downloaded from
WeCom's CDN (`https://*.qpic.cn`).

## Caching

Whole tabs, once read (`list_sheets`, `content="full"`, `search_sheet`, or a read without a range), stay decoded
in memory per process; later reads of any range need no request. An entry belongs to the doc revision (`rev`,
raised by every edit) and is used only while the tab list, itself cached 5 s, reports that revision, so an edit
shows up within 5 s.

## Payload format

See the docstring at the top of `src/wecom_excel_mcp/decoder.py`. The payload is base64(zlib(protobuf)), with
field numbers reverse-engineered. A tab's payload also carries the cell blocks of the tabs its formulas refer to;
only the requested tab's blocks are read, and cross-tab references are rendered with the other tab's name. The
style mapping was checked against WeCom's own xlsx export. Italic, underline and strikethrough are not decoded
yet (no sampled sheet had them on). Undecodable cells are reported in a note under the tool output.
