# walle 服务端部署文档（裸机）

> 适用环境：Ubuntu 24.04（aarch64/arm64），单机裸机部署。
> 组件：EMQX Enterprise 6.3.2 + SRS 7.0.215 + Nginx 门户（walle-ai）。
> 最后验证：2026-11-15 / 门户登录改造 2026-11-16。
> 架构与业务实现细节见 [walle服务端详细设计](./walle服务端详细设计.md)。

---

## 1. 架构与端口

```text
浏览器
  │ http://<host>:8088
  ▼
Nginx（独立实例，手动启动，非 systemd）
  ├── /              → 静态门户 baremetal/nginx/portal/
  ├── /auth/login    → 127.0.0.1:18083/api/v5/login （用户凭据校验）
  ├── /emqx-api/     → 127.0.0.1:18083/api/v5/   （反代注入 API Key）
  ├── /srs-api/      → 127.0.0.1:1985/api/v1/
  ├── /srs/          → 127.0.0.1:8080/            （console/players/FLV/HLS）
  └── /metrics       → 127.0.0.1:9972/metrics
        ▲                          ▲
        │                          │
  EMQX Enterprise 6.3.2       SRS 7.0.215
  （systemd: emqx.service）   （systemd: srs.service）
```

| 组件 | 端口 | 用途 | 建议对外 |
|---|---|---|---|
| Nginx 门户 | 8088/tcp | 统一入口 | 按需（内网/办公网） |
| EMQX | 18083/tcp | Dashboard / REST API | 仅内网 |
| EMQX | 1883/tcp | MQTT | 按业务 |
| EMQX | 8883/tcp | MQTT over TLS | 按业务 |
| EMQX | 8083/8084/tcp | MQTT over WS/WSS | 按业务 |
| SRS | 1935/tcp | RTMP 推流 | 按业务 |
| SRS | 1985/tcp | HTTP API | 仅内网 |
| SRS | 8080/tcp | HTTP Server / console / FLV / HLS | 按业务 |
| SRS | 8000/udp+tcp | WebRTC | 按业务 |
| SRS | 9972/tcp | Prometheus exporter | 仅监控网 |
| 系统 Nginx | 80/tcp | apt 默认站点（未使用） | 可关闭 |

---

## 2. 前置条件

- Ubuntu 24.04 LTS，arm64（本手册在 24.04.5 aarch64 验证）。
- 建议 4C/8G 以上；磁盘至少 3GB（EMQX 安装约 130MB，SRS 源码+objs 约 460MB，临时 deb 84MB 安装后可删）。
- 具有 sudo 权限的普通用户（本文以 `gs` 为例）。
- 可访问 `www.emqx.com`、`packages.emqx.io`、`github.com`。
- 端口 8088/18083/1883/8883/8083/8084/1935/1985/8080/8000/9972 未被占用。

---

## 3. 目录规划

```text
/home/gs/01_walle_service/
├── README.md                          # 系统简介（面向用户）
├── docs/                              # 文档，部署不需要
│   ├── walle服务端详细设计.md          # 架构、登录、MQTT、控制台设计
│   ├── walle服务端部署文档.md          # 本文
│   ├── BAREMETAL_部署报告.md           # 首次部署验证报告
│   ├── MVP_验证报告.md
│   ├── EMQX_SRS_VM_recon_report.md
│   ├── screenshots/                   # 验证截图
│   └── samples/                       # 接口返回样例
└── baremetal/                         # 部署所需文件
    ├── emqx_api_key.json              # API Key/Secret（0600，不入库）
    ├── srs.conf                       # SRS 运行配置（被 srs.service 引用）
    ├── srs/                           # SRS 源码（v7.0-a4，不入库，按 7.1 自行 clone）
    │   └── trunk/objs/srs             # 编译产物
    ├── srs.service                    # systemd 单元源文件
    ├── nginx/
    │   ├── nginx.conf.example         # 门户反代配置模板（入库）
    │   ├── nginx.conf                 # 由模板复制并填入真实 Key（不入库）
    │   ├── portal/
    │   │   ├── index.html             # 登录页
    │   │   ├── home.html              # 角色路由中转页
    │   │   ├── admin.html             # 管理员控制台
    │   │   ├── user.html              # 普通用户控制台
    │   │   └── dashboard.html         # 早期监控 MVP（未接入登录）
    │   └── temp/                      # nginx 临时目录
    ├── start-portal.sh                # 门户启停脚本
    └── mqtt_demo_client.py            # 无依赖 MQTT 测试客户端
```

