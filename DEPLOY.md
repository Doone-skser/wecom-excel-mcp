# 部署指南

这份文档同时写给人和 AI 助手看。照着做，就能在一台新服务器上搭出一套完整的服务：说明页、MCP 接口、使用统计后台。

> **给 AI 助手：** 按顺序执行下面的步骤。只向用户要「需要提供」表里的信息，其他一律用默认值。不要把 cookie、后台密码写进任何文件或日志；后台密码只让用户在终端里输入，或者由脚本随机生成。每一步执行完，都用「验证」一节的命令检查一遍，再告诉用户结果。

## 需要提供

| 信息 | 例子 | 说明 |
|---|---|---|
| 域名 | `wecom.example.com` | 必须已经解析到这台服务器 |
| 邮箱 | `you@example.com` | 可选，Let's Encrypt 用它发证书到期提醒 |
| 后台账号 | `admin` | 可选，默认 `admin` |
| 后台密码 | — | 不传就在终端里输入；没有终端时随机生成并打印一次 |

服务器要求：

- Debian 12 或 Ubuntu 22.04/24.04，x86_64 或 arm64，有 root 权限，内存 ≥ 1 GB；
- 80、443 端口对外开放。云服务器还要在安全组里放行。

## 第 0 步：检查前提

```bash
cat /etc/os-release | head -3
curl -s https://ipinfo.io/ip; echo        # 本机公网 IP
getent hosts wecom.example.com            # 应该解析到上面那个 IP
ss -ltnp | grep -E ':(80|443) '           # 看看 80/443 是谁在用
```

- 80/443 被别的程序占用（不是 nginx）：先跟用户确认再处理。
- 已经装了宝塔面板，或者 nginx 由别的工具管理：跳到下面的「已有 nginx / 宝塔面板」。
- 域名没有解析到本机：先让用户去改 DNS，申请证书会因为这个失败。

## 第 1 步：一键安装

```bash
git clone https://github.com/Doone-skser/wecom-excel-mcp.git /opt/wecom-excel-src
cd /opt/wecom-excel-src
sudo DOMAIN=wecom.example.com EMAIL=you@example.com ADMIN_USER=admin ./deploy/install.sh
```

[`deploy/install.sh`](deploy/install.sh) 会做下面这些事。脚本可以重复运行，再跑一次就是更新：

