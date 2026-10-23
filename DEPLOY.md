# 部署指南

这份文档同时写给人和 AI 助手看。照着做，就能在一台服务器上搭出完整的服务：说明页、MCP 接口和使用统计后台。不需要编译，GitHub Actions 已经为 Linux、macOS、Windows 打好了单文件程序，在 [Releases](https://github.com/Doone-skser/wecom-excel-mcp/releases) 里。

> **给 AI 助手：** 按顺序执行。默认用「端口模式」，不需要域名和证书。只有用户给了域名并且想要 HTTPS 时，才用「域名模式」。不要把 cookie 写进任何文件或日志，也不要让用户把 cookie 发到对话里。每一步做完，都用「验证」一节的命令检查，再把结果告诉用户：说明页地址、后台地址、账号、密码（只显示一次）。

## 方式一：Linux 服务器，一条命令（推荐）

需要 x86_64 或 arm64 的 Linux，带 systemd，有 root 权限。Ubuntu 20.04+、Debian 10+、CentOS/Rocky/Alma 8+、阿里云 Linux 3 都可以。

```bash
curl -fsSL https://raw.githubusercontent.com/Doone-skser/wecom-excel-mcp/main/deploy/install.sh | sudo bash
```

装完后服务就运行在 `http://服务器IP:8765/`，脚本最后会打印说明页、后台地址和后台密码。

- **云服务器**：记得在安全组里放行 TCP 8765。脚本会自动在 ufw/firewalld 里放行，但管不到云厂商的安全组。
- **换端口**：`curl ... | sudo PORT=9000 bash`
- **更新**：再运行一次同样的命令。数据和后台密码都会保留。
- **国内服务器下载 GitHub 慢**：加一个下载代理前缀。

  ```bash
  curl -fsSL https://ghfast.top/https://raw.githubusercontent.com/Doone-skser/wecom-excel-mcp/main/deploy/install.sh \
    | sudo GITHUB_PROXY=https://ghfast.top/ bash
  ```

  也可以先在别处下载 `wecom-excel-mcp-linux-x86_64`，传到服务器上，用 `sudo BINARY=./wecom-excel-mcp-linux-x86_64 bash install.sh` 安装。

> 端口模式是 http，cookie 会**明文**经过网络。在公司内网或可信网络里可以直接用；要放到公网上长期用，建议用下面的域名模式加 HTTPS。

### 域名模式（HTTPS）

域名已经解析到服务器，80 和 443 端口已开放，系统是 Debian/Ubuntu 时：

```bash
curl -fsSL https://raw.githubusercontent.com/Doone-skser/wecom-excel-mcp/main/deploy/install.sh \
  | sudo DOMAIN=wecom.example.com EMAIL=you@example.com bash
```

脚本会额外安装 nginx 和 certbot，申请 Let's Encrypt 证书（之后自动续期），并给 `/mcp` 加上每个 IP 每秒 10 次的限流。这时服务只监听 `127.0.0.1:8765`，对外只开放 `https://wecom.example.com/`。

**已经有宝塔面板或别的 nginx**：加上 `SKIP_NGINX=1`，脚本就不碰 nginx 和证书。

```bash
curl -fsSL .../deploy/install.sh | sudo DOMAIN=wecom.example.com SKIP_NGINX=1 bash
```

然后在宝塔里给这个域名建站、开 SSL，再把整站反向代理到 `http://127.0.0.1:8765`。必须带上 `Host`、`X-Real-IP`、`X-Forwarded-Proto` 这三个头（宝塔默认的反向代理配置已经带了）。`/mcp` 要关闭代理缓冲，超时设为 300 秒。

### 脚本做了什么

1. 从 GitHub Release 下载 `wecom-excel-mcp-linux-<架构>`，用 `SHA256SUMS` 校验后放到 `/opt/wecom-excel/wecom-excel-mcp`；
2. 创建系统用户 `wecom-excel`，数据目录 `/var/lib/wecom-excel`（权限 700）；
3. 设置后台账号（默认 `admin`）。没有给 `ADMIN_PASSWORD` 时随机生成一个，只打印一次；文件里只存 PBKDF2 哈希；
4. 写入 systemd 服务 `wecom-excel-http`（`wecom-excel-mcp serve ...`），设为开机自启；
5. 端口模式下，在 ufw/firewalld 里放行端口；域名模式下，配置 nginx 和证书；
6. 检查服务确实启动了。

全部参数：`PORT`、`DOMAIN`、`EMAIL`、`SKIP_NGINX`、`ADMIN_USER`、`ADMIN_PASSWORD`、`VERSION`（例如 `v0.2.0`，默认最新版）、`GITHUB_PROXY`、`BINARY`。说明写在 [`deploy/install.sh`](deploy/install.sh) 开头。

## 方式二：任何电脑直接运行（macOS / Windows / Linux）

从 [Releases](https://github.com/Doone-skser/wecom-excel-mcp/releases/latest) 下载对应系统的文件，直接运行即可：

| 系统 | 文件 |
|---|---|
| Linux x86_64 / arm64 | `wecom-excel-mcp-linux-x86_64` / `wecom-excel-mcp-linux-arm64` |
| macOS（Apple 芯片） | `wecom-excel-mcp-macos-arm64` |
| Windows | `wecom-excel-mcp-windows-x86_64.exe` |

```bash
# macOS / Linux
curl -fLO https://github.com/Doone-skser/wecom-excel-mcp/releases/latest/download/wecom-excel-mcp-macos-arm64
chmod +x wecom-excel-mcp-macos-arm64
./wecom-excel-mcp-macos-arm64 serve            # --port 9000 换端口
```

```powershell
# Windows PowerShell
Invoke-WebRequest https://github.com/Doone-skser/wecom-excel-mcp/releases/latest/download/wecom-excel-mcp-windows-x86_64.exe -OutFile wecom-excel-mcp.exe
.\wecom-excel-mcp.exe serve
```

启动后会打印说明页地址，以及第一次生成的后台账号和密码。数据存在 `~/.local/share/wecom-excel`（Windows 是 `%LOCALAPPDATA%\wecom-excel`），也可以用 `--data-dir` 指定别的目录。

- macOS 用浏览器下载的文件会被系统拦截，可以运行 `xattr -d com.apple.quarantine <文件>` 解除；用 curl 下载的不会被拦。
- Windows 第一次运行时，防火墙会弹窗询问，选「允许」。
- 改后台密码：`wecom-excel-mcp passwd`（可以加 `--user 账号`）。
- 这样运行关掉终端服务就停了。要常驻的话，用系统自带的方式托管：macOS 用 launchd，Windows 用「任务计划程序」或 NSSM。

Intel 芯片的 Mac 没有单文件程序，用下面的 wheel 包。想用 Python 装的话，Releases 里也有 wheel 包：`pip install wecom_excel_mcp-*.whl`，然后运行 `wecom-excel-mcp serve`。

## 验证

把 `BASE` 换成脚本打印的地址：

```bash
BASE=http://服务器IP:8765
systemctl is-active wecom-excel-http                           # active（方式一）
curl -s -o /dev/null -w '%{http_code}\n' $BASE/                # 200
curl -s $BASE/wecom-mcp.py | grep -m1 '^REMOTE'                # 里面应该是 $BASE/mcp
curl -s $BASE/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'X-Wecom-Cookie: x=1' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}'
# 返回里有 "wecom-excel"
```

然后在一台装了 Claude Code 的电脑上打开 `$BASE/`，按页面上的步骤装好用户端，登录后读一张自己有权限的表。能读出来，整套服务就是通的。

## 告诉用户

- 说明页 `$BASE/`：发给同事。同事点「一键复制」，把内容发给自己的 AI 助手，AI 就会自动装好。
- 后台 `$BASE/admin`：用脚本打印的账号密码登录。

## 文件位置（方式一）

| 路径 | 内容 |
|---|---|
| `/opt/wecom-excel/wecom-excel-mcp` | 程序（单文件） |
| `/etc/systemd/system/wecom-excel-http.service` | systemd 服务 |
| `/var/lib/wecom-excel/usage.db` | 使用统计（SQLite，不含 cookie） |
| `/var/lib/wecom-excel/admin-password` | 后台账号和密码哈希 |
| `/var/lib/wecom-excel/session.key` | 后台会话签名密钥 |
| `/etc/nginx/conf.d/wecom-excel.conf` | 仅域名模式 |

## `serve` 的参数

`wecom-excel-mcp serve [--host 0.0.0.0] [--port 8765] [--data-dir 目录] [--public-url https://...] [--no-stats]`

- `--public-url`：用户访问的地址。不填时从每个请求的 Host 自动得出；放在反向代理后面、并且代理改写了 Host 时才需要填。
- `--no-stats`：不记录使用统计，也不提供 `/admin`。
- 说明页、`llms.txt` 和用户端脚本 `wecom-mcp.py` 都由程序自己提供，里面的服务器地址会自动换成本机的地址。

## 常见问题

- **服务起不来**：运行 `journalctl -u wecom-excel-http -n 50` 看日志。
- **外面访问不到端口**：检查云安全组，以及 `ss -ltnp | grep 8765` 是否在监听。
- **`error: the executable does not run on this system`**：系统太旧（glibc 低于 2.28），换新一点的系统，或者用 wheel 包配合 Python 3.11+。
- **证书申请失败**：多半是域名没有解析到本机，或者 80 端口被拦了。修好后重新运行脚本即可。
- **用户端报 `HTTP 429`**：触发了限流（仅域名模式），稍等片刻再试。
- **用户端报「还没有登录」或「cookie 已失效」**：让 AI 调用 `login` 重新登录。
- **`retcode=200063`**：这个企业微信账号没有这张表的权限。