说明：

- `docs/` 仅存放文档、验证报告、截图和接口样例，供查阅/审计，**部署不需要**，可整体删除。
- `srs/` 是 SRS 官方源码仓库，含 `.git`，因此不做版本管理；SRS 运行配置独立放在 `baremetal/srs.conf`，避免随源码目录一起被忽略。
- EMQX 安装在系统目录（`/etc/emqx`、`/var/lib/emqx`），systemd 单元在 `/etc/systemd/system/srs.service`；这两处不在本目录内。
- 安装用的 EMQX `.deb` 已在清理时删除，部署时按 6.1 的 URL 重新下载即可。

---

## 4. 上云必改项（换机器先看）

> 本节的值来自当前环境（`192.168.31.114`、用户 `gs`、路径 `/home/gs/...`）。直接复制到云主机前必须逐项核对；不改会影响 WebRTC 播放或直接导致启动失败。

### 4.1 SRS WebRTC 候选地址（必改）

`baremetal/srs.conf` 当前值：

```hocon
rtc_server {
  enabled on;
  listen 8000;
  candidate 192.168.31.114;
  tcp {
    enabled on;
    listen 8000;
  }
}
```

- **必须改**：`candidate` 改为云主机的公网 IP / EIP（NAT 环境填公网地址，不要填内网地址）。
- **不改的后果**：SDP answer 中携带的是 `192.168.31.114`，公网浏览器 ICE 无法连通，WebRTC（WHEP/WHIP、`/players/rtc_player.html`）会黑屏或超时；HTTP-FLV、HLS、RTMP 推流不受影响。
- **端口**：`listen 8000` 保持不变；安全组/防火墙需放行 **UDP 8000**。当前配置还启用了 `rtc_server.tcp`，因此 **TCP 8000** 也要放行；若只保留 UDP，可把 `tcp { enabled on; }` 改为 `off`。
- **多网卡/多出口**：SRS 支持配置多条 `candidate`，至少保留一条公网地址。

### 4.2 其他写死值（逐条核对）

| 文件 | 当前实际值 | 换机器时怎么改 |
|---|---|---|
| `baremetal/srs.conf` | `candidate 192.168.31.114;`（第 30 行） | 改为公网 IP/EIP |
| `baremetal/srs.conf` | `exporter { label baremetal; tag baremetal; }` | 非必须；多环境时改成可区分名字 |
| `/etc/systemd/system/srs.service`（源文件 `baremetal/srs.service`） | `User=gs`、`WorkingDirectory=/home/gs/01_walle_service/baremetal/srs/trunk`、`ExecStart=/home/gs/.../objs/srs -c /home/gs/.../baremetal/srs.conf` | 用户名或路径不同则三处全部替换，再 `daemon-reload` |
| `baremetal/nginx/nginx.conf`（由 `nginx.conf.example` 复制） | 所有 `pid`/`error_log`/`access_log`/`temp`/`root` 均为 `/home/gs/01_walle_service/baremetal/nginx/...` | 路径不同则整体替换 |
| `baremetal/nginx/nginx.conf`（由 `nginx.conf.example` 复制） | `proxy_set_header Authorization "Basic <真实 Key>"`（第 49 行） | 重新生成 API Key 并替换（见 4.3） |
| `baremetal/start-portal.sh` | `PREFIX="/home/gs/01_walle_service/baremetal/nginx"` | 路径不同则替换 |
| `baremetal/mqtt_demo_client.py` | `HOST = "127.0.0.1"` | 仅测试用；远程测试时改成 broker 地址 |

> 门户页面已全部改为相对路径，`/auth/login` 也由同源反代提供，因此换 IP/域名时**无需修改任何 HTML**。

### 4.3 EMQX API Key（必须重新生成，不要沿用）