1. 安装 `curl`、`build-essential`、`nginx`、`certbot`，以及 [uv](https://docs.astral.sh/uv/)（用来装 Python）和 Rust（用来编译解码器）；
2. 创建系统用户 `wecom-excel`；
3. 在 `/opt/wecom-excel/venv` 里编译并安装本仓库（Python 3.12，由 uv 安装到 `/opt/wecom-excel/python`）；
4. 把 `deploy/site/` 下的说明页、`llms.txt` 和用户端脚本 `wecom-mcp.py` 复制到 `/var/www/wecom-excel/`，并把里面的 `wecom.example.com` 替换成你的域名；
5. 把后台账号写入 `/var/lib/wecom-excel/admin-password`，格式是 `账号:PBKDF2哈希`，权限 600；
6. 安装并启动 systemd 服务 `wecom-excel-http`，只监听 `127.0.0.1:8765`；
7. 用 webroot 方式申请 Let's Encrypt 证书，写入 `/etc/nginx/conf.d/wecom-excel.conf`，再 reload nginx。证书由 certbot 自带的定时任务自动续期，续期后会自动 reload nginx。

编译 Rust 在 1 核的机器上大约要 3–5 分钟。

**国内服务器下载慢的话**，可以在命令前面加上镜像：

```bash
sudo RUSTUP_DIST_SERVER=https://rsproxy.cn RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup \
     UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
     UV_PYTHON_INSTALL_MIRROR=https://registry.npmmirror.com/-/binary/python-build-standalone \
     DOMAIN=wecom.example.com ./deploy/install.sh
```

系统软件源（apt）本身慢的话，先把 `/etc/apt/sources.list.d/debian.sources`（Ubuntu 是 `/etc/apt/sources.list` 或 `ubuntu.sources`）里的域名换成 `mirrors.aliyun.com`。`sh.rustup.rs` 和 `astral.sh/uv/install.sh` 这两个安装脚本本身也要能访问到。实在访问不到，可以先手动装好 `cargo` 和 `uv`，脚本检测到已经有了就会跳过。

## 第 2 步：验证

```bash
systemctl is-active wecom-excel-http                       # active
curl -s -o /dev/null -w '%{http_code}\n' https://wecom.example.com/            # 200
curl -s -o /dev/null -w '%{http_code}\n' https://wecom.example.com/admin/login # 200
curl -s https://wecom.example.com/wecom-mcp.py | grep -m1 '^REMOTE'            # 应该是你的域名
curl -s https://wecom.example.com/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -H 'X-Wecom-Cookie: x=1' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}'
# 返回里有 "serverInfo":{"name":"wecom-excel"...}
```

然后找一台装了 Claude Code 的电脑，按说明页上的步骤装好用户端，调用 `login` 登录，再读一张自己有权限的表。能读出来，整套服务就是通的。

## 第 3 步：告诉用户

- 说明页 `https://你的域名/`：发给同事。同事点「一键复制」，把内容发给自己的 AI 助手，AI 就会自动装好。
- 后台 `https://你的域名/admin`：用第 1 步设置的账号密码登录。
- 更新：`cd /opt/wecom-excel-src && git pull && sudo DOMAIN=你的域名 ./deploy/install.sh`

## 已有 nginx / 宝塔面板

如果 nginx 由宝塔或别的工具管理，加上 `SKIP_NGINX=1`，脚本就只装服务、说明页和后台账号，不碰 nginx 和证书：

```bash
sudo SKIP_NGINX=1 DOMAIN=wecom.example.com ./deploy/install.sh
```

然后自己配置站点，参考 [`deploy/nginx.conf`](deploy/nginx.conf)。要点如下：

- 站点根目录指向 `/var/www/wecom-excel`（宝塔也可以直接把站点目录设成这个）；
- `location = /mcp` 和 `location ^~ /admin` 反向代理到 `http://127.0.0.1:8765`，并且必须带上这几个头：`Host`、`X-Real-IP`、`X-Forwarded-Proto`；
- `/mcp` 要关闭缓冲（`proxy_buffering off`），超时设为 300 秒；
- 带 `Accept: text/html` 访问 `/mcp` 的浏览器请求，302 跳转到 `/`；
- `limit_req_zone` 必须写在 http 块里。宝塔的站点配置文件本身就在 http 块里 include，可以直接放在文件开头；
- 强制 HTTPS。宝塔的「强制 HTTPS」开关有时不生效，最稳妥的是在 80 端口的 server 里直接 `return 301 https://$host$request_uri;`。

如果站点不在 `/var/www/wecom-excel`，就照第 1 步第 4 点那样，自己把 `deploy/site/` 下的三个文件复制过去，并把里面的域名替换掉。

## 改配置

| 想改什么 | 怎么改 |
|---|---|
| 后台密码 | `sudo ADMIN_PASSWORD='新密码' DOMAIN=你的域名 ./deploy/install.sh`，或者手动执行：`/opt/wecom-excel/venv/bin/wecom-excel-mcp --hash-password` 生成哈希，再写成 `账号:哈希` 存进 `/var/lib/wecom-excel/admin-password`。改完立刻生效，所有已登录的后台会话都会失效 |
| 端口 | `deploy/wecom-excel-http.service` 里的 `--port`，和 `deploy/nginx.conf` 里的 `proxy_pass` 一起改 |
| 限流 | `deploy/nginx.conf` 里的 `rate=10r/s` 和 `burst=40` |
| 关闭使用统计 | 去掉 service 里的 `--usage-db`。去掉后 `/admin` 也就没有了 |
| 说明页文案 | 改 `deploy/site/index.html` 和 `llms.txt`，两边的命令要保持一致 |
| 用户端脚本 | 改 `deploy/site/wecom-mcp.py`，**每次都要调高 `VERSION`**。已安装的用户端每小时检查一次版本，发现新版本会自动更新 |

## 文件位置

| 路径 | 内容 |
|---|---|
| `/opt/wecom-excel/venv` | 服务端程序 |
| `/etc/systemd/system/wecom-excel-http.service` | systemd 服务 |
| `/etc/nginx/conf.d/wecom-excel.conf` | nginx 站点配置 |
| `/var/www/wecom-excel/` | 说明页、`llms.txt`、`wecom-mcp.py` |
| `/var/lib/wecom-excel/usage.db` | 使用统计（SQLite，不含 cookie） |
| `/var/lib/wecom-excel/admin-password` | 后台账号和密码哈希 |
| `/var/lib/wecom-excel/session.key` | 后台会话签名密钥，首次启动时自动生成 |

## 常见问题

- **`journalctl -u wecom-excel-http -n 50`** 能看到服务日志，大部分启动问题都在这里。
- **证书申请失败**：多半是域名没有解析到本机，或者 80 端口被安全组拦了。修好后重新运行脚本即可。
- **`nginx -t` 报 `limit_req_zone` 重复**：别的配置里也定义了 `zone=wecom_mcp`，删掉其中一份。
- **MCP 返回 `421` 或 `Invalid Host`**：service 里的 `--allowed-hosts` 和实际访问的域名不一致。
- **浏览器打开 `/mcp` 一直显示 `: ping`**：nginx 少了「浏览器跳转到 `/`」那一段配置。不影响使用。
- **用户端报 `HTTP 429`**：触发了限流，稍等片刻再试；也可以调大 `deploy/nginx.conf` 里的限流参数。
- **用户端报「还没有登录」或「cookie 已失效」**：让 AI 调用 `login` 重新登录。
- **`retcode=200063`**：这个企业微信账号没有这张表的权限。
