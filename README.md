# wecom-excel-mcp

让 AI 助手（Claude Code、Cursor 等）直接读取**企业微信在线表格**（`doc.weixin.qq.com/sheet/...`）的 MCP 服务，只读。

把表格链接发给 AI，它就能读出每一列、每个 tab，还能读到单元格格式和单元格里的图片，也能按文字搜索。

## 它是怎么工作的

```
你的电脑                                      你的服务器（本仓库）                    企业微信
┌──────────────────────────────┐   HTTPS    ┌──────────────────────────────┐        ┌───────────────────┐
│ AI 助手                       │ ─────────▶ │ wecom-excel-mcp serve        │ ─────▶ │ doc.weixin.qq.com │
│  └ wecom-mcp.py（本地 MCP）    │ 带上 cookie │  解码表格，只在请求期间用 cookie │        └───────────────────┘
│     扫码 / 桌面端登录，cookie 存本机 │            │  不保存、不记录任何人的 cookie   │
└──────────────────────────────┘            └──────────────────────────────┘
```

- **服务器端**：一个 Python + Rust 写的 MCP 服务，负责请求企业微信、解码表格数据（18000 行、10 万个单元格约 70 ms）。它用每个调用者自己带来的 cookie，所以每个人只能读到自己账号有权限的表。cookie 只在一次请求期间留在内存里，不会写盘也不会写进日志。
- **用户端**：一个单文件脚本 `wecom-mcp.py`，只依赖 Python 标准库。它负责登录：可以用本机已登录的企业微信桌面端一键登录，也可以扫码登录（会弹出小窗口；在没有屏幕的服务器上，二维码直接显示在终端里）。cookie 存在用户自己电脑上。脚本会自动更新。
- **说明页**：服务器首页是一个安装说明页，同时给人看、也给 AI 看。用户把链接发给自己的 AI 助手，AI 就会照着装好。
- **后台**：`/admin` 有使用统计，包括调用量、用户、文档、报错，可以给用户写备注。需要账号密码登录。

## 部署一份自己的

GitHub Actions 会为 Linux、macOS、Windows 自动打包好单文件程序，在 [Releases](https://github.com/Doone-skser/wecom-excel-mcp/releases) 里，不需要装 Python 或 Rust。

**Linux 服务器，一条命令：**

```bash
curl -fsSL https://raw.githubusercontent.com/Doone-skser/wecom-excel-mcp/main/deploy/install.sh | sudo bash
```

装完服务就运行在 `http://服务器IP:8765/`，并注册成 systemd 服务，开机自启。脚本最后会打印后台密码。有域名、想用 HTTPS 的话，加上 `DOMAIN=你的域名`，脚本会自动配置 nginx 和证书。

**macOS / Windows / 任意电脑：** 下载对应的文件，运行 `wecom-excel-mcp serve`。

**交给 AI：** 把这段话发给服务器上的 AI 助手（例如 Claude Code）：

> 帮我部署 https://github.com/Doone-skser/wecom-excel-mcp ，按仓库里 DEPLOY.md 的步骤来，用端口模式就行。部署完把说明页地址、后台地址和密码告诉我。

部署完成后：

- `http://服务器IP:8765/`：安装说明页。发给同事，或者发给同事的 AI 助手。
- `http://服务器IP:8765/admin`：使用统计后台。

更新时重新运行同样的命令即可。详细说明、宝塔面板的用法和常见问题见 [DEPLOY.md](DEPLOY.md)。

## 用户怎么用

用户打开你的说明页，点「一键复制」，把复制的内容发给自己的 AI 助手，AI 就会自动安装。手动安装的话，Claude Code 里运行：

```bash
mkdir -p ~/.config/wecom-excel && curl -fsSL https://你的域名/wecom-mcp.py -o ~/.config/wecom-excel/wecom-mcp.py
claude mcp add -s user wecom-excel -- python3 ~/.config/wecom-excel/wecom-mcp.py
```

装好后重启客户端，让 AI 调用一次 `login` 登录企业微信，之后直接发表格链接给它就行。

## 工具

| 工具 | 作用 |
|---|---|
| `login(method?)` | 登录企业微信：桌面端一键登录或扫码（只在用户端脚本里有） |
| `list_sheets(doc)` | 返回标题、所有 tab，以及每个 tab 实际有数据的范围 |
| `read_sheet(doc, sheet?, range?, content?, format?, ...)` | 读取一个范围（`A1:F100`、`A:C`、`2:50`）。`content` 可选 `text`（只取值）、`text_images`（值加图片）、`full`（HTML，带合并单元格、字体、颜色、边框） |
| `search_sheet(doc, query, sheet?)` | 搜索文字，返回所在单元格 |
| `get_images(doc, sheet?, range?)` | 只取单元格里的图片 |

`doc` 可以是表格链接，也可以是 `e3_...` 这样的 padId。链接里的 `tab=` 用来选定 tab。

## 隐私与安全

- 服务器使用 `--caller-cookies-only` 模式运行：只用调用者自己带来的 cookie，不会用服务器上的任何 cookie 兜底。
- 缓存的键是 cookie 的 SHA-256，不是 cookie 本身。企业微信响应里返回的 Set-Cookie 会被丢弃。
- 使用统计只记录时间、工具、耗时、结果、账号名、随机安装 ID、IP 和文档 ID/标题。不记录 cookie，也不记录表格内容。数据保留 180 天。
- 后台密码只存 PBKDF2 哈希。同一个 IP 连续输错 5 次密码，锁定 15 分钟。
- 直接开端口时，服务只认 TCP 连接的真实来源 IP，客户端自己填的 `X-Real-IP` 不算数。域名模式下由 nginx 转发真实 IP，并且 `/mcp` 每个 IP 每秒最多 10 个请求（允许瞬时 40 个）。
- 端口模式是 http，cookie 会明文经过网络，适合内网使用；放到公网上建议用域名模式加 HTTPS。

## 开发

```bash
uv sync && uv run pytest     # 会把 Rust 扩展编译进 src/
```

代码结构、表格数据格式和解码器说明见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。

## License

MIT