`baremetal/nginx/nginx.conf` 由模板 `nginx.conf.example` 复制而来，模板里是占位符；**真实 Key 只存在于本机的 `nginx.conf`，该文件已加入 `.gitignore`，不会入库**。换机器/上线时按下面步骤重建：

1. 登录 EMQX Dashboard（`http://<host>:18083`）→ **Access Control → API Keys → Create**，按需勾选权限（门户读取用 `connections`、`monitoring` 等）。
2. 生成 Basic 值：

```bash
KEY=<新 api_key>
SECRET=<新 api_secret>
printf '%s:%s' "$KEY" "$SECRET" | base64 -w0
```

3. 复制模板（首次部署），把结果替换 `baremetal/nginx/nginx.conf` 中 `Authorization "Basic ..."` 的整段值：

```bash
cp baremetal/nginx/nginx.conf.example baremetal/nginx/nginx.conf
```

4. 重启门户使配置生效：

```bash
/home/gs/01_walle_service/baremetal/start-portal.sh stop
/home/gs/01_walle_service/baremetal/start-portal.sh start
```

> 不要把含真实 Key 的 `nginx.conf` 改名后提交或发给他人；泄露后立即在 Dashboard 删除该 Key 并重建。若误提交了 `nginx.conf`，光删文件不够，必须轮换 Key。

### 4.4 默认口令与 Cookie（必须改）

- Dashboard 默认 `admin` / `public`：首次登录后立即在用户管理里修改密码。门户登录复用的是这套 EMQX 账号，改密后门户同步生效。
- Erlang cookie：`/etc/emqx/emqx.conf` 中的 `node.cookie` 也是默认值，建议改成随机串后 `sudo systemctl restart emqx`。
- 默认口令未改时，任何能访问 18083 的人都能拿到管理权限。

### 4.5 测试进程不在 systemd 内

`ffmpeg` 测试推流与 `mqtt_demo_client.py` 都是手动 `nohup` 启动，主机重启后不会自动恢复；需要按第 9 节命令重新拉起（生产环境应改为 systemd 或不用测试流）。

### 4.6 换机器检查清单

```bash
cd /home/gs/01_walle_service

# 1) WebRTC 候选地址
grep -nE 'candidate|listen 8000' baremetal/srs.conf

# 2) 所有残留内网 IP（应只剩你确认过的项）
grep -Rn '192\.168\.' baremetal/srs.conf baremetal/nginx/nginx.conf baremetal/nginx/portal/

# 3) 绝对路径与真实凭据
grep -nE '/home/gs|Authorization' baremetal/nginx/nginx.conf
grep -nE 'User=|WorkingDirectory=|ExecStart=' /etc/systemd/system/srs.service

# 4) 确认公网出口 IP / EIP
curl -s https://ifconfig.me; echo
```

---

## 5. 系统依赖

```bash
sudo apt-get update
sudo apt-get install -y \
  curl jq git nginx build-essential pkg-config libssl-dev zlib1g-dev \
  automake cmake tcl ffmpeg
```

实测版本（Ubuntu 24.04 arm64）：

| 包 | 版本 |
|---|---|
| curl | 8.5.0-2ubuntu10.16 |
| jq | 1.7.1-3ubuntu0.24.04.2 |
| git | 1:2.43.0-1ubuntu7.4 |
| nginx / nginx-common | 1.24.0-2ubuntu7.19 |
| build-essential | 12.10ubuntu1 |
| pkg-config | 1.8.1-2build1 |
| libssl-dev | 3.0.13-0ubuntu3.16 |
| zlib1g-dev | 1:1.3.dfsg-3.1ubuntu2.3 |
| automake | 1:1.16.5-1.3ubuntu1 |
| cmake | 3.28.3-1build7 |
| tcl | 8.6.14build1 |
| ffmpeg | 7:6.1.1-3ubuntu5 |

---

## 6. EMQX Enterprise 6.3.2

### 6.1 下载与校验

```bash
mkdir -p /home/gs/01_walle_service/baremetal
cd /home/gs/01_walle_service/baremetal

curl -L -o emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb \
  https://www.emqx.com/downloads/enterprise/6.3.2/emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb

echo "ea5252d8f3979ecef714ee1d570558c6f07bad61d18d55d8ebd989243790d717  emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb" | sha256sum -c -
```

> 直链会 302 到 `https://packages.emqx.io/emqx-ee/6.3.2/...`；文件约 84MB。
>
> 安装包不长期保留：当前环境已在清理时删除该 deb（目录树中也没有它）。重复部署时按上面的 URL 重新下载并校验 sha256；安装完成后可直接删除 deb 释放约 84MB。

### 6.2 安装与启动

```bash
cd /home/gs/01_walle_service/baremetal
sudo apt install -y ./emqx-enterprise-6.3.2-ubuntu24.04-arm64.deb
sudo systemctl enable --now emqx
systemctl status emqx --no-pager
ss -ltn | grep -E ':(18083|1883|8883|8083|8084)\b'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18083/
```

> 注意：deb 的 postinst 只 `enable`，不会自动 `start`，必须显式启动。

### 6.3 登录与 API Key

默认账号 `admin` / `public`（首次部署后请立即修改）。

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:18083/api/v5/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"public"}' | jq -r .token)

curl -s -X POST http://127.0.0.1:18083/api/v5/api_key \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"portal","desc":"portal readonly","enable":true}' \
  | tee /home/gs/01_walle_service/baremetal/emqx_api_key.json

chmod 600 /home/gs/01_walle_service/baremetal/emqx_api_key.json
```

文件中的 `api_key` / `api_secret` 用于 Nginx 注入（见第 8 节）。

### 6.4 License 说明

未安装 license 时服务可正常启动，内置开发/社区授权：

```json
{"type":"community","max_connections":10000000,"max_sessions":10000000,
 "customer":"Developer","deployment":"Development","expiry":false,
 "expiry_at":"2029-03-01"}
```

`/api/v5/nodes` 返回 `edition: Enterprise`。生产环境需替换为正式 license。

---

## 7. SRS 7.0.215

### 7.1 编译

```bash
git clone --depth 1 --branch v7.0-a4 https://github.com/ossrs/srs.git \
  /home/gs/01_walle_service/baremetal/srs

cd /home/gs/01_walle_service/baremetal/srs/trunk
./configure --srt=on
make -j"$(nproc)"
./objs/srs -v
```

产物：

```text
trunk/objs/srs                 约 30MB
trunk/objs/                    约 318MB（含 console/players）
trunk/objs/nginx/html/console  原生控制台
```

### 7.2 运行配置 `/home/gs/01_walle_service/baremetal/srs.conf`

> SRS 源码目录 `baremetal/srs/` 只放源码与编译产物；运行配置独立放在 `baremetal/srs.conf`，由 systemd 的 `-c` 参数指定。

```hocon
listen 1935;
max_connections 800;
daemon off;
srs_log_tank console;
srs_log_level trace;

http_api {
  enabled on;
  listen 1985;
  crossdomain on;
}

http_server {
  enabled on;
  listen 8080;
  dir ./objs/nginx/html;
  crossdomain on;
}

exporter {
  enabled on;
  listen 9972;
  label baremetal;
  tag baremetal;
}

rtc_server {
  enabled on;
  listen 8000;
  candidate 192.168.31.114;
  tcp {
    enabled on;
    listen 8000;
  }
}

vhost __defaultVhost__ {
  http_remux {
    enabled on;
    mount [vhost]/[app]/[stream].flv;
  }
  hls {
    enabled on;
    hls_fragment 2;
    hls_window 10;
  }
  rtc {
    enabled on;
    rtmp_to_rtc on;
  }
}
```

### 7.3 systemd 单元 `/etc/systemd/system/srs.service`

```ini
[Unit]
Description=SRS 7.0.215 media server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=gs
Group=gs
WorkingDirectory=/home/gs/01_walle_service/baremetal/srs/trunk
ExecStart=/home/gs/01_walle_service/baremetal/srs/trunk/objs/srs -c /home/gs/01_walle_service/baremetal/srs.conf
Restart=on-failure
RestartSec=5
LimitNOFILE=10000

[Install]
WantedBy=multi-user.target
```

安装：

```bash
sudo install -m 0644 /home/gs/01_walle_service/baremetal/srs.service /etc/systemd/system/srs.service
sudo systemctl daemon-reload
sudo systemctl enable --now srs
systemctl status srs --no-pager
```

> `max_connections` 必须小于 `LimitNOFILE`；本配置为 800 / 10000。

---

## 8. 门户 Nginx（8088）

### 8.1 配置 `/home/gs/01_walle_service/baremetal/nginx/nginx.conf`

仓库里只提交模板 `nginx.conf.example`，首次部署先复制并填入真实 Key：

```bash
cp /home/gs/01_walle_service/baremetal/nginx/nginx.conf.example \
   /home/gs/01_walle_service/baremetal/nginx/nginx.conf
```

模板内容如下（`Authorization` 行为占位符）：
```nginx
worker_processes 1;
pid /home/gs/01_walle_service/baremetal/nginx/nginx.pid;
error_log /home/gs/01_walle_service/baremetal/nginx/error.log warn;

events { worker_connections 1024; }

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log /home/gs/01_walle_service/baremetal/nginx/access.log;

    client_body_temp_path /home/gs/01_walle_service/baremetal/nginx/temp/client_body;
    proxy_temp_path       /home/gs/01_walle_service/baremetal/nginx/temp/proxy;
    fastcgi_temp_path     /home/gs/01_walle_service/baremetal/nginx/temp/fastcgi;
    uwsgi_temp_path       /home/gs/01_walle_service/baremetal/nginx/temp/uwsgi;
    scgi_temp_path        /home/gs/01_walle_service/baremetal/nginx/temp/scgi;

    server {
        listen 8088;
        server_name _;

        root /home/gs/01_walle_service/baremetal/nginx/portal;
        index index.html;

        location / { try_files $uri $uri/ /index.html; }

        location = /auth/login {
            proxy_pass http://127.0.0.1:18083/api/v5/login;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header Content-Type application/json;
            proxy_read_timeout 60s;
        }

        location /emqx-api/ {
            proxy_pass http://127.0.0.1:18083/api/v5/;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header Authorization "Basic <BASE64(api_key:api_secret)>";
            proxy_read_timeout 60s;
        }

        location /srs-api/ {
            proxy_pass http://127.0.0.1:1985/api/v1/;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_read_timeout 60s;
        }

        location /srs/ {
            proxy_pass http://127.0.0.1:8080/;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_buffering off;
            proxy_read_timeout 3600s;
        }

        location = /metrics {
            proxy_pass http://127.0.0.1:9972/metrics;
        }
    }
}
```

生成 `Authorization` 值：

```bash
KEY=$(jq -r .api_key /home/gs/01_walle_service/baremetal/emqx_api_key.json)
SECRET=$(jq -r .api_secret /home/gs/01_walle_service/baremetal/emqx_api_key.json)
printf '%s:%s' "$KEY" "$SECRET" | base64 -w0
```

把结果替换 `<BASE64(api_key:api_secret)>`。

> `/auth/login` 不能注入 API Key，否则 EMQX 会用管理凭据而不是用户提交的凭据做校验；也不要给它加认证 Bypass 之外的缓存。

### 8.2 启动脚本 `/home/gs/01_walle_service/baremetal/start-portal.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

PREFIX="/home/gs/01_walle_service/baremetal/nginx"
CONF="$PREFIX/nginx.conf"
PID_FILE="$PREFIX/nginx.pid"

case "${1:-start}" in
  start)
    mkdir -p "$PREFIX/temp/client_body" "$PREFIX/temp/proxy" "$PREFIX/temp/fastcgi" "$PREFIX/temp/uwsgi" "$PREFIX/temp/scgi"
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "portal already running (pid $(cat "$PID_FILE"))"
      exit 0
    fi
    nginx -p "$PREFIX" -c "$CONF"
    echo "portal started (pid $(cat "$PID_FILE")), http://127.0.0.1:8088/"
    ;;
  stop)
    if [ -f "$PID_FILE" ]; then
      nginx -p "$PREFIX" -c "$CONF" -s quit || true
      echo "portal stopped"
    else
      echo "portal not running"
    fi
    ;;
  status)
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8088/ || true)
    echo "portal http=$code"
    ;;
  *)
    echo "usage: $0 {start|stop|status}"
    exit 1
    ;;
esac
```

```bash
chmod +x /home/gs/01_walle_service/baremetal/start-portal.sh
/home/gs/01_walle_service/baremetal/start-portal.sh start
/home/gs/01_walle_service/baremetal/start-portal.sh status
/home/gs/01_walle_service/baremetal/start-portal.sh stop
```

> 门户刻意不做 systemd，按需手动启动；Nginx 以 `gs` 用户运行，监听 8088（非特权端口）。

---

## 9. 测试推流与 MQTT 客户端

```bash
nohup ffmpeg -hide_banner -loglevel warning -re \
  -f lavfi -i testsrc2=size=640x360:rate=25 \
  -f lavfi -i sine=frequency=1000 \
  -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -g 50 \
  -c:a aac -b:a 64k -f flv rtmp://127.0.0.1:1935/live/test \
  > /home/gs/01_walle_service/baremetal/ffmpeg-push.log 2>&1 &
echo $! > /home/gs/01_walle_service/baremetal/ffmpeg-push.pid
```

无依赖 MQTT 测试客户端（保持连接并订阅 `demo/#`）：

```bash
nohup python3 /home/gs/01_walle_service/baremetal/mqtt_demo_client.py \
  > /home/gs/01_walle_service/baremetal/mqtt_demo.log 2>&1 &
echo $! > /home/gs/01_walle_service/baremetal/mqtt_demo.pid
```

---

## 10. 配置文件清单

| 路径 | 作用 | 关键项 |
|---|---|---|
| `/etc/systemd/system/srs.service` | SRS 服务 | `LimitNOFILE=10000`、`Restart=on-failure`、`User=gs` |
| `/etc/emqx/emqx.conf` | EMQX 配置（deb 自带） | 默认 dashboard 账号、监听端口 |
| `/home/gs/01_walle_service/baremetal/srs.conf` | SRS 运行配置 | `max_connections 800`、8080/1985/9972、`rtc_server` |
| `/home/gs/01_walle_service/baremetal/nginx/nginx.conf`（由模板复制） | 门户反代 | 8088、`/auth/login`、`/emqx-api/` 注入 Basic、`/srs/` 关闭缓冲 |
| `/home/gs/01_walle_service/baremetal/nginx/portal/index.html` | 登录页 | 角色 Tab、`POST /auth/login`、sessionStorage |
| `/home/gs/01_walle_service/baremetal/nginx/portal/home.html` | 角色路由 | 按 `walle_role` 跳 admin/user |
| `/home/gs/01_walle_service/baremetal/nginx/portal/admin.html` | 管理员控制台 | 5s 轮询 EMQX/SRS 接口 |
| `/home/gs/01_walle_service/baremetal/nginx/portal/user.html` | 普通用户控制台 | HTTP-FLV 播放、指令占位 |
| `/home/gs/01_walle_service/baremetal/start-portal.sh` | 门户启停 | start/stop/status |
| `/home/gs/01_walle_service/baremetal/emqx_api_key.json` | API 凭据 | 0600，`api_key`/`api_secret` |
| `/etc/docker/daemon.json` | Docker 守护配置 | 镜像加速、日志轮转（引擎保留） |

---

## 11. 验证步骤

```bash
KEY=$(jq -r .api_key /home/gs/01_walle_service/baremetal/emqx_api_key.json)
SECRET=$(jq -r .api_secret /home/gs/01_walle_service/baremetal/emqx_api_key.json)

# EMQX
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/clients?limit=2' | jq -c '{count:.meta.count}'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/subscriptions?limit=2' | jq -c '{count:.meta.count}'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/nodes' | jq -c '[.[]|{node,version,edition,node_status}]'
curl -s -u "$KEY:$SECRET" 'http://127.0.0.1:18083/api/v5/metrics' | jq -c '.[0]|{node,fields:(keys|length)}'

# SRS
curl -sL http://127.0.0.1:1985/api/v1/streams/ | jq -c '{total,streams:[.streams[]|{name,app,clients:(.clients|length)}]}'
curl -sL http://127.0.0.1:1985/api/v1/clients/ | jq -c '{total}'
curl -s -o /dev/null -w 'exporter=%{http_code}\n' http://127.0.0.1:9972/metrics

# 门户
curl -s -o /dev/null -w 'portal=%{http_code}\n' http://127.0.0.1:8088/
curl -s -o /dev/null -w 'flv=%{http_code} type=%{content_type}\n' --max-time 5 http://127.0.0.1:8088/srs/live/test.flv

# 登录（正确密码 200，错误密码 401）
curl -s -o /dev/null -w 'login=%{http_code}\n' -X POST http://127.0.0.1:8088/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"public"}'
curl -s -o /dev/null -w 'bad-login=%{http_code}\n' -X POST http://127.0.0.1:8088/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"wrong"}'
```

期望（有推流与 MQTT 测试客户端时）：

```text
count=1；count=1；edition=Enterprise / version=6.3.2 / running；fields=113
total=1（live/test）；total>=1；exporter=200
portal=200；flv=200 type=video/x-flv
login=200；bad-login=401
```

浏览器侧：`http://<host>:8088/` 输入 EMQX 账号登录 → 管理员进入控制台、普通用户进入小车控制台 → 退出后回到登录页。

---

## 12. Docker 说明

Docker 引擎保留，但**镜像已全部清空**；`/etc/docker/daemon.json` 内容：

```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://docker.1ms.run"
  ],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
```

用途：加速镜像拉取（直连 Docker Hub 不可用时）、限制容器日志大小。本方案不需要 Docker 运行任何服务。

清理记录：6 个镜像（10 个 tag，约 922.4MB）→ 0；容器 0；保留 6 个本地卷（约 331KB）。

---

## 13. 运行注意事项

1. **EMQX 无 CORS**：浏览器直连 18083 不会返回 `Access-Control-Allow-Origin`，`OPTIONS` 预检 500；必须经门户同源反代。新增接口时也要走 `/emqx-api/`。
2. **SRS ulimit**：`max_connections` 必须小于 `LimitNOFILE`，否则启动报错；systemd 已设 10000。
3. **自动播放**：浏览器会拦截有声自动播放，监控页需点击“播放 / 重连”。
4. **门户无 systemd**：重启主机后需手动执行 `start-portal.sh start`；ffmpeg 推流同理。
5. **默认凭据**：EMQX `admin/public`、默认 Erlang cookie 均不安全，必须修改。
6. **API Key 注入风险**：能访问 8088 的人即可调用只读管理接口，生产应加 BFF 鉴权或限制来源 IP。
7. **SRS 无录制持久化**：当前未启用 DVR；重建主机不会保留录制文件。

其余安全分析与演进项见 [walle服务端详细设计](./walle服务端详细设计.md) 第 12、13 节。

---

## 14. 附录：首次部署验证结果（2026-11-15）

```text
systemctl is-active emqx  → active
systemctl is-active srs   → active
systemctl restart srs     → active，/api/v1/streams/ 仍 200

EMQX /api/v5/clients        → {"count":1,"ids":["srs-mqtt-demo"]}
EMQX /api/v5/subscriptions  → {"count":1,"topics":["demo/#"]}
EMQX /api/v5/nodes          → [{"node":"emqx@127.0.0.1","version":"6.3.2","edition":"Enterprise","node_status":"running"}]
EMQX /api/v5/metrics        → {"node":"emqx@127.0.0.1","fields":113}

SRS /api/v1/streams/        → {"total":1,"streams":[{"name":"test","app":"live","clients":1}]}
SRS /api/v1/clients/        → {"total":1}
SRS /metrics                → 200（srs_build_info version="7.0.215"）

门户 /                       → 200
门户 /srs/live/test.flv      → 200，video/x-flv
浏览器                        → 点击播放后出现彩条测试图案
```

截图：`/home/gs/01_walle_service/docs/screenshots/srs_play_top.jpg`、`srs_play.jpg`（另有容器版 `mvp_*.jpg`）。
